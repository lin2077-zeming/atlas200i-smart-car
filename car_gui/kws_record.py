#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kws_record.py —— 录制 KWS 验证音频（不接小车，纯采集）

用途
----
KWS（关键词识别）对**发音清晰度**很敏感，而 espeak-ng 之类的合成音完全
不能用（实测："急停"合成为音频后，连 SenseVoice 都听成"人"）。所以自定义
关键词**必须用真人录音**来验证。

本脚本就是干这个：按提示逐条录，存成 16bit 单声道 wav，方便后续用
`kws_engine.py --file xxx.wav` 复测、也方便日后回归。

⚠️ 录音流程（2026-09-26 重做，请务必看一遍）
--------------------------------------------
**旧版为什么不能用**：旧版是"按回车 → 立刻开始定长 1.8 秒硬录"。人按下回车
之后才开口，中间有 0.3~1.4 秒的延迟，1.8 秒窗口常常只录到半个词甚至完全
没录到。实测 14 条样本里 5 条全静音、多条贴窗口末尾被截断，直接导致批量
测试召回率只有 25%，而且**降阈值完全无效**（因为漏报的样本里根本没声音）。

**新版怎么改的**：
  1. 打完名字 → **3-2-1 倒计时**，给你心理准备，不再"回车即录"；
  2. 录 **--window 秒（默认 3.0）** 的长窗口，给足延迟余量；
  3. 录完做**能量端点检测**，自动裁掉首尾静音，只留真正的语音段；
  4. 自动**质检**：没录到 / 语音太短 / 被截断 / 削顶 → 当场提示**重录这条**，
     不让你带着坏样本往下走。

用法
----
    # 录默认的一组（急停 / 紧急停止 / 停车 / 刹车 + 负样本）
    bash run_py.sh kws_record.py

    # 只录一条，指定文件名
    bash run_py.sh kws_record.py --name my_jiting --text "急停"

    # 指定设备与窗口
    bash run_py.sh kws_record.py --device 0 --window 3.5

    # 录完自动跑一遍质量体检
    bash run_py.sh kws_record.py --check

    # 跳过倒计时（熟练之后想快一点）
    bash run_py.sh kws_record.py --countdown 0

录好的文件默认落在 ./kws_samples/
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asr_engine as E                                    # noqa: E402

try:
    import numpy as np
except Exception:
    np = None

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kws_samples")

# 默认录制清单： (文件名, 要念的内容, 是否应为"应命中"的正样本)
DEFAULT_ITEMS = [
    ("pos_jiting",       "急停",     True),
    ("pos_jinji",        "紧急停止",  True),
    ("pos_che",          "停车",     True),
    ("pos_shache",       "刹车",     True),
    ("neg_qianjin",      "前进",     False),
    ("neg_zuozhuan",     "左转",     False),
    ("neg_liaotian",     "今天天气不错", False),
]

# ── 录音质检参数（与 kws_sample_check.py 保持一致，便于互相对照）─────
FIXED_WINDOW = 3.0        # 默认录制窗口（秒）。**不必再说多快**，反正会裁
PRE_ROLL_SEC = 0.15       # 裁切时向前多留一点，避免切掉字头辅音
POST_ROLL_SEC = 0.25      # 向后多留一点，避免切掉字尾
VOICE_RMS_RATIO = 4.0     # 帧 RMS 超底噪多少倍算"在说话"
GAP_MERGE_SEC = 0.30      # 语音段之间的静音短于此 → 合并（音节间的自然停顿）
VOICE_MIN_SEC = 0.25      # 有效语音短于此 → 太短，说不清一个词
QUIET_RMS = 0.006         # 整段 RMS 低于此值 → 全静音
CLIP_RATIO = 0.001        # |x|>0.99 占比超过此值 → 削顶


