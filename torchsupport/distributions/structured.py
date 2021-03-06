import torch
from torch.distributions.distribution import Distribution
from torch.distributions.kl import register_kl

from torchsupport.data.match import Matchable, match

class DistributionList(Matchable, Distribution):
  has_rsample = True
  def __init__(self, items):
    self.items = items

  def match(self, other):
    result = 0.0

    # print(self.items[0].loc, other.items[0].loc)
    # print(self.items[0].scale, other.items[0].scale)
    for s, o in zip(self.items, other.items):
      match_result = match(s, o)
      result = result + match_result
    return result

  def log_prob(self, value):
    log_prob = 0.0
    for dist, val in zip(self.items, value):
      log_prob = log_prob + dist.log_prob(val)
    return log_prob

  def sample(self, sample_shape=torch.Size()):
    return [
      dist.sample(sample_shape=sample_shape)
      for dist in self.items
    ]

  def rsample(self, sample_shape=torch.Size()):
    return [
      dist.sample(sample_shape=sample_shape)
      for dist in self.items
    ]
