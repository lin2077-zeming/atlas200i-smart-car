#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kws_engine.py —— 关键词唤醒 / 语音急停引擎（sherpa-onnx Zipformer KWS）

设计目标
--------
给小车加一条**永远在线、不经大模型、低延迟**的语音通道，听到"急停"这个词
立刻停车。它和 ASR（SenseVoice）是两条独立链路：

    ASR 链路：按住说话 → 录音 → SenseVoice 识别整句 → 解析成指令 → 执行
              （延迟 1~3 秒，依赖 CPU，用于"前进一米"这种复杂指令）

    KWS 链路：麦克风常开 → Zipformer 流式扫 → 命中"急停" → 立即停车
              （延迟 <300ms，模型仅 3.3M，用于保命）

为什么不能让 ASR 兼做急停？
    1. ASR 是**一句话说完才出结果**，说"急停"要先等 VAD 断句（默认 0.8s 静音），
       再等 1~3 秒识别 → 反应太慢。
    2. ASR 跑一次吃满 2 个 CPU 核，做不到常开。
    3. KWS 模型只有 3.3M，CPU 占用极低，可以 7×24 挂着。

⚠️ 安全边界（必读）
    - 语音急停**不能替代物理急停**。它只是"多一只手"，不是安全带。
    - 上电 10 秒内下位机不收数据（CONTROL_DELAY=1000），此时急停无效。
    - 麦克风被拔/增益被调 0，急停即失效 → 所以有 watchdog（见 KwsEngine.watchdog_ok）。
    - 严禁把单字"停"作为一级急停词（日常对话误触发率过高）。

本板适配要点（Atlas 200I DK A2 + TI PCM2902）
    1. PCM2902 硬件**只支持 44100/48000**，没有 16kHz → 复用 asr_engine._RATE_CACHE
    2. PCM2902 默认录音增益 **0%** → 必须先跑 setup_mic_gain.sh，否则听到的全是底噪
    3. 用 **FP32** 版 onnx。INT8 版与静态运行时不兼容
       （报 'attention_dims' does not exist in the metadata）
    4. 上一代 RK3576 是静态运行时二进制，本板用 pip 装的 Python API（sherpa_onnx 1.13.8）