def voice_span(a, rate, frame_ms=20):
    """
    能量端点检测：返回语音区间 (起点样本下标, 终点样本下标, 底噪, 帧 RMS)。

    底噪取**全部帧 RMS 的 20% 分位数**。不能用"取开头几帧"——
    很多样本一开口就是词，用开头估底噪会把真正的语音算成底噪。

    ⚠️ 2026-09-26 踩坑：第一版只取"最长的一段连续超阈帧"，结果真人语音被判
    成 0.22 秒——因为说话时**音节之间必然掉到阈值以下**（"急停"里 j 和 t 之间
    有停顿），语音被切成一地碎片。正确做法是**合并间隔 < GAP_MERGE_SEC 的
    相邻段**，这才是标准的语音活动检测（VAD）行为。

    没找到语音时返回 (-1, -1, ...)。
    """
    n = max(1, int(rate * frame_ms / 1000.0))
    nfr = len(a) // n
    if nfr < 2:
        return -1, -1, 0.0, np.zeros(0, dtype=np.float32)
    frames = a[:nfr * n].reshape(nfr, n)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    noise = float(np.percentile(rms, 20))
    thr = max(noise * VOICE_RMS_RATIO, QUIET_RMS)
    voiced = rms > thr
    if not voiced.any():
        return -1, -1, noise, rms

    # ── 按"间隔 < GAP_MERGE_SEC 就合并"把超阈帧归并成语音段 ────────
    gap_frames = max(1, int(GAP_MERGE_SEC * 1000.0 / frame_ms))
    idx = np.where(voiced)[0]
    segs = []
    seg_s = seg_e = int(idx[0])
    for k in range(1, len(idx)):
        i = int(idx[k])
        if i - seg_e <= gap_frames:      # 停顿够短 → 同一句话，继续并
            seg_e = i
        else:                            # 停顿够长 → 断句
            segs.append((seg_s, seg_e))
            seg_s = seg_e = i
    segs.append((seg_s, seg_e))

    # 取**跨度最长**的一段（而不是"帧数最多"，避免长停顿刷长度）
    best_s, best_e = max(segs, key=lambda s: s[1] - s[0])
    start = max(0, int((best_s * n) - rate * PRE_ROLL_SEC))
    end = min(len(a), int((best_e + 1) * n + rate * POST_ROLL_SEC))
    return start, end, noise, rms


def write_wav(path, rate, data):
    """把 float32 [-1,1] 写成 16bit 单声道 wav。"""
    import wave
    clipped = np.clip(data, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate))
        f.writeframes(pcm.tobytes())


def fix_gain(card=0):
    """录音前兜底拉一下增益（PCM2902 默认 0% 的坑）。"""
    import subprocess
    for cmd in (["amixer", "-c", str(card), "sset", "Mic", "100%"],
                ["amixer", "-c", str(card), "sset", "Auto Gain Control", "on"]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=3)
        except Exception:
            pass


def level_bar(v, width=24):
    n = int(min(1.0, v * 3.0) * width)
    return "▮" * n + "▯" * (width - n)


def countdown(n, prompt="准备"):
    """
    3-2-1 倒计时。**这是旧版最大的缺陷的修复**。

    旧版按回车就立刻开录，人还在"按下回车"的惯性里没开口，前 0.3~1.4 秒
    全浪费掉了。加个倒计时，让人把注意力从键盘转到嘴巴上。
    """
    n = int(n)
    if n <= 0:
        print("   ▶ 开始说话（倒计时已关）")
        return
    for i in range(n, 0, -1):
        sys.stdout.write("\r   ▶ 请准备… %d" % i)
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\r   ▶ 现在说！      \n")
    sys.stdout.flush()


