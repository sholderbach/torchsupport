import torch
import torch.nn as nn
import torch.nn.functional as func

from torchsupport.structured.structures import (
  ConstantStructure, ScatterStructure, MessageMode
)
from .. import scatter

def flatten_message(message):
  r"""Flattens the batch and neighbourhood dimensions of an input message.

  Args:
    message (torch.Tensor): batch of messages.

  Shape:
    - Message: :math:`(N, N_{neighbours}, ...)`
    - Output: :math:`(N * N_{neighbours}, ...)`
  """
  return message.view(-1, *message.shape[2:])

def unflatten_message(output, message):
  r"""Reverses a flatten operation given the original neighbourhood dimension.

  Args:
    output (torch.Tensor): result of a computation on a flattened neighbourhood.
    message (torch.Tensor): original neighbourhood tensor to extract neighbourhood
      dimensions from.

  Shape:
    - Output: :math:`(N * N_{neighbours}, B...)`
    - Message: :math:`(N, N_{neighbours}, A...)`
    - Result: :math:`(N, N_{neighbours}, B...)`
  """
  return output.view(*message.shape[:2], *output.shape[1:])

class ConnectedModule(nn.Module):
  def __init__(self, has_scatter=False):
    """Applies a reduction function to the neighbourhood of each entity."""
    super(ConnectedModule, self).__init__()
    self.has_scatter = has_scatter

  def reduce(self, own_data, source_messages):
    raise NotImplementedError("Abstract")

  def reduce_scatter(self, own_data, source_message, indices, node_count):
    r"""Aggregates neighbourhood information using scatter operations on
    ragged tensors. Ragged tensors are represented by a data tensor coupled
    with an index tensor encoding variable-size chunks on which to operate
    (LINK).

    Args:
      own_data (torch.Tensor): tensor containing target features for each node.
      source_message (torch.Tensor): tensor containing neighbourhood features
        for each node neighbourhood.
      indices (torch.Tensor): long tensor of indices. Features of each neighbourhood
        are marked with a unique neighbourhood index.
      node_count (int): total number of nodes in the graph. Used to fill up features
        of nodes for which the neighbourhood is empty.
    """
    raise NotImplementedError("Abstract")

  def forward(self, source, target, structure):
    # constant-width neighbourhoods:
    if structure.mode_is(MessageMode.constant):
      return self.reduce(target, structure.message(source, target))
    if structure.mode_is(MessageMode.scatter):
      if not self.has_scatter:
        raise NotImplementedError(
          "Scattering-based implementation not supported for {self.__class__.__name__}."
        )
      source, target, indices, node_count = structure.message(source, target)
      return self.reduce_scatter(target, source, indices, node_count)

    results = []
    for idx, message in enumerate(structure.message(source, target)):
      reduced = self.reduce(target[idx].unsqueeze(dim=0), message)
      results.append(reduced)
    return torch.cat(results, dim=0)

class NeighbourLinear(ConnectedModule):
  r"""Aggregates neighbourhood information using a linear transformation of
  source and target features, followed by averaging. Corresponds to a standard
  GNN layer.

  Args:
    source_channels (int): number of neighbour features.
    target_channels (int): number of target node features.
    normalization (callable): weight normalization applied to the learned
      linear transformation (e.g. spectral normalization).

  Shape:
    - Source: :math:`(\sum_i M_{i}, C_{source})`
    - Target: :math:`(\sum_i N_{i}, C_{target})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{target})`
  """
  def __init__(self, source_channels, target_channels, normalization=lambda x: x):
    super(NeighbourLinear, self).__init__(has_scatter=True)
    self.linear = normalization(nn.Linear(source_channels, target_channels))

  def reduce_scatter(self, own_data, source_message, indices, node_count):
    return scatter.mean(
      own_data + func.relu(self.linear(source_message)),
      indices, dim_size=node_count
    )

  def reduce(self, own_data, source_message):
    inputs = flatten_message(source_message)
    out = unflatten_message(self.linear(inputs), source_message)
    return own_data + func.relu(out).mean(dim=1)

