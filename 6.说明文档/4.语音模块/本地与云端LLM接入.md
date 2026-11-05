# 本地与云端 LLM 接入

> 目标：让小车**听得懂模糊/听错的话**，并且**回话像人**。
> 底座：llama.cpp（源码编译，纯 CPU）+ Qwen2.5-0.5B-Instruct（GGUF Q4_K_M）。
> 定位：**增强，不是依赖** —— LLM 挂了/关了/超时了，规则解析与手动控车完全不受影响。

---

## 一、为什么需要它（问题陈述）

项目重点是**人机交互**。接入前有两个体验痛点：

1. **ASR 听错就彻底没法用**：SenseVoice 偶尔把「前进」听成「钱进」、把口语「往左边拐个弯」
   听成别的词 → `parse_text_command()` 返回 None → 界面只回一句「没听懂」，交互断掉。
2. **回复太生硬**：不管你说什么，不是执行动作就是「没听懂」，
   没有"聊天"这个状态。演示时想跟车说句话都做不到。

LLM 正好补这两块：
- **指令纠错**：`钱进一米` → 前进 1 米；`往左边拐个弯` → 左转；`开慢点儿` → 减速。
- **拟人化对话**：确实不是指令时，回一句自然的中文，而不是「没听懂」。

---

## 二、架构：LLM 挂在哪一层

```
用户说话 / 打字
      │
      ▼
  ASR（SenseVoice）──────────► 文本
      │
      ▼
 ┌─────────────────────────────────────────────┐
 │ ① 规则快路径 parse_text_command()            │  ← **永远先跑，零延迟**
 │    命中 → 直接执行，**不经过 LLM**            │
 └─────────────────────────────────────────────┘
      │ 没命中
      ▼
 ┌─────────────────────────────────────────────┐
 │ ② 本地 LLM（llm_engine.py）                  │  ← 只在没命中时才被调用
 │    · 高置信度指令 → 当纠错处理，执行           │
 │    · 否则 → 用它的 say 做拟人化回复            │
 └─────────────────────────────────────────────┘
      │ 不可用 / 超时 / 关闭
      ▼
 ③ 退回原来的「没听懂」（行为与接入前一致）
```

**为什么顺序不能反**：常用指令（前进/后退/转向/速度）必须**零延迟**，
而 LLM 一次要几秒。把 LLM 放前面会让所有指令都变慢，还会把急停拖进模型里 —— 那是安全灾难。

### 🔴 三条安全底线（改代码前必读）

| 底线 | 实现位置 | 说明 |
|---|---|---|
| **急停绝不经过 LLM** | 规则层 `_STOP_WORDS` 先命中；`llm_engine.looks_like_stop()` 再兜底 | "急停/停止/停车/刹车/别动…"在规则层就返回了，LLM 一次都不会被调用。有断言锁死（`test_llm_integration.py` 第 1 项） |
| **LLM 没有停车权限** | `llm_engine.ALLOWED_ACTIONS` **刻意不含 `stop`** | 就算模型硬输出 `{"cmd":"stop"}`，也会被丢弃 → 只回话、不下发 |
| **★ 关键词闸门：动作必须有字面证据** | `llm_engine.ACTION_EVIDENCE` + `cmd_has_evidence()` | **这是本模块最重要的安全设计**，见下面 §二.1 |
| **不确定就不动车** | `LLM_MIN_CONFIDENCE = 0.8`（粗筛） | 真正的闸门是关键词闸门；置信度只做粗筛 |

#### 二.1 为什么必须有关键词闸门（**用真实数据换来的教训**）

拿 **14 条真实 ASR 输出**喂给 0.5B，抓到它有强烈的 **"拿不准就猜动作"** 偏置：

```
'能。'            → forward       '是是。'          → forward
'开玩去啊。'       → forward       '好，一遍过。'     → forward
'今天天气怎么样'    → speed_down    '操哼。'          → speed_up
```
🔴 **已连小车时，这就等于"ASR 幻觉让车自己走"** —— 9/14 条误判。

试过两条路，**都失败**：
1. **补 few-shot 示例** → 无效（仍 9/14）；
2. **抬置信度门槛到 0.95** → 不行，因为**置信度区分不了**：
   垃圾输入自称 0.9~0.95，而合法口语（`开慢点儿`）**也才 0.90** —— 抬上去会把真指令一起丢掉。