def record_one(device, window, do_countdown=True):
    """
    录一条**长窗口**音频 → 自动裁掉首尾静音 → 质检。

    与旧版的根本区别：
      - 旧：定长 1.8s 硬录，人在窗口外的部分直接丢失，且无质检。
      - 新：录 window 秒（默认 3.0），用能量端点检测定位语音，自动裁切，
            录完当场判决是否合格。

    返回 (rate, data, ok, problems)。
      - 失败时 data 为 None。
      - ok=False 表示"录到了但质量不合格"，调用方应提示重录。
    """
    if device is None:
        device = E.guess_input_device()
    if device is None:
        print("   ❌ 没有可用的录音设备（先跑 bash run_py.sh kws_record.py --diag）")
        return 0, None, False, ["没有可用录音设备"]

    rec = E.Recorder(device=device)
    ok, msg = rec.start()
    if not ok:
        print("   ❌ %s" % msg)
        return 0, None, False, [msg]

    if do_countdown:
        countdown(3)

    print("   🎙  录音中 @ %d Hz，窗口 %.1f 秒（说完即可，多余静音会被裁掉）"
          % (rec.samplerate, window))
    t0 = time.time()
    try:
        while time.time() - t0 < window:
            left = window - (time.time() - t0)
            sys.stdout.write("\r   [%s] %4.1fs" % (level_bar(rec.level), max(0.0, left)))
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n   已中断")
    sys.stdout.write("\n")

    rate, data = rec.stop()
    if data is None or len(data) == 0:
        print("   ❌ 没录到数据")
        return 0, None, False, ["没录到数据"]

    raw_dur = len(data) / float(rate)
    raw_peak = float(np.max(np.abs(data))) if len(data) else 0.0
    raw_rms = float(np.sqrt(np.mean(data * data) + 1e-12)) if len(data) else 0.0

    # ── 全静音：不用往下分析了，直接重录 ────────────────────────────
    if raw_peak < 0.02 or raw_rms < QUIET_RMS:
        print("   ❌ 没录到声音（RMS %.4f / 峰值 %.4f）" % (raw_rms, raw_peak))
        print("      → 这条不合格，请重录。若连续多条如此，跑 setup_mic_gain.sh")
        return rate, data, False, ["全静音：没录到声音（RMS %.4f 峰值 %.4f）"
                                   % (raw_rms, raw_peak)]

    # ── 有声音：定位语音段并裁切 ───────────────────────────────────
    start, end, noise, frms = voice_span(data, rate)
    if start < 0:
        print("   ⚠️  检测到电平但没找到成段的语音，保留原样")
        trimmed = data
        onset = offset = 0.0
    else:
        trimmed = data[start:end]
        onset = start / float(rate)
        offset = end / float(rate)
    vdur = len(trimmed) / float(rate)
    peak = float(np.max(np.abs(trimmed))) if len(trimmed) else 0.0
    clip = float(np.mean(np.abs(trimmed) > 0.99)) if len(trimmed) else 0.0

    print("   原始 %.2fs → 裁切后 %.2fs（语音段 %.2f~%.2fs，底噪 %.4f）"
          % (raw_dur, vdur, onset, offset, noise))
    print("   峰值 = %.4f   削顶 = %.2f%%" % (peak, clip * 100))

    # ── 质检：不合格就当场说要重录 ────────────────────────────────
    problems = []
    if vdur < VOICE_MIN_SEC:
        problems.append("有效语音只有 %.2fs（<%.2fs），太短" % (vdur, VOICE_MIN_SEC))
    if start > 0 and onset > 0.80:
        problems.append("第 %.2fs 才开口（>0.80s），前半段都浪费了" % onset)
    if offset > 0 and (raw_dur - offset) < 0.02:
        problems.append("语音贴到窗口末尾，疑似被截断 —— 加大 --window 重录")
    if clip > CLIP_RATIO:
        problems.append("削顶 %.2f%%（离麦太近或增益过大）" % (clip * 100))

    if problems:
        print("   ❌ 这条不合格：")
        for p in problems:
            print("        - %s" % p)
        return rate, trimmed, False, problems

    print("   ✅ 合格")
    return rate, trimmed, True, []


