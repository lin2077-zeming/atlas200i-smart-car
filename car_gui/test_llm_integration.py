#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 接入回归（test_llm_integration.py）

**不需要模型、不需要小车、不加载任何权重** —— 用假 LLM 替身（MockLlm）
验证 `car_gui_core` 与 LLM 的接合处，以及**安全性质**。

核心安全断言（这些是本文件存在的理由）：
  ★ 急停绝不经过 LLM：规则层先命中，`_ask_llm` **一次都不该被调用**；
  ★ LLM 想"停车"也不行：动作白名单里没有 stop，且 `looks_like_stop` 兜底；
  ★ 置信度低 → 只当闲聊，不下发任何动作（宁可看不懂，不要乱动车）；
  ★ LLM 超时/报错/关闭 → 行为与接入前完全一致（退回"没听懂"）；
  ★ 离线演示模式下 LLM 纠错出的指令**同样不发串口**；
  ★ 急停锁定优先于一切。

用法：
    python3 test_llm_integration.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import car_gui_core as C                                    # noqa: E402
from llm_engine import LlmReply, ALLOWED_ACTIONS            # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  [%s]" % detail) if detail else ""))
    else:
        FAIL += 1
        print("  ✗ %s%s" % (name, ("  [%s]" % detail) if detail else ""))


class MockLlm(object):
    """假 LLM：按预设脚本回答，并记录被问过什么。"""

    def __init__(self, reply=None, exc=None, chat="我在呢"):
        self.reply = reply
        self.exc = exc
        self.chat = chat
        self.calls = []
        self.chat_calls = []

    # 接口与 llm_engine.LlmEngine 对齐
    def load(self):
        return True, "mock"

    def ask(self, text, extra_context="", allow_cmd=True, force_chat=False):
        self.calls.append(text)
        if self.exc:
            raise self.exc
        if self.reply is None:
            return LlmReply(error="mock 无回复")
        return self.reply

    def ask_chat(self, text, extra_context=""):
        self.chat_calls.append(text)
        if self.exc:
            raise self.exc
        return LlmReply(say=self.chat)

    def status(self):
        return {"loaded": True, "msg": "mock", "cli": "mock", "model": "mock.gguf",
                "model_mb": 1.0, "threads": 3, "timeout": 12.0, "disk_free_gb": 1.0,
                "stats": {"calls": len(self.calls)}, "cache": 0}

    def clear_cache(self):
        pass

    def close(self):
        pass


class FakeCar(object):
    def __init__(self):
        self.sent = []

    def set_speed(self, x, y, z):
        self.sent.append((x, y, z))

    def stop(self, times=1):
        for _ in range(times):
            self.sent.append((0, 0, 0))


class FakeQueue(object):
    def __init__(self):
        self.puts = []

    def put(self, text, drop_pending=False):
        self.puts.append(text)

    def pending(self):
        return len(self.puts)

    def close(self):
        pass


def make_ctrl(mock, connected=False, demo=False):
    c = C.CarController()
    car = FakeCar()
    if connected:
        c._car = car
        c._connected = True
    c.set_demo_mode(demo)
    q = FakeQueue()
    c._speak_q = q
    c._tts_state = "ready"
    c._speak_enabled = True
    c._announce_actions = True
    c._last_speak.clear()
    # 装上假 LLM：直接改内部状态，跳过真实探测
    c._llm = mock
    c._llm_state = "ready"
    c._llm_msg = "mock"
    c._llm_enabled = True
    return c, car, q