最终方案：**要求动作语义在用户原话里有字面证据**（`ACTION_EVIDENCE` 词表）。
LLM 返回的 cmd 必须在原话里找得到对应词，否则**丢弃指令、退化成闲聊**：

```
'是是。'       里找不到"前进/往前/米…" → ★ 永远不可能变成 forward
'钱进一米'     含"米"                  → 允许 forward
'开慢点儿'     含"慢"                  → 允许 speed_down
```

**效果（板子实测，同一批 14 条真实数据）**：

| 指标 | 加闸门前 | 加闸门后 |
|---|---|---|
| 幻觉被瞎猜成指令（**危险**） | **9** | **0** ✅ |
| 该纠错却没纠出 | 0 | **0** ✅ |
| 该闲聊却无回复 | 0 | **0** ✅ |

```
纠错：钱进一米→前进 · 往左边拐个弯→左转 · 开慢点儿→减速 · 退后半米→后退
闲聊：嗯哼。→「你说话声音真响亮」  能。→「好，继续前进！」（只回话，不动作）
     你叫什么名字→「我是你的智能小车呀」  今天天气怎么样→「我只看得到眼前的路」
```

> ⚠️ 增删 `ACTION_EVIDENCE` 词表前想清楚：**放宽 = 多识别，也 = 多误动**。
> 漏掉只是"没反应"，乱动是"车自己跑了"。
> ⚠️ 词表里**刻意没有"开"** —— 它出现在"开玩去啊/开玩笑"里，会放行垃圾输入（实测踩到）。

---

## 三、安装（板子上，一次性）

### 3.1 编译 llama.cpp

```bash
cd ~
# 源码（走代理；直连 github 也行）
curl -fL --max-time 600 -o llama_cpp.tar.gz \
  https://ghfast.top/https://github.com/ggml-org/llama.cpp/archive/refs/heads/master.tar.gz
tar xzf llama_cpp.tar.gz && mv llama.cpp-master llama.cpp && rm llama_cpp.tar.gz

cd ~/llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DGGML_NATIVE=ON \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_SERVER=ON
cmake --build build --config Release -j3 --target llama-cli llama-server
```

⚠️ **实测耗时 25~30 分钟**（3 核 1GHz；llama.cpp 本体模板很重；`llama-server` 的
`server-models.cpp` 单文件就要好几分钟）。
建议后台跑并看日志（仓库里有现成脚本）：

```bash
nohup bash ~/llm_setup.sh > ~/llm_setup.log 2>&1 &
tail -f ~/llm_setup.log      # 看到 "[4/4] 首轮速度实测" 就是完了
```

产物：`~/llama.cpp/build/bin/{llama-cli, llama-server}`（**两个都要** —— 引擎默认用 server）。

### 3.2 下载模型

```bash
mkdir -p ~/models/llm && cd ~/models/llm
# 0.5B（推荐先用它）
curl -fL --max-time 900 -o qwen2.5-0.5b-instruct-q4_k_m.gguf \
  https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf
# 想要更好的对话质量（慢一些）
curl -fL --max-time 1200 -o qwen2.5-1.5b-instruct-q4_k_m.gguf \
  https://hf-mirror.com/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf
```

| 模型 | 体积 | 实测加载(server) | 实测延迟/次 | 质量 |
|---|---|---|---|---|
| **0.5B** Q4_K_M | 469 MB | **4.1 s** | 首次 35s / **之后 16s** | 纠错 3/3 正确，够用 ✅ |
| 1.5B Q4_K_M | 1.1 GB | 7.1 s | 首次 59s / **之后 22~27s** | 回复更自然（"我是你的智能小车"） |

> 📌 **默认用 0.5B**：纠错任务它已经够准，而且快 40%。
> 换 1.5B 只需把文件放进同一目录，然后用 `CAR_LLM_MODEL=...` 指定或用 `--model`。
> ⚠️ 1.5B 运行时内存约 1.2GB+，本板总内存 3.4G 且 TTS 已占 ~600MB，**要留意**。

---

## 四、一键自检

```bash
cd ~/car_gui

bash run_py.sh llm_engine.py --status        # 只看环境探测（cli 与模型在不在）
bash run_py.sh llm_engine.py --ask "钱进一米"  # 问一句，看它怎么理解
bash run_py.sh llm_engine.py --chat "你叫什么名字"   # 只闲聊
bash run_py.sh llm_engine.py --selftest      # 跑内置用例（含"绝不下发动作"的停车用例）
bash run_py.sh llm_engine.py --bench         # 测速度
```

