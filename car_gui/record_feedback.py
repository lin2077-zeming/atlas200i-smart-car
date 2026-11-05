#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遥控器动作录制 —— 用「已知动作」反推真实速度量纲。

为什么需要它
------------
小车回传帧里的 `vx/vy/vz` 是**真实速度**（厂商协议：int16 mm/s；Z 轴单位
取决于车型，未知）。而我们的 App 一直把「速度命令值」当自定义单位用，
导致「慢/中/快」实际只发 25~34 mm/s（3 厘米/秒，肉眼看像没动）。

**遥控器是唯一可信的"人手"基准**：你用它做「前进 1 米 / 左转 90° / 后退
0.5 米 / 右转 180°」，我把 `vx/vz` 对时间积分，就能反推：

    · 直行：Σ(vx·dt) ≈ 1000 mm   → 验证 vx 确实是 mm/s
    · 转向：Σ(vz·dt) ≈ K × 90°   → 求出 K（每单位对应多少度）
    · 顺带得到"丝滑手感"对应的真实速度（例如 0.6 m/s），作为 App 的
      慢/中/快 三档取值依据

用法（在板子上，另开一个 ssh 窗口）
----------------------------------
    python3 record_feedback.py --duration 180
    # 期间用遥控器做动作；到时间自动停。也可以 touch /tmp/car_rec_stop 提前停

输出
----
    /tmp/car_rec.csv     time,elapsed,vx,vy,vz,voltage,action,flag_stop
    /tmp/car_rec.log     实时进度（每 5 秒一行）
"""
from __future__ import print_function

import argparse
import csv
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("CAR_BASE", "http://127.0.0.1:8090")
STOP_FILE = "/tmp/car_rec_stop"


def _stop_requested(t0):
    """是否被要求提前停止。

    ⚠️ 只看"文件是否**在本次录制开始之后**被创建/修改"（mtime >= t0）。
    为什么不只看 exists()：这个文件可能是上一次录制（甚至 root 身份）留下的，
    而本进程（HwHiAiUser）**删不掉 root 的文件** → 旧实现会「一启动就退出」，
    表现为样本=0、用时=0.0s（2026-10-28 踩到）。
    """
    try:
        return os.path.getmtime(STOP_FILE) >= t0
    except OSError:
        return False


def get_status(timeout=1.0):
    try:
        with urllib.request.urlopen(BASE + "/api/status", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=180.0, help="录制秒数")
    ap.add_argument("--rate", type=float, default=20.0, help="采样频率 Hz")
    ap.add_argument("--out", default="/tmp/car_rec.csv")
    ap.add_argument("--log", default="/tmp/car_rec.log")
    a = ap.parse_args()

    try:
        os.unlink(STOP_FILE)
    except OSError:
        pass

    period = 1.0 / max(1.0, a.rate)
    t0 = time.time()
    n = 0
    max_vx = max_vz = 0
    moving_samples = 0
    last_report = t0
    f = open(a.out, "w", newline="")
    w = csv.writer(f)
    w.writerow(["time", "elapsed", "vx", "vy", "vz",
                "voltage", "action", "flag_stop"])
    log = open(a.log, "w", buffering=1)

    print("开始录制（%.0f Hz，最长 %.0f 秒）→ %s" % (a.rate, a.duration, a.out))
    print("现在用遥控器做：前进 1 米 → 左转 90° → 后退 0.5 米 → 右转 180°")
    log.write("START rate=%.0f duration=%.0f out=%s\n"
              % (a.rate, a.duration, a.out))

    try:
        while True:
            now = time.time()
            el = now - t0
            if el > a.duration or _stop_requested(t0):
                break
            s = get_status()
            if s is not None:
                vx = s.get("vx")
                vy = s.get("vy")
                vz = s.get("vz")
                w.writerow([round(now, 3), round(el, 3),
                            vx if vx is not None else "",
                            vy if vy is not None else "",
                            vz if vz is not None else "",
                            s.get("voltage"), s.get("action"), s.get("flag_stop")])
                n += 1
                if isinstance(vx, (int, float)):
                    max_vx = max(max_vx, abs(vx))
                if isinstance(vz, (int, float)):
                    max_vz = max(max_vz, abs(vz))
                if (isinstance(vx, (int, float)) and abs(vx) > 20) or \
                   (isinstance(vz, (int, float)) and abs(vz) > 20):
                    moving_samples += 1
                # ★ 每条都 flush：否则数据积在缓冲区里，
                #   一来看不到进度，二来进程被杀就全丢（实测踩过）。
                f.flush()
            if now - last_report >= 5.0:
                last_report = now
                line = ("  已录 %5.1fs  样本 %5d  运动中 %4d  |vx|max=%5.0f  |vz|max=%5.0f"
                        % (el, n, moving_samples, max_vx, max_vz))
                print(line)
                log.write(line + "\n")
            time.sleep(period)
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，停止录制")
    finally:
        f.close()
        summary = ("DONE 样本=%d 用时=%.1fs |vx|max=%.0f |vz|max=%.0f 运动中样本=%d"
                   % (n, time.time() - t0, max_vx, max_vz, moving_samples))
        print(summary)
        log.write(summary + "\n")
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())