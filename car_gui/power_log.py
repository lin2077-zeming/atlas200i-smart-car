#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
power_log.py —— 板子功耗/温度实时记录（选移动电源用）

## 能测到什么、测不到什么（**先说清楚，别拿去算电池容量时被误导**）

| 项 | 能否拿到 | 来源 |
|---|---|---|
| **NPU 芯片实时功耗** | ✅ | `npu-smi info -t power -i 0`（实测空闲 7.3~8.5 W） |
| NPU 芯片电压 | ✅ | `npu-smi info -t volt -i 0`（0.80 V，是**核压**不是输入电压） |
| NPU 温度 / 额定功耗 | ✅ | `-t temp` / `-t common`（额定 8.4 W） |
| CPU/整板/12V 输入功耗 | ❌ | **本板没有外露的功耗计** |
| 电池电压/电流 | ❌ | `/sys/class/power_supply/` 是空的 |

**为什么测不到整板**：查过了 —— 没有 `hwmon` 电流传感器（`sensors` 只有
CPU 温度和 WiFi 温度）、没有 `power_supply` 接口、设备树里没有 `ina*`/`power`/`current` 节点。
I2C 上那个 `sensor_i2c`（4 个 0x36）是华为专用芯片，**驱动不外露**。

→ **要选移动电源，必须用硬件功率计**（见文末建议）；本脚本负责把
**软件能拿到的部分**连续记录下来，用于观察负载与功耗的关系。

## 用法

    # 每 2 秒采一次，Ctrl+C 停止，结束时汇总
    python3 power_log.py

    # 采 300 次、间隔 1 秒、写 CSV
    python3 power_log.py -n 300 -i 1 -o power.csv

    # 一边跑负载一边采（另开一个终端）
    python3 npu_tts_engine.py --bench
"""
import argparse
import os
import re
import subprocess
import sys
import time

FIELDS = ("time", "npu_power_w", "npu_temp_c", "npu_volt_v",
          "load1", "mem_used_mb", "cpu_busy_pct")


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _num(text, pat, cast=float):
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except Exception:
        return None


def sample_npu():
    """返回 (功耗W, 温度C, 电压V)。"""
    p = _num(_run(["npu-smi", "info", "-t", "power", "-i", "0"]),
             r"Power\(W\)\s*:\s*([0-9.]+)")
    t = _num(_run(["npu-smi", "info", "-t", "temp", "-i", "0"]),
             r"Temperature \(C\)\s*:\s*([0-9]+)")
    v = _num(_run(["npu-smi", "info", "-t", "volt", "-i", "0"]),
             r"Voltage\(V\)\s*:\s*([0-9.]+)")
    return p, t, v


def sample_sys():
    """返回 (load1, 已用内存MB, CPU忙% 或 None)。"""
    try:
        load1 = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        load1 = None
    mem = None
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) / 1024.0
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) / 1024.0
        mem = total - avail
    except Exception:
        pass
    # CPU 忙：读两次 /proc/stat 求差
    def snap():
        f = open("/proc/stat").readline().split()[1:]
        v = [int(x) for x in f]
        return sum(v), v[3]                     # total, idle
    try:
        t1, i1 = snap()
        time.sleep(0.25)
        t2, i2 = snap()
        dt, di = t2 - t1, i2 - i1
        busy = 100.0 * (dt - di) / dt if dt > 0 else None
    except Exception:
        busy = None
    return load1, mem, busy


def main():
    ap = argparse.ArgumentParser(description="板子功耗/温度记录（NPU 部分）")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="采样间隔秒")
    ap.add_argument("-n", "--count", type=int, default=0,
                    help="采样次数，0=无限（Ctrl+C 停止）")
    ap.add_argument("-o", "--out", default=None, help="写 CSV 到该文件")
    ap.add_argument("-q", "--quiet", action="store_true", help="不打印每行")
    a = ap.parse_args()

    print("=" * 74)
    print(" 板子功耗记录（★ 只有 NPU 芯片那一路，整板需硬件功率计）")
    print(" 额定 NPU 功耗 8.4 W；`npu-smi info` 的 Power 就是下面这个值")
    print("=" * 74)
    p, t, v = sample_npu()
    if p is None:
        print("❌ 读不到 NPU 功耗（npu-smi 不可用？）")
        return 1
    print("首个读数: NPU %.1f W / %.0f°C / %.2f V" % (p, t or 0, v or 0))
    print()

    rows = []
    if a.out:
        f = open(a.out, "w", encoding="utf-8")
        f.write(",".join(FIELDS) + "\n")
    else:
        f = None

    hdr = "%-10s %8s %7s %7s %7s %9s %8s" % (
        "时间", "NPU(W)", "温度", "核压", "load1", "内存(MB)", "CPU%")
    if not a.quiet:
        print(hdr)
        print("-" * len(hdr))

    i = 0
    try:
        while a.count == 0 or i < a.count:
            i += 1
            ts = time.strftime("%H:%M:%S")
            p, t, v = sample_npu()
            load1, mem, busy = sample_sys()
            rec = (ts, p, t, v, load1, mem, busy)
            rows.append(rec)
            if f:
                f.write(",".join("" if x is None else str(x) for x in rec) + "\n")
                f.flush()
            if not a.quiet:
                print("%-10s %8s %7s %7s %7s %9s %8s" % (
                    ts,
                    "%.1f" % p if p is not None else "-",
                    "%.0f" % t if t is not None else "-",
                    "%.2f" % v if v is not None else "-",
                    "%.2f" % load1 if load1 is not None else "-",
                    "%.0f" % mem if mem is not None else "-",
                    "%.0f" % busy if busy is not None else "-"))
            if a.count == 0 or i < a.count:
                time.sleep(max(0.0, a.interval - 0.25))
    except KeyboardInterrupt:
        print("\n（Ctrl+C 停止）")
    finally:
        if f:
            f.close()

    ps = [r[1] for r in rows if r[1] is not None]
    ts_ = [r[2] for r in rows if r[2] is not None]
    if ps:
        print()
        print("=" * 74)
        print(" 汇总（%d 个采样点）" % len(ps))
        print("=" * 74)
        print("  NPU 功耗: 最小 %.1f / 平均 %.1f / 最大 %.1f W"
              % (min(ps), sum(ps) / len(ps), max(ps)))
        if ts_:
            print("  NPU 温度: 最小 %.0f / 平均 %.0f / 最大 %.0f °C"
                  % (min(ts_), sum(ts_) / len(ts_), max(ts_)))
        print()
        print("  ⚠️ 这**只是 NPU 芯片**的功耗。")
        print("     整板（CPU+内存+WiFi+USB 外设）远高于此，且本板软件测不到 ——")
        print("     选移动电源请用硬件 USB 功率计实测 12V 输入。")
        print("     项目供电预算（10.采购材料清单.md §2.1）：")
        print("       Atlas 200I DK A2: 12V ≥3A（≥36W），典型 24W，启动峰值更高")
    if a.out:
        print("\n  CSV 已写入: %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
