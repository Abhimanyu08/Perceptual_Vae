
#################################################
### THIS FILE WAS AUTOGENERATED! DO NOT EDIT! ###
#################################################
# file to edit: dev_nb/all_things_01.ipynb

import torch,gzip,pickle
from fastai import datasets
from torch import tensor
from PIL import Image
import matplotlib.pyplot as plt
from torchvision import transforms
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch import nn
from functools import partial
from torch import optim
import math

class Lambda(nn.Module):
    def __init__(self,f):
        super().__init__()
        self.f = f

    def forward(self,x):
        return self.f(x)

from typing import *

def listify(o):
    if o is None: return []
    if isinstance(o, list): return o
    if isinstance(o, str): return [o]
    if isinstance(o, Iterable): return list(o)
    return [o]

import re

_camel_re1 = re.compile('(.)([A-Z][a-z]+)')
_camel_re2 = re.compile('([a-z0-9])([A-Z])')
def camel2snake(name):
    s1 = re.sub(_camel_re1, r'\1_\2', name)
    return re.sub(_camel_re2, r'\1_\2', s1).lower()


class Callback():
    _order=0
    def set_runner(self, run): self.run=run
    def __getattr__(self, k): return getattr(self.run, k)

    @property
    def name(self):
        name = re.sub(r'Callback$', '', self.__class__.__name__)
        return camel2snake(name or 'callback')

    def __call__(self, cb_name):
        f = getattr(self, cb_name, None)
        if f and f(): return True
        return False


class CancelTrainException(Exception): pass
class CancelEpochException(Exception): pass
class CancelBatchException(Exception): pass

class Runner():
    def __init__(self, cbs=None, cb_funcs=None):
        cbs = listify(cbs)
        for cbf in listify(cb_funcs):
            cb = cbf()
            setattr(self, cb.name, cb)
            cbs.append(cb)
        self.stop,self.cbs = False,[TrainEvalCallback()]+cbs

    @property
    def opt(self):       return self.learn.opt
    @property
    def model(self):     return self.learn.model
    @property
    def loss_func(self): return self.learn.loss_func
    @property
    def data(self):      return self.learn.data

    def one_batch(self, xb, yb):
        try:
            self.xb,self.yb = xb,yb
            self('begin_batch')
            self.pred = self.model(self.xb)
            self('after_pred')
            self.loss = self.loss_func(self.pred, self.yb)
            self('after_loss')
            if not self.in_train: return
            self.loss.backward()
            self('after_backward')
            self.opt.step()
            self('after_step')
            self.opt.zero_grad()
        except CancelBatchException: self('after_cancel_batch')
        finally: self('after_batch')

    def all_batches(self, dl):
        self.iters = len(dl)
        try:
            for xb,yb in dl: self.one_batch(xb, yb)
        except CancelEpochException: self('after_cancel_epoch')

    def fit(self, epochs, learn):
        self.epochs,self.learn,self.loss = epochs,learn,tensor(0.)

        try:
            for cb in self.cbs: cb.set_runner(self)
            self('begin_fit')
            for epoch in range(epochs):
                self.epoch = epoch
                if not self('begin_epoch'): self.all_batches(self.data.train_dl)

                with torch.no_grad():
                    if not self('begin_validate'): self.all_batches(self.data.valid_dl)
                self('after_epoch')

        except CancelTrainException: self('after_cancel_train')
        finally:
            self('after_fit')
            self.learn = None

    def __call__(self, cb_name):
        res = False
        for cb in sorted(self.cbs, key=lambda x: x._order): res = cb(cb_name) or res
        return res

class TrainEvalCallback(Callback):
    _order = 1
    def begin_fit(self):
        self.run.n_epoch, self.run.n_iters = 0.,0.


    def begin_epoch(self):
        self.model.train()
        self.run.in_train = True
        self.run.n_epoch = self.epoch

    def begin_batch(self):
        if self.run.in_train:
            self.run.n_epoch += 1/self.iters
            self.run.n_iters += 1

    def begin_validate(self):
        self.model.eval()
        self.run.in_train = False


class BatchTransformCallback(Callback):
    _order = 2
    def __init__(self,tfm):self.f = tfm
    def begin_batch(self): self.run.xb = self.f(self.xb)