**预检结果怎么读**：

| 输出 | 含义 |
|---|---|
| `加载：True —— 就绪（qwen2.5-0.5b-instruct-q4_k_m.gguf）` | 环境就绪 |
| `加载：False —— 没有找到 llama-cli` | 第 3.1 步没做完 |
| `加载：False —— 没有找到 .gguf 模型` | 第 3.2 步没做完 |
| `--selftest` 里 `急停 → chat` 且 `✓` | ★ 安全底线生效（不执行动作） |

> ⚠️ **`llama-cli` 每次都重新加载权重**（约 1~3 秒），这是"进程隔离"换来的代价。
> 超时控制因此天然干净（`subprocess.timeout`），卡死也不会污染 GUI 进程。

---

## 五、接入 GUI

```bash
bash run_gui.sh --demo          # 离线演示 + LLM（默认就带 LLM）
bash run_gui.sh --no-llm        # 关掉 LLM，退回"没听懂"
```

界面新增（「语音播报 / 语音急停」面板下方）：

| 控件 | 作用 |
|---|---|
| ● 本地 LLM 灯 + 文字 | 显示模型名、已问次数/缓存命中；`不可用`时是琥珀色（**不是错误**） |
| ☑ 启用 | LLM 总开关。关掉 = 完全退回旧行为 |
| ☐ 纯聊天 | **只回话、绝不执行指令** —— 演示怕误动车就勾它 |
| 对话记录 | 最近的「你：… / 车：…」 |

**⚠️ 界面不会因为 LLM 冻住**：输入框回车后，LLM 推理在**后台线程**跑
（`_submit`），结果经队列回到界面线程再执行/播报。规则能直接命中的话**根本不走后台**。

---

## 六、代码结构

| 文件 | 作用 |
|---|---|
| `llm_engine.py` | 引擎：提示词、子进程调用、JSON 抠取、**安全校验**、缓存、超时 |
| `car_gui_core.py` | `init_llm()` / `set_llm_enabled()` / `_ask_llm()` / `_ask_llm_chat()` / `ask_chat()` / `llm_status()` / `_llm_reply_to_result()`；`run_text_command()` 的 LLM 分支 |
| `test_llm_integration.py` | **43 项断言**（假 LLM，不需要模型）：安全底线 + 纠错 + 降级 |
| `car_gui_pyside.py` / `car_gui_tk.py` | LLM 面板 + 纯聊天 + 后台推理 |

### 关键接口

```python
from llm_engine import LlmEngine
eng = LlmEngine(timeout=12.0, threads=3)
ok, msg = eng.load()
r = eng.ask("钱进一米")            # -> LlmReply
r.cmd          # 'forward' / None（闲聊）
r.distance_mm  # 1000.0
r.confidence   # 0.95
r.say          # '好，往前开一米'
r.error        # '' 或失败原因
r.from_cache   # 命中缓存
```

### 提示词与输出契约

模型必须输出**单行 JSON**（`extract_json()` 用**平衡花括号**扫描，
能容忍 markdown 围栏、前后夹带解释文字、字符串里含花括号）：

```json
{"cmd": "forward", "distance_mm": 1000, "confidence": 0.95, "say": "好，往前开一米"}
```

`cmd` 允许值：`forward/backward/turn_left/turn_right/speed_up/speed_down/set_speed/chat`
—— **没有 stop**。

---

## 七、性能与调优（🔴 本节的数字都是板上实测，不是估算）

### 7.1 延迟构成（0.5B / server 模式 / 3 核 1GHz）

| 场景 | 实测 | 说明 |
|---|---|---|
| 启动 server（只加载一次模型） | **4.1 s** | 界面启动时后台做 |
| 第 1 次问答 | **35 s** | 要**从头处理约 300 token 的系统提示** |
| 第 2 次起（同一个 server） | **16 s** | ★ **系统提示的 KV 已被复用**，只算用户那句 |
| 命中结果缓存（同一句话） | **0 s** | 语音识别常重复误识别，命中率不低 |
| 规则层能直接命中 | **0 s** | 根本不问 LLM |

### 7.2 🔴 三条让它能用的关键优化（缺一个就不可用）

