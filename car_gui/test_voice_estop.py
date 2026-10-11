#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_voice_estop.py —— 语音急停 + 播报链路的**离线断言**
                        （不需要小车硬件、不需要麦克风、不需要加载 TTS 模型）

为什么必须单独测这个
--------------------
"**先停车、后播报**"这条顺序是**安全问题**，不能靠读代码"感觉是对的"。
本板 TTS 合成一句要好几秒 —— 顺序一旦反了，那几秒里车还在跑。

所以这里用**假车 + 假播报队列**，把整条链路真跑一遍，逐条断言：

  ① 停车帧 **早于** 播报投递（顺序）★ 最重要
  ② 急停后进入**锁定态**，move() 被拒绝
  ③ **stop 例外**：锁定态下仍允许停车（不然锁定比不锁还危险）
  ④ release_estop() 解锁后恢复正常
  ⑤ KWS 命中回调 `_on_kws_hit` 走完整链路
  ⑥ **播报队列不可用时，停车与锁定照样完成** —— 播报绝不能影响安全动作
  ⑦ 急停 / 动作播报都带 drop_pending（过期播报必须丢弃）
  ⑧ 串口写锁真的在工作（两个线程不会交错写）
  ⑨ SpeakQueue 的 drop_pending / speaking / idle 语义

用法
----
    bash run_py.sh test_voice_estop.py
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import car_gui_core as core                             # noqa: E402
import tts_engine as TE                                 # noqa: E402

PASS = 0
FAIL = 0