class CudaCallback(Callback):
    def begin_fit(self): self.model.cuda()
    def begin_batch(self): self.run.xb, self.run.yb = self.xb.cuda(), self.yb.cuda()


class AvgStats():
    def __init__(self, metrics, in_train): self.metrics,self.in_train = listify(metrics),in_train

    def reset(self):
        self.tot_loss,self.count = 0.,0
        self.tot_mets = [0.] * len(self.metrics)

    @property
    def all_stats(self): return [self.tot_loss.item()] + self.tot_mets
    @property
    def avg_stats(self): return [o/self.count for o in self.all_stats]

    def __repr__(self):
        if not self.count: return ""
        return f"{'train' if self.in_train else 'valid'}: {self.avg_stats}"

    def accumulate(self, run):
        bn = run.xb.shape[0]
        self.tot_loss += run.loss * bn
        self.count += bn
        for i,m in enumerate(self.metrics):
            self.tot_mets[i] += m(run.pred, run.yb) * bn

class AvgStatsCallback(Callback):
#     _order = 1
    def __init__(self, metrics):
        self.train_stats,self.valid_stats = AvgStats(metrics,True),AvgStats(metrics,False)

    def begin_epoch(self):
        self.train_stats.reset()
        self.valid_stats.reset()

    def after_loss(self):
        stats = self.train_stats if self.in_train else self.valid_stats
        with torch.no_grad(): stats.accumulate(self.run)

    def after_epoch(self):
        print(self.train_stats)
        print(self.valid_stats)

class ParamSchedulerCallback(Callback):
    def __init__(self,param,sched_func):
        self.param = param
        self.sf = sched_func
        self.pos = []

    def change_param(self):
        self.po = self.run.n_epoch/self.run.epochs
        for i in self.opt.param_groups:
            i[self.param] = self.sf(self.po)
            self.pos.append(self.po)

    def begin_batch(self):
        if self.in_train:
#             print(self.run.n_epoch,self.pos)
            self.change_param()

class RecorderCallback(Callback):
    def begin_fit(self):
        self.lrs = []
        self.losses = []

    def after_loss(self):
        if self.in_train:
            self.lrs.append(self.opt.param_groups[-1]['lr'])
            self.losses.append(self.loss.detach().cpu())

    def plot_lr(self):
        plt.plot(self.lrs)

    def plot_losses(self):
        plt.plot(self.losses)


def view_tfm(*size):
    def _inner(x): return x.view(*((-1,) + size))
    return _inner

def flatten(x): return x.view(x.shape[0],-1)

class Learner():
    def __init__(self,model,opt,data,loss_func):
        self.model,self.opt,self.data,self.loss_func = model,opt,data,loss_func

class DataBunch():
    def __init__(self,train_dl,valid_dl):
        self.train_dl = train_dl
        self.valid_dl = valid_dl
        self.c =self.train_dl.dataset.y.max().item() + 1

    @property
    def train_ds(self): return self.train_dl.dataset

    @property
    def valid_ds(self): return self.valid_dl.dataset

def get_data(path):
    with gzip.open(path, 'rb') as f:
        ((xt,yt), (xv,yv), _) = pickle.load(f, encoding = 'latin-1')

    return map(tensor, (xt,yt,xv,yv))

def normalise(x,m,s):
    return (x-m)/s

def normalise_to(xtrain,xvalid):
    m,s = xtrain.mean(),xtrain.std()
    return normalise(xtrain,m,s), normalise(xvalid,m,s)

class Dataset():
    def __init__(self,x,y): self.x, self.y = x,y
    def __len__(self): return len(self.x)
    def __getitem__(self,i): return self.x[i], self.y[i]


def get_dataset(xt,yt,xv,yv):
    return Dataset(xt,yt), Dataset(xv,yv)

def get_dls(train_ds,valid_ds,bs,shuffle = True,nw = 0):
    return DataLoader(train_ds,bs,shuffle = shuffle,num_workers = nw), DataLoader(valid_ds,2*bs, num_workers=nw)

def accuracy(out,targ): return (torch.argmax(out,dim = 1) == targ).float().mean()

class Hook():
    def __init__(self,m,f): self.hook = m.register_forward_hook(partial(f,self))
    def remove(self): self.hook.remove()
    def __del__(self): self.remove()

