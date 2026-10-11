#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_engine.py —— 小模型做「指令纠错」与「拟人化对话」（**本地 llama.cpp 或 云端 API**）

## 为什么需要它

项目重点是**人机交互**。目前 ASR（SenseVoice）有时会听错，或用户说了规则库覆盖不到的句子，
`parse_text_command()` 返回 None → 界面只能回一句「没听懂」，交互体验很生硬。
本模块补上两块能力：

  ① **指令纠错**：把听错/口语化的文本映射回合法指令。
     例：「钱进一米」→ forward 1000mm；「往左边拐个弯」→ turn_left；
         「开慢点儿」→ speed_down。
  ② **拟人化对话**：确实不是指令时，回一句自然、简短的中文，而不是「没听懂」。

## 设计原则（按重要性排序）

1. **安全第一：急停绝不经过 LLM。**
   `car_gui_core` 的规则快路径**先跑**，急停/停止类词在那里就命中并返回了，不会到这里。
   本模块**再**做一次停止词检查作为兜底；即便真的漏到 LLM，
   `ALLOW_STOP_ACTIONS=False` 也会把 `stop` 从允许动作里剔除 → LLM **不可能让车停或动**。

2. **LLM 只在"规则没命中"时才被调用。** 快路径永远优先，保证常用指令零延迟。

3. **绝不让 LLM 拖慢安全链路。** 调用带超时（默认 12s），超时即放弃并退回原提示。

4. **两种后端，同样的安全约束。**
   - `mode="server"` / `"cli"` → **本地离线**（llama.cpp + Qwen2.5 GGUF），不联网；
   - `mode="api"` → **云端 OpenAI 兼容接口**（默认智谱 GLM-4-Flash），需联网与 API key。
   2026-09-29 板上实测对比：

   | 后端 | 单次问答耗时 | 说明 |
   |---|---|---|
   | 本地 server（Qwen2.5-0.5B Q4） | **16~35 秒** | 首问 35s，之后 16s；本板 prompt 处理仅约 5 tok/s |
   | **云端 GLM-4-Flash** | **2.7~4.5 秒** | 快 5~10 倍，理解力也强得多 |

   ⚠️ 换后端**不放松任何安全规则** —— 云端回复同样要过
   `_parse_and_validate()`：动作白名单 + **「动作必须在用户原话里有字面证据」的关键词闸门**。
   云端 LLM 一样**无权停车**（`stop` 不在 `ALLOWED_ACTIONS` 里）。

## 为什么用 llama-cli 子进程而不是 llama-cpp-python

- 板子内存只剩 ~1.3G、CPU 3 核 1GHz，`llama-cpp-python` 要额外装并重新编 wheel，收益不明确；
- `llama-cli` 是编译 llama.cpp 的直接产物，**进程隔离**：崩了/卡了不影响 GUI 主进程，
  被 kill 也不留残余状态。超时控制用 `subprocess.timeout` 天然干净。

## 为什么默认用 llama-server 常驻，而不是每次起 llama-cli

2026-09-27 在板上实测（3 核 1GHz）得出的硬数据：

| 方式 | 表现 |
|---|---|
| `llama-cli` 每次起进程 | 约 100 token 的**短**提示就要 **20.5 秒**；460 token 约 **50 秒** |
| 原因 | prompt 处理只有约 **5 tok/s**，**每问一次都要把系统提示重算一遍** |
| `--prompt-cache` | **当前版本已移除**（`error: invalid argument`），文件缓存这条路断了 |
| ✅ `llama-server` 常驻 | 模型加载一次、**系统提示 KV 缓存复用**（`--cache-reuse`），后续问答只算新内容 |

→ 所以引擎**默认**走 server（`mode="server"`）；万一 server 起不来会自动退回
`mode="cli"`（功能一致，只是慢）。

## 用法

    from llm_engine import LlmEngine
    eng = LlmEngine()
    ok, msg = eng.load()                      # 探测模型与 llama-cli
    d = eng.ask("钱进一米")                     # -> LlmReply(cmd=..., say=..., ...)
    print(d.cmd, d.say)

命令行自测：

    bash run_py.sh llm_engine.py --selftest       # 跑内置用例（本地）
    bash run_py.sh llm_engine.py --ask "钱进一米"
    bash run_py.sh llm_engine.py --bench          # 测 token/s

    # 云端（需先配 key，见下）
    export CAR_LLM_API_KEY='...'
    bash run_py.sh llm_engine.py --mode api --selftest

配置云端 key（二选一，**别把 key 写进代码库**）：

    export CAR_LLM_API_KEY='你的key'

    # 或写配置文件（权限 600）
    mkdir -p ~/.config/car_llm
    printf '{"key":"你的key","model":"glm-4-flash"}' > ~/.config/car_llm/api.json
    chmod 600 ~/.config/car_llm/api.json
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

# ── 模型与运行时位置 ────────────────────────────────────────────────────
# 按优先级探测；允许用环境变量 CAR_LLM_MODEL / CAR_LLAMA_CLI 覆盖。
HOME = os.path.expanduser("~")
MODEL_DIRS = (
    os.path.join(HOME, "models", "llm"),
    os.path.join(HOME, "models"),
    "/home/HwHiAiUser/models/llm",
)
LLAMA_CLI_CANDIDATES = (
    os.path.join(HOME, "llama.cpp", "build", "bin", "llama-cli"),
    "/home/HwHiAiUser/llama.cpp/build/bin/llama-cli",
    "/usr/local/bin/llama-cli",
)
LLAMA_SERVER_CANDIDATES = (
    os.path.join(HOME, "llama.cpp", "build", "bin", "llama-server"),
    "/home/HwHiAiUser/llama.cpp/build/bin/llama-server",
    "/usr/local/bin/llama-server",
)

#: 优先选用的量化（Q4_K_M 体积/质量平衡最好；Q4_0 次之）
QUANT_PREFERENCE = ("q4_k_m", "q4_0", "q5_k_m", "q8_0")

#: GBNF 语法约束：**强制**模型输出符合我们 schema 的单行 JSON。
#:
#: 为什么必须要它（2026-09-27 实测）：
#:   0.5B 这种小模型**几乎没有指令遵循能力** —— 给它写"只输出一行 JSON"，
#:   它会复读提示词里的字段说明（实测 4/4 条都回 `say='简短中文回复'`）。
#:   而 llama.cpp 原生支持 GBNF 语法约束，能在**采样层面**把输出钉死在合法结构上，
#:   与模型大小无关。这是小模型上做结构化输出**最可靠**的办法。
#:
#: ⚠️⚠️ **每条规则必须写在一行里！**
#:   用反斜杠续行把一条规则拆成多行 → llama.cpp 报
#:   `terminate called ... what(): failed to parse grammar` 并**直接崩溃**（踩过）。
#:   这是本文件最容易改错的地方。
#: ⚠️ 代价：语法掩码让生成速度从约 8.9 → **3.8 tok/s**（实测，1GHz 3 核）。
#: ⚠️ 改 schema 时语法要同步改，否则模型想给的字段会被语法挡掉。
GBNF_GRAMMAR = (
    r'root ::= "{" ws "\"cmd\"" ws ":" ws cmd ("," ws extra)* ws "}"' "\n"
    r'cmd ::= "\"forward\"" | "\"backward\"" | "\"turn_left\"" | "\"turn_right\"" | "\"speed_up\"" | "\"speed_down\"" | "\"set_speed\"" | "\"chat\""' "\n"
    r'extra ::= numfield | sayfield' "\n"
    r'numfield ::= "\"" numkey "\"" ws ":" ws number' "\n"
    r'numkey ::= "confidence" | "distance_mm" | "duration_s" | "speed"' "\n"
    r'sayfield ::= "\"say\"" ws ":" ws string' "\n"
    r'number ::= "-"? [0-9]+ ("." [0-9]+)?' "\n"
    r'string ::= "\"" char* "\""' "\n"
    r'char ::= [^"\\] | "\\" ["\\/bfnrt]' "\n"
    r'ws ::= [ ]?' "\n"
)


# ══════════════════════════════════════════════════════════════════════
#  安全与合法性约束（改这里前先读模块 docstring 的"设计原则 1"）
# ══════════════════════════════════════════════════════════════════════

#: LLM 允许下发的动作 —— **刻意不含 stop**。
#: 理由：急停/停止由规则层负责，绝不能让一个概率模型去决定"停车"这种安全动作。
ALLOW_STOP_ACTIONS = False
ALLOWED_ACTIONS = (
    "forward", "backward", "turn_left", "turn_right",
    "set_speed", "speed_up", "speed_down",
)