def print_device_diag():
    """
    打印音频环境诊断。设备找不到时这是第一手排查工具。

    ⚠️ 关键：本函数必须能在**缺少 sounddevice** 时照常工作——
    因为「缺依赖」正是它要诊断的情况之一。所以这里不预先拦截，
    交给 asr_engine 的依赖状态去报。
    """
    print("=" * 66)
    print("  KWS 录音环境诊断")
    print("=" * 66)
    import os as _os
    for k in ("XDG_RUNTIME_DIR", "PULSE_SERVER", "USER", "CONDA_PREFIX"):
        print("  %-18s = %s" % (k, _os.environ.get(k)))
    print("  %-18s = %s" % ("解释器", sys.executable))
    print("  %-18s = %d.%d.%d" % ("Python 版本", *sys.version_info[:3]))

    dep = E.dependency_status()
    print()
    for m, label in (("numpy", "numpy"), ("sounddevice", "sounddevice"),
                     ("sherpa_onnx", "sherpa-onnx")):
        ok = dep.get(m)
        print("  %-18s = %s%s" % (
            label, "✅" if ok else "❌ 缺失",
            ("  v%s" % dep["versions"].get(m, "?")) if ok else ""))
    if dep["sounddevice"]:
        print("  %-18s = %s" % ("PortAudio", dep.get("portaudio", "?")))

    # ── 依赖不齐：先解决这个，声卡信息无意义 ────────────────────────
    need = [m for m in ("numpy", "sounddevice") if not dep.get(m)]
    if need:
        ok, hint = E.dependency_hint(need)
        print()
        print(hint)
        print()
        print("  看完上面的【第 1 步】，也可以直接跑：")
        print("     python3 env_doctor.py --write     # 自动挑解释器并固定")
        print("=" * 66)
        return

    # ── 依赖齐了，再看声卡 ─────────────────────────────────────────
    devs = E.list_input_devices()
    reals = E.real_input_devices()
    print()
    print("  录音设备总数        = %d（其中真硬件 %d）" % (len(devs), len(reals)))
    for d in devs:
        tag = "真硬件" if E.is_real_device(d["name"]) else "软件插件"
        print("     [%d] %-40s %dHz  %s" % (
            d["index"], d["name"], d["samplerate"], tag))

    sel = E.guess_input_device()
    print("  自动选中            = %s" % sel)

    if not devs:
        ok, hint = E.audio_environment_hint()
        print()
        print(hint)
        print()
        print("  快速自检命令：")
        print("     lsusb | grep 08bb                     # 声卡在不在")
        print("     cat /proc/asound/cards                # ALSA 认没认到")
        print("     fuser -v /dev/snd/*                   # 谁占着声卡")
        print("     pactl list short sources              # PA 录音源")
        print("     env -u XDG_RUNTIME_DIR bash run_py.sh kws_record.py   # 绕开 PA 直连 ALSA")
        print()
        print("  还有一种常见情况：**USB 声卡根本没插**。")
        print("     确认板子上 2 个 USB-A 口之间有插着那个 USB 声卡，")
        print("     然后 cat /proc/asound/cards 应能看到 card 0。")
    else:
        print()
        if sel is None:
            print("  ⚠️  设备在，但没挑出「真硬件」。试试显式指定：")
            for d in reals:
                print("     bash run_py.sh kws_record.py --device %d" % d["index"])
        else:
            print("  设备正常。直接开录：")
            print("     bash run_py.sh kws_record.py")
            print("  若录音仍失败，试：")
            print("     bash run_py.sh kws_record.py --device %d   # 显式指定真硬件" % sel)
    print("=" * 66)


def bad_files(out_dir):
    """
    列出 out_dir 里**质检不合格**的 wav 文件名（用于 --redo 只重录这些）。

    直接复用 kws_sample_check 的判决，保持两处判定标准一致。
    """
    import importlib
    try:
        chk = importlib.import_module("kws_sample_check")
    except Exception:
        return [], []
    if not os.path.isdir(out_dir):
        return [], []
    bad, good = [], []
    for f in sorted(x for x in os.listdir(out_dir) if x.lower().endswith(".wav")):
        ok, probs, _ = chk.check_one(os.path.join(out_dir, f))
        (good if ok else bad).append(f)
    return bad, good