class ListContainer():
    def __init__(self,items): self.items = listify(items)

    def __getitem__(self,idx):
        if isinstance(idx, (int,slice)): return self.items[idx]
        elif isinstance(idx[0], bool):
            assert len(idx) == len(self)
            return [o for m,o in zip(idx,self.items) if m]
        return [self.items[i] for i in idx]

    def __len__(self): return len(self.items)

    def __iter__(self): return iter(self.items)

    def __setitem__(self,i,o): self.items[i] = o
    def __delitem__(self,i): del(self.items[i])

    def __repr__(self):
        res = f'{self.__class__.__name__} ({len(self)} items)\n{self.items[:10]}'
        if len(self)>10: res = res[:-1]+ '...]'
        return res

class Hooks(ListContainer):
    def __init__(self,m,f): super().__init__([Hook(mc,f) for mc in m if isinstance(mc, nn.Sequential)])

    def __enter__(self,*args): return self
    def __exit__(self,*args): self.remove()

    def __del__(self): self.remove()

    def __delitem__(self, i):
        self[i].remove()
        super().__delitem__(i)

    def remove(self):
        for h in self:
            h.remove()

class GeneralRelu(nn.Module):
    def __init__(self,leak = None,ma = None,sub = None):
        super().__init__()
        self.leak = leak
        self.ma = ma
        self.sub = sub

    def forward(self,x):
        x = F.leaky_relu(x,self.leak) if self.leak else F.relu(x)
        if self.ma: x.clamp_max_(self.ma)
        if self.sub: x.sub_(self.sub)
        return x


def conv(ni,no,mom = 0.1,eps = 1e-5,ks = 3,s = 2,bn = False, **kwargs):
    layer = [nn.Conv2d(ni,no,kernel_size = ks, stride = s, padding = ks//2,bias = not bn), GeneralRelu(**kwargs)]
    if bn: layer.append(nn.BatchNorm2d(no,eps,mom))
    return nn.Sequential(*layer)

def annealer(f):
    def _inner(start,end): return partial(f,start,end)
    return _inner

@annealer
def lin_scheduler(start,end,pos): return start + pos*(end-start)

@annealer
def cos_scheduler(start,end,pos): return ((end-start)/2)*math.cos((pos-1)*math.pi) + ((end+start)/2)

@annealer
def exp_scheduler(start,end,pos): return start*(end/start)**pos




def combine_scheds(pcts, scheds):
    assert int(sum(pcts)) == 1
    assert len(pcts) == len(scheds)
    pcts = tensor([0] + pcts)
    pcts = torch.cumsum(pcts,0)
    def _inner(pos):
        idx = (pos >= pcts).sum().int()
        idx -= 2 if idx == len(pcts) else 1
        npos = (pos - pcts[idx])/(pcts[idx+1] - pcts[idx])
        return scheds[idx](npos)
    return _inner

def get_model_opt(channels,data,bn = False,layer = conv,optimizer = 'SGD',lr = 0.1,mom=0.1,eps = 1e-5,**kwargs):
    channels = [1] + channels
    layers = [layer(channels[i], channels[i+1], mom,eps,ks = 5 if i==0 else 3,bn = bn,**kwargs) for i in range(len(channels)-1)]
    layers += [nn.AdaptiveAvgPool2d(1), Lambda(flatten), nn.Linear(channels[-1],data.c)]
    model = nn.Sequential(*layers)
    opt = getattr(optim,optimizer)
    return model,opt(model.parameters(), lr = lr)


def get_learn_run(cbfs,model,opt,data,loss_func = F.cross_entropy):
    learn = Learner(model,opt,data,loss_func)
    run = Runner(cb_funcs= cbfs)
    return learn,run

cbfs = [partial(BatchTransformCallback, view_tfm(1,28,28)), CudaCallback, partial(AvgStatsCallback, accuracy),
       partial(ParamSchedulerCallback, 'lr', combine_scheds([0.3,0.7], [cos_scheduler(0.01,0.1), cos_scheduler(0.2,0.05)])),
        RecorderCallback]

def init_cnn_(m, f):
    if isinstance(m, nn.Conv2d):
        f(m.weight, a=0.1)
        if getattr(m, 'bias', None) is not None: m.bias.data.zero_()
    for l in m.children(): init_cnn_(l, f)

def init_cnn(m, uniform=False):
    f = init.kaiming_uniform_ if uniform else init.kaiming_normal_
    init_cnn_(m, f)