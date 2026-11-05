#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把**真实 ASR 输出**（含幻觉垃圾）喂给本地 LLM，看它怎么处理。

这是最贴近实战的测试：输入不是人手编的错别字，而是 ASR 真的吐出来的东西。

关键观察点：
  · 同音误识别（"钱进一米"）→ **应该纠成 forward**
  · 幻觉垃圾（"操哼。"）    → **不应该瞎猜成指令**，应回一句得体的话
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import car_gui_core as C          # noqa: E402
import llm_engine as L            # noqa: E402

# 真实 ASR 输出（板子上实测得到，见 test_asr_llm_e2e.py 的结果）
CASES = [
    # ── 类型 A：同音/近音误识别 —— 期望纠错成功 ──
    ("钱进一米",       True,  "同音字（前进→钱进）"),
    ("往左边拐个弯",    True,  "口语化"),
    ("开慢点儿",       True,  "口语化"),
    ("退后半米",       True,  "口语化"),
    # ── 类型 B：真实 ASR 幻觉垃圾 —— 期望**不**瞎猜成指令 ──
    ("啊，我操你了。",  False, "ASR 幻觉（实测样本）"),
    ("哎呀，操。",     False, "ASR 幻觉（实测样本）"),
    ("操哼。",         False, "ASR 幻觉（实测样本）"),
    ("嗯哼。",         False, "ASR 幻觉（实测样本）"),
    ("能。",           False, "ASR 幻觉（实测样本）"),
    ("是是。",         False, "ASR 幻觉（实测样本）"),
    ("开玩去啊。",     False, "ASR 幻觉（实测样本）"),
    ("好，一遍过。",    False, "ASR 幻觉（实测样本）"),
    ("哇，不是中等音量中等音量即可。", False, "ASR 幻觉（实测样本）"),
    # ── 类型 C：明显闲聊 —— 期望拟人化回复 ──
    ("你叫什么名字",    False, "闲聊"),
    ("今天天气怎么样",  False, "闲聊"),
]

print("=" * 78)
print(" 真实 ASR 输出 → LLM（指令纠错 + 拟人化对话）")
print("=" * 78)

# 后端可选：--mode auto|api|server|cli（默认 auto：有云端 key 且网络通就用云端）
import argparse as _ap
_p = _ap.ArgumentParser(add_help=False)
_p.add_argument("--mode", default="auto",
                choices=("auto", "api", "server", "cli"))
_a, _ = _p.parse_known_args()
_mode = _a.mode
print("配置的后端模式: %s" % _mode)

ctrl = C.CarController()
ctrl.set_demo_mode(True)
t0 = time.time()
ok, msg = ctrl.init_llm(mode=_mode)
print("init_llm: %s (%.1fs)  %s" % (ok, time.time() - t0, msg))
if not ok:
    raise SystemExit(1)
try:
    _st = ctrl.llm_status()
    print("实际后端: %s   模型: %s   key: %s"
          % (_st.get("mode"), _st.get("model"),
             "已配置" if _st.get("api_key_set") else "无"))
except Exception:
    pass
print("llm_ready:", ctrl.llm_ready())
print()

wrong_cmd = []      # 幻觉被瞎猜成指令（危险）
miss_cmd = []       # 该纠错却没纠出来
boring = []         # 该闲聊却回了"没听懂"

for text, want_cmd, note in CASES:
    t = time.time()
    r = ctrl._ask_llm(text)
    dt = time.time() - t
    if r is None:
        print("  %-32s LLM=None" % text)
        continue
    got_cmd = r.cmd if r.has_cmd else None
    # 置信度门槛（与生产逻辑一致）
    passed = got_cmd is not None and r.confidence >= C.LLM_MIN_CONFIDENCE
    verdict = "指令:" + got_cmd if passed else ("闲聊:" + (r.say or "（空）"))
    if passed and not want_cmd:
        mark = "⚠ 不该给指令"; wrong_cmd.append((text, got_cmd, r.say))
    elif passed and want_cmd:
        mark = "✓"
    elif (not want_cmd):
        mark = "✓" if (r.say or "").strip() else "⚠ 无回复"
        if not (r.say or "").strip():
            boring.append(text)
    else:
        mark = "⚠ 该纠错却没纠出"; miss_cmd.append((text, r.say))
    print("  %-32s %6.1fs  %-28s %s  [%s]"
          % (text, dt, verdict[:28], mark, note))

print()
print("=" * 78)
print(" 汇总")
print("=" * 78)
print("⚠ 幻觉被瞎猜成指令（危险，应为 0）：", len(wrong_cmd))
for t, c, s in wrong_cmd:
    print("     %-24r → %s  say=%r" % (t, c, s))
print("⚠ 该纠错却没纠出：", len(miss_cmd))
for t, s in miss_cmd:
    print("     %-24r say=%r" % (t, s))
print("⚠ 该闲聊却无回复：", len(boring))

st = ctrl.llm_status()
print()
print("stats:", st.get("stats"))
print()
ctrl.shutdown_llm()
print("已关闭 LLM")