#: 🔴 **关键词闸门**（2026-09-27 加，**这是本轮最重要的安全设计**）
#:
#: 背景：拿 14 条**真实 ASR 输出**实测，0.5B 有强烈的"拿不准就猜动作"偏置：
#:   '能。'→forward   '是是。'→forward   '开玩去啊。'→forward
#:   '今天天气怎么样'→speed_down   '操哼。'→speed_up
#: 试过两条路都**失败**：
#:   ① 补 few-shot 示例 → 无效（仍 9/14 误判）；
#:   ② 抬高置信度门槛到 0.95 → 不行，因为**置信度区分不了**：
#:      垃圾输入自称 0.9~0.95，而合法口语（'开慢点儿'）也只有 0.90。
#:
#: → 最终方案：**要求动作语义在用户原话里有"字面证据"**。
#:   LLM 返回的 cmd 必须能在原话里找到下表里任意一个词，否则**丢弃该指令**。
#:   例：`'是是。'` 里找不到"前进/往前/…" → **永远不可能变成 forward**；
#:       `'钱进一米'` 含"米" → 允许 forward；`'开慢点儿'` 含"慢" → 允许 speed_down。
#:
#: ⚠️ 增删这个词表前先想清楚：**放宽 = 多识别，也 = 多误动**。
#:    漏掉只是"没反应"，乱动是"车自己跑了"。
ACTION_EVIDENCE = {
    # ⚠️ 刻意**不含"开"**：它出现在"开玩去啊""开玩笑"里，会放行垃圾输入（实测踩到）。
    #    距离词（米/m）算证据：'钱进一米' 靠"米"通过。
    "forward":    ("前进", "往前", "向前", "前开", "直行", "走", "米", "同"),
    "backward":   ("后退", "退后", "往后", "向后", "倒车", "倒", "退"),
    "turn_left":  ("左转", "左拐", "往左", "向左", "逆时针", "左"),
    "turn_right": ("右转", "右拐", "往右", "向右", "顺时针", "右"),
    "speed_up":   ("快", "加速", "提高"),
    "speed_down": ("慢", "减速", "降低", "降"),
    "set_speed":  ("速度", "时速"),
}

#: 给出指令所需的最低置信度。
#:
#: ⚠️ 这个值**单靠它保证不了安全** —— 实测垃圾输入也会自称 0.9~0.95。
#: 真正的闸门是上面的 `ACTION_EVIDENCE`；这里只做一道粗筛（挡掉明显没底的）。
#: 实测合法纠错最低是 0.90，所以取 0.8 不会误伤；**别再往上抬** ——
#: 抬到 0.95 时合法的 '开慢点儿'(0.90) 会被一起丢掉（踩过）。
LLM_CMD_MIN_CONFIDENCE = 0.8

#: 兜底停止词：即使规则层漏了，这里也会拒绝执行任何动作（只回话）。
#: 与 car_gui_core._STOP_WORDS 保持一致的语义。
_STOP_WORDS = ("急停", "停止", "停下", "停车", "别动", "刹车", "不要动", "停")

SPEED_MIN, SPEED_MAX = 50, 500
DURATION_MIN, DURATION_MAX = 0.1, 30.0

# ── 提示词 ──────────────────────────────────────────────────────────────
#  🔴 **必须短！**（2026-09-27 实测的硬约束）
#    本板 prompt 处理约 **16 tok/s**（1GHz 3 核），生成约 **8.9 tok/s**。
#    早期写了一份约 1200 token 的"详细"系统提示 → 光读提示就要 75 秒以上，
#    每次都超时（实测 5 条全挂在 60s）。
#    → 现在压到约 **250 token**，端到端才能进到十几秒。**改提示词前先算这笔账。**
#
#  同时：小模型（0.5B）**指令遵循很弱**，示例比"讲道理"有效得多。
#  真要提升纠错率，正确做法是**把真实识别错的句子加进 EXAMPLES**，而不是加长规则。
#  同一条实测推论：**提示词每多 100 字 ≈ 多等 15~20 秒**（server 模式第一次请求）。
#  所以这里用"电报体"，一切能省的都省掉。改长之前请先用 `--bench` 量一次。
#
#  同时：小模型（0.5B）**指令遵循很弱**，示例比"讲道理"有效得多。
#  真要提升纠错率，正确做法是**把真实识别错的句子加进示例**（用户说错的每一句
#  都可以变成一条示例），而不是加长规则说明。
SYSTEM_PROMPT = """判断车主想干什么，只输出一行JSON。
cmd: forward前进 backward后退 turn_left左转 turn_right右转 speed_up加速 speed_down减速 chat聊天
只有话里明确说了前进/后退/转/快/慢才用对应cmd；听不清、闲聊、说不通一律chat。
示例：
钱进一米→{"cmd":"forward","distance_mm":1000,"confidence":0.95,"say":"好，往前开一米"}
开慢点儿→{"cmd":"speed_down","confidence":0.9,"say":"好，慢一点"}
你叫什么名字→{"cmd":"chat","confidence":0.95,"say":"我是你的智能小车呀"}
今天天气怎么样→{"cmd":"chat","confidence":0.9,"say":"我只看得到眼前的路"}
是是。→{"cmd":"chat","confidence":0.8,"say":"没听清，再说一遍吧"}
哎呀，操。→{"cmd":"chat","confidence":0.8,"say":"没听清，再说一遍吧"}
"""


# ══════════════════════════════════════════════════════════════════════
#  外部 LLM（云端 API）—— 2026-09-29 新增
# ══════════════════════════════════════════════════════════════════════
#
#  为什么加云端后端：本板本地小模型（0.5B Q4）实测**每次问答 16~35 秒**，
#  交互上"问一句等半分钟"体验很差；而板子现在能独立上网（AX210 WiFi），
#  实测云端 GLM-4-Flash **2.7~4.5 秒**返回，快 5~10 倍，且理解力强得多。
#
#  ★ 安全约束**完全不变**：云端回复仍要过同一套 `_parse_and_validate()`，
#    也就是那道"**动作必须在用户原话里有字面证据**"的关键词闸门 + 动作白名单。
#    换句话说：换后端**不放松任何一条安全规则**，云端 LLM 一样无权停车。
#
#  ★ 零依赖：只用标准库 `urllib.request`（板上没装 requests，也不打算装）。
#
#  凭据只从**环境变量或权限 600 的配置文件**读，**不写进代码库**。

#: OpenAI 兼容的 chat/completions 路径（拼在 base 之后）
API_CHAT_PATH = "/chat/completions"

#: 读 key 的环境变量名（按优先级）
API_KEY_ENVS = ("CAR_LLM_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY",
                "ZHIPU_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")

#: 配置文件位置（按优先级）：JSON，形如
#:   {"base": "https://open.bigmodel.cn/api/paas/v4", "key": "...", "model": "glm-4-flash"}
API_CONFIG_PATHS = (
    os.path.join(HOME, ".config", "car_llm", "api.json"),
    os.path.join(HOME, ".car_llm_api.json"),
    "/etc/car_llm/api.json",
)

#: 默认后端（智谱 GLM-4-Flash：免费额度、国内直连 ~0.4s、0.97s 出结果）
API_DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
API_DEFAULT_MODEL = "glm-4-flash"


def api_config(base=None, key=None, model=None, paths=None):
    """解析云端 LLM 配置。返回 dict(base,key,model,source)；**永不抛异常**。

    优先级：显式入参 > 环境变量 > 配置文件 > 内置默认。
    `key` 为空表示没配好 —— 调用方据此判断能否用 api 模式。
    """
    cfg = {"base": API_DEFAULT_BASE, "key": "", "model": API_DEFAULT_MODEL,
           "source": ""}

    # 1) 配置文件（先读，好让环境变量能覆盖它）
    import glob as _glob
    cand = list(paths) if paths else list(API_CONFIG_PATHS)
    # 允许 ~/.config/car_llm/*.json 里放多个后端
    try:
        cand += sorted(_glob.glob(os.path.join(HOME, ".config", "car_llm", "*.json")))
    except Exception:
        pass
    for p in cand:
        try:
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if not isinstance(obj, dict):
                continue
            cfg["base"] = str(obj.get("base") or obj.get("base_url")
                              or cfg["base"]).strip()
            cfg["key"] = str(obj.get("key") or obj.get("api_key") or "").strip()
            cfg["model"] = str(obj.get("model") or cfg["model"]).strip()
            cfg["source"] = p
            break
        except Exception:
            continue

    # 2) 环境变量覆盖
    ebase = os.environ.get("CAR_LLM_API_BASE") or os.environ.get("OPENAI_BASE_URL")
    if ebase:
        cfg["base"], cfg["source"] = ebase.strip(), "env:CAR_LLM_API_BASE"
    emodel = os.environ.get("CAR_LLM_API_MODEL") or os.environ.get("OPENAI_MODEL")
    if emodel:
        cfg["model"] = emodel.strip()
    if not cfg["key"]:
        for e in API_KEY_ENVS:
            v = os.environ.get(e)
            if v and v.strip():
                cfg["key"] = v.strip()
                cfg["source"] = "env:%s" % e
                break

    # 3) 显式入参最高优先级
    if base:
        cfg["base"] = base.strip()
        cfg["source"] = "arg"
    if model:
        cfg["model"] = model.strip()
    if key:
        cfg["key"] = key.strip()
        cfg["source"] = "arg"

    cfg["base"] = cfg["base"].rstrip("/")
    return cfg


def api_available(**kw):
    """云端后端是否可用（配了 key 就算可用，不去联网探测）。"""
    return bool(api_config(**kw).get("key"))


