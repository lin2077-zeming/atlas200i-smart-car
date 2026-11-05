#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asr_cli.py —— 命令行语音控车（SSH 里就能跑，不需要图形界面）

为什么需要它
------------
GUI 必须有显示器（HDMI0 或 VNC），但**语音识别本身在 SSH 里完全能跑**
（音频走板子本地的 ALSA，和显示环境无关）。所以调试语音链路时用这个脚本
比开 GUI 快得多。

用法
----
    # 看环境和设备
    python3 asr_cli.py --status
    python3 asr_cli.py --list-devices

    # 只识别，不下发运动指令（先验证识别准不准，最安全）
    python3 asr_cli.py --no-exec

    # 识别 + 控车：按回车开始录音，再按回车结束并识别
    python3 asr_cli.py --port /dev/ttyCAR

    # 自动聆听模式：说一句自动断句、自动识别、自动执行，循环
    python3 asr_cli.py --port /dev/ttyCAR --auto

    # 离线验证：识别一个 wav 文件
    python3 asr_cli.py --file test_wavs/zh.wav

退出：Ctrl+C
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asr_engine import (  # noqa: E402
    ASR_SAMPLE_RATE, DEFAULT_LANGUAGE, DEFAULT_THREADS, SILERO_VAD_FILE,
    AsrEngine, dependency_status, find_model_files, guess_input_device,
    guess_model_dir, list_input_devices,
)
from car_gui_core import CarController, guess_default_port  # noqa: E402


HELP_TEXT = """
可用语音指令示例
  前进                  持续前进（用当前速度）
  前进一米              按距离走，自动换算时长
  后退 2 秒             走 2 秒自动停
  左转90度 / 右转45度   按角度转
  以两百的速度后退两秒   指定速度 + 动作 + 时长
  速度 200              调整速度（50~500）
  快一点 / 慢一点        每次 ±50
  停 / 停止 / 急停       立即停止

  输入 :q 回车 退出；直接回车 = 开始录音，再回车 = 结束并识别
"""


def print_status(model_dir):
    st = dependency_status()
    print("=" * 62)
    print(" 语音识别环境")
    print("=" * 62)
    for k, label in (("numpy", "numpy"), ("sounddevice", "sounddevice"),
                     ("sherpa_onnx", "sherpa-onnx")):
        print("  %-14s %s" % (label, "OK" if st[k] else "缺失"))
    for k, v in st["versions"].items():
        print("      %-13s %s" % (k, v))
    if st["portaudio"]:
        print("      %-13s %s" % ("portaudio", st["portaudio"]))
    if st["sherpa_error"]:
        print("      ! sherpa 导入错误：%s" % st["sherpa_error"])

    md = model_dir or guess_model_dir()
    model, tokens = find_model_files(md)
    print("\n  模型目录   : %s" % md)
    print("  模型文件   : %s" % (model or "未找到  ← 先运行 download_asr_model.sh"))
    print("  tokens.txt : %s" % (tokens or "未找到"))
    vad = os.path.join(md, SILERO_VAD_FILE)
    print("  silero_vad : %s" % (vad if os.path.isfile(vad) else "未找到（将用能量断句）"))
    print("\n  提示：模型建议放在 ~/models/sense-voice/")
    return 0


def print_devices():
    devs = list_input_devices()
    print("=" * 62)
    print(" 录音设备")
    print("=" * 62)
    if not devs:
        print("  （没有可用录音设备，检查 USB 声卡是否插好）")
    for d in devs:
        print("  [%d] %-42s %6d Hz  %dch" % (d["index"], d["name"][:42],
                                             d["samplerate"], d["channels"]))
    if devs:
        print("\n  自动选择：设备 [%s]" % guess_input_device())
    return 0


