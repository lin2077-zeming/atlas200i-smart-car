#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pty_sim_test.py —— 在开发板上用伪终端模拟 HK-QXMIC 麦克风的 1M 串口。

用途：麦克风还没接的时候，验证 qxmic_locator.py 的串口链路/解析/事件回调
在真板 Python 3.10 上能否跑通（OS 层是真正的 tty，不是打桩）。

用法（板端，工程根目录）：
    python3 car_gui/pty_sim_test.py
    python3 car_gui/pty_sim_test.py --offset 90 --rounds 20

原理：os.openpty() 得到主/从一对 pty；把从端路径（/dev/pts/N）交给
QxMicLocator，主端按厂商帧格式往里写字节（含噪声/分片），检查回调收到的
sector 序列是否与注入一致。
"""
import argparse
import os
import random
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qxmic_locator as Q


def frame(sector, noise=b""):
    return bytes([0x55, 0xAA, sector, 0xFF - sector]) + noise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--rounds", type=int, default=12, help="每个方位注入几帧")
    ap.add_argument("--sleep", type=float, default=0.05, help="帧间隔(秒)")
    args = ap.parse_args()

    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    print("pty 从端: %s" % path)

    got = []
    loc = Q.QxMicLocator(port=path, baud=1_000_000, offset=args.offset, timeout=0.5)
    loc.on_event(lambda ev: got.append(ev))
    loc.open()

    # 注入：12 个方位各 N 帧，穿插噪声和分片
    seq = list(range(12)) * args.rounds
    try:
        for i, sec in enumerate(seq):
            payload = b"\x00\x13\x37" if i % 5 == 0 else b""      # 偶尔带噪声
            chunk = frame(sec, payload)
            if i % 7 == 0 and len(chunk) > 2:                    # 偶尔分两片写
                cut = random.randint(1, len(chunk) - 1)
                os.write(master, chunk[:cut])
                time.sleep(0.005)
                os.write(master, chunk[cut:])
            else:
                os.write(master, chunk)
            time.sleep(args.sleep)
        time.sleep(0.3)
    finally:
        loc.close()
        os.close(master)

    want = [Q.sector_to_angle(s) for s in seq]
    ok = len(got) == len(seq)
    if ok:
        for ev, sec, angle in zip(got, seq, want):
            if ev["sector"] != sec:
                ok = False
                break
    print("注入 %d 帧，收到 %d 帧" % (len(seq), len(got)))
    print("前 6 帧: %s" % [(e["sector"], e["angle"]) for e in got[:6]])
    if got:
        print("方向示例: sector=%d angle=%d → %s"
              % (got[0]["sector"], got[0]["angle"], Q.angle_to_direction(got[0]["angle"])))
    print("结果: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())