def api_reachable(cfg=None, base=None, timeout=3.0):
    """快速探测云端是否真的连得上（有 key 且网络通）。

    只做一次很短的请求，用于 `mode="auto"` 决策。
    **任何异常都返回 False**（宁可回退本地，也不要卡住界面）。
    """
    import urllib.request
    import urllib.error
    c = dict(cfg) if cfg else api_config(base=base)
    if not c.get("key"):
        return False
    url = c["base"].rstrip("/") + API_CHAT_PATH
    body = json.dumps({
        "model": c["model"],
        "messages": [{"role": "user", "content": "1"}],
        "max_tokens": 1,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + c["key"],
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        # 400/401/402/403/429 都说明**网络是通的**（只是 key/额度/参数问题）
        return e.code in (400, 401, 402, 403, 429)
    except Exception:
        return False


def pick_mode(mode="auto", local="server", cfg=None):
    """决定用哪个后端。返回 "api" / "server" / "cli"。

    - `"api"`            → 强制云端（配置不全时由 `load()` 报错）
    - `"server"`/`"cli"` → 强制本地
    - `"auto"`           → **有 key 且网络通就用云端**，否则回退本地
      （★ 这样"没配 key"或"没网"的板子行为与以前完全一致 —— 不会因为
        加了云端支持就把离线场景弄坏）
    """
    if mode in ("server", "cli", "api"):
        return mode
    c = cfg if cfg is not None else api_config()
    if c.get("key") and api_reachable(cfg=c):
        return "api"
    return local


#: 云端版的系统提示。与本地版（`SYSTEM_PROMPT`）的区别：
#:   ① 云端理解力强，不需要"电报体"，可以讲清规则（**只有本地模式才受 token 速度约束**）；
#:   ② 明确要求 **不动作时必须 `cmd=null`**（本地版用 `"chat"`，两者在 `_parse_and_validate`
#:      里都会被判为"无指令"，但云端返回 null 更干净）；
#:   ③ ★ 修掉实测发现的问题：模型爱说"**已停车**""**左转完成**"这类
#:      **假称已完成**的话（其实只是打算做，甚至根本没做）→ 明确禁止。
SYSTEM_PROMPT_API = """你是一台麦克纳姆轮智能小车的语音交互助手。用户的输入来自语音识别（ASR），\
可能包含错别字、多字少字、同音字错误或语序混乱，你要理解他的**真实意图**。

【输出格式】只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块：
{
  "cmd": 动作或 null,
  "speed": 整数或 null,
  "distance_mm": 整数或 null,
  "confidence": 0.0~1.0,
  "say": "给用户的一句中文口语回复"
}

【cmd 只能取以下之一，其它一律 null】
- "forward"     前进
- "backward"    后退
- "turn_left"   左转
- "turn_right"  右转
- "speed_up"    加速
- "speed_down"  减速

【最重要的规则 —— 宁可 null，不可猜】
只有当用户原话里**字面上**出现了移动意图（如"前进/走/开/后退/退/左转/右转/拐弯/快一点/慢一点"
及其同音误识别）时，才给对应 cmd。以下情况**必须** `cmd: null`：
- 闲聊、提问、寒暄（如"今天天气怎么样""你叫什么名字"）
- 语义不通或明显是识别垃圾（如"能。""是是。"）
- 与移动无关的指令（如"放首歌""停车"）—— 注意**"停车/停下/急停"不在你的权限内**，
  这类词由硬件急停链路处理，你只能回话，**绝不能给 cmd**
- 你只是"觉得可能是"想移动，但话里没有明确字眼

【speed / distance_mm】
- 仅当 cmd 非 null 时才有意义，否则填 null。
- speed 范围 50~500，用户没提速度就别填（填 null）。
- 用户说"一米"就是 1000，"半米"是 500，没提距离就填 null。

【关于 [当前状态] —— 请严格遵守】
用户消息末尾可能附带一段 `[当前状态]…`，如"尚未连接小车"或"已连接小车，当前速度300mm/s"。
🔴🔴 **无论 `[当前状态]` 写的是什么，你都照常按用户原话判断 `cmd`。**
   `[当前状态]` 只影响 `say` 的措辞，**绝不影响 `cmd` 的取值**。
   - ❌ 错误做法：看到"尚未连接小车"就把 `cmd` 填 null、然后说"还没连上车呢"。—— **这是错的！**
   - ✅ 正确做法：用户说"前进一米"，就填 `cmd:"forward","distance_mm":1000`，
     再在 `say` 里补一句当前状态（如"好，等连上小车就往前开一米"）。
   **理由：你的唯一职责是"听懂用户想干什么"。至于此刻能不能真的执行，
   由调用你的程序按状态自行决定 —— 那不是你该管的事，你也不该替它拒绝。**

【say 字段怎么写 —— 必须填，不能为空】
- **每次都必须给 `say`**，中文口语，像朋友说话。
- 🔴🔴 **长度硬约束：最多 12 个汉字（含标点），越短越好，别超！**
  为什么（2026-10-09 实测标定）：本车 TTS 走昇腾 NPU，**只支持静态 shape**，
  一次合成固定 250 梅尔帧 ≈ 2.9 秒音频。实测 12 字的句子稳定在 180~211 帧
  （单段、顺滑），而 **14 字最高会顶到 250 帧上限**（被截断或被迫切段）。
  超限就要把句子切成多段分别合成，**每多一段多约 1 秒停顿**。
  ⇒ 你写 `say` 时**必须自己控制在 12 字内**，这是对听感影响最大的一条。
  - ❌ 太长（16~18 字，会被切成 2~3 段、明显卡顿）：
    "检测到障碍物，请小心避让注意安全"、"你好，我是你的智能小车，很高兴见到你"
  - ✅ 短（≤12 字，一次合成、顺滑）：
    "检测到障碍物，请避让"、"你好，我是智能小车"
  - 想说两件事时，**只挑最重要的一件说短**，不要叠成两句。
- 🔴🔴 **绝对禁止说"已完成""已执行""已经…了""已停车""已收到指令"** ——
  你**只是理解了意图**，车**还没有动**（甚至可能根本没连接、没有权限）。
  必须描述**将要做什么**或**说明状况**。
  - ❌ "已停车"   ❌ "正在前进一米，请稍等"（"正在"也算假称）
  - ✅ "好，往前开一米"   ✅ "停车请用急停按钮"
- 🔴 **绝不编造事实**：不要报天气、温度、时间等你无从得知的信息，
  也不要说"已为你打开/关闭"任何东西。
- 🔴🔴 **不要重复自己**：用户消息末尾可能带 `[最近说过]…`，
  里面是你**最近几次的回复**。你这次的 `say` 必须与之**不同**。
  - ⚠️ **换个助词不算不同**："我是你的智能小车" → "嗨，我是你的小车"
    这种**近义改写仍然是复读**，用户听得出来。要**换一个切入角度**：
      · 自我介绍 → 也可用"你说了算，我开就是"、"随叫随到的小车"、
        "专治开车难题"、"我叫什么好呢？"
      · 做不到某事 → 也可用"这个真帮不上"、"超出我的本事啦"、
        "你另请高明吧"、"我只会开车哦"
  - 同一个问题被反复问时，**每一次都要明显不同**（句式、角度、语气都换）。
- `cmd: null` 时，针对用户这句话自然闲聊（可带一点小车人格）。
- 识别垃圾/听不清时，**也要换着说**，不要每次都"没听清，再说一遍吧"，
  可轮换："没听清，再说一遍"、"这句没懂呢"、"再说一次好吗" 等（都要 ≤12 字）。

【示例】
"钱进一米"        → {"cmd":"forward","distance_mm":1000,"speed":null,"confidence":0.95,"say":"好，往前开一米"}
"往左边拐个弯"     → {"cmd":"turn_left","speed":null,"distance_mm":null,"confidence":0.95,"say":"好，往左转"}
"开慢点儿"        → {"cmd":"speed_down","speed":null,"distance_mm":null,"confidence":0.9,"say":"好，慢一点"}
"退后半米"        → {"cmd":"backward","distance_mm":500,"speed":null,"confidence":0.95,"say":"好，往后退半米"}
"能。"           → {"cmd":null,"speed":null,"distance_mm":null,"confidence":0.9,"say":"这句没懂呢"}
"今天天气怎么样"   → {"cmd":null,"speed":null,"distance_mm":null,"confidence":0.9,"say":"我看不到天气呀"}
"你叫什么名字"     → {"cmd":null,"speed":null,"distance_mm":null,"confidence":0.95,"say":"我是智能小车"}
"停车"           → {"cmd":null,"speed":null,"distance_mm":null,"confidence":0.95,"say":"停车请用急停按钮"}
"帮我放首歌"       → {"cmd":null,"speed":null,"distance_mm":null,"confidence":0.9,"say":"我还没学会放歌"}
"""


#: `say` 的**汉字上限**。
#:
#: 🔴 为什么卡 **12**（2026-10-09 实测标定，不是拍的）：
#:   本车 TTS 在昇腾 NPU 上是**静态 shape**，一次合成固定 **250 梅尔帧 ≈ 2.9s 音频**。
#:   超过就要把句子切成多段分别合成，**每多一段多约 1.1 秒停顿**。
#:   实测各字数的真实帧数（合成后量音频时长 × 86.13）：
#:       8 字  →  89~172 帧   ✅ 单段
#:      10 字  → 146~182 帧   ✅ 单段
#:      11 字  → 159~217 帧   ✅ 单段
#:      12 字  → 180~211 帧   ✅ 单段
#:      13 字  → 176~205 帧   ✅ 单段
#:      **14 字 → 最高顶到 250 帧上限（=被截断）** ⚠
#:   语速因人而异，14 字在"说得慢"时就会超限，所以**留一档余量取 12**。
#:   ⇒ 让云端 LLM 自己把回复写短，是从源头消除停顿的唯一零成本办法。
SAY_MAX_CHARS = 12


def cap_say_length(say, limit=SAY_MAX_CHARS):
    """把 `say` 压到 `limit` 个汉字以内（服务端兜底，**绝不产生破句**）。

    🔴 为什么光靠提示词和 schema 不够（2026-10-09）：
      云端模型**不保证**遵守 `maxLength`（实测 GLM 常给 18~27 字），
      而超过 12 字就接近 250 帧上限、有被切成多段的风险 → 每段多约 1 秒停顿。
      所以这里做**最后一道保险**。

    🔴 为什么不直接硬切（2026-10-09 踩过）：
      硬切会切出**破句** —— 实测出现过
      `"嗨，我是小车，目前还不知道天"`（后半句被砍掉，听起来像坏了）。
      这比停顿更糟。所以策略是：
        ① 若在**标点处**能断开且前半句够长 → 用它（自然）；
        ② 否则**换成一条预置的安全短句**（一定完整、一定 ≤12 字），
           并且**轮换使用**，避免每次都同一句。
    """
    s = " ".join((say or "").split()).strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    # ① 标点处断开，且前半句至少要有一半长度（避免只剩"你好"）
    for i in range(len(head) - 1, max(0, limit // 2) - 1, -1):
        if head[i] in "，。！？、；：,.!?;:":
            cut = head[:i].strip()
            # 前半句至少要 4 个字，否则太短、信息量不够
            if len(cut) >= 4:
                return cut
    # ② 无法自然断开 → 用预置短句兜底（轮换，避免总是同一句）
    return _pick_fallback()


#: 预置的安全短句（都 ≤12 字、语义完整、通用）。
#: 当模型给的回复太长**且切不出自然半句**时用它兜底 ——
#: 宁可换一句短的，也不要播一句破的。
_SAY_FALLBACKS = (
    "这句还没想好怎么说",
    "让我想想再回答你",
    "这个我还不太懂呢",
    "换个说法再说一次吧",
    "我还在学习中呢",
    "说点别的吧",
)
_say_fb_lock = threading.Lock()
_say_fb_i = [0]


def _pick_fallback():
    """轮换取一条预置短句（保证不重复、且一定完整）。"""
    with _say_fb_lock:
        i = _say_fb_i[0] % len(_SAY_FALLBACKS)
        _say_fb_i[0] += 1
        return _SAY_FALLBACKS[i]


#: 云端 function-calling 的工具定义。
#:
#: ★ **为什么要用 tools 而不是 `response_format=json_object`**（2026-09-29 实测）：
#:   用 json_object 时，模型会把"`[当前状态]` 里写着尚未连接小车"理解成
#:   **"不许给指令"**，于是把 `cmd` 填 null、只回一句"还没连上车呢" ——
#:   实测**4 条该纠错的指令全丢**（钱进一米/退后半米/开慢点儿/往左边拐个弯）。
#:   改成 function-calling 后，`cmd` 是 schema 的**必填字段**，模型**必须填出来**，
#:   "用闲聊代替填字段"这条路被堵死 → **9/9 全对**，而且更快（1~2.5s）。
API_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "car_command",
        "description": "汇报对用户这句话的理解结果（必须调用）",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": ["string", "null"],
                    "enum": ["forward", "backward", "turn_left",
                             "turn_right", "speed_up", "speed_down", None],
                    "description": ("用户的移动意图。没有明确移动意图时必须为 null。"
                                    "★ 不受 [当前状态] 影响：即使尚未连接小车，"
                                    "只要用户说了移动意图也要照实填。"),
                },
                "speed": {"type": ["integer", "null"],
                          "description": "50~500；用户未提及则为 null"},
                "distance_mm": {"type": ["integer", "null"],
                                "description": "距离（毫米）；未提及则为 null"},
                "confidence": {"type": "number",
                               "description": "对 cmd 判断的把握，0~1"},
                "say": {"type": "string",
                        "maxLength": SAY_MAX_CHARS,
                        "description": (
                            "给用户的一句中文口语回复，**最多 %d 个汉字**"
                            "（本车 TTS 是昇腾 NPU 静态 shape，单次只能合成约 %d 字，"
                            "超了会被切成多段、每段多约 1 秒停顿 → 听感一顿一顿）。"
                            "越短越好；且**不要与 [最近说过] 里的重复**。"
                            "绝不能说'已完成/已停车/正在…'，"
                            "只能说要做什么或说明状况。"
                            % (SAY_MAX_CHARS, SAY_MAX_CHARS))},
            },
            "required": ["cmd", "confidence", "say"],
        },
    },
}