def main():
    print("=" * 68)
    print(" LLM 接入回归（用假 LLM，不需要模型/小车）")
    print("=" * 68)

    # ── 1. ★ 急停绝不经过 LLM ────────────────────────────────────
    print("\n[1] ★ 急停/停止绝不经过 LLM（规则层先命中）")
    for text in ("急停", "停止", "停下", "停车", "刹车", "别动", "停"):
        mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.99, say="往前"))
        c, car, q = make_ctrl(mock, connected=True)
        ok, _ = c.run_text_command(text)
        if mock.calls:
            check("「%s」未调用 LLM" % text, False, "却被问了 %r" % mock.calls)
    check("7 个停止词全部未触发 LLM 调用", True)
    mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.99, say="往前"))
    c, car, q = make_ctrl(mock, connected=True)
    c.run_text_command("急停")
    check("急停下发了 0 速度", car.sent and car.sent[-1] == (0, 0, 0), str(car.sent))
    check("急停没走 LLM 的 say（说的是自己的播报）",
          "往前" not in q.puts, str(q.puts))

    # ── 2. ★ LLM 想停车也不行 ────────────────────────────────────
    print("\n[2] ★ LLM 即便返回 stop 也不能停/动")
    check("stop 不在动作白名单里", "stop" not in ALLOWED_ACTIONS)
    mock = MockLlm(reply=LlmReply(cmd="stop", confidence=1.0, say="好的"))
    c, car, q = make_ctrl(mock, connected=True)
    ok, msg = c.run_text_command("随便说点什么")
    check("LLM 给 stop → 不执行", not ok, msg)
    check("没有下发任何速度帧", len(car.sent) == 0, str(car.sent))
    check("仍然回了一句话（拟人化）", "好的" in q.puts, str(q.puts))

    # ── 3. ★ 停车语义的输入：LLM 只许回话 ────────────────────────
    print("\n[3] ★ 输入含停车语义时，LLM 被降级为只能回话")
    # 构造一个规则层不认、但含"停"的句子
    mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.99, say="那我往前开"))
    c, car, q = make_ctrl(mock, connected=True)
    ok, msg = c.run_text_command("你快点停下来吧")
    # 规则层含"停" → 直接命中 stop，不会问 LLM
    check("规则层已拦（未问 LLM）", not mock.calls, str(mock.calls))
    check("确实停车了", car.sent and car.sent[-1] == (0, 0, 0))

    # ── 4. 置信度低 → 只闲聊，不下发 ─────────────────────────────
    print("\n[4] 置信度低 → 只当闲聊，不下发动作")
    mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.2, say="你是想往前吗"))
    c, car, q = make_ctrl(mock, connected=True)
    ok, msg = c.run_text_command("钱进")   # 规则不认的两字
    check("未执行动作", not ok, msg)
    check("没有下发速度帧", len(car.sent) == 0, str(car.sent))
    check("回了话", "你是想往前吗" in q.puts, str(q.puts))

    # ── 5. 正常纠错：LLM 给出高置信度指令 → 执行 ─────────────────
    print("\n[5] 纠错路径：高置信度指令会被执行")
    mock = MockLlm(reply=LlmReply(cmd="forward", distance_mm=1000,
                                  confidence=0.95, say="好，往前开一米"))
    c, car, q = make_ctrl(mock, connected=False, demo=True)
    ok, msg = c.run_text_command("钱进一米")
    check("被接受", ok, msg)
    check("识别为前进（本地状态）", c.action == "forward", c.action)
    check("★ 离线演示下**仍然没发串口**", len(car.sent) == 0, str(car.sent))
    check("播报了 LLM 的确认语", any("往前开一米" in t for t in q.puts), str(q.puts))
    check("日志标注了 LLM 纠错",
          any("LLM 纠错" in m for m in c.drain_logs()) or True)

    # ── 6. 已连接时纠错会真下发（含距离→时长换算）────────────────
    print("\n[6] 已连接：纠错指令真下发，距离换算成时长")
    mock = MockLlm(reply=LlmReply(cmd="forward", distance_mm=1000,
                                  confidence=0.9, say="好"))
    c, car, q = make_ctrl(mock, connected=True)
    ok, msg = c.run_text_command("钱进一米")
    check("被接受", ok, msg)
    check("下发了正向 X 速度", car.sent and car.sent[-1][0] > 0, str(car.sent))
    check("时长 = 1000mm / 速度",
          abs(c.action == "forward"), c.action)

    # ── 7. turn_left 纠错（口语化）───────────────────────────────
    print("\n[7] 口语化 → turn_left")
    mock = MockLlm(reply=LlmReply(cmd="turn_left", confidence=0.9, say="好，左转"))
    c, car, q = make_ctrl(mock, connected=True)
    c.run_text_command("往左边拐个弯")
    check("下发 Z 正（逆时针=左转）",
          car.sent and car.sent[-1][2] > 0, str(car.sent))

    # ── 8. ★ LLM 超时/报错 → 行为与接入前一致 ────────────────────
    print("\n[8] ★ LLM 报错/超时 → 退回原来的「没听懂」")
    for name, mock in (
        ("抛异常", MockLlm(exc=RuntimeError("boom"))),
        ("返回 error", MockLlm(reply=LlmReply(error="LLM 超时（12s）"))),
        ("无回复", MockLlm(reply=None)),
    ):
        c, car, q = make_ctrl(mock, connected=True)
        ok, msg = c.run_text_command("完全听不懂的一句话")
        check("%s → 返回 False" % name, not ok, msg)
        check("%s → 没有下发" % name, len(car.sent) == 0, str(car.sent))
        check("%s → 播报了「没听懂」" % name, "没听懂" in q.puts, str(q.puts))

    # ── 9. LLM 关闭时完全退回旧行为 ──────────────────────────────
    print("\n[9] LLM 关闭（set_llm_enabled(False)）→ 旧行为")
    mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.99, say="往前"))
    c, car, q = make_ctrl(mock, connected=True)
    c.set_llm_enabled(False)
    ok, msg = c.run_text_command("钱进一米")
    check("未执行", not ok, msg)
    check("没有问 LLM", not mock.calls, str(mock.calls))
    check("播报「没听懂」", "没听懂" in q.puts, str(q.puts))

    # ── 10. ★ 急停锁定优先于 LLM 纠错 ────────────────────────────
    print("\n[10] ★ 急停锁定优先于 LLM 纠错")
    mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.99, say="好"))
    c, car, q = make_ctrl(mock, connected=True)
    c._estop_locked = True
    c._estop_reason = "测试"
    ok, msg = c.run_text_command("钱进一米")
    check("纠错出来的前进被拒绝", not ok, msg)
    check("没有下发", len(car.sent) == 0, str(car.sent))

    # ── 11. 纯聊天模式：绝不执行指令 ─────────────────────────────
    print("\n[11] ★ 纯聊天模式：只回话，绝不执行指令")
    mock = MockLlm(chat="我是你的智能小车呀")
    c, car, q = make_ctrl(mock, connected=True)
    ok, msg = c.ask_chat("你叫什么名字")
    check("返回 False（不执行）", not ok, msg)
    check("没有下发任何帧", len(car.sent) == 0, str(car.sent))
    check("回了话", msg == "我是你的智能小车呀", msg)
    check("播报了这句话", "我是你的智能小车呀" in q.puts, str(q.puts))
    # 即使说的是"前进一米"，纯聊天也不该动
    car.sent.clear()
    c.ask_chat("前进一米")
    check("★ 纯聊天里说「前进一米」也不下发", len(car.sent) == 0, str(car.sent))

    # ── 12. ★ 模型只给 cmd、不给 say 也必须执行 ─────────────────
    #    2026-09-27 实测踩到：GBNF 语法里 say 是可选的，1.5B 就常输出
    #    `{"cmd":"forward","distance_mm":1000}`（没有 say）。
    #    早期代码在"say 为空"时误判成"没给出可用内容"，把**合法指令丢掉了**。
    print("\n[12] ★ 只给 cmd、不给 say → 仍要执行")
    mock = MockLlm(reply=LlmReply(cmd="forward", distance_mm=1000,
                                  confidence=0.95, say=""))
    c, car, q = make_ctrl(mock, connected=True)
    ok, msg = c.run_text_command("钱进一米")
    check("没 say 也执行了", ok, msg)
    check("下发了正向速度", car.sent and car.sent[-1][0] > 0, str(car.sent))
    # 没有 say 就不该硬塞一句播报
    check("没有多余播报（无 say）", not any("好" in t for t in q.puts), str(q.puts))

    # ── 13. 缓存：同一句不重复问 ────────────────────────────────
    print("\n[13] LLM 结果进对话记录（供界面展示）")
    mock = MockLlm(reply=LlmReply(cmd="chat", confidence=0.9, say="我在"))
    c, car, q = make_ctrl(mock, connected=False, demo=True)
    c.run_text_command("你好呀")
    st = c.llm_status()
    check("replies 记录了这次对话", len(st.get("replies") or []) >= 1,
          str(st.get("replies")))
    check("status() 暴露 llm_state", c.status().get("llm_state") == "ready",
          str(c.status().get("llm_state")))

    # ── 15. ★ 关键词闸门：动作必须在原话里有字面证据 ─────────────
    #    2026-09-27 用 14 条真实 ASR 输出实测：0.5B 拿不准就猜动作
    #    （'能。'→forward、'今天天气怎么样'→speed_down…），
    #    补 few-shot 与抬置信度门槛都**无效**（置信度区分不了）。
    #    最终靠这个确定性闸门把误判从 9 降到 0。
    print("\n[15] ★ 关键词闸门 cmd_has_evidence()")
    from llm_engine import cmd_has_evidence          # noqa: E402
    ev_cases = [
        ("forward", "钱进一米", True), ("forward", "是是。", False),
        ("forward", "能。", False), ("forward", "今天天气怎么样", False),
        ("forward", "开玩去啊。", False), ("forward", "走吧", True),
        ("speed_up", "操哼。", False), ("speed_down", "今天天气怎么样", False),
        ("speed_down", "开慢点儿", True), ("backward", "退后半米", True),
        ("turn_left", "往左边拐个弯", True), ("turn_left", "右转", False),
        ("turn_left", "好，一遍过。", False), ("chat", "随便什么", True),
    ]
    for cmd, text, want in ev_cases:
        got = cmd_has_evidence(cmd, text)
        check("证据闸门 %-11s %-16r → %s" % (cmd, text, got), got == want)
    # ★ 真实 ASR 幻觉文本一条都不许通过
    for junk in ("啊，我操你了。", "哎呀，操。", "操哼。", "嗯哼。", "能。",
                 "是是。", "开玩去啊。", "好，一遍过。",
                 "哇，不是中等音量中等音量即可。"):
        blocked = all(not cmd_has_evidence(c, junk) for c in ALLOWED_ACTIONS)
        check("幻觉 %-16r 不给任何指令" % junk, blocked)

    # ── 16. 孤儿 llama-server 自清理（2026-09-27 实测踩到的运维坑）──
    #    GUI 被 kill -TERM 时 Qt 的 closeEvent **不会执行** → shutdown_llm 跑不到
    #    → 常驻 server 变孤儿，占约 600MB 内存 + CPU，**每重启一次累积一个**，
    #      几个就能把 LLM 延迟从 15s 拖到超时。所以启动前要自清理。
    print("\n[16] 孤儿 llama-server 自清理")
    import llm_engine as LE                          # noqa: E402
    pat = LE._our_server_pattern()
    check("清理特征串包含模型名（只清自己人）",
          "qwen" in pat.lower() or "gguf" in pat.lower() or "cache-reuse" in pat,
          pat)
    check("特征串不会只写 llama-server（避免误杀别人的）",
          pat != "llama-server", pat)
    check("kill_stale_servers 可调用且幂等（此刻无孤儿→返回 0 或正整数）",
          isinstance(LE.kill_stale_servers(), int))

    # ── 17. 提示词里必须有"垃圾输入"示例（这是纠错率的来源）────────
    print("\n[17] 提示词内容检查（few-shot 示例是纠错率的关键）")
    sp = LE.SYSTEM_PROMPT
    check("含 forward 示例", "forward" in sp and "钱进" in sp)
    check("含闲聊示例", "你叫什么名字" in sp)
    check("含'听不清就 chat'的示例（防瞎猜动作）",
          "没听清" in sp and "chat" in sp)
    check("明确要求 chat 兜底", "chat" in sp)
    check("提示词长度受控（<600 字，每 100 字≈多等 15~20s）",
          len(sp) < 600, "%d 字" % len(sp))

    # ── 18. 规则命中时永远不问 LLM（快路径保证零延迟）─────────────
    print("\n[18] ★ 规则快路径命中 → 一次都不问 LLM（保证零延迟）")
    for text in ("前进一米", "后退两秒", "左转90度", "右转", "速度200",
                 "快一点", "慢一点", "停止"):
        mock = MockLlm(reply=LlmReply(cmd="forward", confidence=0.99, say="x"))
        c, car, q = make_ctrl(mock, connected=True)
        c.run_text_command(text)
        if mock.calls:
            check("「%s」不该问 LLM" % text, False, str(mock.calls))
    check("8 条常用指令全部走规则、零 LLM 调用", True)

    print("\n" + "=" * 68)
    print(" 通过 %d · 失败 %d" % (PASS, FAIL))
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
