#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qxmic_locator.py —— HK-QXMIC-V3.0 声源定位麦克风串口驱动 + 自测 CLI

用途
----
读取「好凯盛 HK-QXMIC-V3.0 全向声源定位阵列麦克风」通过 USB-TTL 串口吐出的
方位帧，换算成 0~360° 角度和 8 个方向（前/左前/左/左后/后/右后/右/右前），
供「声源方向控车」「转头朝向说话人」等功能使用。

串口协议（厂商《串口读取坐标操作说明》+ 实测截图核实）
------------------------------------------------------
- 串口：1,000,000 baud，8N1，无流控；设备每 200~300ms 吐一帧。
- 帧：0x55 0xAA <coord> <checksum>，coord = 0~11，checksum = 0xFF - coord。
  例：55 AA 05 FA → coord 5。
- 串口流里混有其它字节 / ASCII 调试串（实测可见 "iGo-CMD timeout"），
  所以解析必须是「扫描式」：只在字节流里找 55 AA，取第 3 字节，校验第 4
  字节，不匹配就丢 1 字节继续找。本模块的 FrameParser 能处理分片到达、
  粘包、噪声、错校验、非法 coord（>11）。

12 方位（角度约定，重要）
------------------------
- sector k 覆盖 [180 + 30k, 210 + 30k)°，中心角 = (195 + 30k) mod 360。
  k=0 中心 195°，k=5 中心 345°，k=8 中心 75°；厂商口径「方位 180° 对应 00」。
- 本模块输出角度：0° = 正前方，**逆时针（向左）为正**，范围 [0, 360)。
  这样 sector 5（345°）就是「前方偏右 15°」，与厂商/项目示例一致。
  ⚠️ 真实装到车上后，0° 朝向与左右可能需要标定（见文档「角度方向待真机标定」），
     麦克风装歪了用 --offset 做安装偏移补偿。

依赖
----
只依赖 pyserial，且**懒加载**：没装 pyserial 也能 import 本模块、跑 --simulate
和单元测试（test_qxmic_locator.py 不依赖任何硬件/第三方库）。

用法
----
    python3 qxmic_locator.py --list-ports              # 看有哪些串口
    python3 qxmic_locator.py --simulate --duration 3   # 无硬件自测（内置模拟帧）
    python3 qxmic_locator.py --port /dev/ttyMIC        # 真机（默认自动找）
    python3 qxmic_locator.py --json --duration 5       # 每行一个 JSON
    python3 qxmic_locator.py --offset 90               # 安装偏移（度）