class LlmReply(object):
    """LLM 的一次答复（已通过安全校验）。"""

    __slots__ = ("cmd", "speed", "distance_mm", "duration_s", "confidence",
                 "say", "raw_say", "raw", "elapsed", "from_cache", "error")

    def __init__(self, cmd=None, speed=None, distance_mm=None, duration_s=None,
                 confidence=0.0, say="", raw="", elapsed=0.0,
                 from_cache=False, error=""):
        self.cmd = cmd
        self.speed = speed
        self.distance_mm = distance_mm
        self.duration_s = duration_s
        self.confidence = confidence
        # ★★ 最后一道保险：**不管从哪条路径来，`say` 一律截到 ≤SAY_MAX_CHARS 字**。
        #    为什么放在构造器里（2026-10-09 踩过）：
        #    我原来只在 `_parse_and_validate` 的**正常路径**加了截断，
        #    结果"模型没返回 JSON"那条**回退路径**漏了 → 实测冒出 19~27 字的
        #    回复，照样被 TTS 切成多段。放在这里就**不可能漏**。
        self.say = cap_say_length(say)
        #: 未经截断的原始 `say` —— 用于"是否复读"的比较
        #: （直接拿截断后的值比会误判，2026-10-09 踩过）
        self.raw_say = say
        self.raw = raw
        self.elapsed = elapsed
        self.from_cache = from_cache
        self.error = error

    @property
    def has_cmd(self):
        return self.cmd in ALLOWED_ACTIONS

    def __repr__(self):
        return ("LlmReply(cmd=%r, speed=%r, dist=%r, dur=%r, conf=%.2f, "
                "say=%r, %.2fs%s)" % (
                    self.cmd, self.speed, self.distance_mm, self.duration_s,
                    self.confidence, self.say, self.elapsed,
                    ", cached" if self.from_cache else ""))


# ══════════════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════════════

def find_llama_cli():
    """找出可用的 llama-cli；找不到返回 None。"""
    env = os.environ.get("CAR_LLAMA_CLI")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    for p in LLAMA_CLI_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("llama-cli")


def find_llama_server():
    """找出可用的 llama-server；找不到返回 None。"""
    env = os.environ.get("CAR_LLAMA_SERVER")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    for p in LLAMA_SERVER_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("llama-server")


def _free_port(preferred=8123):
    """找一个空闲端口（避免和网络音箱 8099 等冲突）。"""
    for p in range(preferred, preferred + 40):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            try:
                s.close()
            except Exception:
                pass
    return preferred


def _our_server_pattern():
    """
    用于识别"**我们自己**起的 llama-server"的 pkill 特征串。

    为什么不用 `pkill -f llama-server`：那会误杀用户手动起的、或别的项目用的
    llama-server。这里用"本模块的模型路径 + 本模块特有的参数"拼出一个
    精确特征，只清自己人。
    """
    model = find_model() or ""
    if model:
        return "llama-server.*%s" % re.escape(os.path.basename(model))
    return "llama-server.*cache-reuse"


def kill_stale_servers(log=None):
    """
    清掉**上一次运行残留**的 llama-server。返回被杀掉的数量。

    🔴 为什么必须有它（2026-09-27 实测）：
      GUI 被 `kill -TERM` 或崩溃时，Qt 的 `closeEvent` **不会执行** →
      `shutdown_llm()` 跑不到 → 常驻 llama-server 变成**孤儿**继续占着
      **约 600MB 内存 + CPU**。本板内存只有 3.4G（可用约 1.3G），
      **每重启一次 GUI 就累积一个**，几个孤儿就能把 CPU 抢干、
      把 LLM 延迟从 15 秒拖到超时。
      → 所以每次启动 server 前先自清理一遍（幂等，没孤儿时是 no-op）。
    """
    pat = _our_server_pattern()
    n = 0
    try:
        p = subprocess.run(["pgrep", "-f", pat], stdout=subprocess.PIPE)
        n = len([x for x in p.stdout.decode().split() if x.strip()])
        if n:
            subprocess.run(["pkill", "-f", pat],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
    except Exception as e:
        if log:
            log("清理残留 llama-server 失败（忽略）：%r" % (e,))
        return 0
    if n and log:
        log("已清理 %d 个上次残留的 llama-server（否则会抢 CPU）" % n)
    return n


def find_model():
    """按优先级找出一个 .gguf 模型；找不到返回 None。"""
    env = os.environ.get("CAR_LLM_MODEL")
    if env and os.path.isfile(env):
        return env
    found = []
    for d in MODEL_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".gguf"):
                found.append(os.path.join(d, name))
    if not found:
        return None
    # 先按量化偏好排序，再按体积（小的快）
    def rank(p):
        low = os.path.basename(p).lower()
        for i, q in enumerate(QUANT_PREFERENCE):
            if q in low:
                return (i, os.path.getsize(p))
        return (len(QUANT_PREFERENCE), os.path.getsize(p))
    found.sort(key=rank)
    return found[0]


def _repair_json(s):
    """对 LLM 常见的 JSON 毛病做**外壳修复**（不动语义）。

    2026-10-11 新增：用户报"闲聊时偶发 无有效 JSON"。
    云端模型偶尔会吐这些变体，导致 `json.loads` 失败：
      · markdown 围栏  ```json ... ```
      · 尾随逗号       {"a": 1,}
      · 注释           // ...  /* ... */
      · **被 max_tokens 截断**（少几个右花括号）—— 由调用方补
    """
    if not s:
        return s
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"//[^\n]*", "", s)                 # 行注释
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)    # 块注释
    s = re.sub(r",\s*([}\]])", r"\1", s)           # 尾随逗号
    return s.strip()


