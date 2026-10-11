# atlas200i-smart-car · 昇腾 200I DK A2 人机交互智能小车

> 在 **Atlas 200I DK A2（昇腾 310B4，8 TOPS）** 上把「**听 → 想 → 说 → 动**」整条链路跑通的语音交互小车：
> 端侧语音识别 + NPU 语音合成 + 云端/本地大模型纠错，全部指令经**规则与安全闸门**校验后，
> 通过串口下发给 STM32 底盘。

[![Platform](https://img.shields.io/badge/Platform-Atlas%20200I%20DK%20A2-1f6feb)](#)
[![SoC](https://img.shields.io/badge/SoC-昇腾%20310B4%20·%208%20TOPS-e34c26)](#)
[![CANN](https://img.shields.io/badge/CANN-7.0.RC1-8a2be2)](#)
[![Python](https://img.shields.io/badge/Python-3.10-3776ab)](#)
[![License](https://img.shields.io/badge/License-未指定-lightgrey)](#许可)

---

## 目录

- [它能做什么](#它能做什么)
- [硬件与软件栈](#硬件与软件栈)
- [架构](#架构)
- [安全设计](#安全设计不是事后补的)
- [快速开始](#快速开始)
- [仓库结构](#仓库结构)
- [文档索引](#文档索引)
- [实测数据](#实测数据)
- [已知限制](#已知限制诚实说明)
- [常见问题](#常见问题)

---

## 它能做什么

对着麦克风说一句话，小车会**听懂、回话、并按需动作**：

| 能力 | 说明 |
|---|---|
| 🎤 **语音识别** | sherpa-onnx **SenseVoice**（int8，228 MB）+ **silero VAD** 自动断句 |
| 🗣️ **语音合成** | **FastSpeech2 跑在昇腾 NPU 上**：平均 **RTF 0.92**，比 CPU 快 **5.9 倍** |
| 🧠 **语义纠错 / 闲聊** | 规则快路径未命中时交给 LLM：**云端智谱 GLM-4-Flash**（快）或**本地 Qwen2.5-0.5B**（离线） |
| 🕹️ **控车** | 串口下发速度/动作帧；带校验帧、断链检测、电压互锁 |
| 🆘 **语音急停** | 常驻关键词识别（KWS），命中即停车 → 锁定 → 播报，**不经过 LLM** |
| ⌨️ **手动与会话** | PySide6 图形界面（Tkinter 备用版）；键盘控制、指令下拉、语音/文本双通道 |
| 🔌 **离线演示** | 没接小车也能全链路跑通（只应答、不发串口）—— 方便在实验室外调试 |

一句话概括交互闭环：

```
麦克风 → ASR → ┬─ 规则快路径（命中即执行）
                └─ LLM（纠错/闲聊）→ 安全闸门 → 串口 → 底盘
                                  ↓
                             TTS（NPU）→ 喇叭
```

---

## 硬件与软件栈

### 硬件

| 部件 | 型号 / 说明 |
|---|---|
| 主控 | **Atlas 200I DK A2**（昇腾 310B4，8 TOPS，3×CPU + 1 AI Core，3.5 GB RAM） |
| 下位机 | **STM32F407 + FreeRTOS**，串口协议（115200，带 BCC 校验帧） |
| 底盘 | M500（4WD 固件；**不支持 Y 轴横移**，见[已知限制](#已知限制诚实说明)） |
| 音频 | USB 声卡 + 有源音箱；麦克风（有线 / 2.4G 无线 + USB-C→A 转接） |
| 网络 | 板载网口（接 PC 共享网络）或 Intel **AX210** WiFi |
| 供电 | 移动电源 / 12V 电池（4S） |

### 软件

| 层 | 选型 |
|---|---|
| 系统 | Ubuntu 22.04 (jammy)，Linux 5.10，LXQt |
| 加速栈 | **CANN 7.0.RC1**，`atc` 离线模型转换，pyACL 推理 |
| 语音识别 | sherpa-onnx · SenseVoice（int8）+ silero-vad |
| 语音合成 | **FastSpeech2 五个 `.om` 跑 NPU**（encoder / variance / decoder / postnet / hifigan） |
| 语音唤醒 | sherpa-onnx · Zipformer KWS（wenetspeech 3.3M）关键词（急停词表） |
| 大模型 | 云端：智谱 `glm-4-flash`（OpenAI 兼容）；本地：`qwen2.5-0.5b-instruct`（llama.cpp） |
| 界面 | **PySide6**（主）/ **Tkinter**（零依赖备用） |

---

## 架构

```
atlas200i-smart-car/
├── car_serial.py               ← 串口协议封装（厂商层，本项目不改动）
├── motion_control.py           ← 动作控制层（厂商层，本项目不改动）
├── example.py                  ← 厂商示例
└── car_gui/                    ← 核心代码（与界面框架无关的业务层 + 两种界面）
    ├── car_gui_core.py          业务核心：指令解析、安全闸门、串口、播报调度
    ├── car_gui_pyside.py        PySide6 界面（主）
    ├── car_gui_tk.py            Tkinter 界面（备用，零额外依赖）
    ├── asr_engine.py            语音识别：设备选择、录音、VAD 断句、识别
    ├── tts_engine.py            语音合成调度：播报队列、抢占、设备探测
    ├── npu_tts_engine.py        ★ NPU 上的 FastSpeech2 推理（本项目的硬核部分）
    ├── acl_infer_session.py     pyACL 封装（线程安全的 InferSession）
    ├── kws_engine.py            常驻关键词识别（语音急停）
    ├── llm_engine.py            大模型接入：云端 API + 本地 llama.cpp + 输出校验
    ├── test_*.py / *_preflight.py   离线回归 + 硬件专项测试
    ├── lint_undefined.py        轻量"未定义名字"检查器（补 py_compile 的盲区）
    └── run_*.sh / setup_*.sh / install_deps.sh / download_*.sh
```

> `car_serial.py` / `motion_control.py` 必须位于 `car_gui/` 的**上一级**
> —— `car_gui_core.py` 就是按这个相对位置查找它们的。

**分层原则**：`car_gui_core.py` 不依赖任何 GUI 库，因此 PySide6 与 Tkinter 两个界面
共用同一份业务逻辑；安全校验只在业务层做一次，任何界面路径都绕不过。

---

## 安全设计（不是事后补的）

移动机器人 + 语音大模型，最容易出的问题是「**模型胡说一句，车就动了**」。
本项目的安全约束是**分层落地**的：

| 约束 | 落地位置 | 说明 |
|---|---|---|
| 🚫 **急停永不经过 LLM** | `car_gui_core` | 「急停/停车」由**规则层**与**KWS**负责，LLM 的动作白名单里**没有 stop** |
| 🔍 **动作必须有字面证据** | `llm_engine.cmd_has_evidence()` | 模型可以纠错（"钱进一米"→前进），但**动作语义必须在原话里有字面依据**，否则只当闲聊 |
| 📉 **低置信度只聊天** | `LLM_CMD_MIN_CONFIDENCE = 0.8` | 置信度不足不下发动作 |
| ⚡ **规则快路径永远先行** | `run_text_command()` | 常用指令零延迟、零网络依赖；LLM 只是兜底 |
| 🔒 **急停后锁定需人工解除** | `estop_locked` | 误触发后车**不会自己又跑起来** |
| ✅ **不合法输出即丢弃动作** | `_parse_and_validate()` | 模型输出不是合法 JSON 时，**只取话术、绝不取动作** |
| 🛡️ **串口帧校验 + 断链检测** | `car_serial.py` | BCC 校验、状态帧超时判定、电压低于锁死线拒绝运动 |

> 离线回归里有一项专门验「**语音急停链路**」，以及一项验「**锁定态下所有运动指令被拒绝**」。

---

## 快速开始

### 1. 板子侧准备

```bash
# 依赖（会装 sounddevice / sherpa-onnx / PySide6 等）
bash car_gui/install_deps.sh

# 环境体检（缺什么一目了然）
bash car_gui/run_py.sh car_gui/env_doctor.py

# 下载模型（ASR 约 228 MB；KWS 约 3 MB）
bash car_gui/download_asr_model.sh
bash car_gui/download_kws_model.sh

# 串口权限 + 固定别名 /dev/ttyCAR
sudo bash car_gui/setup_serial_udev.sh

# 麦克风增益（免驱声卡默认常为 0%，不设就录不到音）
bash car_gui/setup_mic_gain.sh
```

### 2. 起界面

```bash
bash car_gui/run_gui.sh            # PySide6
# 或
bash car_gui/run_py.sh car_gui/car_gui_tk.py   # Tkinter 备用
```

**快捷键**：`F2`/`PgUp`/`PgDn`… 按住说话 · `空格` 急停 · `WASD`/方向键 手动控制 · `[` `]` 调速

### 3. 接入大模型（可选）

```bash
# 云端（推荐，2~4 秒返回）
export CAR_LLM_API_KEY='你的key'
# 或写进配置文件
printf '{"key":"你的key","model":"glm-4-flash"}' > ~/.config/car_llm/api.json
```

> ⚠️ **请勿把 key 提交进仓库**。程序只从环境变量 / `~/.config/car_llm/api.json` 读取。

### 4. 离线回归（不需要硬件）

```bash
bash car_gui/run_py.sh car_gui/test_all_offline.py
```

### 5. 上真机前必读

```bash
# ⚠️ 会把车真的动起来 —— 请先把车轮离地！
bash car_gui/run_py.sh car_gui/test_motion_hw.py

# 语音急停真机链路（只发零速度帧，车不会动）
bash car_gui/run_py.sh car_gui/test_estop_hw.py
```

---

## 仓库结构

| 路径 | 内容 |
|---|---|
| `car_gui/` | 全部源码、离线回归与硬件专项测试、运维脚本 |
| `car_serial.py` / `motion_control.py` / `example.py` | 小车串口协议与动作控制层（**厂商参考代码，本项目只读不改**） |
| `README.md` | 本文件 |

> **本仓库只收代码**。搭建过程的操作文档（18 篇，从「首次上电」到
> 「TTS 上 NPU 全过程」）以及约 966 MB 的厂商原包
> （Keil 安装包、STM32 器件包、AX210 内核模块、视频教程等）
> **均不在此仓库**，需要时请从各厂商官网自行获取。

---

## 实测数据

> 全部为本项目在真机上的实测值，非理论值。

| 指标 | 数值 |
|---|---|
| TTS 平均 RTF（NPU） | **0.92** |
| TTS 平均 RTF（CPU 对照） | 5.43（**NPU 快 5.9×**） |
| 单段合成耗时（NPU，静态 shape） | ≈ **1.11 s**（与文本长短无关） |
| ASR 模型加载 | ≈ 3 s（int8，228 MB） |
| 云端 LLM 往返 | **2.7 ~ 4.5 s** |
| 语音急停响应（KWS 命中） | ≈ **101 ms**（短路径，不被播报拖住） |
| 离线回归 | **通过 10 · 失败 0 · 跳过 1** |
| 真机运动验证 | **通过 23 · 失败 0 · 待人工确认 3** |
| 真机急停链路 | **通过 17 · 失败 0** |

---

## 已知限制（诚实说明）

这些是**实测确认的边界**，不是待办清单里的猜测：

1. **音频是半双工的** —— 本板采集与播放**硬件互斥**（同一 xHCI + 全速 USB 带宽不足）：
   - 播报时麦克风打不开；麦克风被常驻占用时播放失败（`aplay: set_params:1416`）。
   - 因此**播报期间语音急停听不到**（硬件限制）。需要即时急停请用**屏幕按钮 / 空格键**。
   - 程序内部已做「播报时 KWS 暂停让位、播完自动恢复」的分时复用。
2. **底盘不支持 Y 轴横移** —— 下位机按 4WD 驱动，左移/右移下发后 `vy` 恒为 0。
3. **逗号分段之间有停顿** —— 310B 只支持静态 shape，每段合成固定 ≈1.1 s。
   已尝试的三种优化（提高帧数上限 / 流水线 / 放宽分句长度）**均实测无效**：
   300 帧会让单段合成从 1.07 s 涨到 3.06 s；400 / 500 帧则让 decoder **段错误**。
   ⇒ 250 帧是本板实测最优点，逗号停顿属于静态 shape 的固有代价。
4. **昇腾 310B 只支持静态 shape** —— `max_mel_len` 存在硬上限：
   **250 帧 ✅ / 300 帧 ✅（但合成慢 3 倍）/ 400 与 500 帧会让 decoder 段错误**。

---

## 常见问题

<details>
<summary><b>麦克风能识别，但录到的全是底噪 / 电平恒为 0</b></summary>

免驱 USB 声卡的采集增益默认常为 **0%**（PCM2902 实测）。跑一次：

```bash
bash car_gui/setup_mic_gain.sh
```

另外本板插入多个 USB 音频设备时**声卡编号会变**，程序已按「设备能力打分」自动挑选
（优先纯采集设备、排除音箱卡），也可在界面上手动选择并记住。
</details>

<details>
<summary><b>播放报 <code>aplay: set_params:1416: Unable to install hw params</code></b></summary>

麦克风正被占用（本板采集/播放互斥）。若开了「语音急停」，程序会在播报前让它
自动让位；如果是外部程序占着麦克风，先停掉它。
</details>

<details>
<summary><b>语音急停显示「麦克风链路已断」</b></summary>

多半是播报占着麦克风、KWS 正在重试。程序会在播报结束后自动恢复（约 1~2 s）。
持续显示才需要检查麦克风是否被拔掉或增益被调为 0。
</details>

<details>
<summary><b>LLM 报「无有效 JSON」/ 不回话</b></summary>

模型偶发不按 JSON 格式输出。程序会**尽力抢救话术**并在必要时重试，
不会因此丢掉整条回复。若频繁出现，检查网络与 API key 配额。
</details>

---

## 许可

本仓库**尚未指定开源许可证**。若要正式开源，建议补一个 `LICENSE`
（如 MIT / Apache-2.0）；在补上之前，默认保留所有权利。

仓库内**不包含**任何厂商二进制包（Keil / STM32 器件包 / 内核模块等），
相关工具请自行从其官网获取并遵守其许可。

---

<p align="center">
  本项目为大学生创新创业训练计划（大创）作品 · 持续更新中
</p>
