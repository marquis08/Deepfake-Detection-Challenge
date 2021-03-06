import torch
from torch import nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import torchvision.models as models
import pretrainedmodels
from pytorchcv.model_provider import get_model


class MyResNeXt(models.resnet.ResNet):
    def __init__(self, checkpoint):
        super(MyResNeXt, self).__init__(block=models.resnet.Bottleneck,
                                        layers=[3, 4, 6, 3], 
                                        groups=32, 
                                        width_per_group=4)

        self.load_state_dict(torch.load(checkpoint))

        self.fc = nn.Linear(2048, 1)
    

class Pooling(nn.Module):
  def __init__(self):
    super(Pooling, self).__init__()
    
    self.p1 = nn.AdaptiveAvgPool2d((1,1))
    self.p2 = nn.AdaptiveMaxPool2d((1,1))

  def forward(self, x):
    x1 = self.p1(x)
    x2 = self.p2(x)
    return (x1+x2) * 0.5


class Head(torch.nn.Module):
  def __init__(self, in_f, out_f):
    super(Head, self).__init__()
    
    self.f = nn.Flatten()
    self.l = nn.Linear(in_f, 512)
    self.d = nn.Dropout(0.5)
    self.o = nn.Linear(512, out_f)
    self.b1 = nn.BatchNorm1d(in_f)
    self.b2 = nn.BatchNorm1d(512)
    self.r = nn.ReLU()

  def forward(self, x):
    x = self.f(x)
    x = self.b1(x)
    x = self.d(x)

    x = self.l(x)
    x = self.r(x)
    x = self.b2(x)
    x = self.d(x)

    out = self.o(x)
    return out

class FCN(torch.nn.Module):
  def __init__(self, base, in_f):
    super(FCN, self).__init__()
    self.base = base
    self.h1 = Head(in_f, 1)
  
  def forward(self, x):
    x = self.base(x)
    return self.h1(x)


class LinearBlock(nn.Module):
    def __init__(self, in_features, out_features, bias=True,
                 use_bn=True, activation=F.relu, dropout_ratio=-1, residual=False,):
        super(LinearBlock, self).__init__()
        if in_features is None:
            self.linear = LazyLinear(in_features, out_features, bias=bias)
        else:
            self.linear = nn.Linear(in_features, out_features, bias=bias)
        if use_bn:
            self.bn = nn.BatchNorm1d(out_features)
        if dropout_ratio > 0.:
            self.dropout = nn.Dropout(p=dropout_ratio)
        else:
            self.dropout = None
        self.activation = activation
        self.use_bn = use_bn
        self.dropout_ratio = dropout_ratio
        self.residual = residual
    def __call__(self, x):
        h = self.linear(x)
        if self.use_bn:
            h = self.bn(h)
        if self.activation is not None:
            h = self.activation(h)
        if self.residual:
            h = residual_add(h, x)
        if self.dropout_ratio > 0:
            h = self.dropout(h)
        return h


def gem(x, p=3, eps=1e-6):
    return F.avg_pool2d(x.clamp(min=eps).pow(p), (x.size(-2), x.size(-1))).pow(1./p)


class GeM(nn.Module):
    def __init__(self, p=3, eps=1e-6):
        super(GeM,self).__init__()
        self.p = Parameter(torch.ones(1)*p)
        self.eps = eps
    def forward(self, x):
        return gem(x, p=self.p, eps=self.eps)
    def __repr__(self):
        return self.__class__.__name__ + '(' + 'p=' + '{:.4f}'.format(self.p.data.tolist()[0]) + ', ' + 'eps=' + str(self.eps) + ')'


def l2n(x, eps=1e-6):
    return x / (torch.norm(x, p=2, dim=1, keepdim=True) + eps).expand_as(x)


class L2N(nn.Module):
    def __init__(self, eps=1e-6):
        super(L2N,self).__init__()
        self.eps = eps
    def forward(self, x):
        return l2n(x, eps=self.eps)
    def __repr__(self):
        return self.__class__.__name__ + '(' + 'eps=' + str(self.eps) + ')'


class BaseCNN(nn.Module):
    def __init__(self, model_name, pretrained=None, use_bn=True, dropout_ratio=0., h_dims=512, GeM_pool=False, num_class=1):
        super(BaseCNN, self).__init__()
        self.base_model = pretrainedmodels.__dict__[model_name](pretrained=pretrained)
        activation = F.leaky_relu
        self.do_pooling = True
        self.GeM_pool = GeM_pool
        if self.do_pooling:
            inch = self.base_model.last_linear.in_features
            if self.GeM_pool:
                self.pool = GeM()
                self.norm = L2N()
        else:
            inch = None
        hdim = h_dims
        self.lin_1 = LinearBlock(inch, hdim, use_bn=use_bn, activation=activation, dropout_ratio=dropout_ratio, residual=False)
        self.lin_2 = LinearBlock(hdim, num_class, use_bn=use_bn, activation=None, residual=False)
        self.lin_layers = nn.Sequential(self.lin_1, self.lin_2)

    def forward(self, x):
        h = self.base_model.features(x)
        if self.do_pooling:
            if self.GeM_pool:
                h = self.norm(self.pool(h)).squeeze(-1).squeeze(-1)
            else:
                h = torch.sum(h, dim=(-1, -2))
        else:
            bs, ch, height, width = h.shape
            h = h.view(bs, ch * height * width)
        for layer in self.lin_layers:
            h = layer(h)
        return h


def freeze_until(model, param_name):
    found_name = False
    for name, params in model.named_parameters():
        if name == param_name:
            found_name = True
        params.requires_grad = found_name


def build_model(args, device):
    weight_path = args.weight_path
    if args.model == 'resnet50':
        model = BaseCNN('resnet50', pretrained='imagenet', dropout_ratio=args.dropout, GeM_pool=True)
        freeze_until(model, "base_model.layer3.0.conv1.weight")
        print("fine-tuning")

    elif args.model == 'resnext':
        model = MyResNeXt(checkpoint=weight_path)
        freeze_until(model, "layer4.0.conv1.weight")
        print("fine-tuning")

    elif args.model == 'xception':
        model = get_model("xception", pretrained=True)
        model = nn.Sequential(*list(model.children())[:-1])
        model[0].final_block.pool = nn.Sequential(nn.AdaptiveAvgPool2d((1,1)))
        model = FCN(model, 2048)
    else:
        NotImplementedError

    model.to(device)
    return model