def main():
    ap = argparse.ArgumentParser(description="录制 KWS 验证音频")
    ap.add_argument("--device", type=int, default=None, help="录音设备号（默认自动）")
    ap.add_argument("--window", type=float, default=FIXED_WINDOW,
                    help="单次录制窗口秒数（默认 %.1f）。说慢了就加大" % FIXED_WINDOW)
    ap.add_argument("--seconds", type=float, default=None,
                    help=argparse.SUPPRESS)   # 旧参数，保留兼容
    ap.add_argument("--countdown", type=int, default=3,
                    help="录音前倒计时秒数（默认 3，设 0 关闭）")
    ap.add_argument("--out", default=OUT_DIR, help="输出目录")
    ap.add_argument("--name", default=None, help="只录一条，指定文件名")
    ap.add_argument("--text", default=None, help="只录一条，指定要念的内容")
    ap.add_argument("--no-fix-gain", action="store_true", help="不要自动拉增益")
    ap.add_argument("--redo", action="store_true",
                    help="只重录质检不合格的样本")
    ap.add_argument("--check", action="store_true",
                    help="录完后自动跑一遍质量体检")
    ap.add_argument("--diag", action="store_true", help="只打印音频环境诊断，不录音")
    args = ap.parse_args()

    # ⚠️ --diag 必须放在依赖检查之前。「缺依赖」正是它要诊断的情况之一，
    #    先被拦住就等于把唯一的诊断工具也锁死了。
    if args.diag:
        print_device_diag()
        return 0

    if np is None:
        ok, hint = E.dependency_hint(["numpy"])
        print("缺少 numpy。\n")
        print(hint)
        return 1
    if not E.HAS_SOUNDDEVICE:
        ok, hint = E.dependency_hint(["sounddevice"])
        print("缺少 sounddevice。\n")
        print(hint)
        return 1

    os.makedirs(args.out, exist_ok=True)

    if not args.no_fix_gain:
        fix_gain()

    device = args.device
    if device is None:
        device = E.guess_input_device()
        if device is None:
            print("❌ 检测不到录音设备！\n")
            print_device_diag()
            return 1
        print("自动选中录音设备：%s" % device)
        for d in E.list_input_devices():
            mark = " ←" if d["index"] == device else ""
            kind = "真硬件" if E.is_real_device(d["name"]) else "软件插件"
            print("   [%d] %-36s %dHz  %s%s" % (d["index"], d["name"],
                                                d["samplerate"], kind, mark))
    else:
        # 用户显式指定，先验证这个号是不是存在
        idxs = {d["index"] for d in E.list_input_devices()}
        if device not in idxs:
            print("❌ 设备 %s 不存在。当前可用：" % device)
            for d in E.list_input_devices():
                print("   [%d] %s" % (d["index"], d["name"]))
            return 1

    if args.seconds is not None:          # 旧参数兼容
        args.window = args.seconds

    items = [(args.name or "sample", args.text or "", True)] if args.text else DEFAULT_ITEMS

    # ── --redo：把清单换成"只重录不合格的那几条" ─────────────────────
    if args.redo:
        bad, good = bad_files(args.out)
        if not bad:
            print("体检结果：没有不合格样本，无需重录 ✅")
            print("直接跑：bash run_py.sh kws_batch_test.py --sweep")
            return 0
        want = {}
        for f in bad:
            base = os.path.splitext(f)[0]
            # pos_jiting_1.wav -> ("pos_jiting", 1)
            if "_" in base:
                stem, _, tail = base.rpartition("_")
                if tail.isdigit():
                    want[(stem, int(tail))] = True
                    continue
            want[(base, 1)] = True
        text_of = {n: t for n, t, _ in DEFAULT_ITEMS}
        items = []
        for (stem, i) in sorted(want):
            items.append((stem, text_of.get(stem, ""), stem.startswith("pos_")))
        print("\n体检发现 %d 个不合格文件（%d 个合格），只重录这些：\n"
              % (len(bad), len(good)))
        for f in bad:
            print("   - %s" % f)
        print()

    print("\n共 %d 条。每条录到合格为止（最多 3 次）。"
          % len(items))
    print("流程：按回车 → 3 秒倒计时 → 听到「现在说！」后念关键词。\n")
    results = []
    for name, text, is_pos in items:
        print("── %s ──" % name)
        print("   请清晰地说：%s" % text)
        try:
            input("   准备好后按回车开始倒计时…")
        except EOFError:
            break
        # 一条要录两遍（第 1 遍 + 第 2 遍），每遍最多重试到合格
        for i in (1, 2):
            path = None
            for attempt in (1, 2, 3):
                tag = "第 %d 遍" % i
                if attempt > 1:
                    tag += "（第 %d 次尝试）" % attempt
                print("   %s：" % tag)
                rate, data, ok, probs = record_one(
                    device, args.window, do_countdown=(args.countdown > 0))
                if data is None:
                    print("      → 重试")
                    continue
                if ok:
                    path = os.path.join(args.out, "%s_%d.wav" % (name, i))
                    write_wav(path, rate, data)
                    print("   ✅ 已保存 %s" % path)
                    break
                print("      → 这条没录好，再来一次")
            if path is None:
                print("   ⚠️  第 %d 遍 3 次都没录合格，先跳过（可用 --redo 补录）" % i)
            else:
                results.append((name, text, is_pos, path))

    print("\n===== 录制完成 =====")
    for name, text, is_pos, path in results:
        print("  %-14s %-8s %s" % (name, text, os.path.basename(path)))

    print("\n下一步：")
    print("  bash run_py.sh kws_sample_check.py            # 先体检（不加载模型，很快）")
    print("  bash run_py.sh kws_batch_test.py --sweep      # 体检通过再标定阈值")
    if args.check:
        print("\n（--check 已开启，立即体检）\n")
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.call([sys.executable,
                         os.path.join(here, "kws_sample_check.py"),
                         "--dir", args.out])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
