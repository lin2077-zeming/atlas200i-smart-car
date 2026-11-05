#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预设任务离线自检（不接串口、不发运动，纯算术）。

为什么需要它
------------
网页「常用任务」里的三条预设是**硬编码的中文指令**，它们能不能真的
走出「1 米 / 一圈 / 演示路线」，取决于底层的物理标定：

    V  = cmd_to_mmps(cmd)     # 直行
    ω  = turn_degps(cmd)      # 转向
    duration_for_angle() 有 **hi=8.0 s 上限**！

所以「左转三百六十度」**并不会真的转一圈**：
    8.0 s × 41.55 °/s = 332°（差 28°，肉眼看得出一圈没闭上）
本脚本就是用来把这类"看起来对、其实不对"的预设提前抓出来的。

用法
----
    bash run_py.sh verify_presets.py        # 板上
    python car_gui/verify_presets.py        # 电脑（只要不接串口也行）

退出码：0 = 全部符合预期；1 = 有偏差超限。
"""
from __future__ import print_function

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import car_gui_core as C


# ══ 网页里的三条预设（`data-t` 的值必须与这里一致，脚本会自动核对）══════
#   name / 指令文本 / 期望效果
PRESETS = [
    ("前进 1 米", "前进一米",
     {"kind": "dist", "mm": 1000.0, "tol": 60.0}),
    ("原地转一圈", "左转三百六十度",
     {"kind": "deg", "deg": 360.0, "tol": 25.0}),
    ("演示路线", "前进一米，然后左转九十度，再前进一米",
     {"kind": "route", "mm": 1000.0, "deg": 90.0, "tolmm": 60.0, "toldeg": 10.0}),
]

#: 期望值与真机实测值的对照（明天装车后填这里，再跑就自动比对）
#:   格式：{预设名: {"mm": 实测毫米, "deg": 实测角度}}
MEASURED = {}


def _mm(action, speed, duration):
    """按标定把 (速度命令, 时长) 换算成**估计划过的毫米数**。

    ⚠️ 必须用 core 的斜坡反算：`duration_for_distance()` 返回的是
       `dist/v + SPEED_RAMP_SEC/2`（起步 3.1 秒是加速的，平均速度偏低），
       所以距离 = v × (t − 3.1/2)。直接用 `v × t` 会把 1 米算成 2.8 米。
    """
    if action not in ("forward", "backward"):
        return 0.0
    v = C.cmd_to_mmps(speed)
    t = max(0.0, float(duration or 0.0) - C.SPEED_RAMP_SEC / 2.0)
    return v * t


def _deg(action, speed, duration):
    """按标定把 (转向命令, 时长) 换算成**预计转过的度数**。"""
    if action not in ("turn_left", "turn_right"):
        return 0.0
    w = C.turn_degps(C.TURN_CMD if speed is None else speed)
    return w * max(0.0, float(duration or 0.0))


def _steps(cmd):
    """解析一条指令 → [(action, speed, duration, label)]，纯函数不执行。"""
    plan = C.parse_plan(cmd)
    if plan:
        return [(s.get("action"), s.get("speed"), s.get("duration"),
                 s.get("label") or s.get("desc") or "") for s in plan], True
    res, msg = C.parse_text_command(cmd)
    if not res:
        return [], False
    return [(res.get("action"), res.get("speed"), res.get("duration"), msg)], False


def _html_commands():
    """从 web_ui.html 抽出所有 `data-t="..."`（网页上真正会发的指令）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "web_ui.html")
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        html = f.read()
    return re.findall(r'data-t="([^"]+)"', html)