#: 从（哪怕结构损坏的）输出里**抢救 `say` 字段**。
#: 🔴 为什么需要它：JSON 外壳坏掉时，模型其实**已经把话说出来了** ——
#:    丢掉它去回"没听懂"是最差的兜底（用户就是这么看到那句报错的）。
_SAY_RE = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)
#: 没有 say 字段时，退一步抓"整段里最像 JSON 字符串值"的内容
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.){2,})"')


def extract_say(text):
    """尽最大努力从模型输出里抠出"要说的话"。找不到返回 ""。"""
    if not text:
        return ""
    m = _SAY_RE.search(text)
    if m:
        try:
            return json.loads('"%s"' % m.group(1)).strip()
        except Exception:
            return m.group(1).strip()
    return ""


def extract_json(text):
    """从模型输出里抠出第一个 JSON 对象。失败返回 None。

    ★ 2026-10-11 加固：解析失败时会再试几种**常见变体**，包括
      **被截断的 JSON**（补右花括号）—— 云端偶发"无有效 JSON"多半就是这个。
    """
    if not text:
        return None
    # 去掉可能的 markdown 围栏
    text = re.sub(r"```(?:json)?", "", text)
    # 平衡花括号地找第一个 {...}
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        # ① 完整对象：原样 / 修复后
        if end != -1:
            seg = text[start:end + 1]
            for cand in (seg, _repair_json(seg)):
                try:
                    return json.loads(cand)
                except Exception:
                    pass
        # ② ★ 被截断的 JSON：补上缺失的右花括号（还有一个未闭合字符串的情况）
        else:
            seg = text[start:]
            cand = _repair_json(seg)
            if in_str:
                cand += '"'
            for extra in range(0, 4):
                try:
                    return json.loads(cand + ("}" * extra))
                except Exception:
                    continue
        start = text.find("{", start + 1)
    return None


def looks_like_stop(text):
    """兜底：文本里有停车语义吗？（规则层正常会先拦掉，这是第二道闸）"""
    t = text or ""
    return any(w in t for w in _STOP_WORDS)


def cmd_has_evidence(cmd, text):
    """
    动作语义在用户原话里有**字面证据**吗？

    这是防"0.5B 拿不准就猜动作"的**核心闸门**（见 ACTION_EVIDENCE 的注释）。
    `cmd` 不在表里（如 chat）视为无需证据。

        cmd_has_evidence("forward", "钱进一米")   -> True   （含"米"）
        cmd_has_evidence("forward", "是是。")     -> False  ★ 挡住瞎猜
        cmd_has_evidence("speed_down", "开慢点儿") -> True   （含"慢"）
    """
    if not cmd or cmd not in ACTION_EVIDENCE:
        return True
    t = (text or "").lower()
    return any(w.lower() in t for w in ACTION_EVIDENCE[cmd])


#: 判定"这句话像不像控车指令"用的词（比 ACTION_EVIDENCE 更宽，只用于**提醒用户**）。
#: 用途：界面可以据此提示"你开启了 LLM 纠错，但这句话里没有任何控车词，
#: 它只会被当作闲聊" —— 免得用户以为 LLM 坏了。
_COMMAND_HINT_WORDS = tuple(
    sorted({w for ws in ACTION_EVIDENCE.values() for w in ws}
           | {"停", "刹车", "急停", "速度"},
           key=len, reverse=True))


def looks_like_command(text):
    """这句话里有没有任何控车词？（**只用于界面提示，不做安全判断**）"""
    t = (text or "").lower()
    return any(w.lower() in t for w in _COMMAND_HINT_WORDS)


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


# ══════════════════════════════════════════════════════════════════════
#  引擎
# ══════════════════════════════════════════════════════════════════════

