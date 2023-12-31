"""Contains the tensor class that can be used for building neural networks with forward and backward pass.

The module contains the "high-level ops". These are syntax sugar and built on top of the "mid-level ops" containing the
the functions with forward and backward passes in Function.function which is build on top of the "low-level ops"
defining the numpy backend with the most basic operations in data.TensorData.

The high-level ops support many things that you could expect from a tensor library.

"""
# inspired by https://github.com/karpathy/micrograd/blob/master/micrograd/engine.py
from __future__ import annotations
import time
import math
from typing import ClassVar, Sequence, Any

import numpy as np

from edugrad.dtypes import DType, dtypes, DTYPES_DICT
from edugrad.helpers import getenv, DEBUG, prod, all_int, round_up, shape_int
from edugrad.data import TensorData
from edugrad.ops import LoadOps
from edugrad.function import Function
import edugrad.function as function
from edugrad.autograd import backward, collect_backward_graph

# fmt: off
from edugrad._tensor.tensor_create import _loadop, empty, manual_seed, rand
from edugrad._tensor.tensor_create import randn, randint, normal, uniform, scaled_uniform
from edugrad._tensor.tensor_create import full, zeros, ones, arange, eye, full_like, zeros_like, ones_like
from edugrad._tensor.tensor_combine_segment import cat, stack, repeat, chunk
from edugrad._tensor.tensor_reshape import reshape, expand, permute, flip, shrink, pad, pad2d, transpose, _flatten, squeeze, unsqueeze
from edugrad._tensor.tensor_nn import _pool, avg_pool2d, max_pool2d, conv2d, linear, binary_crossentropy, binary_crossentropy_logits, sparse_categorical_crossentropy
from edugrad._tensor.tensor_index_slice import __getitem__, __setitem__, tslice, gather
from edugrad._tensor.tensor_broadcasted_binary_mlops import _broadcasted, _to_float, add, sub, mul, div, pow, matmul, maximum, minimum, where
from edugrad._tensor.tensor_reduce import _reduce, tsum, tmax, tmin, mean, std, _softmax, softmax, log_softmax, argmax, argmin
# fmt: on