def main():
    print("=" * 66)
    print("常用任务预设 · 离线自检（只解析不执行）")
    print("=" * 66)
    print("标定：V = %.1f×(cmd-%.1f) mm/s · ω = %.3f×(cmd-%.1f) °/s · TURN_CMD=%d"
          % (C.SPEED_MMPS_PER_UNIT, C.SPEED_DEADBAND, C.TURN_DEGPS_PER_UNIT,
             C.TURN_DEADBAND, C.TURN_CMD))

    # ── 0. 网页与脚本的指令是否一致（防止改了 HTML 忘了改脚本）──────
    html_cmds = _html_commands()
    bad = 0
    if html_cmds is None:
        print("\n[0] 找不到 web_ui.html，跳过一致性核对")
    else:
        want = [c for (_, c, _) in PRESETS]
        missing = [c for c in want if c not in html_cmds]
        print("\n[0] 网页 data-t 一致性")
        if missing:
            bad += len(missing)
            for c in missing:
                print("  [FAIL] 网页里找不到这条预设：%s" % c)
        else:
            print("  [PASS] 三条预设的指令与网页一致")
        extra = [c for c in html_cmds if c not in want]
        if extra:
            print("  [INFO] 网页另有未纳入自检的预设：%s" % "、".join(extra))

    # ── 1. 逐条解析 + 估算 ────────────────────────────────────────
    for idx, (name, cmd, exp) in enumerate(PRESETS, 1):
        print("\n[%d] %s" % (idx, name))
        print("    指令：%s" % cmd)
        steps, is_plan = _steps(cmd)
        if not steps:
            print("  [FAIL] 解析失败（这条指令在网页上点了不会有反应）")
            bad += 1
            continue
        print("    形态：%s（%d 步）" % ("多步计划" if is_plan else "单步动作",
                                       len(steps)))
        tot_mm = tot_deg = tot_t = 0.0
        for i, (act, spd, dur, lab) in enumerate(steps, 1):
            # 模型距离 × 实测效率（后退/计划步会偏短，见 core 的 GAIN 常量）
            mm = _mm(act, spd, dur) * C.distance_efficiency(act, is_plan)
            dg = _deg(act, spd, dur)
            tot_mm += mm
            tot_deg += dg
            tot_t += float(dur or 0.0)
            print("      %d) %-11s 命令=%-4s 时长=%5.2fs  ≈%7.1f mm /%6.1f °  %s"
                  % (i, act, spd, float(dur or 0.0), mm, dg, lab))
        if is_plan:
            # 步与步之间有 settle 等待
            tot_t += C.PLAN_STEP_SETTLE_SEC * max(0, len(steps) - 1)
        print("    合计：%.2f s · ≈%.0f mm · ≈%.0f °" % (tot_t, tot_mm, tot_deg))

        # ── 2. 与期望比对 ────────────────────────────────────────
        k = exp["kind"]
        if k == "dist":
            ok = abs(tot_mm - exp["mm"]) <= exp["tol"]
            print("  [%s] 期望 %.0f±%.0f mm，实际 ≈%.0f mm（差 %+.0f）"
                  % ("PASS" if ok else "FAIL", exp["mm"], exp["tol"],
                     tot_mm, tot_mm - exp["mm"]))
            bad += 0 if ok else 1
        elif k == "deg":
            ok = abs(tot_deg - exp["deg"]) <= exp["tol"]
            print("  [%s] 期望 %.0f±%.0f °，实际 ≈%.0f °（差 %+.0f）"
                  % ("PASS" if ok else "FAIL", exp["deg"], exp["tol"],
                     tot_deg, tot_deg - exp["deg"]))
            bad += 0 if ok else 1
        elif k == "route":
            ok1 = abs(tot_mm - 2 * exp["mm"]) <= 2 * exp["tolmm"]
            degs = [d for (a, s, d, _l) in steps if a in ("turn_left", "turn_right")]
            got_deg = degs[0] if degs else 0.0
            w = C.turn_degps(C.TURN_CMD)
            got_deg = w * float(got_deg)
            ok2 = abs(got_deg - exp["deg"]) <= exp["toldeg"]
            print("  [%s] 直线合计 期望 %.0f±%.0f mm，实际 ≈%.0f mm"
                  % ("PASS" if ok1 else "FAIL", 2 * exp["mm"], 2 * exp["tolmm"], tot_mm))
            print("  [%s] 转角     期望 %.0f±%.0f °，实际 ≈%.0f °"
                  % ("PASS" if ok2 else "FAIL", exp["deg"], exp["toldeg"], got_deg))
            bad += (0 if ok1 else 1) + (0 if ok2 else 1)

        # ── 3. 真机实测对照（明天装车后填 MEASURED）──────────────
        m = MEASURED.get(name)
        if m:
            if "mm" in m and tot_mm > 0:
                err = 100.0 * (tot_mm - m["mm"]) / m["mm"]
                print("    实测 %s mm → 建模偏高 %+.1f%%（乘 %.3f 可校正）"
                      % (m["mm"], err, m["mm"] / tot_mm))
            if "deg" in m and tot_deg > 0:
                err = 100.0 * (tot_deg - m["deg"]) / m["deg"]
                print("    实测 %s °  → 建模偏高 %+.1f%%（乘 %.3f 可校正）"
                      % (m["deg"], err, m["deg"] / tot_deg))

    # ── 4. 已知限幅提示 ─────────────────────────────────────────
    print("\n[限幅提醒] 计划单步转角上限 %d°（%.2f s）；单条命令时长上限 12 s"
          % (C.PLAN_MAX_ANGLE_DEG, C.duration_for_angle(C.PLAN_MAX_ANGLE_DEG)))
    print("           角速度 %.1f °/s（Z=%d），一圈 360° 需 %.2f s —— 在 12 s 以内，"
          % (C.turn_degps(C.TURN_CMD), C.TURN_CMD, C.duration_for_angle(360)))
    print("           所以「原地转一圈」可以一条命令走完。")

    print("\n" + "=" * 66)
    if bad:
        print("结果：%d 项不达标 ✗" % bad)
    else:
        print("结果：全部通过 ✓")
    print("=" * 66)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())