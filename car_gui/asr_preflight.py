#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASR 环境预检（preflight）—— 在 USB 声卡到位前，把"能做的环境工作"全部验证完。

    python3 asr_preflight.py                # 跑全部检查
    python3 asr_preflight.py --no-model     # 跳过耗时的模型加载项

设计目标：把语音链路的每一层单独体检，**失败项必须给出可执行的修复命令**。
这样插上声卡那天，只需要确认"声卡那一层"，其余全部是已知良好状态。

链路分层（与文档 8 §6 对应）：
    L0 依赖      numpy / sounddevice / sherpa-onnx / PortAudio
    L1 声卡      ALSA 层 + sounddevice 层（板子无硬件 → 预期"未就绪"）
    L2 模型      三个文件是否齐 + 大小是否合理
    L3 引擎      模型能否真的加载（耗时）
    L4 识别      官方 test_wavs 能否识别对
    L5 闭环      合成音频走完整"录音数据 → 识别"路径
    L6 解析      识别文本 → 运动指令（不碰串口）

退出码：0 = 除声卡外全通过；1 = 有硬失败项。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asr_engine as E

# ── 输出 helpers ─────────────────────────────────────────────────────
_W = 64
_results = []          # [(层名, 项名, ok, 备注)]


def head(text):
    print()
    print("─" * _W)
    print(" %s" % text)
    print("─" * _W)


def item(name, ok, note="", soft=False):
    """
    ok   : True / False
    soft : 该失败是"预期内的"（如声卡未到货），不影响退出码
    """
    mark = "✅" if ok else ("⏳" if soft else "❌")
    _results.append((name, ok, note, soft))
    line = "  %s %-26s %s" % (mark, name, note)
    print(line)
    return ok


def fix(line):
    print("       ↳ 修复：%s" % line)


# ── L0 依赖 ──────────────────────────────────────────────────────────
def check_deps():
    head("L0 · Python 依赖")
    st = E.dependency_status()
    all_ok = True

    for key, label, hint in (
        ("numpy", "numpy", "python3 -m pip install numpy -i https://mirrors.cloud.tencent.com/pypi/simple"),
        ("sounddevice", "sounddevice", "python3 -m pip install sounddevice"),
        ("sherpa_onnx", "sherpa-onnx", "bash install_deps.sh"),
    ):
        ok = bool(st[key])
        all_ok &= item(label, ok,
                       st["versions"].get(key, "") if ok else "缺失",
                       soft=False)
        if not ok:
            fix(hint)
            if key == "sherpa_onnx" and st.get("sherpa_error"):
                print("       ↳ 错误详情：%s" % st["sherpa_error"])

    # PortAudio 单独看——它决定 sounddevice 能不能真的打开设备
    pa = st.get("portaudio") or ""
    pa_ok = bool(pa) and "查询失败" not in pa
    item("PortAudio", pa_ok, pa or "缺失", soft=not pa_ok)
    if not pa_ok:
        fix("sudo apt install -y libportaudio2")

    # 系统 python 版本（记下来，避免以后再被"3.9.2"误导）
    print("  ℹ  Python %s" % sys.version.split()[0])
    return all_ok


