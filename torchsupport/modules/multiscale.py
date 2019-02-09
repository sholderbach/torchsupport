import torch
import torch.nn as nn
import torch.nn.functional as func
from torchsupport.modules.attention import AttentionBranch

class Autoscale(nn.Module):
  def __init__(self, multigrid_module, preprocess=None, activation=func.tanh):
    """Pixel-wise feature scale selection layer using attention.
    
    Args:
      multigrid_module (nn.Module): module performing a multi-grid convolution.
      preprocess (nn.Module): module performing feature preprocessing for attention.
      activation (nn.Module): activation function for attention computation. 
    """
    super(Autoscale, self).__init__()
    in_channels = multigrid_module.levels[0].in_channels
    self.branch = AttentionBranch2d(multigrid_module, in_channels,
                                    preprocess=preprocess, activation=activation)

  def forward(self, input):
    return self.branch(input)

class HierarchicalFeatureFusion(nn.Module):
  def __init__(self, modules, in_channels,
               activation=nn.ReLU(), batch_norm=True):
    """Hierarchically fuses features generated by a list of modules.
    
    Args:
      modules (iterable): an iterable of `nn.Module`s.
      in_channels (int): number of input channels.
      activation (nn.Module): activation function.
      batch_norm (bool): use batch normalization?
    """
    super(HierarchicalFeatureFusion, self).__init__()
    self.activation = activation
    self.batch_norm = batch_norm
    if self.batch_norm:
      self.bn_pre = nn.BatchNorm2d(in_channels)
    self.preprocess = nn.Conv2d(in_channels, in_channels // len(modules), 1)
    self.modules = nn.ModuleList(modules)

  def forward(self, input):
    out = self.preprocess(input)
    if self.bn_pre:
      out = self.bn_pre(out)
    outputs = []
    previous_output = 0
    for module in self.modules:
      current_output = module(out) + previous_output
      outputs.append(current_output)
      previous_output = current_output
    out = torch.cat(outputs, dim=1) + input
    out = self.activation(out)
    return out

class DilationCascade(nn.Module):
  def __init__(self, channels, kernel_size, levels=[1,2,4,8], merger=None,
               batch_norm=True, activation=nn.ReLU):
    """Performs a series of dilated convolutions on a single input.
    
    Args:
      channels (int): number of input and output channels.
      kernel_size (int): convolutional kernel size.
      levels (list int): list of convolutional scales.
    """
    super(DilationCascade, self).__init__()
    self.merger = merger
    self.levels = nn.ModuleList([
      nn.Conv2d(channels, channels, kernel_size,
                dilation=level, padding=(kernel_size // 2) * level)
      for level in levels
    ])
    self.activation = activation
    self.batch_norms = None
    if batch_norm:
      self.batch_norms = nn.ModuleList([
        nn.BatchNorm2d(channels)
        for level in levels
      ])

  def forward(self, input):
    if self.merger != None:
      outputs = []
      out = input
      for idx, level in enumerate(self.levels):
        out = level(out)
        if self.batch_norms != None:
          out = self.batch_norms[idx](out)
        out = self.activation(out)
        outputs.append(out)
      return self.merger(outputs)
    else:
      out = input
      for idx, level in enumerate(self.levels):
        out = level(out)
        if self.batch_norms != None:
          out = self.batch_norms[idx](out)
        out = self.activation(out)
      return out

class DilatedMultigrid(nn.Module):
  def __init__(self, in_channels, out_channels, kernel_size, levels=[0,1,2,4],
               merger=lambda x: torch.cat(x, dim=1), batch_norm=True,
               activation=nn.ReLU):
    """Dilated multi-grid convolution block.
    
    Args:
      in_channels (int): number of input channels.
      out_channels (int): number of output channels.
      kernel_size (int): convolutional kernel size.
      levels (list int): list of convolutional scales.
      merger (callable): procedure for merging multiple scale features.
    """
    super(DilatedMultigrid, self).__init__()
    self.merger = merger
    self.levels = nn.ModuleList([
      nn.Conv2d(in_channels, out_channels, kernel_size,
                dilation=level, padding=(kernel_size // 2) * level)
      if level != 0 else nn.Conv2d(in_channels, out_channels, 1)
      for level in levels
    ])
    self.activation = activation
    self.batch_norms = None
    if batch_norm:
      self.batch_norms = nn.ModuleList([
        nn.BatchNorm2d(out_channels)
        for level in levels
      ])

  def __len__(self):
    return len(self.levels)

  def forward(self, input):
    outputs = []
    for idx, level in enumerate(self.levels):
      out = level(input)
      if self.batch_norms != None:
        out = self.batch_norms[idx](out)
      out = self.activation(out)
      outputs.append(out)
    return self.merger(outputs)

def DilatedPyramid(channels, kernel_size, levels=[1,2,4,8],
                   merger=lambda x: torch.cat(x, dim=1),
                   batch_norm=True, activation=nn.ReLU):
  """Pyramid construction version of `DilationCascade`. See `DilationCascade`."""
  return DilationCascade(channels, kernel_size, levels=levels, merger=merger,
                         batch_norm=batch_norm, activation=activation)

class PoolingMultigrid(nn.Module):
  def __init__(self, in_channels, out_channels, kernel_size, levels=[3,5,7,9],
               merger=lambda x: torch.cat(x, dim=1), batch_norm=True,
               activation=nn.ReLU):
    """Pooled multi-grid convolution block.
    
    Args:
      in_channels (int): number of input channels.
      out_channels (int): number of output channels.
      kernel_size (int): convolutional kernel size.
      levels (list int): list of convolutional scales.
      merger (callable): procedure for merging multiple scale features.
    """
    super(PoolingMultigrid, self).__init__()
    self.merger = merger
    self.levels = nn.ModuleList([
      nn.Sequential(
        nn.MaxPool2d(level, stride=1, padding=level // 2),
        nn.Conv2d(in_channels, out_channels, kernel_size, padding=kernel_size // 2)
      )
      if level != 0 else nn.Conv2d(in_channels, out_channels, 1)
      for level in levels
    ])
    self.activation = activation
    self.batch_norms = None
    if batch_norm:
      self.batch_norms = nn.ModuleList([
        nn.BatchNorm2d(out_channels)
        for level in levels
      ])

  def __len__(self):
    return len(self.levels)

  def forward(self, input):
    outputs = []
    for idx, level in enumerate(self.levels):
      out = level(input)
      if self.batch_norms != None:
        out = self.batch_norms[idx](out)
      out = self.activation(out)
      outputs.append(out)
    return self.merger(outputs)

class PoolingPyramid(nn.Module):
  def __init__(self, channels, kernel_size, pooling_size, depth=4,
               merger=lambda x: torch.cat(x, dim=1), batch_norm=False,
               activation=func.relu):
    """Iterative pooling image pyramid construction.
    
    Args:
      channels (int): number of input channels.
      kernel_size (int): convolutional kernel size.
      pooling_size (int): pooling kernel size.
      depth (int): number of pyramid layers.
      merger (callable): procedure for merging pyramid features.
    """
    super(PoolingPyramid, self).__init__()
    self.merger = merger
    self.levels = nn.ModuleList([
      nn.Conv2d(channels, channels, kernel_size, padding=kernel_size // 2)
      for _ in range(depth)
    ])
    self.pools = nn.ModuleList([
      nn.MaxPool2d(pooling_size, padding=pooling_size // 2)
      for _ in range(depth)
    ])
    self.post_levels = nn.ModuleList([
      nn.Conv2d(channels, channels, kernel_size, padding=kernel_size // 2)
      for _ in range(depth)
    ])
    self.activation = activation
    self.batch_norms = None
    self.post_batch_norms = None
    if batch_norm:
      self.batch_norms = nn.ModuleList([
        nn.BatchNorm2d(channels)
        for _ in range(depth)
      ])
      self.post_batch_norms = nn.ModuleList([
        nn.BatchNorm2d(channels)
        for _ in range(depth)
      ])

  def forward(self, input):
    outputs = []
    pass_through = input
    for idx, level in enumerate(self.levels):
      pass_through = level(pass_through)
      if self.batch_norms != None:
        pass_through = self.batch_norms[idx](pass_through)
      pass_through = self.activation(pass_through)
      out = self.post_levels(pass_through)
      if self.post_batch_norms != None:
        out = self.post_batch_norms[idx](out)
      out = self.activation(out)
      outputs.append(out)
    return self.merger(outputs)

class ContextAggregation(nn.Module):
  def __init__(self, modules, branch_from_layers=[], refine=False, activation=nn.Tanh()):
    """Aggregates the output of a given network into a pyramid.
    
    Args:
      modules (iterable or nn.Module): a list of modules to be aggregated.
      branch_from_layers (iterable): list of layers to extract intermediate representations from.
      refine (bool): perform attention refinement on intermediate representations?
      activation (nn.Module): activation function for attention refinement.
    """
    self.is_module = False
    if isinstance(modules, nn.Module):
      self.is_module = True
    self.modules = modules
    self.branch = branch_from_layers
    self.outputs = []
    if self.is_module:
      def hook(module, input, output):
        self.outputs.append(output)
      for module_name, channels in self.branch:
        self.modules.__dict__[module_name].register_forward_hook(hook)
    if self.refine:
      self.attention_refinements = nn.ModuleList([
        nn.Sequential(
          nn.AdaptiveAvgPool2d(1),
          nn.Conv2d(channels, channels, 3),
          nn.BatchNorm2d(channels),
          activation
        )
        for branch, channels in self.branch
      ])
  
  def forward(self, input):
    upsize = tuple(input.size()[-2:])
    if self.is_module:
      out = self.modules(input)
      out = func.adaptive_avg_pool2d(input, 1)
      out = func.interpolate(out, size=upsize)
      for idx, output in enumerate(self.outputs):
        self.outputs[idx] = func.interpolate(output, size=upsize, mode='bilinear')
      outputs = self.outputs + [out]
      self.outputs = []
    else:
      out = input
      outputs = []
      for idx, module in enumerate(self.modules):
        out = module(outputs)
        if idx in self.branch:
          outputs.append(out)
      out = func.adaptive_avg_pool2d(input, 1)
      out = func.interpolate(out, size=upsize)
      outputs.append(out)
    if self.refine:
      for idx, output in enumerate(outputs):
        if idx < len(outputs) - 1:
          outputs[idx] = self.attention_refinements(idx) * output
    return torch.cat(outputs, dim=1)
