import torch
from torch import nn
from torch.nn import functional as F

from network.Modules import GC3d, Refine3d
from network.Resnet3d import resnet50
from network.models import BaseNetwork


class Encoder3d(nn.Module):
  def __init__(self, tw = 16, sample_size = 112):
    super(Encoder3d, self).__init__()
    self.conv1_p = nn.Conv3d(1, 64, kernel_size=7, stride=(1, 2, 2),
                             padding=(3, 3, 3), bias=False)

    resnet = resnet50(sample_size = sample_size, sample_duration = tw)
    self.resnet = resnet
    self.conv1 = resnet.conv1
    self.bn1 = resnet.bn1
    self.relu = resnet.relu  # 1/2, 64
    # self.maxpool = resnet.maxpool
    self.maxpool = nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1,2,2), padding=(0,1,1))

    self.layer1 = resnet.layer1  # 1/4, 256
    self.layer2 = resnet.layer2  # 1/8, 512
    self.layer3 = resnet.layer3  # 1/16, 1024
    self.layer4 = resnet.layer4  # 1/32, 2048

    self.register_buffer('mean', torch.FloatTensor([114.7748, 107.7354, 99.4750]).view(1, 3, 1, 1, 1))
    self.register_buffer('std', torch.FloatTensor([1, 1, 1]).view(1, 3, 1, 1, 1))

  def freeze_batchnorm(self):
    # freeze BNs
    for m in self.modules():
      if isinstance(m, nn.BatchNorm2d) or isinstance(m, nn.BatchNorm3d):
        for p in m.parameters():
          p.requires_grad = False

  def forward(self, in_f, in_p):
    assert in_f is not None or in_p is not None
    f = (in_f * 255.0 - self.mean) / self.std
    f /= 255.0

    if in_f is None:
      p = in_p.float()
      if len(in_p.shape) < 4:
        p = torch.unsqueeze(in_p, dim=1).float()  # add channel dim

      x = self.conv1_p(p)
    elif in_p is not None:
      p = in_p.float()
      if len(in_p.shape) < 4:
        p = torch.unsqueeze(in_p, dim=1).float()  # add channel dim

      x = self.conv1(f) + self.conv1_p(p)  # + self.conv1_n(n)
    else:
      x = self.conv1(f)
    x = self.bn1(x)
    c1 = self.relu(x)  # 1/2, 64
    x = self.maxpool(c1)  # 1/4, 64
    r2 = self.layer1(x)  # 1/4, 64
    r3 = self.layer2(r2)  # 1/8, 128
    r4 = self.layer3(r3)  # 1/16, 256
    r5 = self.layer4(r4)  # 1/32, 512

    return r5, r4, r3, r2


class Decoder3d(nn.Module):
  def __init__(self, tw=5):
    super(Decoder3d, self).__init__()
    mdim = 256
    self.GC = GC3d(2048, mdim)
    self.convG1 = nn.Conv3d(mdim, mdim, kernel_size=3, padding=1)
    self.convG2 = nn.Conv3d(mdim, mdim, kernel_size=3, padding=1)
    self.RF4 = Refine3d(1024, mdim)  # 1/16 -> 1/8
    self.RF3 = Refine3d(512, mdim)  # 1/8 -> 1/4
    self.RF2 = Refine3d(256, mdim)  # 1/4 -> 1

    self.pred5 = nn.Conv3d(mdim, 2, kernel_size=3, padding=1, stride=1)
    self.pred4 = nn.Conv3d(mdim, 2, kernel_size=3, padding=1, stride=1)
    self.pred3 = nn.Conv3d(mdim, 2, kernel_size=3, padding=1, stride=1)
    self.pred2 = nn.Conv3d(mdim, 2, kernel_size=3, padding=1, stride=1)

  def forward(self, r5, r4, r3, r2, support):
    # there is a merge step in the temporal net. This split is a hack to fool it
    # x = torch.cat((x, r5), dim=1)
    x = self.GC(r5)
    r = self.convG1(F.relu(x))
    r = self.convG2(F.relu(r))
    m5 = x + r  # out: 1/32, 64
    m4 = self.RF4(r4, m5)  # out: 1/16, 64
    m3 = self.RF3(r3, m4)  # out: 1/8, 64
    m2 = self.RF2(r2, m3)  # out: 1/4, 64

    p2 = self.pred2(F.relu(m2))
    p3 = self.pred3(F.relu(m3))
    p4 = self.pred4(F.relu(m4))
    p5 = self.pred5(F.relu(m5))

    p = F.interpolate(p2, scale_factor=(1,4,4), mode='trilinear')

    return p, p2, p3, p4, p5


class Decoder3dMaskGuidance(Decoder3d):
  def __init__(self, tw=5):
    super(Decoder3dMaskGuidance, self).__init__()
    self.GC = GC3d(2049, 256)


class Resnet3d(BaseNetwork):
  def __init__(self, tw=16, sample_size=112):
    super(Resnet3d, self).__init__()
    self.encoder = Encoder3d(tw, sample_size)
    self.decoder = Decoder3d()

  def forward(self, x, ref):
    if ref is not None and len(ref.shape) == 4:
      r5, r4, r3, r2 = self.encoder.forward(x, ref.unsqueeze(2))
    else:
      r5, r4, r3, r2 = self.encoder.forward(x, ref)
    p, p2, p3, p4, p5 = self.decoder.forward(r5, r4, r3, r2, None)
    return p, p2, p3, p4, p5, r5


class Resnet3dPredictOne(BaseNetwork):
  def __init__(self, tw=16, sample_size=112):
    super(Resnet3dPredictOne, self).__init__()
    self.encoder = Encoder3d(tw, sample_size)
    self.encoder.maxpool = self.encoder.resnet.maxpool
    self.convG1 = nn.Conv2d(2048, 512, kernel_size=3, padding=1)
    self.convG2 = nn.Conv2d(512, 256, kernel_size=3, padding=1)
    self.pred = nn.Conv2d(256, 2, kernel_size=3, padding=1, stride=1)

  def forward(self, x, ref):
    r5, r4, r3, r2 = self.encoder.forward(x, ref)
    p = self.convG1(F.relu(r5[:, :, -1]))
    p = self.convG2(F.relu(p))
    p = self.pred(p)
    return p, None, None, None, None, r5


class Resnet3dMaskGuidance(BaseNetwork):
  def __init__(self, tw=16, sample_size=112):
    super(Resnet3dMaskGuidance, self).__init__()
    self.encoder = Encoder3d(tw, sample_size)
    self.decoder = Decoder3dMaskGuidance()

  def forward(self, x, ref):
    r5, r4, r3, r2 = self.encoder.forward(x, None)
    assert ref is not None
    ref = F.upsample(ref, size=r5.shape[-2:], mode='nearest')
    r5 = torch.cat((r5, ref.unsqueeze(1)), dim=1)
    p, p2, p3, p4, p5 = self.decoder.forward(r5, r4, r3, r2, None)
    return p, p2, p3, p4, p5, r5