# ── L1 声卡 ──────────────────────────────────────────────────────────
def check_audio_hw():
    # ALSA 层：直接读 /proc，比调 arecord 更可靠
    cards_raw = ""
    try:
        with open("/proc/asound/cards", "r") as f:
            cards_raw = f.read().strip()
    except Exception as e:
        cards_raw = "读取失败：%s" % e

    alsa_ok = bool(cards_raw) and "no soundcards" not in cards_raw
    devs = E.list_input_devices()
    n_dev = len(devs)

    head("L1 · 音频硬件" + ("" if alsa_ok else "（预期未就绪）"))

    item("ALSA 声卡注册", alsa_ok,
         cards_raw.splitlines()[0].strip() if alsa_ok else "无（no soundcards）",
         soft=not alsa_ok)

    item("录音设备数量", n_dev > 0, "%d 个" % n_dev, soft=(n_dev == 0))
    for d in devs:
        # 只打印真实设备，ALSA 软件插件（ch 数异常）只提示不刷屏
        tag = "  ← 建议用这个" if d["index"] == E.guess_input_device() else ""
        print("       [%d] %s  %d Hz  %d ch%s"
              % (d["index"], d["name"][:44], d["samplerate"], d["channels"], tag))

    if alsa_ok and n_dev:
        # 有真设备了，顺便提醒增益这个必踩的坑
        print()
        print("  ⓘ 提醒：USB 免驱声卡的默认录音增益**经常是 0%%**，")
        print("     表现为'设备认得到但录下来全是静音'。先跑一次：")
        print("       bash setup_mic_gain.sh --install")
        print("     再用电平测试确认能采到声音：")
        print("       python3 mic_check.py --seconds 5")

    if not n_dev:
        print()
        print("  ⓘ 说明：这不是配置漏装，是**板子没有音频硬件**。")
        print("     · 内核未编译 CONFIG_SND_DESIGNWARE_I2S / SIMPLE_CARD / AUDIO_GRAPH_CARD")
        print("       → 板载 MIC、HDMI 音频都不可用")
        print("     · CONFIG_SND_USB_AUDIO=y 已内置 → 插 USB 声卡即用，**无需装驱动**")
        print("     · 声卡到货后回来重跑本脚本，L1 会自动通过")

    # 工具链是否齐（缺了插上声卡也没法排查）
    missing = [c for c in ("arecord", "aplay", "amixer")
               if not _which(c)]
    item("ALSA 工具链", not missing,
         "齐备" if not missing else "缺 %s" % ",".join(missing),
         soft=bool(missing))
    if missing:
        fix("sudo apt install -y alsa-utils")

    return True      # 声卡未就绪不计入失败


def _which(cmd):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        fp = os.path.join(p, cmd)
        if os.path.isfile(fp) and os.access(fp, os.X_OK):
            return fp
    return None


# ── L2 模型文件 ──────────────────────────────────────────────────────
def check_model_files(model_dir):
    head("L2 · 模型文件")
    md = model_dir or E.guess_model_dir()
    print("  模型目录：%s" % md)

    if not os.path.isdir(md):
        item("目录存在", False, "不存在")
        fix("bash download_asr_model.sh")
        return md, False

    os_ok = item("目录存在", True, md)

    # 模型文件名有两个候选（int8 优先），用引擎自己的查找函数，避免重复逻辑
    got_model, got_tokens = E.find_model_files(md)
    if got_model:
        size_mb = os.path.getsize(got_model) / 1048576.0
        name = os.path.basename(got_model)
        is_int8 = "int8" in name
        # int8 期望 200~260MB；fp32 是 894MB 左右，会拖慢加载
        good = (200 <= size_mb <= 260) if is_int8 else (850 <= size_mb <= 950)
        note = "%.1f MB%s" % (size_mb, "" if is_int8 else " ⚠ 非 int8，内存吃紧")
        item(name, good, note, soft=not good)
        if not is_int8:
            fix("换 int8 模型（228MB）——4GB 内存跑 fp32 很吃力")
    else:
        item("model.int8.onnx", False, "未找到")
        fix("bash download_asr_model.sh")

    # tokens / VAD：大小由 expect 表校验
    expect = {
        E.TOKENS_FILE: (0.2, 1.0, "MB"),
        E.SILERO_VAD_FILE: (0.3, 1.5, "MB"),
    }
    for fname, (lo, hi, unit) in expect.items():
        path = os.path.join(md, fname)
        if not os.path.isfile(path):
            soft = (fname == E.SILERO_VAD_FILE)
            item(fname, False, "未找到" + ("（将退回能量断句）" if soft else ""), soft=soft)
            if not soft:
                fix("bash download_asr_model.sh")
            continue
        size_mb = os.path.getsize(path) / 1048576.0
        good = lo <= size_mb <= hi
        item(fname, good, "%.1f %s" % (size_mb, unit), soft=not good)
        if not good:
            fix("文件大小异常，删掉重下")

    # test_wavs 是 L4/L5 的前提
    tw = os.path.join(md, "test_wavs")
    wavs = []
    if os.path.isdir(tw):
        wavs = sorted(f for f in os.listdir(tw)
                      if f.endswith(".wav") and not f.startswith("synthetic_"))
    item("test_wavs/", len(wavs) > 0,
         "、".join(wavs) if wavs else "缺失（L4/L5 会跳过）",
         soft=(len(wavs) == 0))
    return md, os_ok