def main():
    ap = argparse.ArgumentParser(description="命令行语音控车", add_help=True)
    ap.add_argument("--status", action="store_true", help="打印环境与依赖状态")
    ap.add_argument("--list-devices", action="store_true", help="列出录音设备")
    ap.add_argument("--model-dir", default=None, help="模型目录")
    ap.add_argument("--device", default=None, help="录音设备号（默认自动选 USB）")
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    ap.add_argument("--lang", default=DEFAULT_LANGUAGE)
    ap.add_argument("--port", default=None, help="小车串口，默认自动探测")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-exec", action="store_true",
                    help="只做识别，不下发任何运动指令（调试识别率时用）")
    ap.add_argument("--auto", action="store_true",
                    help="自动聆听模式：自动断句、循环，说一句执行一句")
    ap.add_argument("--file", default=None, help="识别一个 16bit PCM wav 后退出")
    args = ap.parse_args()

    if args.status:
        return print_status(args.model_dir)
    if args.list_devices:
        return print_devices()

    # ── 加载模型 ───────────────────────────────────────────────────
    eng = AsrEngine()
    ok, msg = eng.load(args.model_dir, args.threads, args.lang)
    print("=" * 62)
    if not ok:
        print("模型加载失败：")
        print("  " + msg.replace("\n", "\n  "))
        print("\n请先运行： bash download_asr_model.sh")
        return 1
    print(" 语音识别就绪：%s" % eng.describe())
    print("=" * 62)

    # ── 识别文件后退出 ─────────────────────────────────────────────
    if args.file:
        ok, res = eng.recognize_file(args.file)
        print("识别结果：%s" % res)
        return 0 if ok else 1

    # ── 连接小车（失败也能只跑识别）───────────────────────────────
    car = None
    if args.no_exec:
        print("  [--no-exec] 只识别，不下发运动指令")
    else:
        car = CarController()
        port = args.port or guess_default_port()
        ok, msg = car.connect(port, args.baud)
        if ok:
            print("  串口已连接：%s" % port)
        else:
            print("  ! 串口连接失败：%s" % msg)
            print("  ! 将只做识别，不下发运动指令（按 Ctrl+C 退出后重试）")
            car = None

    # ── 录音设备 ───────────────────────────────────────────────────
    dev = args.device
    if dev is not None:
        try:
            dev = int(dev)
        except ValueError:
            pass
    else:
        dev = guess_input_device()
    print("  录音设备：%s" % dev)

    def handle(text):
        """识别结果 → 指令解析 → 下发。"""
        if not text:
            return
        if car is None:
            print("     （未连串口，仅显示识别结果）")
            return
        ok, tip = car.run_text_command(text)
        if not ok:
            print("     -> 未执行：%s" % tip)

    try:
        if args.auto:
            print("\n自动聆听模式。直接说话，检测到停顿会自动识别并执行。")
            print("Ctrl+C 退出。\n")
            while True:
                ok, res = eng.listen_auto(device=dev)
                if ok:
                    print("  识别：%s" % res)
                    handle(res)
                else:
                    # 超时/没听清不刷屏，只打一行
                    print("  (%s)" % res)
                sys.stdout.flush()

        else:
            print(HELP_TEXT)
            while True:
                try:
                    cmd = input("按回车开始录音（:q 退出）> ").strip()
                except EOFError:
                    break
                if cmd in (":q", ":quit", "退出"):
                    break
                if cmd.startswith(":"):
                    # 允许直接输入文字指令，方便对比
                    if car is not None:
                        car.run_text_command(cmd[1:])
                    continue

                ok, msg = eng.ptt_start(device=dev)
                if not ok:
                    print("  录音失败：%s" % msg)
                    continue
                print("  ● 录音中…（再按一次回车结束）")
                try:
                    input()
                except EOFError:
                    pass
                ok, res = eng.ptt_stop()
                if ok:
                    print("  识别：%s" % res)
                    handle(res)
                else:
                    print("  (%s)" % res)
                sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n中断")
    finally:
        try:
            eng.cancel_recording()
        except Exception:
            pass
        if car is not None:
            car.emergency_stop()
            car.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