class Tensor:
    __slots__ = "data", "requires_grad", "grad", "_ctx"
    __deletable__ = ("_ctx",)
    training: ClassVar[bool] = False

    class train:
        def __init__(self, val=True):
            self.val = val

        def __enter__(self):
            self.prev, Tensor.training = Tensor.training, self.val

        def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any):
            Tensor.training = self.prev

    no_grad: ClassVar[bool] = False
    default_type: ClassVar[DType] = dtypes.float32

    def __init__(
        self,
        data: None | int | float | list | TensorData | np.ndarray | bytes,
        dtype: DType | None = None,
        requires_grad: bool | None = None,
    ):
        assert dtype is None or isinstance(
            dtype, DType
        ), f"Invalid dtype {dtype}. edugrad only supports {list(DTYPES_DICT.keys())}"

        # tensors have gradients, buffers do not
        self.grad: Tensor | None = None

        # NOTE: this can be in three states. False and None: no gradient, True: gradient
        # None (the default) will be updated to True if it's put in an optimizer
        self.requires_grad: bool | None = requires_grad

        # internal variables used for autograd graph construction
        self._ctx: Function | None = None

        # --------------------------------------------------------------------------------------------------------------
        # Handles Tensor(x) for x with different data types.
        # We cast x = list(y) up to float32 (default_type) for every type that y can have if no other type is provided

        if isinstance(data, TensorData):
            assert dtype is None or dtype == data.dtype, "dtype doesn't match, and casting isn't supported"

        elif isinstance(data, (bool, int, float)):
            data = TensorData.loadop(LoadOps.CONST, tuple(), dtype or dtypes.from_py(data), data)

        elif isinstance(data, list):
            data = TensorData(np.array(data, dtype=(dtype or Tensor.default_type).np))

        elif data is None:
            data = TensorData.loadop(LoadOps.EMPTY, (0,), dtype or dtypes.only_float)

        elif isinstance(data, np.ndarray):
            assert dtype is None or dtype.np is not None, f"{dtype} doesn't have a numpy dtype"
            if data.shape == ():
                data = TensorData.loadop(LoadOps.CONST, tuple(), dtype or dtypes.from_np(data.dtype), data.item())
            else:
                data = TensorData(data.astype(dtype.np) if dtype is not None and dtype.np is not None else data)

        if not isinstance(data, TensorData):
            raise RuntimeError(f"can't create Tensor from {data!r} with type {type(data)}")
        self.data = data

    # ------------------------------------------------------------------------------------------------------------------
    # basic properties

    def __repr__(self):
        return f"<Tensor {self.data!r} with grad {(self.grad.data if self.grad else None)!r}>"

    # Python has a non moving garbage collector, so this should be okay
    def __hash__(self):
        return id(self)

    @property
    def shape(self) -> tuple[shape_int, ...]:
        return self.data.shape

    @property
    def dtype(self) -> DType:
        return self.data.dtype

    # ------------------------------------------------------------------------------------------------------------------
    # data handlers

    def assign(self, x: Any) -> Tensor:
        """Assigns the value of another tensor or array to the current tensor.

        This method is a workaround for writing to disk and is used for in-place modification of tensor data.

        """
        if not isinstance(x, Tensor):
            # Convert x to a Tensor if it's not already one
            x = Tensor(x, dtype=self.dtype)

        assert self.shape == x.shape, f"assign shape mismatch {self.shape} != {x.shape}"
        assert not x.requires_grad  # Ensure x doesn't require gradient computation

        if DEBUG >= 4:
            print(f"assign {self.data} <- {x.data}")

        # If dtype matches and assignment is allowed, perform the assignment
        if self.dtype == x.dtype and self.data is not None and not getenv("DISALLOW_ASSIGN"):
            x.data.output_buffer = self.data

        self.data = x.data
        return self

    # ------------------------------------------------------------------------------------------------------------------
    # basic tensor manipulations

    def detach(self) -> Tensor:
        return Tensor(self.data, requires_grad=False)

    def numpy(self) -> np.ndarray:
        assert all_int(self.shape), f"no numpy if shape is symbolic, {self.shape=}"
        assert self.dtype.np is not None, f"no numpy dtype for {self.dtype}"
        return self.detach().cast(dtypes.from_np(self.dtype.np)).data.data.reshape(self.shape)

    def item(self) -> float | int:
        return self.numpy().item()

    # fmt: off

    # ------------------------------------------------------------------------------------------------------------------
    # tensor_create.py
    # creation low-level op entrypoint

    @staticmethod
    def _loadop(op, sz, dtype:DType | None=None, arg=None, **kwargs): return _loadop(op, sz, dtype, arg, **kwargs)

    @staticmethod
    def empty(*shape, **kwargs): return empty(*shape, **kwargs)

    _seed: int = int(time.time())
    @staticmethod
    def manual_seed(seed=0): return manual_seed(seed)

    @staticmethod
    def rand(*shape, **kwargs): return rand(*shape, **kwargs)

    # creation helper functions

    @staticmethod
    def full(shape:tuple[shape_int, ...], fill_value, **kwargs): return full(shape, fill_value, **kwargs)

    @staticmethod
    def zeros(*shape, **kwargs): return zeros(*shape, **kwargs)

    @staticmethod
    def ones(*shape, **kwargs): return ones(*shape, **kwargs)

    @staticmethod
    def arange(start, stop=None, step=1, **kwargs):
        return arange(start, stop, step, **kwargs)

    @staticmethod
    def eye(dim:int, **kwargs): return eye(dim, **kwargs)

    def full_like(self, fill_value, **kwargs): return full_like(self, fill_value, **kwargs)
    def zeros_like(self, **kwargs): return zeros_like(self, **kwargs)
    def ones_like(self, **kwargs): return ones_like(self, **kwargs)

    # random number generation high level ops

    @staticmethod
    def randn(*shape, dtype:DType | None=None, **kwargs) -> Tensor: return randn(*shape, dtype=dtype, **kwargs)
    @staticmethod
    def randint(*shape, low=0, high=10, **kwargs) -> Tensor: return randint(*shape, low=low, high=high, **kwargs)
    @staticmethod
    def normal(*shape, mean=0.0, std=1.0, **kwargs) -> Tensor:  return normal(*shape, mean=mean, std=std, **kwargs)
    @staticmethod
    def uniform(*shape, low=0.0, high=1.0, **kwargs) -> Tensor:
        return uniform(*shape, low=low, high=high, **kwargs)
    @staticmethod
    def scaled_uniform(*shape, **kwargs) -> Tensor: return scaled_uniform(*shape, **kwargs)

    def multinomial(self: Tensor, num_samples: int = 1, replacement: bool = False) -> Tensor:
        """Draws samples from the multinomial distribution based on the probability values in the tensor."""
        # Validate input dimensions and sample count
        assert 1 <= self.ndim <= 2 and num_samples > 0, f"{self.ndim=} must be 1 or 2 dim, {num_samples=} must be positive"
        assert replacement or num_samples == 1, "no replacement only supports num_samples = 1"

        # If tensor is 1D, add a new dimension at the beginning
        weight = self.unsqueeze(0) if self.ndim == 1 else self

        # Compute the cumulative distribution function (CDF) for the weights
        cdf = (cw := weight.cumsum(1)) / cw[:, -1].unsqueeze(1)

        # Generate uniform random samples
        unif_samples = Tensor.rand(num_samples, cdf.shape[0], 1)

        # Determine indices based on CDF
        indices = (unif_samples.expand((-1, -1, cdf.shape[1])) >= cdf).sum(2).permute((1, 0))

        # If the original tensor was 1D, squeeze the resulting indices tensor
        return (indices.squeeze(0) if self.ndim == 1 else indices).cast(dtypes.int32)

    # ------------------------------------------------------------------------------------------------------------------
    # tensor_autograd.py
    # toposort and backward pass

    def collect_backward_graph(self): return collect_backward_graph(self)
    def backward(self): return backward(self)

    # ------------------------------------------------------------------------------------------------------------------
    # tensor_reshape.py
    # movement mlops

    def reshape(self, shape, *args) -> Tensor: return reshape(self, shape, *args)
    def expand(self, shape, *args) -> Tensor: return expand(self, shape, *args)
    def permute(self, order, *args) -> Tensor: return permute(self, order, *args)
    def flip(self, axis, *args) -> Tensor: return flip(self, axis, *args)
    def pad(self, arg:tuple[tuple[int, int] | None, ...], value:float=0.0) -> Tensor: return pad(self, arg, value)
    # (padding_left, padding_right, padding_top, padding_bottom)
    def pad2d(self, padding:list[int] | tuple[int, ...], value:float=0) -> Tensor: return pad2d(self, padding, value)
    def shrink(self, arg:tuple[tuple[shape_int, shape_int] | None, ...]) -> Tensor: return shrink(self, arg)
    def squeeze(self, dim=None) -> Tensor: squeeze(self, dim)
    def unsqueeze(self, dim) -> Tensor: return unsqueeze(self, dim)

    @property
    def T(self) -> Tensor: return self.transpose()
    def transpose(self, ax1=1, ax2=0) -> Tensor: return transpose(self, ax1, ax2)
    def flatten(self, start_dim=0): return _flatten(self, start_dim)

    # ------------------------------------------------------------------------------------------------------------------
    # tensor_index_slice.py
    # movement high level ops

    def __getitem__(self, val) -> Tensor: # val: Union[int, slice, Tensor, None, Ellipsis, Tuple[Union[int, slice, Tensor, None, Ellipsis], ...]]
        return __getitem__(self, val)

    def __setitem__(self,s,v): return __setitem__(self,s,v)

    # NOTE: using slice is discouraged and things should migrate to pad and shrink
    def slice(self, arg:Sequence[tuple[int, shape_int] | None], value:float=0) -> Tensor:
        return tslice(self, arg, value)

    def gather(self: Tensor, idx: Tensor, dim: int) -> Tensor: return gather(self, idx, dim)

    # ------------------------------------------------------------------------------------------------------------------
    # tensor_combine_segment.py

    def cat(self, *args, dim=0) -> Tensor: return cat(self, *args, dim=dim)
    @staticmethod
    def stack(tensors, dim=0) -> Tensor: stack(tensors, dim)
    def repeat(self, repeats) -> Tensor: repeat(self, repeats)
    def chunk(self, num:int, dim:int=0) -> list[Tensor]: chunk(self, num, dim)

    # reduce ops

    def _reduce(self, fxn:type[Function], axis:int | tuple[int, ...] | None=None, keepdim=False) -> Tensor:
        return _reduce(self, fxn, axis, keepdim)

    def sum(self, axis=None, keepdim=False): return tsum(self, axis, keepdim)
    def max(self, axis=None, keepdim=False): return tmax(self, axis, keepdim)
    def min(self, axis=None, keepdim=False): return tmin(self, axis, keepdim)

    def mean(self, axis=None, keepdim=False): return mean(self, axis, keepdim)
    def std(self, axis=None, keepdim=False, correction=1): return std(self, axis, keepdim, correction)

    def _softmax(self, axis): return _softmax(self, axis)
    def softmax(self, axis=-1): return softmax(self, axis)
    def log_softmax(self, axis=-1): return log_softmax(self, axis)

    def argmax(self, axis=None, keepdim=False): return argmax(self, axis, keepdim)
    def argmin(self, axis=None, keepdim=False): return argmin(self, axis, keepdim)

    # ------------------------------------------------------------------------------------------------------------------
    # tensor_nn.py
    # processing ops

    def _pool(self, k_:tuple[shape_int, ...], stride:tuple[int, ...] | int=1, dilation:tuple[int, ...] | int=1) -> Tensor:
      return _pool(self, k_, stride, dilation)

    # NOTE: these work for more than 2D
    def avg_pool2d(self, kernel_size=(2,2), stride=None, dilation=1): return avg_pool2d(self, kernel_size, stride, dilation)
    def max_pool2d(self, kernel_size=(2,2), stride=None, dilation=1): return max_pool2d(self, kernel_size, stride, dilation)

    wino = int(getenv("WINO", "0")) # no winograd convolution
    def conv2d(self, weight:Tensor, bias:Tensor | None=None, groups=1, stride=1, dilation=1, padding=0) -> Tensor:
        return conv2d(self, weight, bias, groups, stride, dilation, padding)

    # ------------------------------------------------------------------------------------------------------------------

    def dot(self, w:Tensor) -> Tensor:
        n1, n2 = len(self.shape), len(w.shape)
        assert n1 != 0 and n2 != 0, f"both arguments to matmul need to be at least 1D, but they are {n1}D and {n2}D"
        assert self.shape[-1] == w.shape[-min(n2, 2)], f"Input Tensor shapes {self.shape} and {w.shape} cannot be multiplied ({self.shape[-1]} != {w.shape[-min(n2, 2)]})"
        x = self.reshape(*self.shape[0:-1], *[1]*min(n1-1, n2-1, 1), self.shape[-1])
        w = w.reshape(*w.shape[0:-2], *[1]*min(n1-1, n2-1, 1), *w.shape[-min(n2, 2):]).transpose(-1, -min(n2, 2))
        return (x*w).sum(-1)

    def _cumsum(self, axis:int=0, _first_zero=False) -> Tensor: return self.transpose(axis,-1).pad2d((self.shape[axis]-int(not _first_zero),0))._pool((self.shape[axis],)).sum(-1).transpose(axis,-1)
    def cumsum(self, axis:int=0) -> Tensor:
        # TODO: someday the optimizer will find this on it's own
        # for now this is a two stage cumsum
        SPLIT = 256
        if self.shape[axis] <= SPLIT*2:
            return self._cumsum(axis)
        ret = self.transpose(axis,-1).pad2d((round_up(self.shape[axis], SPLIT)-self.shape[axis], 0))
        ret = ret.reshape(*ret.shape[0:-1], ret.shape[-1]//SPLIT, SPLIT)._cumsum(-1)
        base_add = ret[..., -1]._cumsum(-1, _first_zero=True)[..., :-1]
        base_add = base_add.unsqueeze(-1).expand(*base_add.shape, ret.shape[-1])
        def fix(x:Tensor): return x.reshape(*ret.shape[0:-2], ret.shape[-2] * ret.shape[-1])[..., -self.shape[axis]:].transpose(axis,-1)
        return fix(ret) + fix(base_add)

    # mlops (unary)

    def neg(self): return function.Neg.apply(self)
    def log(self): return function.Log.apply(self)
    def exp(self): return function.Exp.apply(self)
    def relu(self): return function.Relu.apply(self)
    def sigmoid(self): return function.Sigmoid.apply(self)
    def sqrt(self): return function.Sqrt.apply(self)
    def sin(self): return function.Sin.apply(self)
    def cos(self): return ((math.pi/2)-self).sin()

    # math functions (unary) skipped

    # activation functions (unary) skipped
    def elu(self, alpha=1.0): return self.relu() - alpha*(1-self.exp()).relu()
    # ------------------------------------------------------------------------------------------------------------------
    # tensor_bradcasted_binary_mlops.py

    # broadcasted binary mlops

    def _broadcasted(self, y:Tensor | float, reverse:bool=False) -> tuple[Tensor, Tensor]:
        return _broadcasted(self, y, reverse)

    def _to_float(self, x:Tensor | float): return _to_float(self, x)

    def add(self, x:Tensor | float, reverse=False) -> Tensor: return add(self, x, reverse)
    def sub(self, x:Tensor | float, reverse=False) -> Tensor: return sub(self, x, reverse)
    def mul(self, x:Tensor | float, reverse=False) -> Tensor: return mul(self, x, reverse)
    def pow(self, x:Tensor | float, reverse=False) -> Tensor: return pow(self, x, reverse)
    def div(self, x:Tensor | float, reverse=False) -> Tensor: return div(self, x, reverse)
    def matmul(self, x:Tensor, reverse=False) -> Tensor: return matmul(self, x, reverse)

    def maximum(self, x:Tensor | float) -> Tensor: return maximum(self, x)
    def minimum(self, x:Tensor | float) -> Tensor: return minimum(self, x)
    def where(self:Tensor, input_:Tensor | float, other:Tensor | float): return where(self, input_, other)

    # op wrappers (wasted lines to make the typechecker happy)

    def __neg__(self) -> Tensor: return self.neg()

    def __add__(self, x) -> Tensor: return self.add(x)
    def __sub__(self, x) -> Tensor: return self.sub(x)
    def __mul__(self, x) -> Tensor: return self.mul(x)
    def __pow__(self, x) -> Tensor: return self.pow(x)
    def __truediv__(self, x) -> Tensor: return self.div(x)
    def __matmul__(self, x) -> Tensor: return self.matmul(x)

    def __radd__(self, x) -> Tensor: return self.add(x, True)
    def __rsub__(self, x) -> Tensor: return self.sub(x, True)
    def __rmul__(self, x) -> Tensor: return self.mul(x, True)
    def __rpow__(self, x) -> Tensor: return self.pow(x, True)
    def __rtruediv__(self, x) -> Tensor: return self.div(x, True)
    def __rmatmul__(self, x) -> Tensor: return self.matmul(x, True)

    def __iadd__(self, x) -> Tensor: return self.assign(self.add(x))
    def __isub__(self, x) -> Tensor: return self.assign(self.sub(x))
    def __imul__(self, x) -> Tensor: return self.assign(self.mul(x))
    def __ipow__(self, x) -> Tensor: return self.assign(self.pow(x))
    def __itruediv__(self, x) -> Tensor: return self.assign(self.div(x))
    def __imatmul__(self, x) -> Tensor: return self.assign(self.matmul(x))

    def __lt__(self, x) -> Tensor: return function.Less.apply(*self._broadcasted(x, False))
    def __gt__(self, x) -> Tensor: return function.Less.apply(*self._broadcasted(x, True))
    def __ge__(self, x) -> Tensor: return 1.0-(self<x)
    def __le__(self, x) -> Tensor: return 1.0-(self>x)
    def __ne__(self, x) -> Tensor: return (self<x) + (self>x)     # type: ignore
    def __eq__(self, x) -> Tensor: return 1.0-(self != x)             # type: ignore

    # functional nn ops

    def linear(self, weight:Tensor, bias:Tensor | None=None): return linear(self, weight, bias)

    def binary_crossentropy(self, y:Tensor) -> Tensor: return binary_crossentropy(self, y)

    def binary_crossentropy_logits(self, y:Tensor) -> Tensor: return binary_crossentropy_logits(self, y)

    def sparse_categorical_crossentropy(self, Y, ignore_index=-1) -> Tensor:
        return sparse_categorical_crossentropy(self, Y, ignore_index)

    # cast ops

    def cast(self, dtype:DType) -> Tensor: return function.Cast.apply(self, dtype=dtype) if self.dtype != dtype else self
    def bitcast(self, dtype:DType) -> Tensor:
        assert self.dtype.itemsize == dtype.itemsize, "can't bitcast mismatched dtype itemsizes"
        return function.Cast.apply(self, dtype=dtype, bitcast=True) if self.dtype != dtype else self
    def float(self) -> Tensor: return self.cast(dtypes.float32)
    def half(self) -> Tensor: return self.cast(dtypes.float16)

    # convenience stuff

    @property
    def ndim(self) -> int: return len(self.shape)
    def numel(self) -> shape_int: return prod(self.shape)
    def element_size(self) -> int: return self.dtype.itemsize
    def nbytes(self) -> int: return self.numel() * self.element_size()
    def is_floating_point(self) -> bool: return dtypes.is_float(self.dtype)