# ── L3 引擎加载 ──────────────────────────────────────────────────────
def check_load(eng, md):
    head("L3 · 引擎加载")
    t0 = time.time()
    ok, msg = eng.load(md)
    dt = time.time() - t0

    item("加载模型", ok, "%.2f 秒" % dt if ok else msg)
    if ok:
        item("VAD 模式", True, eng.vad_mode)
        print("       describe: %s" % eng.describe())
        # 与文档 9 的实测值对照
        if dt > 15:
            print("       ⚠ 比实测基准（5.45s）慢很多，可能磁盘 IO 受限")
    return ok


# ── L4 识别官方音频 ──────────────────────────────────────────────────
def check_recognize(eng, md, lang="zh"):
    head("L4 · 识别官方测试音频")
    src = os.path.join(md, "test_wavs", "%s.wav" % lang)
    if not os.path.isfile(src):
        item("源音频 %s.wav" % lang, False, "不存在", soft=True)
        return True

    t0 = time.time()
    ok, res = eng.recognize_file(src)
    dt = time.time() - t0
    if not ok:
        item("识别", False, res)
        return False

    # 估算 RTF
    try:
        import wave
        with wave.open(src, "rb") as w:
            dur = w.getnframes() / float(w.getframerate())
        rtf = dt / dur if dur else 0
        item("识别", True, "RTF=%.3f（音频 %.1fs / 耗时 %.2fs）" % (rtf, dur, dt))
    except Exception:
        item("识别", True, "耗时 %.2fs" % dt)

    print("       结果：%s" % res)
    return True


# ── L5 合成音频闭环 ──────────────────────────────────────────────────
def check_synthetic(eng):
    """
    ⭐ 这是声卡到货前最有价值的一项：
    用模型自带音频裁一段，走「PCM 数据 → 识别」的完整路径，
    提前排掉采样率/位深/声道/重采样/静音裁剪等一整类问题。
    """
    head("L5 · 合成音频闭环（声卡替代验证）")
    ok, path = eng.synthesize_test_wav(seconds=2.5, src="zh")
    if not ok:
        item("生成合成音频", False, path, soft=True)
        return True

    info = eng.synthetic or {}
    item("生成合成音频", True, "%.2fs @ %d Hz" % (info.get("seconds", 0),
                                              info.get("rate", 0)))

    # 关键：用 recognize() 而不是 recognize_file()，
    # 这才是真实录音走的那条路径（float32 数组 + 显式采样率）
    import wave
    with wave.open(path, "rb") as w:
        frames = w.readframes(w.getnframes())
    import numpy as np
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    t0 = time.time()
    ok2, res = eng.recognize(16000, data)
    dt = time.time() - t0

    if ok2:
        item("录音路径识别", True, "耗时 %.2fs" % dt)
        print("       结果：%s" % res)
        print("       ✅ 「录音数据 → 识别」契约成立，剩的只有真实声卡")
    else:
        item("录音路径识别", False, res)
    return ok2