class NeighbourAssignment(ConnectedModule):
  r"""Aggregates neighbourhood information using a neighbour-weighted sum of
  linear maps, approximating convolution on irregular graphs (FeaStNet).

  Args:
    source_channels (int): number of neighbour features.
    target_channels (int): number of target node features.
    out_channels (int): number of output feature maps.
    size (int): number of linear maps to be aggregated per neighbourhood.
      Roughly corresponds to the size of a convolution kernel.
    normalization (callable): weight normalization applied to the learned
      linear transformation (e.g. spectral normalization).

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{source})`
    - Target: :math:`(\sum_i N_{i}, C_{target})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def __init__(self, source_channels, target_channels, out_channels, size,
               normalization=lambda x: x):
    super(NeighbourAssignment, self).__init__(has_scatter=True)
    self.linears = nn.ModuleList([
      normalization(nn.Linear(source_channels, out_channels, bias=False))
      for _ in range(size)
    ])
    self.bias = nn.Parameter(torch.randn(1, out_channels))
    self.source = normalization(nn.Linear(source_channels, size))
    self.target = normalization(nn.Linear(target_channels, size))

  def reduce_scatter(self, own_data, source_message, indices, node_count):
    target = self.target(own_data)
    source = self.source(source_message)
    weight_tensors = []
    for module in self.linears:
      weight_tensors.append(module(source_message).unsqueeze(0))
    weighted = torch.cat(weight_tensors, dim=0)
    assignment = func.softmax(source + target, dim=-1).unsqueeze(0)
    result = assignment.transpose(0, 3) * weighted
    return scatter.mean(result, dim_size=node_count)

  def reduce(self, own_data, source_message, idx=None):
    inputs = flatten_message(source_message)
    target = self.target(own_data).unsqueeze(0)
    source = self.source(inputs)
    weight_tensors = []
    for module in self.linears:
      result = unflatten_message(module(inputs), source_message)
      weight_tensors.append(result.unsqueeze(0))
    weighted = torch.cat(weight_tensors, dim=0)
    source = unflatten_message(source, source_message)
    assignment = func.softmax(source + target.transpose(0, 1), dim=1).unsqueeze(0)
    result = (assignment.transpose(0, 3) * weighted).sum(dim=0).mean(dim=1)
    return result + self.bias

class NeighbourAttention(ConnectedModule):
  r"""Aggregates neighbourhood information using pairwise attention with a
  user-defined softmax kernel. Common kernel choices include dot-product and
  :math:`L^2` attention. The resulting kernel has the form
  :math:`softmax_i(attend(K_i, Q_i))`, where
  :math:`softmax_i(x_i) := \frac{\exp(x_i)}{\sum_i \exp(x\i)}` is the softmax
  function and :math:`attend(K, Q)` is a user-defined kernel.

  Args:
    in_size (int): number of neighbour features.
    out_size (int): number of output features.
    query_size (int): number of target node features.
    attention_size (int): number of feature maps used in the attention kernel.

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{in})`
    - Target: :math:`(\sum_i N_{i}, C_{query})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def __init__(self, in_size, out_size, query_size=None, attention_size=None):
    """Aggregates a node neighbourhood using a pairwise dot-product attention mechanism.
    Args:
      size (int): size of the attention embedding.
    """
    super(NeighbourAttention, self).__init__(has_scatter=True)
    query_size = query_size if query_size is not None else in_size
    attention_size = attention_size if attention_size is not None else in_size
    self.query = nn.Linear(query_size, attention_size)
    self.key = nn.Linear(in_size, attention_size)
    self.value = nn.Linear(in_size, out_size)

  def attend(self, query, data):
    r"""Compares query and data features defining an attention kernel.
    Common choices include the dot-product, addition and :math:`L^p` norms.

    Args:
      query (torch.Tensor): tensor containing query information.
      data (torch.Tensor): tensor containing key information.

    Shape:
      - Query: :math:`(\sum_i N_i^2, C)`
      - Data: :math:`(\sum_i N_i^2, C)`
      - Output: :math:`\sum_i N_i^2`
    """
    raise NotImplementedError("Abstract.")

  def reduce_scatter(self, own_data, source_message, indices, node_count):
    target = self.query(own_data)
    source = self.key(source_message)
    value = self.value(source_message)
    attention = scatter.softmax(
      self.attend(target, source), indices, dim_size=node_count
    )
    result = scatter.add(
      (attention.unsqueeze(-1) * value), indices, dim_size=node_count
    )
    return result

  def reduce(self, own_data, source_message):
    target = self.query(own_data)
    inputs = flatten_message(source_message)
    source = self.key(inputs)
    value = self.value(inputs)
    source = unflatten_message(source, source_message)
    value = unflatten_message(value, source_message)

    attention = func.softmax(self.attend(target.unsqueeze(1), source), dim=1)
    result = (attention.unsqueeze(-1) * value).sum(dim=1)
    return result