对应文档：《../6.说明文档/4.语音模块/声源定位麦克风.md》
"""

import argparse
import json
import os
import sys
import threading
import time

__all__ = [
    "BAUD", "SECTOR_ANGLES", "SECTOR_COUNT", "DIRECTIONS", "FRAME_HEAD",
    "QxMicError", "FrameParser", "QxMicLocator",
    "sector_to_angle", "angle_to_direction", "describe_angle",
    "find_port", "list_serial_ports", "simulated_frame",
    "iter_simulated_frames", "main",
]

# ── 协议常量 ────────────────────────────────────────────────────────────
#: 厂商规定的串口波特率（1,000,000 8N1，无流控）。
BAUD = 1_000_000

#: 帧头两字节。
FRAME_HEAD = b"\x55\xaa"

#: 方位帧固定 4 字节：55 AA coord checksum。
FRAME_LEN = 4

#: 12 个方位（sector 0~11）。
SECTOR_COUNT = 12

#: 每个扇区的中心角（度）：(195 + 30k) mod 360。
SECTOR_ANGLES = tuple((195 + 30 * k) % 360 for k in range(SECTOR_COUNT))

#: 8 个方向名，按角度递增（逆时针）排列：0°/45°/…/315°。
DIRECTIONS = ("前", "左前", "左", "左后", "后", "右后", "右", "右前")

#: 方向名 → 中心角（度）。
_DIRECTION_CENTERS = tuple(idx * 45 for idx in range(8))

#: 每个方向的「偏左/偏右」用词：索引 = 方向序号，值为 (+角偏移, -角偏移)。
#: 例如「前」+偏移 = 偏左，「前」-偏移 = 偏右（因为本模块角度逆时针为正）。
_DIRECTION_OFFSET_WORDS = (
    ("左", "右"),    # 前
    ("左", "前"),    # 左前
    ("后", "前"),    # 左
    ("后", "左"),    # 左后
    ("右", "左"),    # 后
    ("右", "后"),    # 右后
    ("前", "后"),    # 右
    ("前", "右"),    # 右前
)

#: WCH（沁恒）USB 转串口芯片的厂商 ID。CH340/CH341/CH343/CH9102 都是 1a86。
_WCH_VID = 0x1A86

#: 小车下位机用的 CH9102 的 PID —— **必须排除**，否则会误抢小车的串口。
#: 工程里小车已由 setup_serial_udev.sh 固定成 /dev/ttyCAR。
_CAR_CH9102_PID = 0x55D4

#: 已知 CH34x 串口芯片 PID（用于给设备打分/提示；不代表只支持这些）。
_CH34X_PIDS = {
    0x7523: "CH340",
    0x7522: "CH340K",
    0x5523: "CH341",
    0x55D3: "CH343",
}

#: Linux 下 udev 固定的声源麦克风设备名（见 setup_qxmic_udev.sh）。
UDEV_LINK = "/dev/ttyMIC"

#: 读线程每次从串口读的阻塞超时（秒）。要小，close() 才能及时退出。
_READ_TIMEOUT = 0.05

#: FrameParser 缓冲区上限（字节）。异常数据流太脏时避免内存无限涨。
_MAX_BUFFER = 4096


class QxMicError(RuntimeError):
    """声源定位麦克风相关的可预期错误（会带中文提示，CLI 捕获后友好退出）。"""


# ── 角度换算 ────────────────────────────────────────────────────────────
def _normalize_angle(angle):
    """把任意角度规整到 [0, 360)。"""
    return float(angle) % 360.0


def sector_to_angle(sector):
    """
    扇区号 → 中心角（度）。

    sector k 覆盖 [180 + 30k, 210 + 30k)°，中心角 = (195 + 30k) mod 360。
    sector 0 → 195°，5 → 345°，8 → 75°。
    """
    if not isinstance(sector, int) or isinstance(sector, bool):
        raise TypeError("sector 必须是 int，收到 %r" % (sector,))
    if sector < 0 or sector >= SECTOR_COUNT:
        raise ValueError("sector 必须在 0~%d 之间，收到 %r"
                         % (SECTOR_COUNT - 1, sector))
    return SECTOR_ANGLES[sector]


def angle_to_direction(angle):
    """
    角度（度）→ 8 方向之一：前/左前/左/左后/后/右后/右/右前。

    每个方向覆盖 45°，以 0°/45°/…/315° 为中心，边界取 [center-22.5, center+22.5)。
    0° = 正前方，逆时针（向左）为正；任意角度都会先规整到 [0, 360)。
    """
    a = _normalize_angle(angle)
    idx = int(((a + 22.5) % 360.0) // 45.0) % 8
    return DIRECTIONS[idx]


def describe_angle(angle):
    """
    角度（度）→ 更适合终端显示的中文描述，如 345° → "前方偏右 15°"。

    显示 = 8 方向名 + 相对该方向中心的偏角（只保留整数度；正巧在中心时
    只输出方向，如 "前方"）。例如：
        345° → 前方偏右 15°      15° → 前方偏左 15°
        195° → 后方偏右 15°      45° → 左前方
    """
    a = _normalize_angle(angle)
    idx = int(((a + 22.5) % 360.0) // 45.0) % 8
    center = _DIRECTION_CENTERS[idx]
    diff = ((a - center + 180.0) % 360.0) - 180.0   # (-180, 180]
    d = int(round(abs(diff)))
    name = DIRECTIONS[idx] + "方"
    if d < 1:
        return name
    word = _DIRECTION_OFFSET_WORDS[idx][0 if diff > 0 else 1]
    return "%s偏%s %d°" % (name, word, d)


# ── pyserial 懒加载 ─────────────────────────────────────────────────────
_SERIAL_MOD = None      # 缓存已导入的 serial 模块
_SERIAL_ERROR = ""      # 导入失败原因（再次调用时直接抛出，不重复尝试）


def _import_serial():
    """
    懒加载 pyserial。

    没装时抛 QxMicError（中文提示），这样 `--simulate` / 单元测试 / import
    本模块都不需要 pyserial。
    """
    global _SERIAL_MOD, _SERIAL_ERROR
    if _SERIAL_MOD is not None:
        return _SERIAL_MOD
    if _SERIAL_ERROR:
        raise QxMicError(_SERIAL_ERROR)
    try:
        import serial  # noqa: F401  （延迟到真正用串口时才导入）
        from serial.tools import list_ports  # noqa: F401
    except Exception as exc:                          # pragma: no cover
        _SERIAL_ERROR = (
            "没有安装 pyserial，无法打开/枚举串口：%s\n"
            "  安装：python3 -m pip install pyserial（或 bash install_deps.sh）\n"
            "  没有硬件也可以先用 --simulate 自测。" % exc
        )
        raise QxMicError(_SERIAL_ERROR)
    _SERIAL_MOD = serial
    return serial


def list_serial_ports():
    """
    列出本机串口，返回 dict 列表（pyserial 没装时返回空列表，绝不抛异常）。

    每项字段：device / description / hwid / vid / pid / is_ch34x / is_car。
    """
    try:
        _import_serial()
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except Exception:
        return []

    out = []
    for p in ports:
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        out.append({
            "device": p.device,
            "description": (getattr(p, "description", "") or "").strip(),
            "hwid": (getattr(p, "hwid", "") or "").strip(),
            "vid": vid,
            "pid": pid,
            "is_ch34x": vid == _WCH_VID,
            "is_car": vid == _WCH_VID and pid == _CAR_CH9102_PID,
        })
    return out


def find_port():
    """
    自动找声源定位麦克风的串口，找不到返回 None（不会抛异常）。

    优先级：
      1. Linux 下 udev 固定的 /dev/ttyMIC（最稳，推荐先跑 setup_qxmic_udev.sh）；
      2. 任意 WCH CH34x（VID=1a86）串口，且**排除小车的 CH9102（PID=55d4）**；
         其中优先 CH340（7523），其次 CH341/CH343。
      3. 没装 pyserial 时，Linux 上若只有一个 /dev/ttyUSB* 就返回它。
    """
    if os.name != "nt" and os.path.exists(UDEV_LINK):
        return UDEV_LINK

    ports = list_serial_ports()
    cands = [p for p in ports if p["is_ch34x"] and not p["is_car"]]
    if not cands:
        # 没装 pyserial / 枚举不到时的兜底：Linux 只有一个 ttyUSB* 才敢用
        if os.name != "nt":
            try:
                import glob as _glob
                nodes = sorted(_glob.glob("/dev/ttyUSB*"))
            except Exception:
                nodes = []
            if len(nodes) == 1:
                return nodes[0]
        return None

    def _score(p):
        """给候选打分：CH340 最常见，其次 CH341/CH343，其余 CH34x 兜底。"""
        score = 0
        if p["pid"] == 0x7523:
            score += 4
        elif p["pid"] in (0x5523, 0x55D3):
            score += 3
        else:
            score += 1
        if "CH34" in (p["description"] or "").upper():
            score += 1
        return score

    cands.sort(key=lambda p: (-_score(p), p["device"]))
    return cands[0]["device"]


# ── 帧解析 ──────────────────────────────────────────────────────────────
class FrameParser:
    """
    扫描式帧解析器：往 `feed()` 里塞任意字节流，吐出解析好的方位帧。

    为什么是扫描式：串口流里混有 ASCII 调试信息（如 "iGo-CMD timeout"）和
    其它杂字节，不能假设「每次 read 正好是一帧」。做法：
      · 在缓冲区里找 55 AA；
      · 找到后取第 3 字节 coord、第 4 字节 checksum，校验 coord <= 11 且
        checksum == 0xFF - coord；
      · 校验失败只丢 1 个字节（丢掉那个 0x55）继续往后找，避免把后面
        真正的帧头一起吞掉。
    同时天然支持分片（半帧留在缓冲区等下次）和粘包（一次 feed 吐多帧）。

    输出 dict 字段：
        sector: int   扇区号 0~11（原始值，未加安装偏移）
        angle:  int   中心角 + 安装偏移，规整到 [0, 360)
        raw:    bytes 原始 4 字节帧（含帧头，便于排障/回放）
        ts:     float 解析时刻（time.time()）
    """

    def __init__(self, offset=0):
        #: 安装偏移（度），会加到每个 frame 的 angle 上。
        self.offset = int(offset) % 360
        self._buf = bytearray()

    # ── 只读属性 ──
    @property
    def buffered(self):
        """当前缓冲区里还没凑成帧的字节数。"""
        return len(self._buf)

    def reset(self):
        """清空缓冲区（重开串口时用，避免上一轮的残帧串进来）。"""
        self._buf.clear()

    def feed(self, data):
        """
        喂入一段字节流，返回本次凑出来的帧列表（可能 0 个、1 个或多个）。

        `data` 可以是 bytes / bytearray / memoryview；None 视为空。
        """
        if data:
            self._buf.extend(data)
        frames = []

        while True:
            pos = self._buf.find(FRAME_HEAD)
            if pos < 0:
                # 没找到帧头：只保留最后 1 字节（可能是下一帧的 0x55），其余丢弃
                if len(self._buf) > 1:
                    del self._buf[:-1]
                break
            if pos > 0:
                del self._buf[:pos]
            if len(self._buf) < FRAME_LEN:
                break                           # 半帧：等下一次 feed

            coord = self._buf[2]
            checksum = self._buf[3]
            if coord < SECTOR_COUNT and checksum == (0xFF - coord) & 0xFF:
                raw = bytes(self._buf[:FRAME_LEN])
                del self._buf[:FRAME_LEN]
                frames.append({
                    "sector": coord,
                    "angle": (SECTOR_ANGLES[coord] + self.offset) % 360,
                    "raw": raw,
                    "ts": time.time(),
                })
            else:
                # 非法 coord 或校验错：只丢 1 字节，然后重新扫描（可能后面还有帧）
                del self._buf[0]

        if len(self._buf) > _MAX_BUFFER:        # 兜底，防脏数据把内存吃满
            del self._buf[:-_MAX_BUFFER]
        return frames


# ── 驱动类 ──────────────────────────────────────────────────────────────
class QxMicLocator:
    """
    声源定位麦克风串口驱动：后台线程持续读串口 → 解析 → 缓存最新帧/回调。

    典型用法（上下文管理器会自动 open/close）：

        with QxMicLocator() as loc:                 # 自动找 /dev/ttyMIC 或 CH34x
            frame = loc.wait_next(timeout=2.0)      # 等下一帧
            if frame:
                print(frame["angle"], angle_to_direction(frame["angle"]))

    参数：
        port     串口名；None = 自动找（find_port()）
        baud     波特率，默认 BAUD（1,000,000）
        offset   安装偏移（度），加到角度上，默认 0
        timeout  wait_next() 的默认等待秒数，默认 0.5
    """

    def __init__(self, port=None, baud=BAUD, offset=0, timeout=0.5):
        self.port = port
        self.baud = int(baud)
        self.offset = int(offset) % 360
        self.timeout = float(timeout)

        self._ser = None
        self._parser = FrameParser(offset=self.offset)
        self._thread = None
        self._running = False
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

        self._latest = None       # 最近一帧
        self._seq = 0             # 已收到的帧序号（内部自增）
        self._consumed = 0        # wait_next 已经消费到的序号
        self._callbacks = []      # 事件回调
        self._error = ""          # 读线程里的错误信息
        self._last_cb_error = ""  # 回调里抛出的最近的异常（不影响读线程）

    # ── 只读属性 ──
    @property
    def error(self):
        """读线程/串口出错时的错误信息；正常时为空字符串。"""
        return self._error

    @property
    def parser(self):
        """内部 FrameParser（排障用，一般不需要直接碰）。"""
        return self._parser

    def __repr__(self):
        return "<QxMicLocator port=%r baud=%d offset=%d running=%s>" % (
            self.port, self.baud, self.offset, self._running)

    # ── 事件回调 ──
    def on_event(self, cb):
        """
        注册「每收到一帧就回调」的函数，返回 cb 本身（方便链式/装饰器）。

        ⚠️ 回调运行在后台读线程里，里面不要做耗时/阻塞操作。
        """
        if not callable(cb):
            raise TypeError("on_event 需要可调用对象，收到 %r" % (cb,))
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)
        return cb

    def off_event(self, cb):
        """注销回调；成功返回 True，本来就没注册返回 False。"""
        with self._lock:
            try:
                self._callbacks.remove(cb)
                return True
            except ValueError:
                return False

    # ── 打开 / 关闭 ──
    def open(self):
        """打开串口并启动后台读线程；成功返回 self。失败抛 QxMicError。"""
        if self._running:
            return self
        serial = _import_serial()

        if not self.port:
            self.port = find_port()
        if not self.port:
            raise QxMicError(
                "没有找到声源定位麦克风的串口。\n"
                "  1) 模组 USB 供电 + 接口3 的 TX→USB-TTL RX、GND→GND 是否接好"
                "（只接这两根，别把 5V 接到 TTL）\n"
                "  2) 运行 --list-ports 看有没有 CH340/CH343 设备\n"
                "  3) 板子上推荐先 bash setup_qxmic_udev.sh 固定成 /dev/ttyMIC\n"
                "  4) 也可以 --port 手动指定串口"
            )

        try:
            self._ser = serial.Serial(
                self.port,
                self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=_READ_TIMEOUT,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as exc:
            raise QxMicError(
                "打开串口 %s（%d baud）失败：%s\n"
                "  排查：设备名对不对（--list-ports）？是否被别的程序占用？"
                "部分 CH340 不支持 1M 波特率，可换 CH343。"
                % (self.port, self.baud, exc)
            )

        # 丢掉打开瞬间的残留数据，避免上一轮的半帧/脏数据混进来
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._parser.reset()

        self._error = ""
        self._running = True
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._reader_loop, name="qxmic-reader", daemon=True)
        self._thread.start()
        return self

    def close(self):
        """停止后台线程并关闭串口（可重复调用，不会抛异常）。"""
        self._running = False
        self._stop_evt.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=1.0)
        self._thread = None
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        with self._cond:
            self._cond.notify_all()

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ── 取帧 ──
    def get(self):
        """
        取最近一帧（非阻塞，可能重复拿到同一帧）；还没有任何帧时返回 None。
        需要「等下一帧」请用 wait_next()。
        """
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def wait_next(self, timeout=None):
        """
        等下一帧（阻塞），超时/串口已关闭返回 None。

        timeout=None 时用构造参数里的 timeout（默认 0.5s）。
        返回的 dict 与 FrameParser 输出一致（sector/angle/raw/ts）。
        """
        deadline = time.time() + (self.timeout if timeout is None else float(timeout))
        with self._cond:
            while self._seq <= self._consumed:
                remain = deadline - time.time()
                if remain <= 0:
                    return None
                if not self._running and self._seq <= self._consumed:
                    return None
                self._cond.wait(remain)
            self._consumed = self._seq
            return dict(self._latest) if self._latest is not None else None

    # ── 后台读线程 ──
    def _reader_loop(self):                             # pragma: no cover（要真串口）
        """持续读串口 → 喂给 FrameParser → 更新最新帧 + 触发回调。"""
        while not self._stop_evt.is_set() and self._running:
            try:
                waiting = getattr(self._ser, "in_waiting", 0)
                data = self._ser.read(waiting if waiting > 0 else 1)
            except Exception as exc:
                self._error = "读串口出错（设备被拔掉？）：%s" % exc
                with self._cond:
                    self._cond.notify_all()
                break
            if not data:
                continue

            for frame in self._parser.feed(data):
                with self._cond:
                    self._latest = frame
                    self._seq += 1
                    self._cond.notify_all()
                for cb in list(self._callbacks):
                    try:
                        cb(frame)
                    except Exception as exc:             # 回调异常不拖垮读线程
                        self._last_cb_error = repr(exc)


# ── 模拟数据源（无硬件自测用）──────────────────────────────────────────
#: --simulate / demo 默认跑的扇区顺序：从 5 开始（345°，正好是「前方偏右 15°」），
#: 再按 6→11→0→4 绕一圈，一次演示能覆盖 12 个扇区。
SIM_SECTORS = (5, 6, 7, 8, 9, 10, 11, 0, 1, 2, 3, 4)


def simulated_frame(sector):
    """按协议拼一帧原始字节：55 AA <sector> <0xFF - sector>。"""
    if not 0 <= int(sector) < SECTOR_COUNT:
        raise ValueError("sector 必须在 0~%d 之间，收到 %r"
                         % (SECTOR_COUNT - 1, sector))
    s = int(sector)
    return bytes((0x55, 0xAA, s, (0xFF - s) & 0xFF))


def iter_simulated_frames(period=0.25, sectors=None, noise=False, offset=0):
    """
    无限生成模拟帧（dict），按 period 秒的节奏，供 --simulate 和演示脚本共用。

    生成的字节会先喂给真正的 FrameParser，所以模拟模式验证的就是真实解析链路。
    noise=True 时每 4 帧前面插一段 "iGo-CMD timeout" ASCII 噪声，模拟真机混流。
    """
    seq = tuple(int(s) for s in sectors) if sectors else SIM_SECTORS
    parser = FrameParser(offset=offset)
    idx = 0
    while True:
        sector = seq[idx % len(seq)]
        idx += 1
        blob = b""
        if noise and idx % 4 == 0:
            blob += b"iGo-CMD timeout\r\n"
        blob += simulated_frame(sector)
        for frame in parser.feed(blob):
            yield frame
        time.sleep(max(0.0, float(period)))


# ── CLI ─────────────────────────────────────────────────────────────────
def format_line(frame, show_raw=False):
    """把一帧格式化成终端里的一行，如：
    sector=5  角度=345°  →  前方偏右 15°"""
    line = "sector=%d  角度=%d°  →  %s" % (
        frame["sector"], frame["angle"], describe_angle(frame["angle"]))
    if show_raw:
        line += "  raw=%s" % frame["raw"].hex(" ").upper()
    return line


def frame_as_json(frame):
    """把一帧转成可 json.dumps 的 dict（raw 用十六进制字符串）。"""
    return {
        "sector": frame["sector"],
        "angle": frame["angle"],
        "direction": angle_to_direction(frame["angle"]),
        "text": describe_angle(frame["angle"]),
        "raw": frame["raw"].hex(),
        "ts": round(float(frame["ts"]), 3),
    }


def _print_frame(frame, args):
    """按 --json 与否打印一帧。"""
    if args.json:
        print(json.dumps(frame_as_json(frame), ensure_ascii=False), flush=True)
    else:
        print(format_line(frame, show_raw=args.raw), flush=True)


def _build_arg_parser():
    """构造命令行参数解析器（--help 文案要能直接给用户看）。"""
    ap = argparse.ArgumentParser(
        prog="qxmic_locator.py",
        description="HK-QXMIC-V3.0 声源定位麦克风串口驱动 / 自测工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 qxmic_locator.py --list-ports\n"
               "  python3 qxmic_locator.py --simulate --duration 3\n"
               "  python3 qxmic_locator.py --port /dev/ttyMIC\n"
               "  python3 qxmic_locator.py --json --duration 5\n"
               "  python3 qxmic_locator.py --offset 90 --simulate\n"
               "\n对应文档：《6.说明文档/4.语音模块/声源定位麦克风.md》",
    )
    ap.add_argument("--list-ports", action="store_true",
                    help="列出本机串口（自动标出疑似声源麦克风 / 小车串口）")
    ap.add_argument("--port", default=None,
                    help="串口设备名，如 /dev/ttyMIC、COM5（默认自动找）")
    ap.add_argument("--baud", type=int, default=BAUD,
                    help="波特率，默认 1000000（厂商固定值，一般不用改）")
    ap.add_argument("--offset", type=int, default=0,
                    help="安装偏移（度）：麦克风装歪了用它补偿角度，默认 0")
    ap.add_argument("--json", action="store_true",
                    help="每帧输出一行 JSON（sector/angle/direction/text/raw/ts）")
    ap.add_argument("--raw", action="store_true",
                    help="普通输出时附带打印原始字节（排障用）")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="运行多少秒后自动结束；0 = 一直跑到 Ctrl-C")
    ap.add_argument("--simulate", action="store_true",
                    help="无硬件自测：用内置模拟帧跑解析/显示链路（不打开串口）")
    ap.add_argument("--quiet", action="store_true",
                    help="只输出数据行，不打印横幅/提示")
    return ap


def _wants_forever(duration):
    """duration <= 0 或 NaN 视为「一直跑」。"""
    try:
        return not (duration and duration > 0)
    except TypeError:
        return True


def _cmd_list_ports(args):
    """--list-ports：列出串口并标注用途。pyserial 没装也返回 0（不崩）。"""
    if not args.quiet:
        print("=" * 62)
        print(" 串口列表（找声源定位麦克风的 USB-TTL）")
        print("=" * 62)
    if os.name != "nt":
        if os.path.exists(UDEV_LINK):
            print("  %s  ← udev 固定别名（存在，推荐直接用）" % UDEV_LINK)
        else:
            print("  %s  （不存在；可运行 sudo bash setup_qxmic_udev.sh 固定）"
                  % UDEV_LINK)
        print()

    ports = list_serial_ports()
    if not ports:
        print("  没有枚举到任何串口。")
        print("  · 没装 pyserial 时无法枚举：python3 -m pip install pyserial")
        print("  · 麦克风需要 USB 供电，且接口3 的 TX→USB-TTL RX、GND→GND 接好")
        print("  · 没有硬件也可以先跑：python3 qxmic_locator.py --simulate")
        return 0

    for p in ports:
        if p["device"] == UDEV_LINK:
            mark = "← 声源麦克风（udev 固定）"
        elif p["is_car"]:
            mark = "← 小车下位机 CH9102（别让本模块抢它）"
        elif p["is_ch34x"]:
            pid_name = _CH34X_PIDS.get(p["pid"], "CH34x")
            mark = "← 疑似声源麦克风（%s）" % pid_name
        else:
            mark = ""
        line = "  %-14s %s" % (p["device"], p["description"] or "(无描述)")
        if mark:
            line += "  " + mark
        print(line)
    return 0


def _cmd_simulate(args):
    """--simulate：内置模拟帧跑一遍解析 + 显示，不打开任何串口。"""
    if not args.quiet:
        print("=" * 62)
        print(" HK-QXMIC 声源定位麦克风 —— 模拟模式（无硬件）")
        print("=" * 62)
        print("  不打开任何串口，用内置模拟帧验证「解析 → 角度 → 方向」链路。")
        print("  帧顺序：%s（每帧约 250ms）"
              % " ".join(str(s) for s in SIM_SECTORS))
        print("  对应文档：《6.说明文档/4.语音模块/声源定位麦克风.md》")
        print()

    deadline = None if _wants_forever(args.duration) else time.time() + args.duration
    gen = iter_simulated_frames(period=0.25, offset=args.offset)
    count = 0
    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                break
            frame = next(gen)
            _print_frame(frame, args)
            count += 1
    except KeyboardInterrupt:
        if not args.quiet:
            print("\n  已停止（Ctrl-C）。")

    if not args.quiet:
        print("\n  模拟结束：共 %d 帧。" % count)
    return 0


def _cmd_real(args):
    """真机模式：打开串口逐帧打印；打不开时友好报错（不抛栈）。"""
    loc = QxMicLocator(port=args.port, baud=args.baud, offset=args.offset)
    try:
        loc.open()
    except QxMicError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        print("  没有硬件时可先用 --simulate 自测："
              "python3 qxmic_locator.py --simulate --duration 3", file=sys.stderr)
        return 2

    if not args.quiet:
        print("=" * 62)
        print(" HK-QXMIC 声源定位麦克风 —— 真机模式")
        print("=" * 62)
        print("  串口：%s   波特率：%d   安装偏移：%d°"
              % (loc.port, loc.baud, loc.offset))
        print("  设备每 200~300ms 吐一帧；Ctrl-C 退出。")
        print("  对应文档：《6.说明文档/4.语音模块/声源定位麦克风.md》")
        print()

    deadline = None if _wants_forever(args.duration) else time.time() + args.duration
    count = 0
    idle = 0          # 连续多少次 wait_next 超时（真机 200~300ms 一帧，通常不会超时）
    warned = False
    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                break
            frame = loc.wait_next(timeout=0.5)
            if frame is None:
                if loc.error:
                    print("[错误] %s" % loc.error, file=sys.stderr)
                    return 3
                if deadline is not None and time.time() >= deadline:
                    break
                idle += 1
                # 连续 ~1s 收不到帧才提示一次，避免刚打开设备时的误报
                if not warned and idle >= 2 and not args.quiet:
                    print("  （还没收到帧：等设备上电，或检查 TX/RX 是否接反、"
                          "波特率是否 1M）", file=sys.stderr)
                    warned = True
                continue
            idle = 0
            _print_frame(frame, args)
            count += 1
    except KeyboardInterrupt:
        if not args.quiet:
            print("\n  已停止（Ctrl-C）。")
    finally:
        loc.close()

    if not args.quiet:
        print("\n  结束：共收到 %d 帧。" % count)
    return 0


def main(argv=None):
    """CLI 入口；返回退出码（0=正常，2=串口打不开，3=读串口出错）。"""
    args = _build_arg_parser().parse_args(argv)
    if args.baud <= 0:
        print("[错误] --baud 必须是正整数，收到 %r" % args.baud, file=sys.stderr)
        return 2
    if args.list_ports:
        return _cmd_list_ports(args)
    if args.simulate:
        return _cmd_simulate(args)
    return _cmd_real(args)


if __name__ == "__main__":
    sys.exit(main())