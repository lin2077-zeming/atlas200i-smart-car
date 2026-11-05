#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析遥控器录制数据（配合 record_feedback.py 使用）。

为什么要它
----------
App 自己的标定可能整体错位（我们就被"速度单位"坑过 40 倍）。**遥控器是
唯一独立于我们代码的"人肉基准"**：让用户做「已知距离/角度」的动作，
再把它回传的 vx/vz 对时间积分，就能反查我们常量对不对。

用法
----
    # 板子上：先开录制（300~360 秒）
    python3 record_feedback.py --duration 360 --out /tmp/remote.csv
    # 让用户用遥控器做：前进1米×2 / 后退1米 / 前进0.5米 /
    #   左转90°×2 / 右转90°×2 / 左转180° / 右转180°（每步停 2 秒）
    # 然后分析（本地或板上都行）
    python3 analyze_remote.py /tmp/remote.csv

判读要点（都是踩过的坑）
------------------------
1. **直行**：`∫vx·dt` 应≈真实毫米数（若 1 单位=1 mm/s）。偏差 >10% 才值得动
   标定；用户目测的距离本来就有 ±5% 误差。
2. **转向**：同方向、同角度的多段取平均，再做左右比（≈1.0 即对称）。
3. ⚠️ **`vz` 不能跨控制路径比较**：遥控转 180° 时 vz≈1400~2200，
   而 App 发 Z=1600 时只有 790~836，但两者肉眼转速接近 —— 这条反馈是
   "轮速当量"，两条路径换算不同。所以转向只能**同路径**标定。
4. 积分阈值 `|vz|>TH` 会漏掉起步爬升段，**短行程积分偏低**，反推要用长行程。
"""
import csv
import sys

TH = 30.0        # 运动阈值（噪声基线 |vx|max≈13, |vz|max≈15）
GAP = 0.45       # 段间最小间隔
MIND = 0.25      # 最短段


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append((float(r["time"]), float(r["vx"] or 0),
                             float(r["vy"] or 0), float(r["vz"] or 0),
                             float(r["voltage"] or 0)))
            except (TypeError, ValueError):
                continue
    return rows


def segments(rows):
    out, cur = [], None
    for t, vx, vy, vz, _v in rows:
        moving = abs(vx) > TH or abs(vz) > TH or abs(vy) > TH
        if moving:
            if cur is None:
                cur = [t, t, [(t, vx, vy, vz)]]
            else:
                cur[1] = t
                cur[2].append((t, vx, vy, vz))
        elif cur is not None:
            out.append(cur); cur = None
    if cur:
        out.append(cur)
    res = []
    for s in out:
        if s[1] - s[0] < MIND:
            continue
        if res and s[0] - res[-1][1] < GAP:
            res[-1][1] = s[1]; res[-1][2].extend(s[2])
        else:
            res.append(s)
    return res


def integ(p, idx):
    acc = 0.0
    for a, b in zip(p, p[1:]):
        dt = b[0] - a[0]
        if 0 < dt < 0.5:
            acc += (a[idx] + b[idx]) / 2.0 * dt
    return acc


def main():
    rows = load(sys.argv[1] if len(sys.argv) > 1 else "remote4.csv")
    if not rows:
        print("无数据"); return 1
    dts = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]) if 0 < b[0] - a[0] < 0.5)
    med = dts[len(dts) // 2] if dts else 0
    print("样本 %d，时长 %.1f s，采样 %.0f Hz" % (len(rows), rows[-1][0] - rows[0][0], 1 / med if med else 0))
    idle = [r for r in rows if abs(r[1]) <= TH and abs(r[3]) <= TH]
    if idle:
        print("静止基线: |vx|max=%.0f |vz|max=%.0f（%d 个静止样本）"
              % (max(abs(r[1]) for r in idle), max(abs(r[3]) for r in idle), len(idle)))

    segs = segments(rows)
    print("\n共 %d 段运动：" % len(segs))
    print("  #  t(s)   时长  ∫vx·dt(mm)  ∫vz·dt   vx均  vx峰  vz峰   判定")
    result = []
    for i, s in enumerate(segs, 1):
        p = s[2]
        dur = s[1] - s[0]
        ax, az = integ(p, 1), integ(p, 3)
        mvx = [abs(x[1]) for x in p if abs(x[1]) > TH]
        mvz = [abs(x[3]) for x in p if abs(x[3]) > TH]
        mean_vx = sum(mvx) / len(mvx) if mvx else 0
        peak_vx = max(abs(x[1]) for x in p)
        peak_vz = max(abs(x[3]) for x in p)
        kind = "直行" if abs(ax) > abs(az) else "转向"
        result.append(dict(i=i, t0=s[0] - rows[0][0], dur=dur, ax=ax, az=az,
                           mean_vx=mean_vx, peak_vx=peak_vx, peak_vz=peak_vz,
                           kind=kind))
        print("  %-2d %6.1f %5.2f  %10.0f  %7.0f  %5.0f %5.0f %5.0f   %s"
              % (i, s[0] - rows[0][0], dur, ax, az, mean_vx, peak_vx, peak_vz, kind))

    # ── 直行：∫vx 应约等于真实毫米数（若 1 单位=1mm/s）──
    fwd = [r for r in result if r["kind"] == "直行" and r["ax"] > 200]
    bwd = [r for r in result if r["kind"] == "直行" and r["ax"] < -200]
    if fwd:
        print("\n【直行】正向量程 ∫vx:")
        for r in fwd:
            print("   段%-2d %.2fs  %6.0f mm（若标称1米→偏差 %+.0f%%）"
                  % (r["i"], r["dur"], r["ax"],
                     (r["ax"] - 1000) / 10.0 if r["ax"] > 700 else (r["ax"] - 500) / 5.0))
        print("   速度区间: 均值 %.0f~%.0f mm/s，峰值 %.0f mm/s"
              % (min(r["mean_vx"] for r in fwd), max(r["mean_vx"] for r in fwd),
                 max(r["peak_vx"] for r in fwd)))
    if bwd:
        for r in bwd:
            print("   反向 段%-2d %.2fs  %6.0f mm" % (r["i"], r["dur"], r["ax"]))

    # ── 转向：按方向汇总 ──
    left = [r for r in result if r["kind"] == "转向" and r["az"] > 300]
    right = [r for r in result if r["kind"] == "转向" and r["az"] < -300]
    print("\n【转向】左转 %d 段 / 右转 %d 段" % (len(left), len(right)))
    for tag, arr in (("左", left), ("右", right)):
        if not arr:
            continue
        vals = [abs(r["az"]) for r in arr]
        print("   %s转 ∫vz: %s" % (tag, " ".join("%.0f" % v for v in vals)))
        print("        均值 %.0f 单位，vz峰值 %.0f" % (sum(vals) / len(vals),
                                                   max(r["peak_vz"] for r in arr)))
    if left and right:
        ml = sum(abs(r["az"]) for r in left) / len(left)
        mr = sum(abs(r["az"]) for r in right) / len(right)
        print("   左右比 = %.3f（1.000 表示完全对称）" % (ml / mr))
        print("   ⇒ 若左 90°(×2)+180° 与右 90°(×2)+180° 分别对应 360°，"
              "则 单位/度 ≈ %.2f（左）/ %.2f（右）"
              % (ml / 360.0, mr / 360.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())