# ── L6 指令解析 ──────────────────────────────────────────────────────
def check_parse():
    """
    只测**纯函数** parse_text_command()，绝不构造 CarController ——
    因为 run_text_command() 会真的调 move() 下发串口，预检阶段不能碰硬件。
    """
    head("L6 · 识别文本 → 运动指令（纯解析，不碰串口/不碰控制器）")
    try:
        from car_gui_core import parse_text_command, DEFAULT_SPEED
    except Exception as e:
        item("导入 parse_text_command", False, str(e), soft=True)
        return True

    # 覆盖真实口语 + 典型 int8 识别误差（"开放"→"开饭"那类）
    # want=None 表示"应当拒绝"
    cases = [
        ("前进一米", "forward"),
        ("前进 2 秒", "forward"),
        ("往前走", "forward"),
        ("后退一点", "backward"),
        ("左转90度", "turn_left"),
        ("右转45度", "turn_right"),
        ("左移", "strafe_left"),
        ("右移", "strafe_right"),
        ("速度调到三百", "set_speed"),
        ("速度 200", "set_speed"),
        ("以两百的速度后退两秒", "backward"),
        ("快一点", "speed_up"),
        ("慢一点", "speed_down"),
        ("急停", "stop"),
        ("停", "stop"),
        ("刹车", "stop"),
        ("给我讲个笑话", None),
        ("今天天气怎么样", None),
    ]

    passed = 0
    for text, want in cases:
        try:
            result, msg = parse_text_command(text, DEFAULT_SPEED)
        except Exception as e:
            result, msg = None, "异常：%s" % e

        act = (result or {}).get("action")
        if want is None:
            hit = result is None
            shown = "%s（应拒绝）" % (msg or "")
        else:
            hit = act == want
            extra = ""
            if result:
                if result.get("speed"):
                    extra += " speed=%s" % result["speed"]
                if result.get("duration"):
                    extra += " dur=%s" % result["duration"]
            shown = "%s%s" % (act or "None", extra)

        passed += 1 if hit else 0
        print("       %s %-18s → %s" % ("✓" if hit else "✗", text, shown))

    item("指令解析", passed == len(cases), "%d/%d" % (passed, len(cases)))
    return passed == len(cases)


# ── 汇总 ─────────────────────────────────────────────────────────────
def summary():
    head("汇总")
    hard_fail = [r for r in _results if not r[1] and not r[3]]
    soft_fail = [r for r in _results if not r[1] and r[3]]
    ok_cnt = sum(1 for r in _results if r[1])

    print("  通过 %d · 硬失败 %d · 待硬件 %d"
          % (ok_cnt, len(hard_fail), len(soft_fail)))

    if hard_fail:
        print("\n  ❌ 需要立刻修：")
        for name, _, note, _ in hard_fail:
            print("     · %s %s" % (name, note))

    if soft_fail:
        print("\n  ⏳ 等硬件/可延后（不影响现在推进）：")
        for name, _, note, _ in soft_fail:
            print("     · %s %s" % (name, note))

    print()
    if not hard_fail:
        if not soft_fail:
            print("  ✅ 全部就绪（依赖 + 声卡 + 模型 + 识别 + 解析）。")
            print("     下一步做真机语音联调（不接小车，安全）：")
            print("       bash setup_mic_gain.sh --install     # 先修增益 0% 的坑")
            print("       python3 mic_check.py                 # 说话 → 识别 → 看解析")
            print("     识别准了再接小车：")
            print("       python3 asr_cli.py --no-exec         # 只识别，不动车")
            print("       python3 asr_cli.py --port /dev/ttyCAR")
        else:
            print("  ✅ 除声卡外，ASR 环境已全部就绪。")
            print("     插上 USB 声卡后重跑本脚本，L1 通过即可直接：")
            print("       python3 asr_cli.py --no-exec          # 只识别，不动车")
    else:
        print("  ⚠ 先按上面的「修复」逐条处理，再重跑本脚本。")

    return 0 if not hard_fail else 1


# ── main ─────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="ASR 环境预检")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--no-model", action="store_true",
                    help="跳过 L3/L4/L5（不加载模型，快）")
    args = ap.parse_args()

    print("=" * _W)
    print(" 语音识别（ASR）环境预检 · Atlas 200I DK A2")
    print("=" * _W)

    check_deps()
    check_audio_hw()
    md, _ = check_model_files(args.model_dir)

    if args.no_model:
        head("L3~L5 · 已跳过（--no-model）")
    elif not E._HAS_SHERPA:
        head("L3~L5 · 已跳过（sherpa-onnx 不可用）")
    elif not E.find_model_files(md)[0]:
        head("L3~L5 · 已跳过（模型文件缺失）")
    else:
        eng = E.AsrEngine()
        if check_load(eng, md):
            check_recognize(eng, md, args.lang)
            check_synthetic(eng)

    if not args.no_model:
        check_parse()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