"""

import os
import threading
import time

# 复用 ASR 那边的设备探测与采样率缓存（同一套坑，不要重复踩）
try:
    from asr_engine import (
        HAS_NUMPY, HAS_SOUNDDEVICE, _np, _sd,
        _RATE_CACHE, _dev_cache_key, _rms, _default_input_rate,
        ASR_SAMPLE_RATE, list_input_devices, guess_input_device,
        audio_environment_hint, is_real_device, real_input_devices,
        _classify_open_error, friendly_audio_error, _rate_cache_store,
        alsa_capture_free,
    )
    _REUSE_ASR = True
except Exception:                                     # pragma: no cover
    _REUSE_ASR = False
    try:
        import numpy as _np
        HAS_NUMPY = True
    except Exception:
        _np = None
        HAS_NUMPY = False
    HAS_SOUNDDEVICE = False
    _RATE_CACHE = {}
    ASR_SAMPLE_RATE = 16000

    def _classify_open_error(err):
        return "other"

    def friendly_audio_error(err, device=None):
        return " ".join(str(err).split())[:200]

    def _rate_cache_store(key, rate):
        try:
            _RATE_CACHE[key] = int(rate)
        except (TypeError, ValueError):
            pass

    def _dev_cache_key(device):
        return "__default__"

    def alsa_capture_free(device):
        """asr_engine 不可用时的降级桩：保守放行（见那边同名函数的说明）。"""
        return True

    def _rms(samples):
        return 0.0

    def _default_input_rate(device=None, fallback=44100):
        return fallback

    def list_input_devices():
        return []

    def guess_input_device(prefer="usb"):
        return None

    def audio_environment_hint():
        return False, "asr_engine 不可用，无法诊断音频环境"

    def is_real_device(name):
        return False

    def real_input_devices():
        return []

_HAS_SHERPA = False
_KWS_ERROR = ""
try:
    import sherpa_onnx as _sherpa
    _HAS_SHERPA = True
except Exception as _e:                                # pragma: no cover
    _sherpa = None
    _KWS_ERROR = str(_e)


# ══════════════════════════════════════════════════════════════════════
#  默认路径与参数
# ══════════════════════════════════════════════════════════════════════
DEFAULT_KWS_DIRS = (
    os.path.expanduser("~/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"),
    "/data/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
    "/home/HwHiAiUser/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
)

# 模型文件名（用 epoch-99-avg-1 这组，官方推荐；FP32，不要 int8）
ENCODER_FILE = "encoder-epoch-99-avg-1-chunk-16-left-64.onnx"
DECODER_FILE = "decoder-epoch-99-avg-1-chunk-16-left-64.onnx"
JOINER_FILE = "joiner-epoch-99-avg-1-chunk-16-left-64.onnx"
TOKENS_FILE = "tokens.txt"

# 关键词文件（默认用同目录的急停词表）
DEFAULT_KEYWORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "kws_keywords_emergency.txt")

KWS_SAMPLE_RATE = 16000        # KWS 模型的特征采样率（固定，不可改）
KWS_FEATURE_DIM = 80           # Zipformer KWS 固定 80 维 fbank
DEFAULT_THREADS = 1            # 3.3M 小模型 1 个线程就够；板子只有 4 核，要留给 GUI/串口
DEFAULT_THRESHOLD = 0.25       # 官方推荐；实测官方 test_wavs 在 0.25 即命中

BLOCK_SECONDS = 0.20           # 每次喂给 KWS 的音频块长度（秒）
COOLDOWN_SECONDS = 1.0         # 命中后冷却，避免同一句话连续触发多次

# watchdog：超过这么久没有成功读到过音频，就认为麦克风链路已死
WATCHDOG_SECONDS = 2.0

# ── 打开麦克风的重试策略（2026-10-11，修"按住说话后急停失效"）──────────
# 本板播放与采集硬件互斥：PTT 说完一句后车立刻播报，KWS 恢复时若撞上播报，
# `_open_stream()` 必然失败。这是**瞬时**状态，必须重试而不是放弃。
OPEN_RETRY_DELAY = 0.3         # 首次重试前等待（秒）
OPEN_RETRY_MAX_DELAY = 2.0     # 退避上限（秒）
OPEN_RETRY_SECONDS = 30.0      # 总重试预算；超了才认定"真的拿不到麦克风"

#: ★ **回声保护期**（2026-10-11 实测踩到）：
#:   播报结束→恢复监听后，扬声器的**余音/混响**会被麦克风收进去。
#:   如果刚播的那句里含急停词（「已急停」「停车」「刹车」…），
#:   KWS 会**把自己说的当成用户在喊急停** → 自我触发锁定。
#:   实测：播报「停车」后 1 秒内命中「急停」，车被自己的声音锁死。
#:   ⇒ 重新打开麦克风后的这段时间内**忽略命中**，等声场静下来。
ECHO_GUARD_SECONDS = 0.6

# 一级急停词（命中即停车）。顺序无关，只是留个清单给上层做展示/校验。
LEVEL1_KEYWORDS = ("急停", "紧急停止", "小车急停", "停车", "刹车")


# ══════════════════════════════════════════════════════════════════════
#  模型文件查找
# ══════════════════════════════════════════════════════════════════════
def guess_kws_dir():
    """按常见位置找一个含完整 KWS 模型的目录。"""
    for d in DEFAULT_KWS_DIRS:
        if find_kws_files(d)[0]:
            return d
    for d in DEFAULT_KWS_DIRS:
        if os.path.isdir(d):
            return d
    return DEFAULT_KWS_DIRS[0]


def find_kws_files(kws_dir):
    """
    在 kws_dir 里找 (encoder, decoder, joiner, tokens)。

    找不到就返回 (None, None, None, None)。
    注意：优先 FP32 的 epoch-99-avg-1；如果那个不存在，退一步在目录里扫。
    """
    if not kws_dir or not os.path.isdir(kws_dir):
        return None, None, None, None

    def pick(name):
        p = os.path.join(kws_dir, name)
        return p if os.path.isfile(p) else None

    enc = pick(ENCODER_FILE)
    dec = pick(DECODER_FILE)
    joi = pick(JOINER_FILE)
    tok = pick(TOKENS_FILE)

    # 退路：目录里扫不含 int8 的 onnx
    if enc is None:
        for f in sorted(os.listdir(kws_dir)):
            if f.startswith("encoder") and f.endswith(".onnx") and "int8" not in f:
                enc = os.path.join(kws_dir, f)
                break
    if dec is None:
        for f in sorted(os.listdir(kws_dir)):
            if f.startswith("decoder") and f.endswith(".onnx") and "int8" not in f:
                dec = os.path.join(kws_dir, f)
                break
    if joi is None:
        for f in sorted(os.listdir(kws_dir)):
            if f.startswith("joiner") and f.endswith(".onnx") and "int8" not in f:
                joi = os.path.join(kws_dir, f)
                break

    return enc, dec, joi, tok


def load_keywords(path):
    """
    读关键词文件，返回 [(显示标签, 拼音串), ...]。

    跳过空行与 # 注释行。拼音串里 @ 左边是拼音、右边是标签。
    """
    out = []
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "@" not in s:
                continue
            pinyin, label = s.rsplit("@", 1)
            out.append((label.strip(), pinyin.strip()))
    return out


def check_keywords_file(path):
    """
    给关键词文件做静态体检，返回 [(行号, 原文, 问题), ...]。

    ⚠️ 为什么需要这个？
        sherpa-onnx 的 KeywordSpotter 读关键词文件时**用的是极简的 str -> float
        解析**，遇到不认识的内容直接抛 `std::invalid_argument: stof`，既没有
        行号也没有原因，非常难排查。实测（2026-09-26，sherpa_onnx 1.13.8）：

            ✅ 支持：  j í t íng @急停
            ✅ 支持：  空行
            ❌ 不支持： # 这是注释          → 抛 stof
            ❌ 不支持： 缺少 @ 的行          → 抛 stof
            ❌ 不支持： >1 个 @              → 语义错乱

        所以本函数把已知的坑全部提前拦下来，给出人能看懂的错误。
    """
    problems = []
    if not path or not os.path.isfile(path):
        return [(0, "", "文件不存在")]

    # 合法拼音字符集：声母（含 zh/ch/sh）、韵母（含声调符号）、字母、空格
    _TONE = "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü"
    _LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _VALID = set(_LETTERS + _TONE)

    with open(path, "r", encoding="utf-8") as f:
        for ln, raw in enumerate(f, 1):
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#"):
                problems.append((ln, s, "不支持 # 注释行（sherpa 会抛 stof），"
                                         "请把说明写到文件外面"))
                continue
            if "@" not in s:
                problems.append((ln, s, "缺少 @ 标签"))
                continue
            if s.count("@") > 1:
                problems.append((ln, s, "@ 只能出现 1 次"))
                continue
            pinyin, label = s.rsplit("@", 1)
            label = label.strip()
            if not label:
                problems.append((ln, s, "@ 后面的展示标签为空"))
            bad = sorted({c for c in pinyin if c != " " and c not in _VALID})
            if bad:
                problems.append((ln, s, "拼音里有非法字符：%s" % "".join(bad)))
            if not pinyin.strip():
                problems.append((ln, s, "@ 前面没有拼音"))
            else:
                # 单字词检查：如果整个拼音只对应一个音节，提示风险
                tokens = pinyin.split()
                # 一个汉字通常 = 1 声母 + 1 韵母（或只有韵母），所以 <2 个 token 基本都是单字
                if len(tokens) < 2:
                    problems.append((ln, s, "看起来是单字词，日常对话误触发风险高"
                                             "（不建议作一级急停词）"))
    return problems


# ══════════════════════════════════════════════════════════════════════
#  关键词检测引擎（纯计算，不含录音）
# ══════════════════════════════════════════════════════════════════════
class KwsEngine(object):
    """
    关键词检测引擎。

    用法（离线，喂 wav）：
        k = KwsEngine()
        ok, msg = k.load()
        hits = k.detect_file("test.wav")

    用法（实时）见 KwsListener。
    """

    def __init__(self, log=None):
        self._log = log or (lambda s: None)
        self._spotter = None
        self._dir = None
        self._keywords = []
        self._threshold = DEFAULT_THRESHOLD
        self._streams = {}          # 每个"通道"一个 stream（支持多路复用，一般只用 0）
        self._lock = threading.Lock()

    # ── 状态查询 ────────────────────────────────────────────────────────
    @property
    def ready(self):
        return self._spotter is not None

    @property
    def keywords(self):
        return list(self._keywords)

    @property
    def model_dir(self):
        return self._dir

    def describe(self):
        if not self._spotter:
            return "KWS 未加载"
        labels = "、".join(lb for lb, _ in self._keywords)
        return "KWS · %d 个关键词（%s）· 阈值 %.2f" % (
            len(self._keywords), labels, self._threshold)

    # ── 加载 ────────────────────────────────────────────────────────────
    def load(self, kws_dir=None, keywords_file=None,
             num_threads=DEFAULT_THREADS, threshold=DEFAULT_THRESHOLD,
             provider="cpu"):
        """加载模型。返回 (是否成功, 提示)。"""
        # ★ 防御：调用方可能显式传 None（本意是"用默认值"），
        #   但显式 None 会**覆盖**函数签名的默认值 → 后面 `float(threshold)`
        #   直接抛 `float() argument must be a string or a real number, not 'NoneType'`。
        #   2026-09-29 实测踩到：GUI 里点「开启语音急停」必然失败
        #   （`car_gui_core.enable_voice_emergency(threshold=None)`），
        #   而**语音急停失效是安全问题**，不该因为一个 None 就静默废掉。
        #   在这里归一化，任何调用方传 None 都退化为默认阈值。
        if threshold is None:
            threshold = DEFAULT_THRESHOLD
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        if num_threads is None:
            num_threads = DEFAULT_THREADS
        if not _HAS_SHERPA:
            return False, "未安装 sherpa_onnx：%s" % _KWS_ERROR

        kws_dir = kws_dir or guess_kws_dir()
        enc, dec, joi, tok = find_kws_files(kws_dir)
        missing = [n for n, p in (("encoder", enc), ("decoder", dec),
                                  ("joiner", joi), ("tokens", tok)) if not p]
        if missing:
            return False, "模型目录 %s 缺少文件：%s" % (kws_dir, "、".join(missing))

        kw_file = keywords_file or DEFAULT_KEYWORDS_FILE
        if not os.path.isfile(kw_file):
            return False, "找不到关键词文件：%s" % kw_file

        # 先做静态体检，把 sherpa 那个没头没尾的 `stof` 错误提前变成人话
        problems = check_keywords_file(kw_file)
        fatal = [p for p in problems if "单字词" not in p[2]]
        if fatal:
            detail = "；".join("第%d行 %r：%s" % (ln, txt, why)
                              for ln, txt, why in fatal[:4])
            return False, "关键词文件有问题（%s）→ %s" % (kw_file, detail)

        self._keywords = load_keywords(kw_file)
        if not self._keywords:
            return False, "关键词文件里没有有效条目：%s" % kw_file

        t0 = time.time()
        try:
            self._spotter = _sherpa.KeywordSpotter(
                tokens=tok,
                encoder=enc,
                decoder=dec,
                joiner=joi,
                keywords_file=kw_file,
                num_threads=int(num_threads),
                sample_rate=KWS_SAMPLE_RATE,
                feature_dim=KWS_FEATURE_DIM,
                keywords_threshold=float(threshold),
                provider=provider,
            )
        except Exception as e:
            self._spotter = None
            return False, "加载 KWS 模型失败：%s" % e

        self._dir = kws_dir
        self._threshold = float(threshold)
        cost = time.time() - t0
        self._log("KWS 加载完成，用时 %.2f 秒；%s" % (cost, self.describe()))
        return True, "加载完成，用时 %.2f 秒" % cost

    # ── 流式解码 ────────────────────────────────────────────────────────
    def make_stream(self, name="main"):
        """建（或复用）一条解码流。"""
        if not self._spotter:
            return None
        with self._lock:
            st = self._streams.get(name)
            if st is None:
                st = self._spotter.create_stream()
                self._streams[name] = st
            return st

    def reset_stream(self, name="main"):
        """清空一条流（切句/命中后调用）。"""
        st = self._streams.get(name)
        if st is None or not self._spotter:
            return
        try:
            self._spotter.reset_stream(st)
        except Exception:
            pass

    def push(self, samples, sample_rate, name="main"):
        """
        喂一段音频（float32，单声道，任意采样率），返回本次命中的关键词列表。

        sherpa 内部会自动重采样到 16k，所以 44100 直接喂即可。
        """
        if not self._spotter or not HAS_NUMPY or samples is None or len(samples) == 0:
            return []
        st = self.make_stream(name)
        if st is None:
            return []
        hits = []
        try:
            st.accept_waveform(int(sample_rate), samples)
            while self._spotter.is_ready(st):
                self._spotter.decode_stream(st)
                r = self._spotter.get_result(st)
                if r:
                    hits.append(r)
                    self._spotter.reset_stream(st)
        except Exception as e:
            self._log("KWS 解码异常：%s" % e)
        return hits

    # ── 离线：直接喂一个 wav ─────────────────────────────────────────────
    def detect_file(self, path):
        """
        识别一个 wav 文件里出现的全部关键词，返回 [(标签, 起始秒), ...]。

        注意：sherpa 的 KWS 结果**不带时间戳**，这里只能给出"在整段音频里命中"。
        需要定位的话可以把音频切块后逐块喂（KwsListener 就是这么做的）。
        """
        if not self.ready:
            return []
        rate, data = _read_wav(path)
        if data is None:
            return []
        self.reset_stream("offline")
        return self.push(data, rate, name="offline")

    def detect_file_segmented(self, path, block_seconds=BLOCK_SECONDS):
        """
        把 wav 切块喂进去，这样能拿到命中时刻（粗粒度到块）。

        返回 [(标签, 秒), ...]
        """
        if not self.ready:
            return []
        rate, data = _read_wav(path)
        if data is None:
            return []
        self.reset_stream("offline")
        blk = max(1, int(rate * block_seconds))
        out = []
        for i in range(0, len(data), blk):
            chunk = data[i:i + blk]
            hits = self.push(chunk, rate, name="offline")
            for h in hits:
                out.append((h, round(i / float(rate), 2)))
        return out


# ══════════════════════════════════════════════════════════════════════
#  实时监听（麦克风常开）
# ══════════════════════════════════════════════════════════════════════
class KwsListener(object):
    """
    后台线程常开麦克风，边录边判，命中就回调。

        def on_hit(label, ts):
            print("命中", label)
        lis = KwsListener(on_hit=on_hit)
        ok, msg = lis.start()
        ...
        lis.stop()

    回调在**监听线程**里执行，所以回调必须尽量快、且不能碰 GUI 控件。
    默认由调用方自己保证（例如回调里只 set 一个 Event / 压一个队列）。
    """

    def __init__(self, engine=None, device=None, on_hit=None,
                 cooldown=COOLDOWN_SECONDS, block_seconds=BLOCK_SECONDS,
                 log=None):
        self.engine = engine or KwsEngine(log=log)
        self.device = device
        self.on_hit = on_hit
        self.cooldown = float(cooldown)
        self.block_seconds = float(block_seconds)
        self._log = log or (lambda s: None)

        self._thread = None
        self._running = False
        #: ★ 是否正在**尝试打开麦克风**（见 `_loop` 与 `watchdog_ok`）。
        #:   本板播放/采集硬件互斥，"播报中打不开"是**瞬时**状态，
        #:   重试期间不能算"链路已死"，否则每次播报都会闪一次红字告警。
        self._opening = False
        #: ★ 是否被**要求暂停让位**（正在播报）——见 `pause()`/`resume()`。
        #:   本板采集与播放双向互斥，播报时必须让出麦克风。
        self._paused = False
        #: ★ 这个时刻之前**忽略命中**（回声保护，见 `ECHO_GUARD_SECONDS`）
        self._accept_hits_after = 0.0
        self._stream = None
        self._last_hit_at = 0.0
        self._last_audio_at = 0.0
        self._samplerate = 0
        self._level = 0.0
        self._hits = []              # [(标签, 时间戳), ...] 供上层查历史
        self._hits_lock = threading.Lock()
        self._state_lock = threading.Lock()

    # ── 状态 ────────────────────────────────────────────────────────────
    @property
    def running(self):
        return self._running

    @property
    def samplerate(self):
        return self._samplerate

    @property
    def level(self):
        return self._level

    @property
    def hits(self):
        with self._hits_lock:
            return list(self._hits)

    def watchdog_ok(self, max_silence=WATCHDOG_SECONDS):
        """
        麦克风链路是否还活着。

        判据：最近 max_silence 秒内**收到过音频块**（不代表有声音，只代表
        设备在出数据）。如果麦克风被拔掉，sounddevice 的流会停住，这里就会变
        False —— 上层应该据此提示"语音急停已离线"。

        ★ 例外（2026-10-11）：**正在尝试打开麦克风**时返回 True。
          本板播放/采集硬件互斥，PTT 说完一句后车立刻播报，
          KWS 恢复时会有一两秒打不开 —— 那是正常的瞬时状态，
          不该报"链路已断"。真正的失败由 `_loop` 的重试预算兜底
          （超预算会 `_running=False`，那时这里自然变 False）。
        """
        if not self._running:
            return False
        # ★ 主动让位给播报（`pause()`）→ 不算故障。
        #   本板采集/播放互斥，播报期间本来就听不了，不该报"链路已断"。
        if getattr(self, "_paused", False):
            return True
        if getattr(self, "_opening", False):
            return True
        return (time.time() - self._last_audio_at) <= max_silence

    # ── 启动 / 停止 ─────────────────────────────────────────────────────
    def start(self, device=None):
        """启动监听。返回 (是否成功, 提示)。"""
        if self._running:
            return True, "已在监听"
        if not self.engine.ready:
            return False, "KWS 引擎未加载，先调用 engine.load()"
        if not (HAS_SOUNDDEVICE and HAS_NUMPY):
            return False, "缺少 sounddevice / numpy"

        self.device = device if device is not None else self.device
        if self.device is None:
            self.device = guess_input_device()
        if self.device is None:
            ok, hint = audio_environment_hint()
            return False, hint or "检测不到录音设备（麦克风是否插好？）"

        with self._state_lock:
            self._running = True
            self._last_audio_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="kws-listener",
                                        daemon=True)
        self._thread.start()
        return True, "监听已启动（设备 %s）" % self.device

    def stop(self, timeout=3.0):
        """停止监听并释放麦克风。"""
        self._running = False
        self._paused = False          # 停掉时清掉暂停态，下次 start 能正常跑
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        self._close_stream()
        return True

    def _close_stream(self):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

    # ── 主循环（在后台线程里跑）──────────────────────────────────────────
    def _loop(self):
        """KWS 监听主循环（状态机：暂停让位 / 重试打开 / 读音频）。

        🔴 **打开麦克风必须重试**（2026-10-11 修「按住说话后急停失效」）：
        本板播放与采集硬件互斥，PTT 说完一句后车立刻播报，
        KWS 恢复时撞上播报 → `_open_stream()` 必然失败。
        这是瞬时状态，重试即可恢复；旧代码失败一次就 `_running=False`
        → 语音急停**永久失效**（实测日志见 `OPEN_RETRY_SECONDS` 附近）。

        🔴🔴 **还要支持暂停让位**（2026-10-11 第二轮，修「播报一直失败」）：
        本板采集与播放是**双向互斥**（同一 xHCI + 全速 USB 带宽不足）。实测：
            · 麦克风空闲        → `aplay -D plughw:1,0` rc=0 ✅ 正常播 2.0s
            · 麦克风被常驻占着  → `aplay: set_params:1416:
                                   Unable to install hw params` ❌ 必失败
        而 KWS 是**常驻监听** ⇒ "急停活着"与"能播报"互相掐死。
        ⇒ 播报前 `pause()`（关流、让出麦克风），播报后 `resume()`（自动重开）。
          这对急停**零损失**：播报期间麦克风本来也打不开。
        """
        rate = None
        blk = 0
        delay = OPEN_RETRY_DELAY
        t_try = time.time()

        try:
            while self._running:
                # ① 被要求暂停（正在播报）→ 关流让位，等恢复
                if self._paused:
                    if self._stream is not None:
                        self._close_stream()
                        rate = None
                        self._log("KWS 已让出麦克风（播报中）")
                    # ★ 无论之前是"在听"还是"正在重试"，都清掉 `_opening`：
                    #   这样恢复后会**重新开始**计算重试预算，
                    #   不会因为"暂停期间时间流逝"而一恢复就超预算放弃。
                    self._opening = False
                    time.sleep(0.08)
                    continue

                # ② 流还没打开 → 打开（带重试预算）
                if self._stream is None:
                    if not self._opening:
                        self._opening = True
                        t_try = time.time()
                        delay = OPEN_RETRY_DELAY
                    if (time.time() - t_try) > OPEN_RETRY_SECONDS:
                        self._opening = False
                        self._running = False
                        self._log("KWS 监听启动失败：麦克风被占用超过 %.0f 秒，"
                                  "语音急停不可用" % OPEN_RETRY_SECONDS)
                        return
                    rate = self._open_stream()
                    if rate is None:
                        self._log("KWS 麦克风暂时打不开（多半正在播报），"
                                  "%.1fs 后自动重试…" % delay)
                        slept = 0.0
                        while (self._running and not self._paused
                               and slept < delay):
                            time.sleep(0.05)
                            slept += 0.05
                        delay = min(delay * 1.8, OPEN_RETRY_MAX_DELAY)
                        continue
                    self._opening = False
                    self._samplerate = rate
                    self._last_audio_at = time.time()
                    blk = max(1, int(rate * self.block_seconds))
                    # ★ 回声保护：刚打开麦克风后的一小段时间内忽略命中，
                    #   避免把扬声器余音（可能就是"已急停"）当成用户喊急停。
                    self._accept_hits_after = time.time() + ECHO_GUARD_SECONDS
                    self._log("KWS 监听中 @ %d Hz（设备 %s）" % (rate, self.device))

                # ③ 读一块音频
                try:
                    data, overflowed = self._stream.read(blk)
                except Exception as e:
                    self._log("KWS 读音频失败：%s" % e)
                    self._close_stream()
                    rate = None
                    self._opening = True
                    t_try = time.time()
                    time.sleep(0.1)
                    continue

                mono = data[:, 0] if data.ndim > 1 else data
                self._level = _rms(mono)
                self._last_audio_at = time.time()

                hits = self.engine.push(mono, rate, name="live")
                for label in hits:
                    now = time.time()
                    # ★ 回声保护：刚恢复监听时忽略命中，别被自己的喇叭声触发
                    if now < getattr(self, "_accept_hits_after", 0.0):
                        continue
                    if now - self._last_hit_at < self.cooldown:
                        continue          # 冷却期内，忽略重复命中
                    self._last_hit_at = now
                    with self._hits_lock:
                        self._hits.append((label, now))
                    self._emit(label, now)
        finally:
            self._close_stream()
            self._opening = False
            self._running = False
            self._log("KWS 监听已停止")

    # ── 暂停 / 恢复（给播报让出麦克风）─────────────────────────────────
    def pause(self):
        """**暂停监听并让出麦克风**（播报前调用）。返回是否真的从"在听"转暂停。

        见 `_loop` 的说明：本板采集与播放双向互斥，播报时必须先把麦克风放开。
        这对急停**零损失** —— 播报期间麦克风本来也打不开。
        """
        with self._state_lock:
            was = self._running and not self._paused
            self._paused = True
            # 别让看门狗把"主动让位"误判成"链路已死"
            self._last_audio_at = time.time()
        return was

    def resume(self):
        """恢复监听（播报后调用）。循环会自动重新打开麦克风。"""
        with self._state_lock:
            if not self._paused:
                return False
            self._paused = False
            self._last_audio_at = time.time()
        return True

    @property
    def paused(self):
        return self._paused

    def _emit(self, label, ts):
        if self.on_hit is None:
            return
        try:
            self.on_hit(label, ts)
        except Exception as e:
            self._log("KWS 回调异常：%s" % e)

    def _open_stream(self):
        """
        打开麦克风输入流。

        采样率策略与 asr_engine.Recorder 一致：优先复用缓存，再试 16k，最后
        用设备默认值。PCM2902 只支持 44100/48000，所以缓存命中很关键。
        """
        if self.device is None:
            return None

        # ★★ 先问 ALSA"设备空闲吗"（2026-10-11）：
        #    上面 `_loop` 现在会**反复重试**打开麦克风，而**反复失败的
        #    AlsaOpen 会让 PortAudio 的 ALSA 后端断言 abort**
        #    （实测闪退日志：PaAlsaStream_Initialize: Assertion
        #      `self->capture.nfds || self->playback.nfds' failed）。
        #    `try/except` 抓不住 abort —— 只能从源头避免：
        #    设备不空闲就**直接返回 None**，一次失败的 AlsaOpen 都不产生。
        if not alsa_capture_free(self.device):
            return None

        key = _dev_cache_key(self.device)
        candidates = []
        cached = _RATE_CACHE.get(key)
        if cached:
            candidates.append(cached)
        if ASR_SAMPLE_RATE not in candidates:
            candidates.append(ASR_SAMPLE_RATE)
        candidates.append(None)

        # ★ 与 asr_engine.Recorder.start() 同一套策略（2026-09-29）：
        #   区分「采样率不支持」（换下一个）与「设备忙」（等一轮再试同一个）。
        #   语音急停必须尽快拿到麦克风 —— 但"设备忙"时死等没意义，
        #   只等一轮，仍失败就如实报错（安全上宁可明确失败也不要假成功）。
        _BUSY_BACKOFF = 0.5

        last_err = None
        busy_waited = False
        for rate in candidates:
            stream = None
            try:
                r = _default_input_rate(self.device) if rate is None else rate
                stream = _sd.InputStream(
                    device=self.device,
                    samplerate=r,
                    channels=1,
                    dtype="float32",
                    blocksize=0,
                    latency="high",
                )
                stream.start()
                self._stream = stream
                _RATE_CACHE[key] = r
                return r
            except Exception as e:
                last_err = e
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                try:
                    kind = _classify_open_error(e)
                except Exception:
                    kind = "other"
                if kind == "busy":
                    if busy_waited:
                        break
                    busy_waited = True
                    time.sleep(_BUSY_BACKOFF)
                    try:
                        s2 = _sd.InputStream(device=self.device, samplerate=r,
                                             channels=1, dtype="float32",
                                             blocksize=0, latency="high")
                        s2.start()
                        self._stream = s2
                        _RATE_CACHE[key] = r
                        return r
                    except Exception as e2:
                        last_err = e2
                        break
                continue
        self._log("打开麦克风失败（设备 %s）：%s"
                  % (self.device, friendly_audio_error(last_err, self.device)))
        return None


# ══════════════════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════════════════
def _read_wav(path):
    """读一个 wav，返回 (采样率, float32 单声道数组)；失败返回 (0, None)。"""
    if not HAS_NUMPY:
        return 0, None
    try:
        import wave
        with wave.open(path, "rb") as f:
            ch = f.getnchannels()
            sr = f.getframerate()
            n = f.getnframes()
            raw = f.readframes(n)
        data = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch)[:, 0]
        return sr, data
    except Exception:
        return 0, None


def dependency_status():
    """给自检脚本用的依赖状态。"""
    st = {"numpy": HAS_NUMPY, "sounddevice": HAS_SOUNDDEVICE,
          "sherpa_onnx": _HAS_SHERPA, "kws_error": _KWS_ERROR}
    if _HAS_SHERPA:
        st["version"] = getattr(_sherpa, "__version__", "?")
    return st


# ══════════════════════════════════════════════════════════════════════
#  命令行自检
# ══════════════════════════════════════════════════════════════════════
def _main():
    import argparse

    ap = argparse.ArgumentParser(
        description="KWS 语音急停引擎自检（Atlas 200I DK A2 适配版）")
    ap.add_argument("--dir", default=None, help="KWS 模型目录")
    ap.add_argument("--keywords", default=None, help="关键词文件")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    ap.add_argument("--file", default=None, help="对一个 wav 文件做检测")
    ap.add_argument("--test-wavs", action="store_true",
                    help="跑官方 test_wavs 全量验证")
    ap.add_argument("--mic", action="store_true", help="实时麦克风监听（Ctrl-C 退出）")
    ap.add_argument("--list-devices", action="store_true", help="列出录音设备")
    args = ap.parse_args()

    def log(s):
        print(s)

    if args.list_devices:
        for d in list_input_devices():
            print("  [%d] %-38s %dHz  %dch" % (d["index"], d["name"],
                                               d["samplerate"], d["channels"]))
        print("  自动选中：", guess_input_device())
        return

    dep = dependency_status()
    print("依赖：numpy=%s sounddevice=%s sherpa_onnx=%s(%s)" % (
        dep["numpy"], dep["sounddevice"], dep["sherpa_onnx"],
        dep.get("version", "-")))

    # 依赖不齐时给出可执行指引（本板多套 Python 是主要坑因）
    missing = [m for m in ("numpy", "sounddevice", "sherpa_onnx") if not dep.get(m)]
    if missing:
        try:
            from asr_engine import dependency_hint as _dh
            _, hint = _dh(missing)
            print()
            print(hint)
        except Exception:
            print("  提示：先跑 bash install_deps.sh；"
                  "或用 python3 env_doctor.py 确认解释器。")
        raise SystemExit(1)

    eng = KwsEngine(log=log)
    ok, msg = eng.load(kws_dir=args.dir, keywords_file=args.keywords,
                       num_threads=args.threads, threshold=args.threshold)
    print("加载：", ok, msg)
    if not ok:
        raise SystemExit(1)
    print("模型目录：", eng.model_dir)
    print("关键词：")
    for lb, py in eng.keywords:
        print("    %-10s <= %s" % (lb, py))

    if args.test_wavs:
        md = eng.model_dir
        tw = os.path.join(md, "test_wavs")
        kw = os.path.join(tw, "test_keywords.txt")
        if not os.path.isfile(kw):
            print("  找不到 test_wavs/test_keywords.txt，跳过")
        else:
            print("\n== 官方 test_wavs 验证（用官方关键词表）==")
            eng2 = KwsEngine(log=log)
            eng2.load(kws_dir=md, keywords_file=kw, threshold=args.threshold)
            for fn in sorted(os.listdir(tw)):
                if not fn.endswith(".wav"):
                    continue
                hits = eng2.detect_file(os.path.join(tw, fn))
                print("  %-8s -> %s" % (fn, hits if hits else "（无命中）"))

    if args.file:
        print("\n== 单文件检测 ==")
        t0 = time.time()
        seg = eng.detect_file_segmented(args.file)
        cost = time.time() - t0
        rate, data = _read_wav(args.file)
        dur = (len(data) / float(rate)) if data is not None else 0
        print("  %s" % args.file)
        print("  时长 %.2fs，耗时 %.2fs，RTF=%.3f" % (
            dur, cost, (cost / dur) if dur else 0))
        for lb, t in seg:
            print("    ✅ 命中 %s @ %.2fs" % (lb, t))
        if not seg:
            print("    （无命中）")

    if args.mic:
        print("\n== 实时监听（Ctrl-C 退出）==")
        print("  请对着麦克风清晰说：急停")

        def on_hit(label, ts):
            print("  🔴 %s  →  语音急停！" % label)

        lis = KwsListener(engine=eng, on_hit=on_hit, log=log)
        ok, msg = lis.start()
        print("  ", ok, msg)
        if not ok:
            raise SystemExit(1)
        try:
            while True:
                time.sleep(0.5)
                if not lis.watchdog_ok():
                    print("  ⚠️  麦克风无数据，语音急停已失效！")
        except KeyboardInterrupt:
            pass
        finally:
            lis.stop()
            print("  已停止")


if __name__ == "__main__":
    _main()