| 优化 | 不做会怎样 |
|---|---|
| **`llama-server` 常驻 + `cache_prompt`** | 每次起 `llama-cli` → **每次 45s**（系统提示反复重算） |
| **提示词压到 ~300 字** | 早期写了 1200 token 的"详细"提示 → **光读提示就要 75s+，必然超时** |
| **GBNF 语法约束** | 0.5B **几乎不遵循指令**，实测 5/5 复读提示词里的字段说明 |

> 📌 **可推断的规律：提示词每多 100 字 ≈ 多等 15~20 秒。**
> 想加示例提升纠错率时，请**同量地删掉别的内容**，或者接受更慢。
> 用 `bash run_py.sh llm_engine.py --bench` 复核。

### 7.3 已废弃的路子（别再试）

| 路子 | 结论 |
|---|---|
| `llama-cli -no-cnv` | **参数已移除** → `error: invalid argument`（返回空输出） |
| 不加单轮开关跑 `llama-cli` | 进入**交互模式等 stdin** → 挂住 |
| `--prompt-cache` 文件缓存 | **当前版本已移除** |
| 用回环/多行 GBNF | **每条规则必须写在一行**；续行会 `failed to parse grammar` 并崩溃 |
| 靠"补 few-shot 示例"治幻觉误判 | **无效**（实测仍 9/14）→ 必须用关键词闸门 |
| 靠"抬置信度门槛"治幻觉误判 | **反向失效**：抬到 0.95 会把合法口语（0.90）一起丢掉 |
| `-no-cnv` 后续版本可能再改名 | 升级 llama.cpp 后请复核 `--help` |

### 7.4 ⚠️ 一个必须知道的副作用：接入 LLM 后「没听懂」基本不再出现

接入前：规则不认 → 回一句「没听懂」。
接入后：规则不认 → **LLM 兜底** → 变成一句闲聊回复（或纠正成指令）。

也就是说 **`没听懂` 这条路几乎不可达了**（只有 LLM 超时/被 `--no-llm` 关掉时才回）。
演示时这会改变观感：**说什么它都会"接话"**，不再有"它没听懂"这个明确信号。

- 想恢复旧行为：`--no-llm`，或用面板勾掉 **☑ 启用**；
- 界面上若你输入的句子**不含任何控车词**，LLM 灯会提示
  「这句话里没有控车词，只会当闲聊处理」—— 免得以为 LLM 坏了。

### 7.5 运维：孤儿 server 必须清（**已有自清理**）

GUI 被 `kill -TERM` 或崩溃时，Qt 的 `closeEvent` **不会执行** →
`shutdown_llm()` 跑不到 → 常驻 server 变**孤儿**，继续占 **约 600MB 内存 + CPU**。
本板一共 3.4G 内存（可用约 1.3G），**每重启一次 GUI 就累积一个**；
几个孤儿就能把 CPU 抢干、把 LLM 延迟从 15 秒拖成超时（实测踩到，一次剩 4 个）。

→ **已加自清理**：每次启动 server 前 `kill_stale_servers()` 先清一遍
（用"模型名 + 本模块参数"拼精确特征，**只清自己人**，不误杀别的进程）。
实测日志：`已清理 1 个上次残留的 llama-server（否则会抢 CPU）`。

手动清理（万一手动跑脚本留下的）：
```bash
pkill -f 'llama-server'
```

### 7.6 觉得慢怎么办

1. **靠缓存**（`cache_size` 默认 128 条）—— 重复误识别直接 0 秒；
2. **缩短提示词**（最有效，见 7.2）；
3. **`--no-llm` 直接关掉** —— 常用指令本来就走规则，关掉只损失"纠错/闲聊"。

### 7.5 觉得听不懂怎么办

**在 `SYSTEM_PROMPT` 的示例里加你自己的说法。**
对小模型来说，**few-shot 示例比讲道理有效得多** —— 这是最划算的调优手段。
建议做法：把你被识别错的每一句，都变成一条示例。

---

## 八、故障排查

