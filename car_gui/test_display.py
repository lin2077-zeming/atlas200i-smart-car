#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发板展示屏 · 离线回归（离屏渲染，不需要显示器）。

v3 的测试重点
-------------
设计原则是"**靠字号分层，不靠容器**"，所以这里断言的核心是：
  · 只有 4 块 + 2 条线（没有竖条、没有胶囊、没有装饰色条）
  · 字号序列 16 / 18 / 40 / 48 / 40 / 20（比例 48:40 = 1.2）
  · 颜色只有语义色：绿=运动、红=急停、黄=未连接/降级，其余全灰阶
  · 停止/急停时**不报速度**（"停止"和"1.2 m/s"会互相打架）
  · 进度条与 k/n 只在多步计划时出现，且都在状态带里

```
QT_QPA_PLATFORM=offscreen python3 test_display.py
```
"""
from __future__ import print_function

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 必须在 import Qt 之前

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_ok = _bad = 0
_fails = []


def check(name, cond, detail=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print("  [PASS] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        _bad += 1
        _fails.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def main():
    try:
        from PySide6.QtWidgets import QApplication, QFrame
    except Exception as e:
        print("  [SKIP] 没有 PySide6（%s），跳过展示屏测试" % e)
        return 0

    app = QApplication(sys.argv[:1])   # noqa: F841

    try:
        import display as D
    except Exception as e:
        print("  [FAIL] 导入 display.py 失败：%r" % (e,))
        return 1

    print("=" * 62)
    print(" 开发板展示屏 · 离线回归（v3 减法版）")
    print("=" * 62)

    # ── 1. 版式：只有四块 ───────────────────────────────────────
    print("\n[1] 版式（1024×600）")
    p = D.Panel(screen_size=(1024, 600))
    p.resize(1024, 600)
    p.refresh()
    check("缩放系数 = 1.0", abs(p.k - 1.0) < 0.02, "k=%.2f" % p.k)
    check("顶部 44 px", p.top_box.height() == D.H_TOP, str(p.top_box.height()))
    check("听到 214 px", p.heard_box.height() == D.H_HEARD, str(p.heard_box.height()))
    check("状态带 64 px", p.band.height() == D.H_BAND, str(p.band.height()))
    check("回应区吃掉剩余高度（>=236）", p.reply_box.height() >= p._px(D.H_REPLY_MIN),
          str(p.reply_box.height()))
    check("四块合计 ≈ 满屏（无空档）",
          p.top_box.height() + p.heard_box.height() + p.reply_box.height()
          + p.band.height() >= 594,
          str(p.top_box.height() + p.heard_box.height()
              + p.reply_box.height() + p.band.height()))

    # ── 2. 字号层级：16 / 18 / 40 / 48 / 40 / 20 ───────────────
    print("\n[2] 字号层级（靠字号分层，不靠容器）")
    check("顶部 16 px", p.lb_top.font().pixelSize() == 16, str(p.lb_top.font().pixelSize()))
    check("标签 18 px", p.lb_tag_heard.font().pixelSize() == 18)
    check("听到 40 px", p.lb_heard.font().pixelSize() == 40,
          str(p.lb_heard.font().pixelSize()))
    check("回应 48 px（主角）", p.lb_reply.font().pixelSize() == 48,
          str(p.lb_reply.font().pixelSize()))
    check("状态带 40 px", p.lb_band.font().pixelSize() == 40,
          str(p.lb_band.font().pixelSize()))
    check("元信息 20 px", p.lb_meta.font().pixelSize() == 20)
    ratio = float(p.lb_reply.font().pixelSize()) / p.lb_heard.font().pixelSize()
    check("回应:听到 = 1.2（不是 1.4）", abs(ratio - 1.2) < 0.02, "%.2f" % ratio)

    # ── 3. 减法：竖条 / 胶囊 / 装饰条都删掉了 ──────────────────
    print("\n[3] 减法（不该再有容器）")
    thin = [w for w in p.heard_box.findChildren(QFrame) if w.width() <= p._px(5)]
    check("听到区没有 4px 竖条", len(thin) == 0, "%d 个细条" % len(thin))
    thin_r = [w for w in p.reply_box.findChildren(QFrame) if w.width() <= p._px(5)]
    check("回应区没有 4px 竖条", len(thin_r) == 0, "%d 个细条" % len(thin_r))
    check("没有顶部胶囊（只有一个顶部文本标签）",
          not hasattr(p, "chip_car") and not hasattr(p, "chip_ctl"))
    check("没有独立装饰色条", not hasattr(p, "strip"))
    check("ACTION_STYLE 只有 (中文, 配色键)",
          all(len(v) == 2 for v in D.ACTION_STYLE.values()),
          str(D.ACTION_STYLE.get("forward")))

    # ── 4. 自适应缩放 ───────────────────────────────────────────
    print("\n[4] 自适应缩放")
    for (w, h), k in (((1024, 600), 1.0), ((1920, 1080), 1.8), ((2560, 1440), 2.4)):
        q = D.Panel(screen_size=(w, h))
        check("%dx%d -> 缩放 %.1f" % (w, h, k), abs(q.k - k) < 0.05, "k=%.2f" % q.k)
    check("--scale 可强制覆盖",
          abs(D.Panel(screen_size=(1024, 600), scale=1.5).k - 1.5) < 0.01)

    # ── 5. 顶部一行灰字：正常全灰，异常才上色 ──────────────────
    print("\n[5] 顶部一行（正常灰 / 异常黄）")
    p2 = D.Panel(screen_size=(1024, 600))
    p2.resize(1024, 600)
    p2._set_top({"connected": True, "voltage": 15.9, "llm_state": "ready",
                 "console_online": True})
    t = p2.lb_top.text()
    check("含「车已连接」", "车已连接" in t, t)
    check("含电量 94%", "94%" in t, t)
    check("含「电脑在线」", "电脑在线" in t, t)
    check("正常态**没有**黄色 span", D.LIGHT["warn"] not in t, t)
    p2._set_top({"connected": False, "voltage": 15.9, "llm_state": "ready",
                 "console_online": True})
    t = p2.lb_top.text()
    check("未连接 → 车未连接 + 黄色", "车未连接" in t and D.LIGHT["warn"] in t, t)
    p2._set_top({"connected": True, "voltage": 15.9, "llm_state": "ready",
                 "console_online": False})
    t = p2.lb_top.text()
    check("电脑离线 → 黄色", "电脑离线" in t and D.LIGHT["warn"] in t, t)
    check("电量曲线 16.2V=100%", D._batt_pct(16.2) == 100)
    check("电量曲线 14.8V=70%", D._batt_pct(14.8) == 70)
    check("无电压 -> None", D._batt_pct(0) is None)

    # ── 6. 状态带：颜色语义 + 速度只在运动时报 ─────────────────
    print("\n[6] 底部状态带")
    p3 = D.Panel(screen_size=(1024, 600))
    p3.resize(1024, 600)
    p3.online_since = time.time()
    p3.st = {"connected": True, "console_online": True, "action": "forward",
             "voltage": 15.9, "llm_state": "ready", "speed": 30, "speed_mmps": 1158}
    p3.refresh()
    check("运动 → 状态带绿底", D.LIGHT["band_move"] in p3.band.styleSheet(),
          p3.band.styleSheet())
    check("运动 → 显示「前进」", "前进" in p3.lb_band.text(), p3.lb_band.text())
    check("运动 → 右侧报速度", "m/s" in p3.lb_meta.text(), p3.lb_meta.text())
    check("状态带里没有箭头（「前进」两个字够了）",
          not any(ch in p3.lb_band.text() for ch in "▲▼◀▶"),
          repr(p3.lb_band.text()))
    check("非多步 → 进度条隐藏", p3.bar_plan.isHidden() and p3.lb_step.isHidden())

    p3.st.update({"action": "stop", "plan_running": False})
    p3.refresh()
    check("停止 → 状态带灰底", D.LIGHT["band_stop"] in p3.band.styleSheet(),
          p3.band.styleSheet())
    check("停止 → 文案「停止」", p3.lb_band.text().strip() == "停止",
          repr(p3.lb_band.text()))
    check("★ 停止时不再报速度（不再自相矛盾）",
          "m/s" not in p3.lb_meta.text(), repr(p3.lb_meta.text()))

    p3.st.update({"action": "turn_left", "speed_mmps": 1158})
    p3.refresh()
    check("左转 → 也是绿底（绿只表示运动正常）",
          D.LIGHT["band_move"] in p3.band.styleSheet())
    check("左转 → 文案「左转」", "左转" in p3.lb_band.text(), p3.lb_band.text())

    # 多步：进度与 k/n 都在状态带里
    p3.st.update({"action": "forward", "plan_running": True,
                  "plan_step": 1, "plan_total": 2, "plan_label": "前进"})
    p3.refresh()
    check("多步 → 进度条出现", not p3.bar_plan.isHidden())
    check("多步 → 进度 50%", p3.bar_plan.value() == 50, str(p3.bar_plan.value()))
    check("多步 → k/n 在状态带里", not p3.lb_step.isHidden()
          and p3.lb_step.text() == "1/2", p3.lb_step.text())

    # ── 7. 急停态：红只出现两处 ────────────────────────────────
    print("\n[7] 急停态（红只给边框 + 状态带）")
    p4 = D.Panel(screen_size=(1024, 600))
    p4.resize(1024, 600)
    p4.online_since = time.time()
    p4.st = {"connected": True, "console_online": True, "action": "stop",
             "estop_locked": True, "speed": 30, "speed_mmps": 1158}
    t = time.time()
    p4.apply_event({"type": "heard", "turn": 1, "seq": 1, "ts": t - 2.4,
                    "text": "前进一米"})
    p4.apply_event({"type": "reply", "turn": 1, "seq": 2, "ts": t, "text": "好的"})
    p4.refresh()
    check("状态带红底", D.LIGHT["band_danger"] in p4.band.styleSheet())
    check("状态带文案「已锁定」（不与回应重复）",
          p4.lb_band.text().strip() == "已锁定", repr(p4.lb_band.text()))
    check("★ 回应区用**常规色**（不是红字）",
          D.LIGHT["band_danger"] not in p4.lb_reply.styleSheet(),
          p4.lb_reply.styleSheet())
    check("回应区文字说明怎么解除",
          "已急停" in p4.lb_reply.text() and "解除" in p4.lb_reply.text(),
          p4.lb_reply.text())
    check("★ 急停时不报速度", "m/s" not in p4.lb_meta.text(), repr(p4.lb_meta.text()))
    img = p4.grab().toImage()
    corner = img.pixelColor(3, 3)
    check("整屏 6px 红边", corner.red() > 150 and corner.green() < 120, corner.name())

    # ── 8. 待机 / 断连 ─────────────────────────────────────────
    print("\n[8] 待机 / 断连")
    p5 = D.Panel(screen_size=(1024, 600))
    p5.resize(1024, 600)
    p5.st = {"connected": True, "console_online": False}
    p5.online_since = time.time() - 120
    p5.refresh()
    check("待机 → 状态带「等待控制台连接」",
          "等待控制台连接" in p5.lb_band.text(), p5.lb_band.text())
    check("待机 → 灰底", D.LIGHT["band_stop"] in p5.band.styleSheet())
    p5.online_since = time.time() - 10
    p5.refresh()
    check("离线 10 秒不切待机（刷新页面不闪）",
          "等待控制台连接" not in p5.lb_band.text(), p5.lb_band.text())

    p6 = D.Panel(screen_size=(1024, 600))
    p6.resize(1024, 600)
    p6.fails = 6
    p6.last_ok = time.time() - 10
    p6.refresh()
    check("断连 → 状态带「与主控断开」", "与主控断开" in p6.lb_band.text(),
          p6.lb_band.text())
    check("断连 → 右侧提示重连中", "重连" in p6.lb_meta.text(), p6.lb_meta.text())

    # ── 9. 对话区与耗时 ────────────────────────────────────────
    print("\n[9] 对话区")
    p7 = D.Panel(screen_size=(1024, 600))
    p7.resize(1024, 600)
    p7.online_since = time.time()
    p7.st = {"connected": True, "console_online": True, "action": "forward",
             "speed": 30, "speed_mmps": 600, "llm_state": "ready"}
    t = time.time()
    p7.apply_event({"type": "heard", "turn": 1, "seq": 1, "ts": t - 2.4,
                    "text": "明天吃什么"})
    p7.apply_event({"type": "reply", "turn": 1, "seq": 2, "ts": t,
                    "text": "我只看得到眼前的路"})
    p7.refresh()
    check("听到上屏", "明天吃什么" in p7.lb_heard.text(), p7.lb_heard.text())
    check("回应上屏", "我只看得到眼前的路" in p7.lb_reply.text(), p7.lb_reply.text())
    check("★ 耗时并进状态带（2.4s），不再单独一行",
          "2.4s" in p7.lb_meta.text(), p7.lb_meta.text())
    check("速度用 m/s", "m/s" in p7.lb_meta.text(), p7.lb_meta.text())
    check("0.6 m/s 慢档 → 慢速", D._speed_label(600, 25).startswith("慢速"),
          D._speed_label(600, 25))
    check("1.6 m/s 快档 → 快速", D._speed_label(1600, 34).startswith("快速"),
          D._speed_label(1600, 34))

    got = p7._fit("钱进一米" * 20, D.F_HEARD, 2)
    check("听到超长截断且加省略号", got.endswith("…"), "len=%d" % len(got))
    check("回应只给 1 行",
          p7._fit("好的先前进再左转然后再后退一点点吧" * 3, D.F_REPLY, 1).endswith("…"))
    check("短文本不截断", p7._fit("前进一米", D.F_HEARD, 2) == "前进一米")
    check("空白归一", p7._fit("前进   一米", D.F_HEARD, 2) == "前进 一米")

    # ── 10. 深色主题单独调过 ──────────────────────────────────
    print("\n[10] 深色主题")
    d = D.Panel(screen_size=(1024, 600), theme="dark")
    check("深色分隔线用半透明白（不然层次全塌）",
          "rgba(255,255,255,0.12)" in D.DARK["line"], D.DARK["line"])
    check("深色绿色降饱和（不是亮绿）",
          D.DARK["band_move"].lower() != "#1d9e75" and D.DARK["band_move"] != "",
          D.DARK["band_move"])
    check("深色也有完整语义色", all(k in D.DARK for k in
          ("band_move", "band_stop", "band_danger", "warn")))

    # ── 12. 冷启动语义（2026-11-03 开机自启后在真机屏上抓到的问题）──
    print("\n[12] 冷启动：车没连上时不能显示「前进 + 速度」")
    q = D.Panel(screen_size=(1024, 600))
    q.apply_packet({"ok": True, "status": {
        "connected": False, "action": "forward", "action_label": "前进",
        "speed": 300, "speed_mmps": 300, "console_online": False,
        "estop_locked": False, "plan_running": False}})
    q.refresh()
    check("状态带说「等待小车连接」", q.lb_band.text() == "等待小车连接", q.lb_band.text())
    check("状态带是灰色（不是绿色「前进」）",
          q.C["band_stop"] in q.band.styleSheet(), q.band.styleSheet())
    check("车没连上时不报速度", "m/s" not in q.lb_meta.text(), q.lb_meta.text())
    check("顶部仍报「车未连接」", "车未连接" in q.lb_top.text(), q.lb_top.text())

    f = D.Fetcher("http://127.0.0.1:1")
    seen = []
    f._get = lambda path, timeout: (seen.append(path)
                                    or {"seq": 42, "since": 42, "events": []})
    okp = f._prime()
    check("首帧只问游标、不回放历史",
          bool(okp) and f.cursor == 42 and "since=999999999" in seen[0],
          "%s cursor=%s" % (seen, f.cursor))
    f2 = D.Fetcher("http://127.0.0.1:1", replay=True)
    check("只有调试时才回放历史（replay=True）", f2._primed is True and f2.cursor == 0)

    # ── 13. 防息屏（2026-11-03 用户报「长期不动黑屏，web 下指令也唤不醒」）──
    print("\n[13] 防息屏")
    _real_xset = D._xset
    calls = []
    D._xset = lambda *a: (calls.append(a) or True)
    k = D.Panel(screen_size=(1024, 600))
    k._keep_awake()
    check("每 20 s 重申 xset s off -dpms",
          ("s", "off", "-dpms", "s", "noblank") in calls, str(calls))
    check("重申时同时把 DPMS 拉回 on", ("dpms", "force", "on") in calls, str(calls))
    check("把空闲超时清零（0 0 0 = 永不黑屏）",
          ("dpms", "0", "0", "0") in calls, str(calls))
    check("★ 顺序：先点亮、最后禁 DPMS（反了会留下 600 s 超时 → 10 分钟黑屏）",
          calls.index(("dpms", "force", "on")) < calls.index(
              ("s", "off", "-dpms", "s", "noblank")), str(calls))
    calls.clear()
    k._last_wake = 0
    k._wake()
    k._wake()                                  # 2 s 内的第二次应被节流
    check("新指令立刻点亮屏幕（且 2 s 内只点一次）",
          calls.count(("dpms", "force", "on")) == 1, str(calls))
    calls.clear()
    k._last_wake = 0
    k.apply_event({"type": "heard", "turn": 9, "seq": 99, "ts": time.time(),
                   "text": "前进一米"})
    check("收到新话就点亮屏幕（web 指令唤醒的关键）",
          ("dpms", "force", "on") in calls, str(calls))
    check("没有 DISPLAY 时不乱调 xset（离屏渲染/跑测试安全）",
          'if not os.environ.get("DISPLAY")' in
          open(os.path.join(HERE, "display.py"), encoding="utf-8").read())
    D._xset = _real_xset

    # ── 11. 其它约束 ──────────────────────────────────────────
    print("\n[11] 约束")
    check("中文字体可用（否则屏幕全方块）", p.font_ok, "字体=%s" % p.family)
    check("Turn 聚合成对", p7.turns.get(1, {}).get("reply") is not None)
    for i in range(10):
        p7.apply_event({"type": "heard", "turn": 100 + i, "seq": i,
                        "ts": 1, "text": "y"})
    check("只保留最近 6 轮（防内存增长）", len(p7.turns) <= 6, str(len(p7.turns)))

    print("\n" + "=" * 62)
    if _bad:
        print(" 通过 %d · 失败 %d" % (_ok, _bad))
        print(" 失败清单：")
        for n in _fails:
            print("   x %s" % n)
    else:
        print(" 通过 %d · 失败 0" % _ok)
    print("=" * 62)
    return 1 if _bad else 0


ACTION_KEYS = ()


def _exit_now(code):
    """flush 后立刻退出：避免守护线程在解释器收尾时写 stdout 触发
    `_enter_buffered_busy`（断言全过却返回码 1，会让 test_all_offline 误判）。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    _exit_now(main())