class LlmEngine(object):
    """本地小模型封装：纠错 + 对话。

    线程安全：内部用锁串行化推理（1GHz 3 核上并发只会互相抢），
    并带一层结果缓存（同一句话不重复算 —— 语音识别常出现重复误识别）。
    """

    def __init__(self, cli=None, model=None, n_ctx=1024, threads=3,
                 timeout=60.0, max_tokens=96, temp=0.3, cache_size=128,
                 grammar=True, mode="server", server_port=None, log=None,
                 api_base=None, api_key=None, api_model=None, use_tools=True):
        self._cli = cli
        self._model = model
        self.n_ctx = n_ctx
        self.threads = threads
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temp = temp
        self.cache_size = cache_size
        self.use_grammar = grammar
        self.mode = mode                      # "server" / "cli" / "api" / "auto"
        self._srv = None                      # llama-server 子进程
        self._srv_proc = None
        self._srv_port = server_port
        self._grammar_file = None
        self._log = log or (lambda *_a, **_k: None)
        self._lock = threading.Lock()
        self._cache = {}
        self._cache_order = []
        #: 最近几条 `say`（防复读用，见 `_remember_say` / `_build_prompt_api`）
        self._recent = []
        self._recent_max = 6
        #: 已经问过的"裸问题"（不含历史），用于检测复读并强制换说法
        self._asked_bare = set()
        self._loaded = False
        self._msg = ""
        self._err = ""
        # ── 云端后端（mode="api" 时用）──
        self._api = api_config(base=api_base, key=api_key, model=api_model)
        #: 云端是否用 function-calling 强约束输出（关掉则退回 json_object/纯提示词）
        self.use_tools = bool(use_tools)
        self._stats = {"calls": 0, "hits": 0, "timeouts": 0, "errors": 0,
                       "last_elapsed": 0.0, "total_elapsed": 0.0,
                       "server_ready": False}

    @property
    def api(self):
        """当前云端配置（只读快照，key 不回显）。"""
        c = dict(self._api)
        c["key"] = "已配置" if c.get("key") else ""
        return c

    def _ensure_grammar(self):
        """把 GBNF 写到临时文件（llama.cpp 的 --grammar-file 要文件路径）。"""
        if self._grammar_file and os.path.isfile(self._grammar_file):
            return self._grammar_file
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".gbnf", prefix="carcmd_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(GBNF_GRAMMAR)
        self._grammar_file = path
        return path

    # ── 生命周期 ────────────────────────────────────────────────────
    def load(self, background=False):
        """
        探测模型与运行时。server 模式下会**启动常驻 llama-server**并等它就绪。

        `background=True`：在后台线程里做启动（本板要几秒~几十秒，别冻界面）。
        之后用 `ready` 查询是否已就绪。

        返回 (ok, msg)：
          · background=True 时 ok 表示"已经开始启动"，不代表已就绪；
          · 重复调用是安全的 —— **已启动的 server 不会被重启**
            （重启会丢掉 KV 提示缓存，第一条又会变回 45 秒）。
        """
        # ── 已经就绪/正在启动 → 直接返回，**绝不重复启动** ──
        if self._loaded and self._stats.get("server_ready"):
            return True, self._msg
        if getattr(self, "_starting", False):
            return True, "正在启动…"
        # ── ★ mode="auto"：先定后端（有 key 且网络通 → 云端，否则本地）──
        if self.mode == "auto":
            self.mode = pick_mode("auto", cfg=self._api)
            self._log("LLM：auto 模式选定后端 = %s" % self.mode)

        # ── ★ 云端 API 模式：不用探测模型/运行时，只检查配置 ──
        if self.mode == "api":
            if not self._api.get("key"):
                self._msg = ("云端模式未配置 API key —— 请设置环境变量 "
                             "CAR_LLM_API_KEY，或写配置文件 ~/.config/car_llm/api.json")
                self._loaded = False
                return False, self._msg
            self._loaded = True
            self._msg = "就绪（云端 %s @ %s）" % (
                self._api.get("model"), self._api.get("base"))
            self._log("LLM：%s" % self._msg)
            return True, self._msg

        self._model = self._model or find_model()
        if not self._model:
            self._msg = ("没有找到 .gguf 模型 —— 放到 ~/models/llm/ 下，例如\n"
                         "  qwen2.5-0.5b-instruct-q4_k_m.gguf")
            self._loaded = False
            return False, self._msg

        if background:
            self._starting = True

            def _work():
                try:
                    self.load(background=False)
                finally:
                    self._starting = False
            threading.Thread(target=_work, daemon=True).start()
            return True, "正在启动（后台）…"

        # ── 优先 server 模式（快得多，见模块 docstring 的实测数据）──
        if self.mode == "server":
            srv = find_llama_server()
            if srv:
                ok, msg = self._start_server(srv)
                if ok:
                    self._loaded = True
                    self._msg = msg
                    self._log("LLM：%s" % msg)
                    return True, msg
                self._log("LLM：server 启动失败（%s），退回 cli 模式" % msg)
            else:
                self._log("LLM：没找到 llama-server，退回 cli 模式")
            self.mode = "cli"

        self._cli = self._cli or find_llama_cli()
        if not self._cli:
            self._msg = ("没有找到 llama-cli / llama-server —— 需要先编译 llama.cpp：\n"
                         "  cd ~/llama.cpp && cmake -B build -DCMAKE_BUILD_TYPE=Release "
                         "&& cmake --build build -j3 --target llama-cli llama-server")
            self._loaded = False
            return False, self._msg
        self._loaded = True
        self._msg = "就绪（cli 模式，%s）" % os.path.basename(self._model)
        self._log("LLM：%s" % self._msg)
        return True, self._msg

    @property
    def ready(self):
        """现在能立刻推理吗？"""
        if getattr(self, "_starting", False):
            return False
        if not self._loaded:
            return False
        if self.mode == "api":
            # 云端：配置了 key 就算就绪（不做联网探测 —— 探测反而拖慢界面）
            return bool(self._api.get("key"))
        if self.mode == "server":
            return bool(self._stats.get("server_ready")) and self._health()
        return True

    # ── 防重复：记住最近的回复，喂回给模型 ──────────────────────────
    def _recent_says(self, n=4):
        """最近 n 条 `say`（最新的在后），用于让模型"换句话讲"。"""
        with self._lock:
            return [s for s in list(self._recent)[-n:] if s]

    def _remember_say(self, say):
        """记一条 say（去重、限量），供下次提示词里做"别重复"依据。"""
        say = (say or "").strip()
        if not say:
            return
        with self._lock:
            try:
                self._recent.remove(say)      # 挪到最新
            except ValueError:
                pass
            self._recent.append(say)
            while len(self._recent) > self._recent_max:
                self._recent.pop(0)

    def _build_prompt_api(self, text, extra_context=""):
        """云端模式的提示：(system, user) 元组，由 `_run_api` 发成 messages。

        ★ 会把**最近的回复**作为 `[最近说过]` 附在用户消息后，
          让模型换句话讲、避免复读（见 `_remember_say`）。
        """
        user = text if not extra_context else "%s\n[当前状态]%s" % (text, extra_context)
        # 只在"闲聊/回话"时才提防重复 —— 指令类回复本来就很短且固定，
        # 而且每次都加会让提示词变长、拖慢推理。
        recent = self._recent_says()
        if recent:
            user += "\n[最近说过]" + "｜".join(recent)
        return SYSTEM_PROMPT_API, user
        return SYSTEM_PROMPT_API, user

    def _run_api(self, text, extra_context="", plain=False):
        """向云端 OpenAI 兼容接口发一次请求，返回 (content, err, 耗时)。

        ★ 零依赖：只用标准库 urllib。
        ★ **首选 function-calling**（`tools` + 强制 `tool_choice`）：
          把 cmd 变成 schema 必填字段，模型无法"用闲聊代替填字段"。
        ★ 若后端不支持 tools → 自动退回 `response_format=json_object`；
          再不行就退回**纯提示词约束**（此时靠 `extract_json` 解析）。
        ★ `plain=True`：**跳过 tools/json_object，直接用纯提示词**。
          用于"模型返回了空壳（既没 cmd 也没 say）"时的重试
          —— 有些后端在强制 tool_choice 下会给出空 arguments。
        """
        import urllib.request
        import urllib.error

        system, user = self._build_prompt_api(text, extra_context)
        url = self._api["base"] + API_CHAT_PATH
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._api["key"],
            "User-Agent": "car-gui/1.0",
        }

        def _post(extra):
            body = {
                "model": self._api["model"],
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": self.temp,
                "max_tokens": max(self.max_tokens, 200),
                "stream": False,
            }
            body.update(extra)
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))

        t0 = time.time()
        try:
            obj = None
            if plain:
                obj = _post({})          # ★ 只走纯提示词
            # ① function-calling（最可靠）
            if obj is None and self.use_tools:
                try:
                    obj = _post({
                        "tools": [API_TOOL_SPEC],
                        "tool_choice": {"type": "function",
                                        "function": {"name": "car_command"}},
                    })
                    # 有些实现把结果放 tool_calls，有些直接放 content
                    msg = obj["choices"][0]["message"]
                    tc = msg.get("tool_calls") or []
                    if tc:
                        obj = {"_tool_args":
                               (tc[0].get("function") or {}).get("arguments") or "{}",
                               "_usage": obj.get("usage") or {}}
                    # 没有 tool_calls → 走下面通用的 content 解析
                except urllib.error.HTTPError as e:
                    if e.code in (400, 404, 422, 500):
                        self._log("LLM：该后端不支持 tools，改用 response_format")
                        obj = None
                    else:
                        raise
            # ② json_object
            if obj is None and self.use_tools:
                try:
                    obj = _post({"response_format": {"type": "json_object"}})
                except urllib.error.HTTPError as e:
                    if e.code in (400, 404, 422):
                        self._log("LLM：该后端不支持 response_format，改用纯提示词约束")
                        obj = None
                    else:
                        raise
            # ③ 纯提示词
            if obj is None:
                obj = _post({})

            dt = time.time() - t0
            u = obj.get("_usage") or obj.get("usage") or {}
            if u:
                self._stats["api_prompt_tokens"] = u.get("prompt_tokens", 0)
                self._stats["api_total_tokens"] = u.get("total_tokens", 0)

            # tool_calls 形态：参数是 JSON 字符串
            if "_tool_args" in obj:
                return obj["_tool_args"], "", dt
            try:
                content = obj["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                return "", "API:响应结构异常 %s" % (str(obj)[:200],), dt
            return content, "", dt
        except Exception as e:
            dt = time.time() - t0
            detail = ""
            if hasattr(e, "read"):
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
            if "timed out" in str(e).lower():
                self._stats["timeouts"] += 1
                return "", "TIMEOUT", dt
            return "", "API:%s %s" % (e, detail), dt

    # ── server 模式 ─────────────────────────────────────────────────
    def _start_server(self, srv):
        """启动常驻 llama-server 并等它 /health 就绪。"""
        # ★ 先清掉上次运行残留的孤儿（GUI 被 kill 时 shutdown 跑不到，见 kill_stale_servers）
        kill_stale_servers(log=self._log)
        port = self._srv_port or _free_port()
        cmd = [
            srv, "-m", self._model,
            "-c", str(self.n_ctx), "-t", str(self.threads),
            "--port", str(port), "--host", "127.0.0.1",
            "-ngl", "0", "--no-warmup",
            # 复用 KV 前缀：系统提示只算一次，这是本方案快起来的**关键**
            "--cache-reuse", "256",
        ]
        if self.use_grammar:
            try:
                cmd += ["--grammar-file", self._ensure_grammar()]
            except Exception as e:
                self._log("LLM：语法文件准备失败（%r）" % (e,))
        try:
            self._srv_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            return False, "启动 llama-server 异常：%r" % (e,)
        self._srv_port = port
        # 等就绪：模型加载 + 首次图构建，本板要几十秒，给足时间
        deadline = time.time() + 180
        while time.time() < deadline:
            if self._srv_proc.poll() is not None:
                err = ""
                try:
                    err = (self._srv_proc.stderr.read() or b"").decode(
                        "utf-8", "replace")[-300:]
                except Exception:
                    pass
                return False, "llama-server 退出（%s）" % (err.strip() or "无输出")
            if self._health():
                self._stats["server_ready"] = True
                return True, "就绪（server 模式 :%d，%s）" % (
                    port, os.path.basename(self._model))
            time.sleep(1.0)
        return False, "llama-server 启动超时（180s）"

    def _health(self):
        """GET /health —— 就绪返回 True。"""
        if not self._srv_port:
            return False
        try:
            import urllib.request
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % self._srv_port, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _run_server(self, prompt):
        """向常驻 server 发一次补全请求，(out, err, 耗时)。"""
        import urllib.request
        body = {
            "prompt": prompt,
            "n_predict": self.max_tokens,
            "temperature": self.temp,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "cache_prompt": True,        # ★ 关键：复用已算过的提示前缀
            "stream": False,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/completion" % self._srv_port, data=data,
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            dt = time.time() - t0
            return (obj.get("content") or ""), "", dt
        except Exception as e:
            dt = time.time() - t0
            if "timed out" in str(e).lower():
                self._stats["timeouts"] += 1
                return "", "TIMEOUT", dt
            return "", "SRV:%r" % (e,), dt


    @property
    def loaded(self):
        return self._loaded

    @property
    def msg(self):
        return self._msg

    def status(self):
        import shutil as _sh
        total = 34.0
        try:
            st = os.statvfs(os.path.dirname(self._model or HOME))
            total = st.f_bavail * st.f_frsize / (1024.0 ** 3)
        except Exception:
            pass
        return {
            "loaded": self._loaded,
            "msg": self._msg,
            "mode": self.mode,
            "cli": self._cli or "",
            "model": (self._api.get("model") if self.mode == "api"
                      else (os.path.basename(self._model) if self._model else "")),
            "model_path": self._model or "",
            "model_mb": (round(os.path.getsize(self._model) / 1048576.0, 1)
                         if self._model and os.path.isfile(self._model) else 0),
            "api_base": self._api.get("base", "") if self.mode == "api" else "",
            "api_key_set": bool(self._api.get("key")),
            "api_source": self._api.get("source", ""),
            "threads": self.threads,
            "timeout": self.timeout,
            "disk_free_gb": round(total, 1),
            "stats": dict(self._stats),
            "cache": len(self._cache),
        }

    # ── 提示词与进程调用 ────────────────────────────────────────────
    def _build_prompt(self, text, extra_context=""):
        """拼 Qwen2.5 的 ChatML 模板（llama-cli 只吃裸文本，模板要自己写）。"""
        user = text if not extra_context else "%s\n[当前状态]%s" % (text, extra_context)
        return ("<|im_start|>system\n%s<|im_end|>\n"
                "<|im_start|>user\n%s<|im_end|>\n"
                "<|im_start|>assistant\n" % (SYSTEM_PROMPT, user))

    def _run_cli(self, prompt):
        """起一次 llama-cli，返回 (stdout, err, 耗时)。"""
        cmd = [
            self._cli, "-m", self._model,
            "-p", prompt,
            "-n", str(self.max_tokens),
            "-c", str(self.n_ctx),
            "-t", str(self.threads),
            "--temp", str(self.temp),
            "--top-p", "0.9",
            "--repeat-penalty", "1.1",
            # ⚠️ 必须是 `-st/--single-turn`：本版本 llama.cpp 已移除 `-no-cnv`，
            #    传它会直接 `error: invalid argument` 并返回空输出（踩过）。
            #    而**不加**这个开关，llama-cli 会进入**交互模式等 stdin** → 挂住。
            "-st",
            "--no-warmup",        # 不预热，省一次前向
            "--no-display-prompt",
            "--log-disable",
            "-ngl", "0",          # 纯 CPU（本板没有 CUDA/Metal）
        ]
        # ★ GBNF 语法约束：小模型唯一可靠的结构化输出手段（见 GBNF_GRAMMAR 注释）
        if self.use_grammar:
            try:
                cmd += ["--grammar-file", self._ensure_grammar()]
            except Exception as e:
                self._log("LLM：语法文件准备失败，退回无约束（%r）" % (e,))
        t0 = time.time()
        try:
            # ⚠️ 注意：**不能**同时传 `stdin=` 和 `input=`
            #    （会 ValueError: stdin and input arguments may not both be used. —— 踩过）
            #    这里只用 input=：`-st` 已保证读完一轮就退出。
            #    万一将来版本行为变了而挂住，靠 timeout 兜底，DEVNULL 不是必需的。
            p = subprocess.run(cmd, input=prompt.encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=self.timeout)
            dt = time.time() - t0
            out = p.stdout.decode("utf-8", "replace")
            err = p.stderr.decode("utf-8", "replace")
            return out, err, dt
        except subprocess.TimeoutExpired:
            self._stats["timeouts"] += 1
            return "", "TIMEOUT", time.time() - t0
        except Exception as e:
            return "", "EXC:%r" % (e,), time.time() - t0

    # ── 主入口 ──────────────────────────────────────────────────────
    def ask(self, text, extra_context="", allow_cmd=True, force_chat=False):
        """
        问一次模型。**永不抛异常**，失败时返回带 error 的 LlmReply。

        allow_cmd=False / force_chat=True：只允许闲聊，不允许给指令
        （GUI 的"纯聊天模式"用，或规则层已判过不是指令时）。
        """
        text = (text or "").strip()
        if not text:
            return LlmReply(error="空输入")
        if not self._loaded:
            ok, _ = self.load()
            if not ok:
                return LlmReply(error=self._msg)

        # ★ 安全兜底：这句话有停车语义 → 不执行任何动作，只允许回话。
        #    （正常流程里规则层先拦掉了，这里是第二道闸。）
        if looks_like_stop(text):
            allow_cmd = False
            self._log("LLM：检测到停车语义，已禁用指令下发（只回话）")

        # 🔴 缓存键必须**包含最近回复历史**（2026-10-09 修）：
        #   原来键只有 `text`，而 `_build_prompt_api` 会把 `[最近说过]` 塞进提示词
        #   → 同一句话问第二次**直接命中缓存**，模型根本没被调用，
        #     于是"防复读"完全失效（实测同问 5 次回复一字不差）。
        #   把历史纳入键后，历史一变就是新请求 → 会真的去问模型。
        recent = self._recent_says()
        key = ("c" if allow_cmd else "n") + "|" + ("f" if force_chat else "-") \
            + "|" + "|".join(recent) + "|" + text
        with self._lock:
            if key in self._cache:
                self._stats["hits"] += 1
                r = self._cache[key]
                return LlmReply(r.cmd, r.speed, r.distance_mm, r.duration_s,
                                r.confidence, r.say, r.raw, 0.0, True)

            # ★ 防复读第二道保险：同一句话（不含历史）如果刚答过，
            #    就提醒一句"换个角度" —— 因为云端模型对同一输入
            #    本来就会给同样的答案，光靠 `[最近说过]` 提示不一定管用。
            #    ⚠️ 措辞要**轻**（只说"换个角度"）：实测用强硬的
            #      "必须换一句完全不同的说法"会把模型带偏 ——
            #      问"今天天气"它竟回"你今天穿对衣服了吗？"（跑题）。
            _bare_key = ("c" if allow_cmd else "n") + "|" \
                + ("f" if force_chat else "-") + "|" + text
            _repeat = _bare_key in self._asked_bare
            self._asked_bare.add(_bare_key)
            # 只留最近 200 条，避免无限增长
            if len(self._asked_bare) > 200:
                self._asked_bare.clear()
                self._asked_bare.add(_bare_key)

        if _repeat and self.mode == "api":
            extra_context = (extra_context
                             + "\n[提示]这句话你刚才答过了，"
                               "请换一个完全不同的切入角度和句式，"
                               "不要再自我介绍，别说和上次一样的话。"
                             ).strip()

        if self.mode == "api":
            out, err, dt = self._run_api(text, extra_context)
        else:
            prompt = self._build_prompt(text, extra_context)
            if self.mode == "server" and self._stats.get("server_ready"):
                out, err, dt = self._run_server(prompt)
            else:
                out, err, dt = self._run_cli(prompt)

        with self._lock:
            self._stats["calls"] += 1
            self._stats["last_elapsed"] = dt
            self._stats["total_elapsed"] += dt

        if err == "TIMEOUT":
            return LlmReply(raw=out, elapsed=dt,
                            error="LLM 超时（%.0fs）" % self.timeout)
        if not out.strip():
            self._stats["errors"] += 1
            return LlmReply(raw=err[:400], elapsed=dt,
                            error="LLM 无输出：%s" % err.strip()[:150])

        reply = self._parse_and_validate(out, text, dt, allow_cmd, force_chat)

        # ★★ 空壳回复重试（2026-10-11）：既没 cmd 也没 say，多半是
        #    后端在**强制 tool_choice** 下给了空 arguments（`{}`）。
        #    这时用**纯提示词**再问一次，通常就能拿到正常回复。
        #    ⚠️ 只在"没报错但没用"时做，且只重试一次 —— 不拖慢正常路径。
        if (not reply.error and not reply.cmd
                and not (reply.say or "").strip() and self.mode == "api"):
            try:
                out2, err2, dt2 = self._run_api(text, extra_context, plain=True)
                if out2.strip():
                    r2 = self._parse_and_validate(out2, text, dt2,
                                                  allow_cmd, force_chat)
                    if r2.cmd or (r2.say or "").strip():
                        self._log("LLM 空壳回复，已用纯提示词重试成功")
                        dt += dt2
                        reply = r2
            except Exception as e:
                self._log("LLM 空壳重试异常（忽略）：%r" % (e,))

        if reply.error and reply.error != "无有效 JSON":
            self._stats["errors"] += 1
        # 只缓存成功的（失败多半是超时/环境问题，缓存住会一直失败）
        if not reply.error:
            with self._lock:
                self._cache[key] = reply
                self._cache_order.append(key)
                while len(self._cache_order) > self.cache_size:
                    self._cache.pop(self._cache_order.pop(0), None)
        return reply

    def _parse_and_validate(self, out, text, dt, allow_cmd, force_chat):
        """把模型输出解析成受约束的 LlmReply。**这里是所有安全校验的落点。**"""
        obj = extract_json(out)
        if obj is None:
            # 🔴 模型没按 JSON 格式来。**但通常它已经把话说出来了** ——
            #    2026-10-11 修：原来这里直接标 error="无有效 JSON"，
            #    而 `run_text_command()` 看到任何 error 就当成"LLM 不可用"，
            #    把模型的话整个丢掉去回"没听懂" ——
            #    用户看到的就是「LLM 不可用，退回规则提示：无有效 JSON」。
            #
            #    ⇒ 改成：**尽力抢救 `say`**。
            #      抢救到 → 当作一条正常的"纯闲聊回复"返回（error 留空），
            #               调用方就会把这句话说出来；
            #      完全抢救不到 → 才保留原来的错误提示。
            #    ⚠️ 安全不受影响：这里**永远 cmd=None**，绝不下发任何动作。
            say = extract_say(out)
            if not say:
                # 再退一步：整段里最长的那个引号字符串（很多模型会直接说人话）
                cands = [m.group(1) for m in _QUOTED_RE.finditer(out or "")]
                cands = [c for c in cands if len(c) >= 2]
                if cands and not re.search(r'"(cmd|confidence|speed|distance)"',
                                           out or ""):
                    say = max(cands, key=len)
            say = re.sub(r"<\|.*?\|>", "", say).strip()
            if say:
                self._log("LLM 输出不是合法 JSON，已抢救出回复：%s" % say[:40])
                return LlmReply(say=cap_say_length(say), raw=out, elapsed=dt,
                                cmd=None, confidence=0.0,
                                error="")          # ← 关键：不算失败
            # 真的连引号字符串都没有：退回"整段文本当 say"的老逻辑（仍不下发动作）
            say = out.strip().splitlines()[-1] if out.strip() else ""
            say = re.sub(r"<\|.*?\|>", "", say).strip()
            if say:
                # ★★ 关键（2026-10-11）：**有可用文本就别标错**。
                #    原来这里返回 error="无有效 JSON"，而
                #    `run_text_command()` 看到任何 error 就当"LLM 不可用"，
                #    于是把模型说的整段话丢掉、只回「没听懂」——
                #    用户看到的就是「LLM 不可用，退回规则提示：无有效 JSON」。
                #    模型只是**没按 JSON 格式包装**，话说得好好的，该说出来。
                #    ⚠️ 安全不受影响：`cmd=None`，`_llm_reply_to_result` 因
                #      `has_cmd=False` 直接返回 None ⇒ **绝不下发任何动作**。
                self._log("LLM 未按 JSON 输出，按纯文本回复处理")
                return LlmReply(say=cap_say_length(say), raw=out, elapsed=dt,
                                cmd=None, confidence=0.0, error="")
            return LlmReply(say="", raw=out, elapsed=dt,
                            error="无有效 JSON")

        cmd = obj.get("cmd")
        say = str(obj.get("say") or "").strip()
        say = re.sub(r"<\|.*?\|>", "", say).strip()
        try:
            conf = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0

        # ── 校验 1：动作必须在白名单里（stop 不在，故意）──────────
        if cmd not in ALLOWED_ACTIONS:
            cmd = None
        # ── 校验 1b：★ 关键词闸门 —— 动作语义必须在原话里有字面证据 ──
        #    这是防"拿不准就猜动作"的**核心防线**，比置信度可靠得多。
        #    （置信度实测区分不了：垃圾 0.9~0.95，合法口语也才 0.90）
        if cmd is not None and not cmd_has_evidence(cmd, text):
            self._log("LLM：原话「%s」里找不到 %s 的依据，丢弃该指令（只看作闲聊）"
                      % (text[:16], cmd))
            cmd = None
        # ── 校验 1c：置信度粗筛（挡掉明显没底的；真正的闸门是上面那条）──
        if cmd is not None and conf < LLM_CMD_MIN_CONFIDENCE:
            self._log("LLM：置信度 %.2f < %.2f，丢弃指令 %s（只看作闲聊）"
                      % (conf, LLM_CMD_MIN_CONFIDENCE, cmd))
            cmd = None
        # ── 校验 2：调用方禁止给指令 / 强制闲聊 ─────────────────
        if force_chat or not allow_cmd:
            if cmd is not None:
                self._log("LLM：本次不允许下发指令，已丢弃 cmd=%s" % cmd)
            cmd = None

        speed = _clamp(obj.get("speed"), SPEED_MIN, SPEED_MAX) if cmd else None
        dist = None
        if cmd and obj.get("distance_mm") is not None:
            dist = _clamp(obj.get("distance_mm"), 100, 50000)
        dur = None
        if cmd and obj.get("duration_s") is not None:
            dur = _clamp(obj.get("duration_s"), DURATION_MIN, DURATION_MAX)

        # ★ 记住这条回复 → 下次提示词里带上 `[最近说过]`，让模型换句话讲
        self._remember_say(say)
        return LlmReply(cmd=cmd, speed=int(speed) if speed else None,
                        distance_mm=dist, duration_s=dur, confidence=conf,
                        say=cap_say_length(say), raw=out, elapsed=dt)

    def ask_chat(self, text, extra_context=""):
        """只闲聊，不允许给指令（GUI「纯聊天」模式 / 已判定不是指令时）。"""
        return self.ask(text, extra_context=extra_context,
                        allow_cmd=False, force_chat=True)

    def clear_cache(self):
        with self._lock:
            self._cache.clear()
            self._cache_order = []

    def close(self):
        """停掉常驻 server、删掉临时语法文件。**进程退出前务必调用。**"""
        self.clear_cache()
        p = self._srv_proc
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            self._srv_proc = None
            self._stats["server_ready"] = False
        if self._grammar_file:
            try:
                os.unlink(self._grammar_file)
            except Exception:
                pass
            self._grammar_file = None


# ══════════════════════════════════════════════════════════════════════
#  命令行自测
# ══════════════════════════════════════════════════════════════════════

#: 内置用例：(输入, 期望能否给出指令, 说明)
#: 前 4 条是**真实 ASR 常见误识别**，不应被下发为动作（安全）；
#: 中间是纠错用例；最后是闲聊。
SELFTEST_CASES = (
    ("急停",                 False, "★ 停车语义：绝不下发动作"),
    ("快停下",               False, "★ 停车语义：绝不下发动作"),
    ("钱进一米",             True,  "同音字纠错 → forward"),
    ("往左边拐个弯",          True,  "口语化 → turn_left"),
    ("开慢点儿",             True,  "口语化 → speed_down"),
    ("退后半米",             True,  "口语化 → backward"),
    ("你叫什么名字",          False, "闲聊 → 不动作"),
    ("今天天气怎么样",        False, "闲聊 → 不动作"),
    ("我饿了",               False, "闲聊 → 不动作"),
)


def _selftest(eng):
    print("=" * 72)
    print(" llm_engine 自测")
    print("=" * 72)
    st = eng.status()
    print("llama-cli : %s" % (st["cli"] or "（缺失）"))
    print("模型      : %s  (%.1f MB)" % (st["model"] or "（缺失）", st["model_mb"]))
    print("线程/超时 : %d / %.0fs" % (st["threads"], st["timeout"]))
    print("-" * 72)
    fails = 0
    t_all = 0.0
    for text, want_cmd, note in SELFTEST_CASES:
        r = eng.ask(text)
        t_all += r.elapsed
        got = r.cmd or "chat"
        ok = (r.has_cmd == want_cmd)
        if not ok:
            fails += 1
        print("%s %-14s → %-11s %-8s say=%r"
              % ("✓" if ok else "✗", text, got,
                 ("%.2fs" % r.elapsed) if not r.from_cache else "cache",
                 r.say))
        if r.error:
            print("      error: %s" % r.error)
    print("-" * 72)
    print("失败 %d / %d    总耗时 %.1fs   平均 %.2fs/次"
          % (fails, len(SELFTEST_CASES), t_all, t_all / max(len(SELFTEST_CASES), 1)))
    if t_all > 0:
        print("（速度数据：用于判断要不要换更大的模型）")
    return 0 if fails == 0 else 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="本地 LLM（纠错 + 对话）")
    ap.add_argument("--ask", help="问一句，看它怎么理解")
    ap.add_argument("--chat", help="只闲聊（禁止下发指令）")
    ap.add_argument("--selftest", action="store_true", help="跑内置用例")
    ap.add_argument("--bench", action="store_true", help="测速度（token/s 与首字延迟）")
    ap.add_argument("-n", "--n-predict", type=int, default=96, help="最多生成多少 token")
    ap.add_argument("-t", "--threads", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--model", default=None, help="指定 gguf 路径")
    ap.add_argument("--cli", default=None, help="指定 llama-cli 路径")
    ap.add_argument("--mode", default="auto",
                    choices=("auto", "server", "cli", "api"),
                    help="auto=有key且网络通就用云端否则本地(推荐) / "
                         "server=本地常驻(快) / cli=本地每次起进程(慢) / api=强制云端")
    ap.add_argument("--api-base", default=None, help="云端 base_url（默认智谱 GLM）")
    ap.add_argument("--api-model", default=None, help="云端模型名（默认 glm-4-flash）")
    ap.add_argument("--api-key", default=None,
                    help="云端 key（一般不用传，用环境变量 CAR_LLM_API_KEY）")
    ap.add_argument("--status", action="store_true", help="只看环境探测结果")
    args = ap.parse_args()

    # ── 云端可用性自检（不消耗额度）──
    if args.mode == "api":
        cfg = api_config(base=args.api_base, key=args.api_key, model=args.api_model)
        print("云端 base : %s" % cfg["base"])
        print("云端 model: %s" % cfg["model"])
        print("key 来源  : %s" % (cfg["source"] or "（未配置）"))
        print("key 状态  : %s" % ("已配置（长度 %d）" % len(cfg["key"])
                                  if cfg["key"] else "❌ 未配置"))
        if not cfg["key"]:
            print("\n配置方法（二选一）：")
            print("  ① export CAR_LLM_API_KEY='你的key'")
            print("  ② mkdir -p ~/.config/car_llm && "
                  "echo '{\"key\":\"...\",\"model\":\"glm-4-flash\"}' > "
                  "~/.config/car_llm/api.json && chmod 600 ~/.config/car_llm/api.json")
            return 1

    eng = LlmEngine(cli=args.cli, model=args.model, threads=args.threads,
                    timeout=args.timeout, max_tokens=args.n_predict,
                    mode=args.mode, api_base=args.api_base,
                    api_key=args.api_key, api_model=args.api_model,
                    log=lambda m: print("  " + m))
    ok, msg = eng.load()
    print("加载：%s —— %s" % (ok, msg))
    if args.status:
        st = eng.status()
        for k in sorted(st):
            print("  %-14s %r" % (k, st[k]))
        return 0 if ok else 1
    if not ok:
        return 1

    if args.selftest:
        return _selftest(eng)
    if args.bench:
        print("测速中（首次最慢，不含模型加载，因为 llama-cli 每次都要重加载权重）…")
        for t in ("你好", "前进一米", "你叫什么名字"):
            t0 = time.time()
            r = eng.ask(t)
            dt = time.time() - t0
            ntok = max(len(r.say), len(r.raw) // 4, 1)
            print("  %-12s 墙钟 %.2fs  (约 %.1f tok/s)  raw=%d字"
                  % (t, dt, ntok / dt if dt > 0 else 0, len(r.raw)))
        print("stats:", eng.status()["stats"])
        return 0
    if args.chat:
        r = eng.ask_chat(args.chat)
        print("reply: %r" % (r,))
        return 0
    if args.ask:
        r = eng.ask(args.ask)
        print("reply: %r" % (r,))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