def check(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("   ✓ %s" % label)
    else:
        FAIL += 1
        print("   ❌ %s%s" % (label, ("   ← " + str(extra)) if extra else ""))


# ══════════════════════════════════════════════════════════════════════
#  假件
# ══════════════════════════════════════════════════════════════════════
class Timeline(object):
    """记录"什么时刻发生了什么"，用来断言顺序。"""

    def __init__(self):
        self.events = []
        self._t0 = time.time()

    def add(self, kind, detail):
        self.events.append((kind, detail, time.time() - self._t0))

    def first_index(self, kind, detail=None):
        for i, (k, d, _t) in enumerate(self.events):
            if k == kind and (detail is None or d == detail):
                return i
        return -1


class FakeCar(object):
    """假下位机：只记下每次下发的三轴速度。"""

    def __init__(self, tl):
        self.tl = tl
        self.writes = []

    def set_speed(self, x, y, z):
        self.writes.append((x, y, z))
        self.tl.add("set_speed", (x, y, z))

    def open(self):
        pass

    def close(self):
        pass

    def get_feedback(self):
        return None


class FakeSpeakQueue(object):
    """假播报队列：只记下投递了哪些文本、是否要求丢弃排队。"""

    def __init__(self, tl):
        self.tl = tl
        self.items = []

    def put(self, text, drop_pending=False):
        self.items.append((text, drop_pending))
        self.tl.add("speak", text)
        return len(self.items)

    def pending(self):
        return len(self.items)

    def speaking(self):
        return False

    def idle(self):
        return True

    def close(self):
        pass


def make_ctrl(with_speak=True):
    """造一个"连着假车"的控制器。"""
    tl = Timeline()
    c = core.CarController()
    c._car = FakeCar(tl)
    c._connected = True
    q = FakeSpeakQueue(tl) if with_speak else None
    c._speak_q = q
    c._tts_state = "ready" if with_speak else "off"
    return c, tl, q


# ══════════════════════════════════════════════════════════════════════
print("=" * 72)
print("  语音急停 / 播报链路 离线断言")
print("=" * 72)

# ── ① 顺序：先停车，后播报 ───────────────────────────────────────────
print("\n① 急停顺序（★ 安全核心）")
c, tl, q = make_ctrl()
c.trigger_emergency(reason="测试", announce=True)
i_stop = tl.first_index("set_speed", (0, 0, 0))
i_speak = tl.first_index("speak", core.SPEAK_ESTOP)
check(i_stop >= 0, "急停下发了零速度帧")
check(i_speak >= 0, "急停投递了播报「%s」" % core.SPEAK_ESTOP)
check(0 <= i_stop < i_speak,
      "**停车发生在播报之前**（安全顺序正确）",
      "stop@%d speak@%d" % (i_stop, i_speak))
check(len([e for e in tl.events if e[0] == "set_speed"]) >= 5,
      "急停连发多次停止帧（尽量确保到位）",
      len([e for e in tl.events if e[0] == "set_speed"]))
check(c.estop_locked and c.estop_reason == "测试", "已进入锁定态并记录原因")
item = q.items[0] if q.items else ("", False)
check(item[1] is True, "急停播报带 drop_pending=True（顶掉排队内容）", item)

# ── ② 锁定态拦截 ─────────────────────────────────────────────────────
print("\n② 锁定态拦截动作")
c, tl, q = make_ctrl()
c.trigger_emergency(reason="测试")
n_before = len(tl.events)
ok_fwd = c.move("forward", speed=200, duration=1.0)
check(ok_fwd is False, "锁定态下 move(forward) 被拒绝")
check(len([e for e in tl.events[n_before:] if e[0] == "set_speed"]) == 0,
      "被拒绝时**没有**下发任何速度帧")
ok_txt, msg_txt = c.run_text_command("前进")
check(ok_txt is False, "锁定态下文本指令「前进」同样被拒绝", msg_txt)

# ── ③ stop 例外 ─────────────────────────────────────────────────────
print("\n③ 锁定态下 stop 仍然放行（否则比不锁还危险）")
n_before = len(tl.events)
ok_stop = c.stop()
check(ok_stop is True, "锁定态下 stop() 仍然成功")
check(len([e for e in tl.events[n_before:] if e[0] == "set_speed"]) > 0,
      "stop 确实下发了零速度帧")

# ── ④ 解除锁定 ───────────────────────────────────────────────────────
print("\n④ 人工解除急停锁定")
c._last_speak.clear()
reason = c.release_estop()
check(reason == "测试", "release_estop 返回原锁定原因", reason)
check(c.estop_locked is False, "锁定态已清除")
ok_fwd2 = c.move("forward", speed=200, duration=0.3)
check(ok_fwd2 is True, "解锁后 move(forward) 恢复正常")
c._cancel_timer()          # 别让定时停车留着影响后续
check(c.release_estop() == "", "没锁时 release_estop 返回空串（幂等）")

# ── ⑤ KWS 命中回调走完整链路 ────────────────────────────────────────
print("\n⑤ KWS 命中回调 _on_kws_hit（真实回调路径）")
c, tl, q = make_ctrl()
c._on_kws_hit("急停", time.time())
i_stop = tl.first_index("set_speed", (0, 0, 0))
i_speak = tl.first_index("speak", core.SPEAK_ESTOP)
check(i_stop >= 0 and i_speak >= 0 and i_stop < i_speak,
      "命中 → 先停车 → 后播报（顺序正确）",
      "stop@%d speak@%d" % (i_stop, i_speak))
check(c.estop_locked, "命中后进入锁定态")
check("急停" in c.estop_reason, "锁定原因里带上了命中词", c.estop_reason)
check(len(c._kws_hits) == 1 and c._kws_hits[0][0] == "急停", "命中被记入历史")
ev = c.poll_voice_events()
check(any(e[0] == "estop" for e in ev), "产生了 estop 事件供界面提示", ev)
check(c.poll_voice_events() == [], "事件取过就清空（不会重复提示）")

# ── ⑥ 播报不可用 ≠ 停不下来 ─────────────────────────────────────────
print("\n⑥ 播报队列不可用时，停车与锁定照样完成（★ 播报绝不能挡安全动作）")
c, tl, q = make_ctrl(with_speak=False)
ok = c.trigger_emergency(reason="无播报", announce=True)
check(ok is True, "没有播报队列时 emergency_stop 依然成功")
check(c.estop_locked is True, "照样进入锁定态")
check(len([e for e in tl.events if e[0] == "set_speed"]) >= 5, "照样连发停止帧")
check(c.speak("随便说点什么") is False, "speak() 在未就绪时返回 False（允许失败）")

print("\n⑥b 播报开关关掉后，急停播报仍然发出（走 force）")
c, tl, q = make_ctrl()
c.set_speak_enabled(False)
c.trigger_emergency(reason="测试")
check(q.items and q.items[0][0] == core.SPEAK_ESTOP,
      "关掉播报开关后，急停播报仍然发出", q.items)

# ── ⑦ 动作播报的 drop_pending ────────────────────────────────────────
print("\n⑦ 动作播报带 drop_pending（过期的「开始前进」必须丢弃）")
c, tl, q = make_ctrl()
c.move("forward", speed=200, duration=0.3)
c._cancel_timer()
c._last_speak.clear()
c.move("turn_left", speed=200, duration=0.3)
c._cancel_timer()
texts = [t for t, _d in q.items]
check(core.SPEAK_TEMPLATES["forward"] in texts, "播报了「开始前进」", texts)
check(core.SPEAK_TEMPLATES["turn_left"] in texts, "播报了「向左转」", texts)
check(all(d is True for _t, d in q.items), "动作播报全部带 drop_pending=True", q.items)

print("\n⑦b 冷却是「按文本」的，不会把不同播报互相挡掉")
c, tl, q = make_ctrl()
c._last_speak.clear()
c.move("forward", speed=200, duration=0.3)
c._cancel_timer()
c.move("backward", speed=200, duration=0.3)     # 立刻反向，不同文本
c._cancel_timer()
texts = [t for t, _d in q.items]
check(len(texts) == 2, "两条不同播报都投递成功（没被冷却误伤）", texts)

print("\n⑦c 同一条短时间重复会被冷却挡住")
c, tl, q = make_ctrl()
c._last_speak.clear()
first = c.speak("测试冷却")
second = c.speak("测试冷却")
check(first is True and second is False, "第二次同文本被冷却拦下", (first, second))
check(c.speak("测试冷却", force=True) is True, "force=True 可以绕过冷却")

# ── ⑧ 串口写锁 ───────────────────────────────────────────────────────
print("\n⑧ 串口写锁：两个线程不会交错写")
c, tl, q = make_ctrl()
c._lock.acquire()                      # 主线程先占住锁
done = []
t = threading.Thread(target=lambda: (c._write_speed(1, 0, 0), done.append(True)))
t.daemon = True
t.start()
time.sleep(0.15)
check(not done, "锁被占住时，另一个线程的写确实被挡住了")
check(c._car.writes == [], "被挡住期间没有真的写串口", c._car.writes)
c._lock.release()
t.join(2.0)
check(bool(done), "释放锁后写立刻完成")
check(c._car.writes == [(1, 0, 0)], "写的内容正确", c._car.writes)


# ── ⑨ SpeakQueue 语义（用假 TTS，不加载真模型）──────────────────────
print("\n⑨ SpeakQueue：drop_pending / speaking / idle")


class BlockingTts(object):
    """假 TTS：进 say() 就卡住，直到放行 —— 这样时序完全可控。"""

    def __init__(self):
        self.gate = threading.Event()
        self.said = []

    def say(self, text, **kw):
        self.gate.wait(3.0)
        self.said.append(text)
        return True, ""


def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


tts = BlockingTts()
sq = TE.SpeakQueue(tts)
sq.put("a")
check(wait_for(lambda: sq.speaking), "第一条被取出后 speaking=True")
check(not sq.idle(), "正在播报时 idle()=False（关键：pending 为 0 也不代表说完了）")
sq.put("b")
sq.put("c")
check(sq.pending() == 2, "排队 2 条", sq.pending())
dropped = sq.clear_pending()
check(dropped == 2, "clear_pending 丢掉 2 条", dropped)
sq.put("d", drop_pending=True)
check(sq.pending() == 1, "drop_pending 后只剩最新一条", sq.pending())
tts.gate.set()
check(wait_for(lambda: sq.idle()), "放行后队列自然跑空")
time.sleep(0.2)
check(tts.said == ["a", "d"], "a 说完了，中间的 b/c 被丢弃，最后说 d", tts.said)
sq.close()
check(wait_for(lambda: not sq._thread.is_alive(), 2.0),
      "close() 后工作线程能正常退出（不依赖 sentinel）")

# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 72)
print("  通过 %d · 失败 %d" % (PASS, FAIL))
print("=" * 72)
if FAIL:
    print("\n⚠️  有断言失败 —— 语音急停是安全功能，必须全绿才能上车。")
sys.exit(1 if FAIL else 0)