class NeighbourDotAttention(NeighbourAttention):
  r"""Aggregates neighbourhood information pairwise dot-product attention.
  The attention kernel is :math:`softmax_j(\sum_i K^T_{ji} Q_{ji})`, where
  :math:`softmax_i(x_i) := \frac{\exp(x_i)}{\sum_i \exp(x\i)}` is the softmax
  function.

  Args:
    in_size (int): number of neighbour features.
    out_size (int): number of output features.
    query_size (int): number of target node features.
    attention_size (int): number of feature maps used in the attention kernel.

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{in})`
    - Target: :math:`(\sum_i N_{i}, C_{query})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def attend(self, query, data):
    return (query * data).sum(dim=-1)

class NeighbourAddAttention(NeighbourAttention):
  r"""Aggregates neighbourhood information using pairwise additive attention.
  The attention kernel is :math:`softmax_j(\sum_i Q_{ji} + K_{ji})`, where
  :math:`softmax_i(x_i) := \frac{\exp(x_i)}{\sum_i \exp(x\i)}` is the softmax
  function.

  Args:
    in_size (int): number of neighbour features.
    out_size (int): number of output features.
    query_size (int): number of target node features.
    attention_size (int): number of feature maps used in the attention kernel.

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{in})`
    - Target: :math:`(\sum_i N_{i}, C_{query})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def attend(self, query, data):
    return (query + data).sum(dim=-1)

class NeighbourMultiHeadAttention(ConnectedModule):
  r"""Aggregates neighbourhood information using pairwise multi-head attention
  with a user-defined softmax kernel. Common kernel choices include dot-product
  and :math:`L^2` attention. The resulting kernel has the form
  :math:`softmax_i(attend(K_i, Q_i))`, where
  :math:`softmax_i(x_i) := \frac{\exp(x_i)}{\sum_i \exp(x\i)}` is the softmax
  function and :math:`attend(K, Q)` is a user-defined kernel.
  The attention operation is replicated across a number of independent heads
  which are then aggregated into a set of common output feature maps.

  Args:
    in_size (int): number of neighbour features.
    out_size (int): number of output features.
    attention_size (int): number of feature maps used in the attention kernel.
    query_size (int): number of target node features.
    heads (int): number of parallel attention heads. The final number of
      feature maps is calculated as
      :math:`\texttt{attention_size} \cdot \texttt{heads}`.
    normalization (callable): weight normalization applied to the learned
      linear transformation (e.g. spectral normalization).

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{in})`
    - Target: :math:`(\sum_i N_{i}, C_{query})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def __init__(self, in_size, out_size, attention_size, query_size=None, heads=64,
               normalization=lambda x: x):
    super(NeighbourMultiHeadAttention, self).__init__(has_scatter=True)
    query_size = query_size if query_size is not None else in_size
    self.query_size = query_size
    self.attention_size = attention_size
    self.heads = heads
    self.out_size = out_size
    self.query = normalization(nn.Linear(query_size, heads * attention_size))
    self.key = normalization(nn.Linear(in_size, heads * attention_size))
    self.value = normalization(nn.Linear(in_size, heads * attention_size))
    self.output = normalization(nn.Linear(heads * attention_size, out_size))

  def attend(self, query, data):
    r"""Compares query and data features defining an attention kernel.
    Common choices include the dot-product, addition and :math:`L^p` norms.

    Args:
      query (torch.Tensor): tensor containing query information.
      data (torch.Tensor): tensor containing key information.

    Shape:
      - Query: :math:`(\sum_i N_i^2, C)`
      - Data: :math:`(\sum_i N_i^2, C)`
      - Output: :math:`\sum_i N_i^2`
    """
    raise NotImplementedError("Abstract.")

  def reduce_scatter(self, own_data, source_message, indices, node_count):
    if indices.size(0) == 0:
      return torch.zeros(node_count, self.out_size, dtype=own_data.dtype, device=own_data.device)
    target = self.query(own_data).view(*own_data.shape[:-1], -1, self.heads)
    source = self.key(source_message).view(*source_message.shape[:-1], -1, self.heads)
    value = self.value(source_message).view(*source_message.shape[:-1], -1, self.heads)
    attention = scatter.softmax(
      self.attend(target, source), indices, dim_size=node_count
    )
    result = scatter.add(
      (attention.unsqueeze(-2) * value), indices, dim_size=node_count
    )
    result = self.output(result.view(*result.shape[:-2], -1))
    return result

  def reduce(self, own_data, source_message):
    target = self.query(own_data).view(*own_data.shape[:-1], -1, self.heads)
    source = flatten_message(source_message)
    inputs = self.value(source).view(*source_message.shape[:-1], -1, self.heads)
    source = self.key(source).view(*source_message.shape[:-1], -1, self.heads)
    attention = func.softmax(self.attend(target.unsqueeze(1), source), dim=1).unsqueeze(-2)
    out = (attention * inputs).sum(dim=1)
    out = out.view(*out.shape[:-2], -1)
    result = self.output(out)
    return result

class NeighbourDotMultiHeadAttention(NeighbourMultiHeadAttention):
  r"""Aggregates neighbourhood information using pairwise multi-head attention
  with a dot-product kernel. The resulting kernel has the form
  :math:`softmax_j(\sum_i K_{jhi} \cdot Q_{jhi}))`, where
  :math:`softmax_i(x_i) := \frac{\exp(x_i)}{\sum_i \exp(x\i)}` is the softmax
  function.
  The attention operation is replicated across a number of independent heads
  :math:`h` which are then aggregated into a set of common output feature maps.

  Args:
    in_size (int): number of neighbour features.
    out_size (int): number of output features.
    attention_size (int): number of feature maps used in the attention kernel.
    query_size (int): number of target node features.
    heads (int): number of parallel attention heads. The final number of
      feature maps is calculated as
      :math:`\texttt{attention_size} \cdot \texttt{heads}`.
    normalization (callable): weight normalization applied to the learned
      linear transformation (e.g. spectral normalization).

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{in})`
    - Target: :math:`(\sum_i N_{i}, C_{query})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def attend(self, query, data):
    scaling = torch.sqrt(torch.tensor(self.attention_size, dtype=torch.float))
    return (query * data).sum(dim=-2) / scaling

