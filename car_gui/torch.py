#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""torch.py —— 极简 numpy 替身

官方 FastSpeech2 样例只用到了 torch 的几个基础张量操作，而本板 3.5G 内存 +
3 核 1GHz 装不动真的 PyTorch（aarch64 wheel 也大）。这里用 numpy 实现等价语义，
**让官方 synthesize.py / utils.py 一行不改即可运行**。

⚠️ 只实现了这份样例实际用到的 API；不是通用替代品。
"""
import numpy as np


class _Tensor(np.ndarray):
    """numpy 数组，带几个 torch 风格的便捷方法。

    ⚠️ **必须是 ndarray 的子类视图**，不能直接 `_Tensor(arr)` 构造。
    `np.ndarray` 的 `__new__` 会把"传进来的 ndarray"当成 **shape 参数**解读：
        `_Tensor(np.arange(0, 64))`
        → `ValueError: maximum supported dimension for an ndarray is 32, found 64`
    （把 64 个元素当成了 64 个维度）。这是 ndarray 子类最经典的坑。
    正确做法是 `np.asarray(arr).view(_Tensor)` —— 走 view 得到子类实例。
    """

    def __new__(cls, arr):
        return np.asarray(arr).view(cls)

    def unsqueeze(self, dim):
        return np.expand_dims(self, dim).view(_Tensor)

    def expand(self, *sizes):
        # torch 的 expand 既可传 tuple 也可传散参：
        #   `t.expand(bs, -1)` 或 `t.expand((bs, -1))`
        # 且 **-1 表示"保持该维原大小"** —— 而 np.broadcast_to 不接受负数
        # （会报 "all elements of broadcast shape must be non-negative"）。
        # 所以要把 -1 替换成对应维度的实际长度。
        if len(sizes) == 1 and isinstance(sizes[0], (tuple, list)):
            sizes = tuple(sizes[0])
        shp = self.shape
        # 从右往左对齐（torch 语义），-1 取对应的原维
        n = len(sizes)
        m = len(shp)
        out = []
        for i, s in enumerate(sizes):
            if s == -1:
                j = i - (n - m)          # 对齐到原张量的维
                out.append(shp[j] if 0 <= j < m else 1)
            else:
                out.append(int(s))
        return np.broadcast_to(self, tuple(out)).view(_Tensor)

    def item(self):
        return self.reshape(-1)[0].item() if self.size else 0

    def squeeze(self, dim=None):
        return np.squeeze(self, axis=dim).view(_Tensor)

    def astype(self, *a, **kw):
        return np.ndarray.astype(self, *a, **kw).view(_Tensor)

    @property
    def shape(self):
        return np.ndarray.shape.__get__(self)


def _wrap(a):
    return _Tensor(np.asarray(a))


class _DType(object):
    pass


#: LongTensor 是 torch 的构造函数，这里返回 numpy 数组
def LongTensor(x, **kw):
    return _wrap(np.asarray(x, dtype=np.int64))


def tensor(x, **kw):
    return _wrap(np.asarray(x))


def from_numpy(x):
    return _wrap(np.asarray(x))


def arange(*args, **kw):
    # ⚠️ 这里原来写成了
    #     `_wrap(np.arange(*a).astype(d) if d is not None else np.arange(*a))`
    #   三元表达式的优先级导致 `if/else` 被绑到 np.arange 的结果上，
    #   于是 arange(0, 64) 被解释成"要构造 64 维数组" →
    #   `ValueError: maximum supported dimension for an ndarray is 32, found 64`。
    #   **必须显式加括号**把三元表达式整个包起来。
    dtype = kw.pop("dtype", None)
    arr = np.arange(*args, **kw)
    if dtype is not None:
        arr = arr.astype(dtype)
    return _wrap(arr)


def max(x, *a, **kw):
    t = np.asarray(x)
    r = _wrap(np.max(t))
    return r


def min(x, *a, **kw):
    return _wrap(np.min(np.asarray(x)))


def cat(xs, dim=0):
    return _wrap(np.concatenate([np.asarray(x) for x in xs], axis=dim))


def stack(xs, dim=0):
    return _wrap(np.stack([np.asarray(x) for x in xs], axis=dim))


def zeros(*shape, **kw):
    dtype = kw.pop("dtype", np.float32)
    return _wrap(np.zeros(shape, dtype=dtype))


def ones(*shape, **kw):
    dtype = kw.pop("dtype", np.float32)
    return _wrap(np.ones(shape, dtype=dtype))


#: torch.Tensor 用于 isinstance 判断
Tensor = _Tensor

__version__ = "numpy-shim-1.0"
