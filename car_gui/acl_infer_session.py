#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""acl_infer_session.py —— 用 pyACL 实现官方 `ais_bench.infer.interface.InferSession`

## 为什么需要它
官方 FastSpeech2 样例（Gitee `ascend/ascend_community_projects` 310B 分支）依赖
`ais_bench`（昇腾推理工具链）做 .om 推理，但**本板 CANN 7.0.RC1 没装 ais_bench**。
而 pyACL（`acl.so`）是 toolkit 自带的、**实测可用**。

本模块用 pyACL 复刻 `InferSession` 的接口，于是**官方 synthesize.py 一行不用改**。

## 复刻的接口（只实现官方用到的部分）
    s = InferSession(device_id, om_path)
    s.get_inputs()   -> [TensorDesc(shape=...), ...]   # 官方读 [0].shape[1] 取 max_len
    s.infer([a, b, ...]) -> [np.ndarray, ...]          # 按模型输入顺序喂

## 用法
    from acl_infer_session import InferSession
"""
import os
import sys
import threading

import numpy as np

import acl

# acl 常量（不同版本名字略有差异，做个兜底）
ACL_MEM_MALLOC_HUGE_FIRST = getattr(acl, "ACL_MEM_MALLOC_HUGE_FIRST", 0)
ACL_MEMCPY_HOST_TO_DEVICE = getattr(acl, "ACL_MEMCPY_HOST_TO_DEVICE", 1)
ACL_MEMCPY_DEVICE_TO_HOST = getattr(acl, "ACL_MEMCPY_DEVICE_TO_HOST", 2)
ACL_SUCCESS = 0


def _check(ret, what):
    # ⚠️ 注意：pyACL 有些 API 返回 int 0，有些返回 **bool True**
    #    （实测 acl.init() 返回 True）→ 必须两种都当成功，否则会误报失败。
    if ret is True or ret == ACL_SUCCESS:
        return
    raise RuntimeError("%s 失败，错误码 %s" % (what, ret))


class _TensorDesc(object):
    """模拟 ais_bench 的 Tensor 描述对象（官方只用到 .shape / .name / .datatype）。"""

    __slots__ = ("name", "shape", "datatype", "size")

    def __init__(self, name, shape, datatype, size):
        self.name = name
        self.shape = tuple(shape)
        self.datatype = datatype
        self.size = size

    def __repr__(self):
        return "TensorDesc(name=%r, shape=%r, datatype=%r)" % (
            self.name, self.shape, self.datatype)


#: aclDataType → numpy dtype
#: ⚠️ **血泪教训**：不要靠"猜"或自己编数字表。
#: 早期版本手写了一张表，把 `ACL_DTYPE_INT16` 和 `ACL_DTYPE_INT64` 的位置搞错了，
#: 结果 encoder 的 int64 token 被强制转成 int16 →
#: **buffer 字节数和实际写入数据对不上 → `mdl.execute 失败，错误码 107004`**，
#: 而报错信息完全看不出是 dtype 问题，极难定位。
#: 现在改为**从 `acl` 模块动态读取常量**，缺哪个就跳过哪个。
def _build_acl_dtype_map():
    m = {}
    pairs = [
        ("ACL_FLOAT", np.float32), ("ACL_FLOAT16", np.float16),
        ("ACL_INT8", np.int8), ("ACL_INT16", np.int16),
        ("ACL_INT32", np.int32), ("ACL_INT64", np.int64),
        ("ACL_UINT8", np.uint8), ("ACL_UINT16", np.uint16),
        ("ACL_UINT32", np.uint32), ("ACL_UINT64", np.uint64),
        ("ACL_DOUBLE", np.float64), ("ACL_BOOL", np.bool_),
    ]
    for name, dt in pairs:
        v = getattr(acl, name, None)
        if v is None:
            # acl.rt 下也可能有
            v = getattr(getattr(acl, "rt", None), name, None)
        if v is not None:
            m[v] = dt
    return m


_ACL_DTYPE = _build_acl_dtype_map()


def _dtype_of(kind, desc, index, size=0, shape=(), hint=None):
    """取某个输入/输出的 numpy dtype。

    ★ **关键设计：用 buffer 大小反推，而不是只信 dtype 码。**
    实测（CANN 7.0.RC1, encoder_bs1.om）：
        texts     dtype 码 9   size 512  shape(1,64) → 8 字节/元素 → **INT64**
        src_masks dtype 码 12  size  64  shape(1,64) → 1 字节/元素 → **BOOL**
        输出      dtype 码 0   size 65536            → FLOAT32
    早期手写映射表把码 9 当成 INT16（真值是 INT64），于是 int64 token 被截成 int16 →
    buffer 内容与声明大小不符 → `mdl.execute 失败，错误码 107004`
    （**报错完全看不出是 dtype 问题，极难定位**）。
    → 现在以 size/shape 反推的**字节数为准**，dtype 码只用于在同类字节数里消歧义。
    """
    getter = (acl.mdl.get_input_data_type if kind == "in"
              else acl.mdl.get_output_data_type)
    cand = np.float32
    try:
        v = getter(desc, index)
        if isinstance(v, tuple):
            v = v[0]
        cand = _ACL_DTYPE.get(v, np.float32)
    except Exception:
        pass

    try:
        n = int(np.prod(list(shape))) if len(shape) else 1
        if n > 0 and size > 0 and size % n == 0:
            nbytes = size // n
            if cand.itemsize != nbytes:
                if kind == "out":
                    cand = {1: np.int8, 2: np.int16, 4: np.float32,
                            8: np.int64}.get(nbytes, np.float32)
                elif hint is not None and np.issubdtype(np.dtype(hint), np.floating):
                    # ★★ 用户传进来的是**浮点**值，就绝不该判成整型。
                    #    实测坑：variance_adaptor 的 p/e/d_control 是标量 [1]，
                    #    desc.size=8 → 被推断成 int64 → 1.0 被转成整数 1，
                    #    8 字节里放的是整数 1 的字节，而模型要的是浮点 1.0
                    #    → 时长预测输出全 0（`Current mel length: [0]`，音频为空）。
                    cand = {8: np.float64, 4: np.float32,
                            2: np.float16}.get(nbytes, np.float32)
                else:
                    cand = {1: np.bool_, 2: np.int16, 4: np.int32,
                            8: np.int64}.get(nbytes, cand)
    except Exception:
        pass
    return cand


class _AclRuntime(object):
    """进程级 ACL 运行时（引用计数）。

    ⚠️ **关键教训**：`acl.init()` / `acl.finalize()` / `set_device()` 是
    **进程级全局**资源，不是每个模型一份。早期版本在 `free_resource()` 里直接
    `acl.finalize()`，结果**第一个模型释放后整个运行时被关掉**，
    后续模型全部报 `acl.init 失败，错误码 100002`
    （官方 FastSpeech2 要连开 5 个 om，必然踩到）。
    → 用引用计数：最后一个 session 释放时才真正 finalize。
    """

    _refs = 0
    _inited = False
    _device = None
    _ctx = None          # ★ 全局唯一的 device context
    _tls = threading.local()   # 记录"本线程是否已绑定 context"

    @classmethod
    def acquire(cls, device_id):
        if not cls._inited:
            _check(acl.init(), "acl.init")
            cls._inited = True
        if cls._device != device_id:
            _check(acl.rt.set_device(device_id), "set_device")
            cls._device = device_id
            if cls._ctx is not None:
                try:
                    acl.rt.destroy_context(cls._ctx)
                except Exception:
                    pass
                cls._ctx = None
        if cls._ctx is None:
            cls._ctx, ret = acl.rt.create_context(device_id)
            _check(ret, "create_context")
        cls._refs += 1
        return cls._ctx

    @classmethod
    def bind_thread(cls):
        """把全局 context **绑定到当前线程**。

        ⚠️ **关键教训**：ACL 的 device context 是**线程局部**的 ——
        `create_context` 只对调用它的那个线程生效。
        实测：主线程里连续 `infer` 完全正常，可一旦放进
        `SpeakQueue` 的**后台线程**，立刻报
            `malloc(512) 失败，错误码 107002`
        （连内存都申请不了）。单线程测试**永远发现不了**这个问题。

        → 每次 `infer` 前，若本线程还没绑定过，就 `acl.rt.set_context(ctx)`。
        """
        if getattr(cls._tls, "bound", False):
            return
        if cls._ctx is not None:
            try:
                acl.rt.set_context(cls._ctx)
            except Exception:
                pass
        cls._tls.bound = True

    @classmethod
    def release(cls):
        cls._refs -= 1
        if cls._refs <= 0:
            cls._refs = 0
            # 这里**不**主动 finalize —— 进程退出时自然回收。
            # 反复 init/finalize 很容易把运行时搞坏，而收益几乎为零。


class _Dataset(object):
    """pyACL dataset 封装。

    ⚠️ `acl.mdl.execute` 要求传 **dataset 对象**，不能传 list 或裸指针
    （传 list 会报 `argument 2 must be int, not list`）。
    正确做法是 `create_dataset` → 逐个 `add_dataset_buffer`，用完 `destroy_dataset`。
    """

    def __init__(self):
        self._ds = acl.mdl.create_dataset()
        self._bufs = []

    def add(self, dev_ptr, size):
        buf = acl.create_data_buffer(dev_ptr, size)
        acl.mdl.add_dataset_buffer(self._ds, buf)
        self._bufs.append(buf)

    @property
    def handle(self):
        return self._ds

    def destroy(self):
        for buf in self._bufs:
            try:
                acl.destroy_data_buffer(buf)
            except Exception:
                pass
        self._bufs = []
        if self._ds is not None:
            try:
                acl.mdl.destroy_dataset(self._ds)
            except Exception:
                pass
            self._ds = None


class InferSession(object):
    """pyACL 版推理会话（接口对齐 ais_bench）。"""

    def __init__(self, device_id, model_path):
        if not os.path.isfile(model_path):
            raise RuntimeError("模型文件不存在：%s" % model_path)
        self.device_id = int(device_id)
        self.model_path = model_path
        self._bufs = []          # [(dev_ptr, size), ...]
        self._inputs = []
        self._outputs = []
        self._mdl = None
        self._desc = None
        self._last_shapes = []      # 调试用：记录上一次 infer 的输入形状
        # ★ 全局共享 context（勿在本类里 create/destroy context，见 _AclRuntime 注释）
        self._ctx = _AclRuntime.acquire(self.device_id)
        try:
            self._load()
        except Exception:
            _AclRuntime.release()
            raise

    # ── 生命周期 ────────────────────────────────────────────────
    def _load(self):
        self._mdl, ret = acl.mdl.load_from_file(self.model_path)
        _check(ret, "load_from_file(%s)" % os.path.basename(self.model_path))

        self._desc = acl.mdl.create_desc()
        _check(acl.mdl.get_desc(self._desc, self._mdl), "get_desc")

        # 输入
        n_in = acl.mdl.get_num_inputs(self._desc)
        for i in range(n_in):
            size = acl.mdl.get_input_size_by_index(self._desc, i)
            dims, ret = acl.mdl.get_input_dims(self._desc, i)
            _check(ret, "get_input_dims")
            shape = list(dims.get("dims") or [])
            dtype = _dtype_of("in", self._desc, i, size, shape)
            while len(shape) > 1 and shape[-1] == 1 and np.prod(shape) != size // 4:
                shape.pop()
            name = acl.mdl.get_input_name_by_index(self._desc, i)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            self._inputs.append(_TensorDesc(name, shape, dtype, size))

        # 输出
        n_out = acl.mdl.get_num_outputs(self._desc)
        for i in range(n_out):
            size = acl.mdl.get_output_size_by_index(self._desc, i)
            dims, ret = acl.mdl.get_output_dims(self._desc, i)
            _check(ret, "get_output_dims")
            shape = list(dims.get("dims") or [])
            dtype = _dtype_of("out", self._desc, i, size, shape)
            name = acl.mdl.get_output_name_by_index(self._desc, i)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            self._outputs.append(_TensorDesc(name, shape, dtype, size))

    def free_resource(self):
        try:
            for ptr, _sz in self._bufs:
                try:
                    acl.rt.free(ptr)
                except Exception:
                    pass
            self._bufs = []
            if self._desc is not None:
                acl.mdl.destroy_desc(self._desc)
                self._desc = None
            if self._mdl is not None:
                acl.mdl.unload(self._mdl)
                self._mdl = None
            # ★ 不 destroy 共享 context（见 _AclRuntime 注释）
        except Exception as e:
            print("[acl_infer] free_resource 警告: %r" % (e,), file=sys.stderr)
        finally:
            # 引用计数释放（**不**在这里 finalize 全局运行时）
            _AclRuntime.release()

    # ── 描述查询（对齐 ais_bench）────────────────────────────────
    def get_inputs(self):
        return list(self._inputs)

    def get_outputs(self):
        return list(self._outputs)

    # ── 推理 ────────────────────────────────────────────────────
    def _alloc(self, size, into=None):
        """申请设备内存。

        `into` 给定时登记到该列表（**局部于一次推理，用完即释放**）；
        否则登记到 `self._bufs`（长生命周期，由 `free_resource` 统一释放）。

        ⚠️ **必须区分这两者**：早期版本把**每次推理**的 buffer 都塞进
        `self._bufs` 且从不释放 → 设备内存持续泄漏，
        连跑几次后报 `malloc(512) 失败，错误码 107002`
        （单次测试完全看不出来，一进 SpeakQueue 连续播报就炸）。
        """
        ptr, ret = acl.rt.malloc(size, ACL_MEM_MALLOC_HUGE_FIRST)
        _check(ret, "malloc(%d)" % size)
        (self._bufs if into is None else into).append((ptr, size))
        return ptr

    def infer(self, feeds, mode="static"):
        """喂一组 numpy 输入，返回一组 numpy 输出（顺序同模型输出）。"""
        # ★ 必须在每次推理前确保 context 已绑定到**当前线程**
        #   （SpeakQueue 在后台线程里调用，见 _AclRuntime.bind_thread 的说明）
        _AclRuntime.bind_thread()
        if len(feeds) != len(self._inputs):
            raise ValueError("输入个数不符：给了 %d，模型要 %d"
                             % (len(feeds), len(self._inputs)))

        # ★ 本次推理的**局部** buffer 列表：用完在 finally 里立刻释放（防设备内存泄漏）
        bufs = []
        in_bufs = []
        self._last_shapes = []
        for feed, desc in zip(feeds, self._inputs):
            # ⚠️ 关键：**每个输入 buffer 必须正好等于模型声明的大小**
            #    （不足或超出会报 `mdl.execute 失败，错误码 107004`）。
            #    这里统一按 desc.size 个字节准备内存，再把数据填进去。
            nbytes = desc.size
            raw = np.zeros(nbytes, dtype=np.uint8)      # 保证大小精确
            arr = np.asarray(feed)
            # ★★ 关键：**dtype 以"用户实际传进来的数组"为准，而不是 _load 时的推测**。
            #    实测坑：variance_adaptor 的 p/e/d_control 是标量（shape=[]，size=8），
            #    _load 时无法判断是 int64 还是 float64，误判成 int64 →
            #    1.0 被转成整数 1，8 字节里放整数 1 的字节，而模型要浮点 1.0
            #    → 时长预测输出全 0（`Current mel length: [0]`，音频为空、RTF=inf）。
            #    判据：**用户传浮点，就该按浮点喂**；而且 nbytes 能进一步消歧义。
            use_dt = desc.datatype
            if np.issubdtype(arr.dtype, np.floating):
                use_dt = {8: np.float64, 4: np.float32,
                          2: np.float16}.get(nbytes, np.float32)
            if arr.dtype != use_dt:
                arr = arr.astype(use_dt)
            arr = np.ascontiguousarray(arr)
            n = min(arr.nbytes, nbytes)
            if n:
                raw[:n] = arr.reshape(-1).view(np.uint8)[:n]
            dptr = self._alloc(nbytes, into=bufs)
            # ⚠️ acl.rt.memcpy 的 host 参数**必须是 int 地址**，不能传 bytes
            _check(acl.rt.memcpy(dptr, nbytes, int(raw.ctypes.data),
                                 nbytes, ACL_MEMCPY_HOST_TO_DEVICE),
                   "H2D memcpy[%s]" % desc.name)
            self._last_shapes.append(
                (desc.name, tuple(desc.shape), desc.datatype.__name__,
                 arr.shape, arr.dtype.name, nbytes))
            in_bufs.append((dptr, nbytes))

        out_bufs = []
        for desc in self._outputs:
            dptr = self._alloc(desc.size, into=bufs)
            out_bufs.append((dptr, desc.size))

        in_ds = _Dataset()
        out_ds = _Dataset()
        try:
            for ptr, size in in_bufs:
                in_ds.add(ptr, size)
            for ptr, size in out_bufs:
                out_ds.add(ptr, size)
            _check(acl.mdl.execute(self._mdl, in_ds.handle, out_ds.handle),
                   "mdl.execute")
        except Exception as e:
            raise RuntimeError("模型推理失败：%r" % (e,))
        finally:
            in_ds.destroy()
            out_ds.destroy()
            # ★ 立刻释放本次推理占用的设备内存（否则连跑几次就 malloc 失败 107002）
            for ptr, _sz in bufs:
                try:
                    acl.rt.free(ptr)
                except Exception:
                    pass
            bufs = []

        results = []
        for (dptr, size), desc in zip(out_bufs, self._outputs):
            buf = np.zeros(size, dtype=np.uint8)     # 需要可写、连续的内存
            _check(acl.rt.memcpy(int(buf.ctypes.data), size, dptr, size,
                                 ACL_MEMCPY_DEVICE_TO_HOST), "D2H memcpy")
            arr = buf.view(desc.datatype)
            if np.prod(desc.shape) == arr.size:
                arr = arr.reshape(desc.shape)
            results.append(np.array(arr))            # 拷贝一份再返回
        return results
