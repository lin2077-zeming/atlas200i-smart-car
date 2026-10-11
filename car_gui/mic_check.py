#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mic_check.py —— 麦克风 + ASR 真机联调（不用接小车）

在 SSH 里跑，交互式验证「说话 → 识别」的完整效果：

    python3 mic_check.py                 # 交互模式：按回车录音，说一句，再回车
    python3 mic_check.py --auto          # 自动聆听：说一句自动断句识别
    python3 mic_check.py --seconds 3     # 固定录 3 秒就停，不用按回车

设计要点：
  · **不碰串口**，只做「录音 → 识别 → 显示解析结果」，安全调试用。
  · 每次识别都打印：识别原文 + 解析出的动作 + 电平/耗时，一眼看出
    是「没录到声音」还是「识别错了」还是「解析错了」。
  · 跑之前会自动把 Mic 增益拉到 100%（PCM2902 默认是 0，会全静音）。

对应文档：《8.语音识别接入操作文档.md》
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asr_engine as E


C_OK = "\033[32m"
C_WARN = "\033[33m"
C_ERR = "\033[31m"
C_DIM = "\033[2m"
C_END = "\033[0m"


def fix_mic_gain(card=0):
    """把 Mic 采集增益拉到 100%（PCM2902 免驱声卡默认为 0）。"""
    import subprocess
    try:
        r = subprocess.run(["amixer", "-c", str(card), "sget", "Mic"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace")
        if "0%" in out and "[off]" not in out:
            subprocess.run(["amixer", "-c", str(card), "sset", "Mic", "100%"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("%s  ⓘ 已自动把 Mic 增益从 0%% 拉到 100%%%s" % (C_WARN, C_END))
    except Exception:
        pass


def level_bar(v, width=24):
    """把 0~1 的电平画成条。"""
    n = int(min(max(v, 0.0), 1.0) * width)
    return "▮" * n + "▯" * (width - n)


def show_result(eng, ok, res, dur, cost):
    """统一打印识别结果 + 解析结果。"""
    if not ok:
        print("  %s✗ 识别失败：%s%s" % (C_ERR, res, C_END))
        return
    print("  %s识别：%s%s" % (C_OK, res, C_END))
    print("  %s音频 %.2fs · 识别 %.2fs · 实时率 %.3f%s"
          % (C_DIM, dur, cost, (cost / dur if dur else 0), C_END))

    # 顺带显示解析结果，方便判断是识别问题还是解析问题
    try:
        from car_gui_core import parse_text_command, DEFAULT_SPEED
        r, msg = parse_text_command(res, DEFAULT_SPEED)
        if r is None:
            print("  %s解析：没听懂（%s）—— 换种说法再试%s"
                  % (C_WARN, msg, C_END))
        else:
            print("  %s解析：%s%s" % (C_OK, r, C_END))
    except Exception as e:
        print("  %s（解析不可用：%s）%s" % (C_DIM, e, C_END))


def main():
    ap = argparse.ArgumentParser(description="麦克风 + ASR 真机联调")
    ap.add_argument("--model-dir", default="/home/HwHiAiUser/models/sense-voice")
    ap.add_argument("--device", type=int, default=None,
                    help="录音设备号（默认自动选 USB）")
    ap.add_argument("--seconds", type=float, default=None,
                    help="固定录音秒数；不给则用「回车开始/回车结束」")
    ap.add_argument("--auto", action="store_true",
                    help="自动聆听模式：说一句停顿后自动断句识别")
    ap.add_argument("--threads", type=int, default=E.DEFAULT_THREADS)
    args = ap.parse_args()

    print("=" * 62)
    print(" 麦克风 + 语音识别 真机联调（不接小车）")
    print("=" * 62)

    # ── 设备检查 ────────────────────────────────────────────────
    devs = E.list_input_devices()
    if not devs:
        print("%s  ✗ 没有找到录音设备。%s" % (C_ERR, C_END))
        print("    请确认 USB 麦克风已插好，然后运行： arecord -l")
        return 1
    if args.device is None:
        args.device = E.guess_input_device()
    print("  录音设备：[%d] %s" % (args.device, _dev_name(devs, args.device)))

    fix_mic_gain()

    # ── 加载模型 ────────────────────────────────────────────────
    eng = E.AsrEngine()
    t0 = time.time()
    ok, msg = eng.load(args.model_dir, args.threads)
    if not ok:
        print("%s  ✗ 模型加载失败：%s%s" % (C_ERR, msg, C_END))
        return 1
    print("  模型就绪：%s（%.1fs）" % (eng.describe(), time.time() - t0))

    # ── 自动聆听模式 ────────────────────────────────────────────
    if args.auto:
        print()
        print("  ── 自动聆听模式 ──")
        print("  说一句话，停顿约 0.8 秒自动识别。Ctrl+C 退出。")
        print()
        try:
            while True:
                t0 = time.time()
                ok, res = eng.listen_auto(device=args.device)
                cost = time.time() - t0
                if ok:
                    show_result(eng, True, res, cost - 0.5, 0.5)
                elif "没听到" in res or "超时" in res:
                    print("  %s· 等待中…（没听到说话）%s" % (C_DIM, C_END))
                else:
                    print("  %s✗ %s%s" % (C_WARN, res, C_END))
                print()
        except KeyboardInterrupt:
            print("\n  已退出。")
            return 0

    # ── 按住说话 / 固定时长 ─────────────────────────────────────
    print()
    if args.seconds:
        print("  ── 固定录 %.1f 秒 ──" % args.seconds)
    else:
        print("  ── 按回车开始录音，说完再按回车 ──")
    print("  （输入 q 回车退出）")
    print()

    while True:
        try:
            cmd = input("  按回车开始 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if cmd in ("q", "quit", "exit"):
            break

        # 开始录音
        t0 = time.time()
        if args.seconds:
            rec = E.Recorder(device=args.device)
            ok, msg = rec.start()
            if not ok:
                print("  %s✗ 打开麦克风失败：%s%s" % (C_ERR, msg, C_END))
                print("    试试换设备号：python3 asr_engine.py --list-devices")
                continue
            # 边录边显示电平，让用户看得到麦克风在工作
            deadline = t0 + args.seconds
            while time.time() < deadline:
                time.sleep(0.1)
                sys.stdout.write("\r  %s  %5.2fs  %s"
                                 % (level_bar(rec.level),
                                    deadline - time.time(), " "))
                sys.stdout.flush()
            print()
            rate, samples = rec.stop()
            dur = len(samples) / float(rate or 16000)
        else:
            ok, msg = eng.ptt_start(device=args.device)
            if not ok:
                print("  %s✗ 开始录音失败：%s%s" % (C_ERR, msg, C_END))
                continue
            # 显示电平直到用户按回车
            import threading
            stop_flag = threading.Event()

            def meter():
                peak = 0.0
                while not stop_flag.is_set():
                    lv = eng.level
                    peak = max(peak, lv)
                    sys.stdout.write("\r  录音中 %s  峰值 %.3f   （说完按回车）"
                                     % (level_bar(lv), peak))
                    sys.stdout.flush()
                    time.sleep(0.08)

            th = threading.Thread(target=meter, daemon=True)
            th.start()
            input()
            stop_flag.set()
            time.sleep(0.15)
            print()

            t1 = time.time()
            ok, res = eng.ptt_stop()
            cost = time.time() - t1
            if not ok:
                print("  %s✗ %s%s" % (C_ERR, res, C_END))
                continue
            # ptt_stop 内部已算过识别耗时，这里直接用
            show_result(eng, True, res, cost, eng._last_cost)
            print()
            continue

        # 固定时长模式的识别
        t1 = time.time()
        ok, res = eng.recognize(rate, samples)
        cost = time.time() - t1
        show_result(eng, ok, res, dur, cost)
        print()

    print("  再见。")
    return 0


def _dev_name(devs, idx):
    for d in devs:
        if d["index"] == idx:
            return d["name"]
    return "?"


if __name__ == "__main__":
    sys.exit(main())