| 现象 | 原因与对策 |
|---|---|
| `没有找到 llama-cli` | §3.1 没编译完；`ls ~/llama.cpp/build/bin/llama-cli` |
| `没有找到 .gguf 模型` | §3.2 没下完；文件应在 `~/models/llm/` |
| `LLM 超时` | 板子被别的任务占满（如 TTS 合成中）。加大 `--timeout` 或等忙完 |
| `LLM 无输出：...` | 看 `llama-cli` 的 stderr；多半是模型文件损坏（`sha256` 对一下） |
| 回复是空/乱码 | `--ask` 手动跑一次看 raw；必要时换模型 |
| 界面卡住 | **不该发生**（推理在后台线程）。若真卡住，检查是不是走了 `--no-llm` 之外的同步路径 |
| `llm_cli` 起不来报 `libggml.so` 找不到 | 用绝对路径调 `llama-cli`，或在 `~/llama.cpp/build/bin` 下跑 |
| 内存不足（OOM） | TTS 已占 ~600MB。0.5B 约需 500MB；1.5B 需 1.2GB+，本板 3.4G 内存要留意 |

---

## 九、与其它模块的关系

| 模块 | 关系 |
|---|---|
| **ASR** | LLM 是它下游的"纠错层"。ASR 越准，走 LLM 的次数越少（更好） |
| **KWS 急停** | **完全独立**。KWS 命中直接 `controller.stop()`，不经过 LLM（毫秒级） |
| **TTS** | LLM 的 `say` 交给 `SpeakQueue` 播报。⚠️ TTS RTF≈5.4，所以提示词里强制"短句" |
| **规则解析** | 永远优先。LLM 是它的**补充**，不是替代 |
| **离线演示模式** | 组合使用最佳：`--demo` 让没接车也能用，LLM 让它听懂模糊的话 |

---

## 十、当前状态与后续

### 已完成（2026-09-27，全部真机实测）

- ✅ 板子上从零编译 **llama.cpp**（25~30 min）+ 下好 0.5B / 1.5B 两个 GGUF
- ✅ `llm_engine.py`：**常驻 server 模式**（默认）+ cli 兜底、GBNF 语法约束、
  **关键词证据闸门**、结果缓存、超时、严格安全校验、**孤儿 server 自清理**
- ✅ `car_gui_core.py` 接入：规则优先、LLM 兜底、失败退回旧行为、`shutdown_llm()` 清理
- ✅ 两个 GUI 都有 LLM 面板 + 纯聊天 + **后台推理（不冻界面）**
- ✅ `test_llm_integration.py` **77 项断言全通过**，已入 `test_all_offline.py`
- ✅ 板子完整回归 **11 通过 0 失败**（11 套全跑）
- ✅ **真实 ASR 数据验收**（14 条真人录音 → ASR → 规则/LLM）：
  纠错 4/4 正确、幻觉误判 **0**、闲聊全部得体回复
- ✅ GUI 端到端：`--demo` 启动 → 注入中文指令 → 正确识别并响应（用户也已手动确认）
- ✅ 运维：验证 GUI 被 SIGTERM 后孤儿 server **会被下次启动自动清掉**

### 本轮修掉的 5 个真 bug（全都是实测才暴露的）

1. **`-no-cnv` 参数已从 llama.cpp 移除** → llama-cli 直接返回空输出
   （表现成"0.03s 就返回"，极易误判成模型问题）
2. **`subprocess` 不能同时传 `stdin=` 和 `input=`** → `ValueError` 被吞成空输出
3. **模型只给 `cmd` 不给 `say` 时被误判成"没给出可用内容"** →
   **丢掉合法指令**（1.5B 必现；GBNF 里 `say` 本就可选）
4. **孤儿 `llama-server` 无自清理** → 累积后抢 CPU、把延迟拖成超时
5. **（安全）LLM 对垃圾输入瞎猜动作** → 9/14 误判 → 加关键词闸门后归零

### 待办（留给人工）

- ⏸ 真实场景调优：把**你自己说错的句子**逐条加进 `ACTION_EVIDENCE` 或示例
- ⏸ 可选：流式输出（首句边生成边播报）
- ⏸ 可选：界面里切换 0.5B / 1.5B
- ⏸ 可选：若嫌 ~16s 慢，可考虑把 LLM 也接到网络侧（PC 上跑大模型）

### 一句话总结性能

> **规则命中 → 0 秒；LLM 缓存命中 → 0 秒；真需要 LLM 推理 → 约 16 秒（首次约 35 秒）。**
> 常用控车指令全部走规则，所以日常体验不受这 16 秒影响。

---

# 十六、★ 外部（云端）LLM 接入 —— 2026-09-29 完成