class NeighbourAddMultiHeadAttention(NeighbourMultiHeadAttention):
  r"""Aggregates neighbourhood information using pairwise multi-head attention
  with a dot-product kernel. The resulting kernel has the form
  :math:`softmax_j(\sum_i K_{jhi} + Q_{jhi}))`, where
  :math:`softmax_i(x_i) := \frac{\exp(x_i)}{\sum_i \exp(x\i)}` is the softmax
  function.
  The attention operation is replicated across a number of independent heads
  :math:`h` which are then aggregated into a set of common output feature maps.

  Args:
    in_size (int): number of neighbour features.
    out_size (int): number of output features.
    attention_size (int): number of feature maps used in the attention kernel.
    query_size (int): number of target node features.
    heads (int): number of parallel attention heads. The final number of
      feature maps is calculated as
      :math:`\texttt{attention_size} \cdot \texttt{heads}`.
    normalization (callable): weight normalization applied to the learned
      linear transformation (e.g. spectral normalization).

  Shape:
    - Source: :math:`(\sum_i N_{i}, C_{in})`
    - Target: :math:`(\sum_i N_{i}, C_{query})`
    - Structure: ScatterStructure with :math:`\sum_i N_i` nodes
        or ConstantStructure with :math:`(\sum_i N_i, N_{neighbours})` nodes
        and neighbours.
    - Output: :math:`(\sum_i N_i, C_{out})`
  """
  def attend(self, query, data):
    return (query + data).sum(dim=-2)

class NeighbourReducer(ConnectedModule):
  def __init__(self, reduction):
    super(NeighbourReducer, self).__init__()
    self.reduction = reduction

  def reduce(self, own_data, source_message):
    return self.reduction(source_message, dim=1)

class NeighbourMean(NeighbourReducer):
  def __init__(self):
    super(NeighbourMean, self).__init__(torch.mean)

class NeighbourSum(NeighbourReducer):
  def __init__(self):
    super(NeighbourSum, self).__init__(torch.sum)

class NeighbourMin(NeighbourReducer):
  def __init__(self):
    super(NeighbourMin, self).__init__(torch.min)

class NeighbourMax(NeighbourReducer):
  def __init__(self):
    super(NeighbourMax, self).__init__(torch.max)

class NeighbourMedian(NeighbourReducer):
  def __init__(self):
    super(NeighbourMedian, self).__init__(torch.median)

class GraphResBlock(nn.Module):
  """Residual block for graph networks.

  Args:
    channels (int): number of input and output features.
    aggregate (:class:`ConnectedModule`): neighbourhood aggregation function.
    activation (nn.Module): activation function. Defaults to ReLU.
  """
  def __init__(self, channels, aggregate=NeighbourMax,
               activation=nn.ReLU()):
    super(GraphResBlock, self).__init__()
    self.activation = activation
    self.aggregate = aggregate
    self.linear = nn.Linear(2 * channels, channels)

  def forward(self, graph, structure):
    out = self.aggregate(graph, graph, structure)
    out = self.linear(out)
    out = self.activation(out + graph)
    return out
