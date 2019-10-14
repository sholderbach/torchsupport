import pytest
import torch
import numpy as np
from torch.testing import assert_allclose

from torchsupport.ops.distance import (mean_squared_distance_matrix, 
                                       multi_rbf_distance_matrix, 
                                       rbf_distance_matrix, 
                                       sum_of_squared_distance_matrix)

shape1 = (20, 50)
shape2 = (30, 50)
data1 = torch.randn(shape1)
data2 = torch.randn(shape2)

@pytest.mark.parametrize('func', [mean_squared_distance_matrix,
                                  multi_rbf_distance_matrix,
                                  rbf_distance_matrix,
                                  sum_of_squared_distance_matrix])
def test_shapes(func):
  assert func(data1, data1).shape == (20, 20)
  assert func(data1).shape == (20, 20)
  assert func(data1, data2).shape == (20, 30)
  with pytest.raises(ValueError):
    func(data1, torch.ones((20, 51)))

@pytest.mark.parametrize('func', [mean_squared_distance_matrix,
                                  multi_rbf_distance_matrix,
                                  rbf_distance_matrix,
                                  sum_of_squared_distance_matrix])
def test_transpose(func):
  res = func(data1, data1)
  assert_allclose(res, res.transpose(0,1))

  res = func(data1)
  assert_allclose(res, res.transpose(0,1))

  res1 = func(data1, data2)
  res2 = func(data2, data1)
  assert_allclose(res1, res2.transpose(0,1))


@pytest.mark.parametrize('func,val', [(mean_squared_distance_matrix,0),
                                  (multi_rbf_distance_matrix,1),
                                  (rbf_distance_matrix,1),
                                  (sum_of_squared_distance_matrix,0)])
def test_diag(func, val):
  res = func(data1)
  assert_allclose(res.diag(), torch.tensor([val]*20, dtype=torch.float))