> 上面「待办」里最后一条 *"若嫌 ~16s 慢，可考虑把 LLM 也接到网络侧"* —— **本轮做完了**。
> 实测：**云端 2.7~4.5 秒** vs 本地 **16~35 秒**，**快 5~10 倍**，理解力也强得多。

## 16.1 为什么值得做

板子装上 AX210 WiFi 后**能独立上网**（见工作区 `../../.workbuddy/memory/PROJECT_FACTS.md` §2b）。
而本地小模型（Qwen2.5-0.5B Q4）受本板算力限制，一次问答要 16~35 秒 ——
"问一句等半分钟"在人机交互上是硬伤。换云端后：

| 后端 | 单次问答 | 说明 |
|---|---|---|
| 本地 server（Qwen2.5-0.5B Q4） | **16~35 秒** | 本板 prompt 处理约 5 tok/s |
| **云端 GLM-4-Flash** | **2.7~4.5 秒** | 国内直连 ~0.4s，有免费额度 |

## 16.2 ★ 安全约束**一条都没放松**（最重要）

换后端**不等于放宽规则**。云端回复同样要过 `_parse_and_validate()`：

- **动作白名单**：`forward/backward/turn_left/turn_right/speed_up/speed_down`，**`stop` 不在其中**；
- **关键词证据闸门**（`cmd_has_evidence()`）：**动作语义必须在用户原话里有字面证据**；
- **置信度粗筛** `LLM_CMD_MIN_CONFIDENCE = 0.8`；
- **停车语义兜底**（`looks_like_stop()`）→ 直接禁用指令下发，只回话；
- **急停永不经过 LLM**（规则快路径先跑）。

> 换句话说：**云端 LLM 和本地 LLM 一样无权让车停或动。**
> 这套校验在 `_parse_and_validate()` 里，**与后端完全解耦** —— 这是当初分层设计的红利。

## 16.3 三种后端与 `auto` 模式

```bash
python3 car_gui_pyside.py                 # 默认 --llm-mode auto
python3 car_gui_pyside.py --llm-mode api      # 强制云端
python3 car_gui_pyside.py --llm-mode server   # 强制本地常驻
python3 car_gui_pyside.py --llm-mode cli      # 强制本地每次起进程
```

| 模式 | 行为 |
|---|---|
| `auto`（默认） | **配了 key 且网络通 → 云端**；否则**自动回退本地** |
| `api` | 强制云端（配置不全时 `load()` 会明确报错） |
| `server` / `cli` | 强制本地 llama.cpp |

> 🔑 `auto` 的设计意义：**没配 key 或没网的板子，行为与以前完全一致** ——
> 不会因为"加了云端支持"就把离线场景弄坏。

## 16.4 配置（key 绝不进代码库）

```bash
# 方式①：环境变量
export CAR_LLM_API_KEY='你的key'

# 方式②：配置文件（推荐，权限 600）
mkdir -p ~/.config/car_llm
cat > ~/.config/car_llm/api.json <<'EOF'
{
  "base": "https://open.bigmodel.cn/api/paas/v4",
  "model": "glm-4-flash",
  "key": "你的key"
}
EOF
chmod 600 ~/.config/car_llm/api.json
```

**读 key 的优先级**：显式入参 > 环境变量（`CAR_LLM_API_KEY` / `GLM_API_KEY` /
`ZHIPUAI_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`）> 配置文件 > 内置默认。

**零依赖**：只用标准库 `urllib.request`（板上没装 `requests`，也不需要装）。

自检（不消耗额度）：

```bash
bash run_py.sh llm_engine.py --mode api --status
```

## 16.5 ★★ 两个必须用 function-calling 的理由（实测踩出来的）

第一版用 `response_format={"type":"json_object"}`，结果**该纠错的 4 条指令全丢**：

```
钱进一米   → cmd=null  say='现在离线演示，动不了呢'      ← 指令丢了！
退后半米   → cmd=null  say='当前处于离线演示模式…'
开慢点儿   → cmd=null  say='现在还无法调节速度，请先连接小车'
往左边拐个弯 → cmd=null  say='现在还没连上小车呢'
```

**根因**：模型把 `[当前状态]` 里的"尚未连接小车/离线演示"理解成**"不许给指令"**，
于是用一句闲聊**替掉了 `cmd` 字段**。加提示词规则、改 `_llm_context()` 文案都压不住。

**解法**：改用 **function-calling**（`tools` + 强制 `tool_choice`），把 `cmd` 变成
**schema 必填字段** —— 模型**必须填出来**，"用闲聊代替填字段"这条路被堵死。

```
✓ 钱进一米    [尚未连接小车] → cmd=forward     1.9s
✓ 退后半米    [尚未连接小车] → cmd=backward    1.2s
✓ 开慢点儿    [尚未连接小车] → cmd=speed_down  2.0s
✓ 往左边拐个弯 [尚未连接小车] → cmd=turn_left   2.5s
→ 9/9 全对，而且比 json_object 更快
```

> 📌 **可复用的设计教训**：
> **"解析用户想干什么"和"此刻允不允许干"是两件事。**
> 前者归 LLM，后者归执行层（`_do_action()` / 演示模式开关）。
> 把执行状态塞进 LLM 上下文，会让模型**越权替执行层做决定**。
> 所以 `_llm_context()` 已改成**只给"怎么回话"有帮助的真事实**（连没连、速度、动作），
> **不给任何许可信息**。

## 16.6 修掉的另一个真问题：模型"假称已完成"

云端模型爱说 **"已停车""左转完成！""正在前进一米，请稍等"** ——
**它只是理解了意图，车根本没动**，但这话会让用户以为真执行了。已在提示词里**明令禁止**
（`已…` / `正在…` 一律不许），要求只描述**将要做什么**或**说明状况**：

```
停车  → cmd=None  say='还没连上车呢，需要先连接小车'          ✅
急停  → cmd=None  say='急停请使用急停按钮或说急停口令，目前尚未连接小车。'  ✅
钱进一米 → cmd=forward  say='好，等连上小车就往前开一米'        ✅
```

## 16.7 验收结果（全绿）

| 测试 | 结果 |
|---|---|
| 内置用例 `--mode api --selftest` | **9/9 通过**，平均 **4.30s/次** |
| **真实 ASR 14 条**（`test_real_asr_llm.py --mode api`） | **危险误判 0**、**该纠错却漏 0**、**该闲聊却空 0** |
| 边界复测（停车/急停/垃圾输入/假称完成） | **违规项 0** |
| 完整离线回归 `test_all_offline.py` | **10 通过 · 0 失败 · 1 跳过** |
| GUI 端到端 | `--demo` 启动 → **`auto` 自动选中 api** → 真实人声"你好。"→ **云端回复** |

**真实 ASR 关键用例**（云端）：

```
钱进一米   → forward      ✓  往左边拐个弯 → turn_left   ✓
开慢点儿   → speed_down   ✓  退后半米    → backward    ✓
能。/ 是是。/ 开玩去啊。/ 哎呀，操。→ 全部闲聊，无指令   ✓
今天天气怎么样 / 你叫什么名字     → 得体闲聊            ✓
```

## 16.8 改了哪些文件

| 文件 | 改动 |
|---|---|
| `car_gui/llm_engine.py` | 新增 `api`/`auto` 模式、`api_config()`、`api_reachable()`、`pick_mode()`、`SYSTEM_PROMPT_API`、`API_TOOL_SPEC`、`_run_api()`、`_build_prompt_api()`；`status()` 暴露后端信息 |
| `car_gui/car_gui_core.py` | `init_llm(mode=...)`；`_llm_context()` **不再传执行许可信息**；`llm_status()` 增加 `mode` |
| `car_gui/car_gui_pyside.py` / `car_gui_tk.py` | 新增 `--llm-mode`；面板标签区分**云端/本地**（原来一律写"本地"，接云端后会显示成"本地 LLM：glm-4-flash"，误导） |
| `car_gui/test_real_asr_llm.py` | 支持 `--mode`，可对任一后端做验收 |

## 16.9 待办

- ⏸ 真机联调：接上小车后验证云端 LLM 纠错出的指令**能正确下发**（本轮全是离线演示）
- ⏸ 成本观察：`status()` 里已记录 `api_prompt_tokens` / `api_total_tokens`，可据此评估
- ⏸ 可选：网络抖动时的降级（现在 `auto` 只在**启动时**判一次，运行中掉网会走超时路径）
- ⏸ 可选：把 `say` 直接接到 TTS 播报（现已接通，`--demo` 下已验证播报链路）
