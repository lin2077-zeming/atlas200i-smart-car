# 语音识别（ASR）

> **本文对应的代码：项目根目录下的 `car_gui/`**（在《GUI语音交互面板.md》基础上新增语音功能）
> 目标：在 **Atlas 200I DK A2（昇腾 310B4 + Ubuntu 22.04）** 上实现**离线中文语音控车**——
> 对着麦克风说「前进一米」，小车就走一米；说「急停」立刻停。
>
> 引擎：**sherpa-onnx + SenseVoice-Small（int8）**，全程离线，不联网、不上传音频。

---
> 🔵 **板子 IP 提示**：本文若出现 `192.168.137.100`，那是**网线直连 PC** 时的静态地址（一直有效）。
> 走 **WiFi** 时地址由路由器 DHCP 分配、**会变**。找不到就：
> ① PC 上双击 `../2.环境配置/工具脚本\find_car.cmd`；② 板子上 `bash find_ip.sh`；
> ③ 详见《../2.环境配置/快捷命令速查.md》**§0.1**。

## 一、这次做了什么

在已有的手动控制 GUI 上增加了一条完整的语音链路：

```
USB 麦克风 → 端点检测（断句） → SenseVoice 识别成文字 → 规则解析 → 串口下发 → 小车动
```

三层交付：

| 层 | 文件 | 作用 |
|---|---|---|
| 引擎层 | `asr_engine.py` | 录音、断句、识别，**不依赖 GUI** |
| 命令行层 | `asr_cli.py` | **SSH 里就能跑的语音控车**，调试识别率用它最快 |
| 界面层 | `car_gui_pyside.py` / `car_gui_tk.py` | GUI 里新增「语音输入」面板 |

**关键设计：识别在后台线程做，界面永不冻住。** SenseVoice 单次识别要 1–3 秒，如果放在界面线程里做，GUI 会卡住那么久——这是上一代踩过的同类型坑（`MotionController` 的 `time.sleep`），本次统一用「后台线程 + 结果队列」解决。

**本次不包含**：语音唤醒词（KWS 急停）、TTS 语音播报。

> 📄 这两项已各自独立成文档：
> - **KWS 语音急停** → 《语音急停-KWS.md》
> - **TTS 语音播报** → 《语音合成/TTS接入.md》

---

## 二、文件清单（本次新增/改动）

```
car_gui/
├── asr_engine.py            ← 新增：语音识别引擎（录音 / 断句 / 识别）
├── asr_cli.py               ← 新增：命令行语音控车（SSH 可用）
├── asr_preflight.py         ← 【新】ASR 环境预检（L0~L6 一键体检，无声卡也能跑）
├── env_doctor.py            ← 【新】Python 解释器体检（本板多套 Python 并存）
├── _pyenv.sh                ← 【新】解释器解析器（被各脚本 source）
├── run_py.sh               ← 【新】用正确解释器跑任意脚本（推荐入口）
├── mic_check.py             ← 【新】麦克风 + ASR 真机联调（不接小车，带电平条）
├── setup_mic_gain.sh        ← 【新】修 USB 声卡"默认增益 0%"问题（可装开机自启）
├── download_asr_model.sh    ← 新增：下载 SenseVoice 模型（约 156 MB）
├── car_gui_pyside.py        ← 改动：新增「语音输入」面板 + F2 快捷键
├── car_gui_tk.py            ← 改动：同上（Tkinter 备用版）
├── install_deps.sh          ← 改动：新增语音依赖（--no-asr 可跳过）+ 收尾提示预检
├── run_gui.sh               ← 改动：启动前提示语音依赖缺失
├── selftest_offline.py      ← 改动：新增 17 项语音逻辑断言（合计 52 项）
└── test_all_offline.py      ← 【新】统一回归入口（现为 6 套，含 asr_preflight / kws_preflight）
```

> **解释器解析（2026-09-26 新增）**：本板同时存在 `/usr/bin/python3`（3.10）、
> `~/miniconda3/bin/python3`（3.9）与若干 conda 环境，而语音依赖只装在其中一套。
> 直接敲 `python3 xxx.py` 很容易撞上「缺少 sounddevice」这类假故障。
> 为此新增 `env_doctor.py` / `_pyenv.sh` / `run_py.sh`，
> 并把 `install_deps.sh`、`run_gui.sh`、`setup_mic_gain.sh`、
> `download_asr_model.sh` 内部全部改为通过 `$CAR_PY` 调用，
> 装完会把解释器写进 `.python_for_car` 固定下来。

> **`asr_engine.py` 本次新增两项能力（2026-09-26）**：
> - `synthesize_test_wav()` —— 从官方 test_wavs 裁一段，**在没有麦克风的条件下**
>   生成标准 16bit PCM 音频，用于验证「录音数据 → 识别」的接口契约。
> - `--synthetic-selftest` 命令行入口，直接跑上面这条闭环。

原有文件**一律未改**：`car_serial.py`、`motion_control.py`、`example.py`、`car_gui_core.py` 的控制逻辑。

---

## 三、为什么选这套方案

| 候选 | 结论 | 理由 |
|---|---|---|
| **sherpa-onnx + SenseVoice（本次采用）** | ✅ | 上一代已在 RK3576/麒麟 上验证过同一套框架（见《../6.历史资料/上一代RK3576-KWS方案.md》），经验可直接迁移；官方提供 **aarch64 + Python 3.9 的预编译 wheel**，pip 一条命令装完，**不用源码编译**；SenseVoice int8 模型只有 228 MB，4 GB 内存放得下；中文识别效果好、自带标点 |
| 官方 WeNet 样例（跑在 NPU 上） | ⏸ 二期 | 官方"智能语音台灯"样例确实把 WeNet 转成 `.om` 跑在 NPU 上，但转换链路复杂、要动 CANN，一期不碰 |
| 云端 ASR（讯飞/百度 API） | ❌ | 断网即失效，直接推翻项目"离线"的核心卖点 |
| **sherpa-onnx 的 Ascend(NPU) 后端** | ⏸ 二期 | 已支持，但**要求 CANN 8.x 且需源码编译**，官方预置的 `.om` 只有 310P3 的，310B4 得自己用 ATC 导出。官方镜像现在是 CANN 7.0.RC1，升级有风险 → 定二期 |

> **一句话**：一期把 ASR 放在 **CPU** 上，先跑通、先能演示；NPU 留给视觉和后续优化。
> 这与《../1.总览与架构/项目总览导读.md》§3.4 的路线一致。

---

## 四、在开发板上需要安装什么

### 4.1 依赖总表

| 类别 | 包 | 用途 | 必须 |
|---|---|---|---|
| 音频底层 | **`libportaudio2`** | `sounddevice` 的底层库，**不装就一定报错** | ✅ |
| 音频工具 | `alsa-utils` | `arecord -l` / `aplay -l` 排查设备 | ✅ |
| ALSA 插件 | `libasound2-plugins` | 让声卡支持 16 kHz（自动重采样） | 建议 |
| Python | `numpy` | 音频数组运算 | ✅ |
| Python | `sounddevice` | 录音 | ✅ |
| Python | **`sherpa-onnx`** | 识别引擎 | ✅ |
| 解压 | `bzip2` | 解压模型包 | ✅ |
| 界面 | `PySide6-Essentials` | GUI（见文档 7） | 想用 GUI 才需要 |

### 4.2 一键安装

```bash
cd /home/HwHiAiUser/car_gui
bash install_deps.sh
```

新增的 `--no-asr` 参数可以只装手动控制那一套（省 300 MB+ 磁盘）：

```bash
bash install_deps.sh --no-asr     # 不装语音
bash install_deps.sh --minimal    # 只要手动控制（不装 Qt 也不装语音）
```

### 4.3 手工安装（脚本不好使时）

```bash
sudo apt update
sudo apt install -y libportaudio2 libasound2-plugins alsa-utils bzip2

python3 -m pip install numpy sounddevice sherpa-onnx \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> **为什么不用源码编译？** sherpa-onnx 官方在 PyPI 上提供
> `sherpa_onnx-1.13.8-cp310-cp310-manylinux2014_aarch64.manylinux_2_17_aarch64.whl`，
> 与板子的 Python **3.10.6** + aarch64 匹配，pip 直接装即可。**不需要 CANN，不需要编译。**
>
> 📌 **实测确认（2026-09-26）**：实际装上的是
> `sherpa_onnx-1.13.8-cp310-cp310-manylinux2014_aarch64.whl`，
> 板子系统 Python 是 **3.10.6**（早前记录的 3.9.2 有误），
> numpy 2.2.6 / sounddevice 0.5.6 一并装好，`import sherpa_onnx` 正常。

### 4.4 麦克风

> ### 🔴 **实测重大修正：板子根本没有板载 MIC**
>
> 2026-09-26 实机验证发现，官方内核**没有编译板载音频子系统**：
>
> ```
> $ cat /proc/asound/cards
> --- no soundcards ---
> $ arecord -l
> arecord: device_list:274: no soundcards found...
> ```
>
> 内核配置里三个关键项**全是关闭的**：
> ```
> # CONFIG_SND_DESIGNWARE_I2S is not set
> # CONFIG_SND_SIMPLE_CARD is not set
> # CONFIG_SND_AUDIO_GRAPH_CARD is not set
> ```
>
> **所以"板载有 1 个 MIC"这个说法要修正为：板载 MIC 也用不了**（不是没插好，是没驱动）。
> HDMI 音频同样没有声卡注册（`drv_hdmi` 报 `check_audio_attr fail`）。
>
> ✅ **好消息**：`CONFIG_SND_USB_AUDIO=y` **已编译进内核**，
> **USB 声卡插上即用、不需要装任何驱动**。
>
> ### 👉 **结论：USB 声卡不是"建议"，是"语音功能的唯一前提"**
>
> **没有 USB 声卡 = ASR 录音和 TTS 播放全部不可用**（手动控制不受影响）。

#### 声卡没到货之前，能做什么？—— 除了"真机录音"，其余全部可以做完

2026-09-26 已经把**不依赖声卡的环境工作全部完成并验证**，插上声卡那天只需确认最后一层：

| 链路层 | 是否需要声卡 | 当前状态 |
|---|---|---|
| L0 依赖（numpy / sounddevice / sherpa-onnx / PortAudio） | ❌ | ✅ 已验证 |
| L1 声卡（ALSA + sounddevice 设备枚举） | ✅ **需要** | ⏳ 等声卡 |
| L2 模型文件（3 个文件 + 大小校验） | ❌ | ✅ 已验证 |
| L3 引擎加载（5.5 秒） | ❌ | ✅ 已验证 |
| L4 识别官方音频（RTF 0.155） | ❌ | ✅ 已验证 |
| L5 **合成音频闭环**（见下） | ❌ | ✅ 已验证 |
| L6 指令解析（18/18） | ❌ | ✅ 已验证 |

**L5 是关键创新点**：用 `asr_engine.py --synthetic-selftest`，从模型自带的官方 `test_wavs`
裁一段 2.5 秒音频，**走 `recognize(16000, samples)` —— 也就是真实录音一模一样的那条代码路径**。
这样能在没有麦克风的情况下，提前排掉采样率、位深、声道、重采样、静音裁剪**一整类**问题。

```bash
python3 asr_preflight.py                    # 一键跑完 L0~L6 全部体检
python3 asr_preflight.py --no-model         # 快速版（不加载模型，约 1 秒）
```

期望结尾：

```
  通过 16 · 硬失败 0 · 待硬件 2

  ⏳ 等硬件/可延后（不影响现在推进）：
     · ALSA 声卡注册 无（no soundcards）
     · 录音设备数量 0 个
  ✅ 除声卡外，ASR 环境已全部就绪。
```

> **判断标准**：只要「硬失败」是 **0**，就说明环境侧没有遗留问题，
> 剩下的纯粹是硬件。声卡到货后重跑，L1 会自动从 ⏳ 变成 ✅。
>
> 这个脚本已接进统一回归入口：`python3 test_all_offline.py`（现为 **5 套全过**）。

#### ✅ 已实测接入的声卡（2026-09-26，推荐照这个买）

实际插上并跑通了 **TI PCM2902**（C-Media 封装版），**正是规格表里推荐的芯片**：

```
$ lsusb | grep -i audio
Bus 003 Device 008: ID 08bb:2902 Texas Instruments PCM2902 Audio Codec

$ cat /proc/asound/cards
 0 [Device]: USB-Audio - USB PnP Sound Device
              C-Media Electronics Inc. USB PnP Sound Device
$ arecord -l
card 0: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
```

**零配置**：插上直接注册 `card 0`，**不需要装任何驱动、不需要改任何配置文件**。

##### ⚠️ 两个必须先知道的坑（都踩过了）

> ### 🔴 坑 1：默认录音增益是 **0%**，录音全是静音
>
> **现象**：设备认得到、`arecord` 能出 wav、但录下来峰值只有 **0.0017**（纯底噪），
> ASR 识别不出任何内容。**极易误判成"麦克风坏了"或"驱动有问题"。**
>
> ```bash
> $ amixer -c 0 sget Mic
>   Mono: Capture 0 [0%] [0.00dB] [on]      ← 0%！完全静音
> ```
>
> **修复**：
> ```bash
> amixer -c 0 sset Mic 100%                  # 拉到 16 [100%] [23.81dB]
> amixer -c 0 sset 'Auto Gain Control' on    # 顺便开 AGC
> alsactl store 0                            # 持久化
> ```
>
> **修复前后对比（同一颗麦克风、同样环境）**：
>
> | 文件 | 增益 | 峰值 | 分段峰值（每 0.5s） |
> |---|---|---|---|
> | 修复前 | **0%** | 0.0017 | 0.002 0.001 0.001 … 全是底噪 |
> | 修复后 | **100%** | **0.8580** | 0.158 0.275 0.524 **0.858** 0.380 ← 典型语音包络 |
>
> **本项目已做自动化处理**：新增 `setup_mic_gain.sh`，一键设置 + 验证 + 可选装开机自启：
> ```bash
> bash setup_mic_gain.sh --install    # 装成 mic-gain.service，断电重启也不怕
> ```

> ### 🟡 坑 2：硬件**只支持 44100 / 48000 Hz，没有 16kHz**
>
> ```bash
> 16000 Hz  失败: Invalid sample rate
> 22050 Hz  失败: Invalid sample rate
> 32000 Hz  失败: Invalid sample rate
> 44100 Hz  OK
> 48000 Hz  OK
> ```
>
> **影响**：引擎原本"优先试 16kHz"策略在这颗声卡上**必然失败一次**，
> PortAudio 会刷 3 行错误日志再回落到 44100，观感差、且白撞一次打开动作。
>
> **已优化**：`Recorder.start()` 现在带**采样率缓存**（key 用设备名，重插后 index 变了也不失效）——
> 首次探测成功后记住，后续录音**直接命中、零报错**。实测：
> ```
> 第1次: 录音中 @ 44100 Hz    ← 有一次探测报错
> 第2次: 录音中 @ 44100 Hz    ← 直接命中，无报错
> ```
>
> **不必强行追求 16k**：sherpa-onnx 内部会自动重采样（实测 RTF 仍为 0.17 左右），
> 识别质量没有可感知的下降。

> ### 🟡 注意 3：这类声卡常常**只有输入、没有输出**
>
> 实测这颗 `aplay -l` 是**空的** —— 只能录音，没有播放设备。
> **对 ASR 完全没影响**（ASR 本来只需要输入），但意味着：
> **将来做 TTS 时必须另配输出设备**（有源音箱 / 带 DAC 的 USB 声卡）。

##### 录音设备号怎么选

```bash
$ python3 asr_engine.py --list-devices
  [0] USB PnP Sound Device: Audio (hw:0,0)  44100 Hz  1 ch   ← 选这个
  [1] sysdefault                            48000 Hz  128 ch  ← ALSA 软插件，别选
  [2] spdif                                 44100 Hz  1 ch
  ...
建议使用设备号：0
```

> **只认第一个真设备**。`sysdefault` / `spdif` / `samplerate` / `speexrate` / `upmix` / `vdownmix`
> 都是 ALSA 的**软件插件虚拟节点**（注意 channel 数是 128/8/6 这种假值），不是真实硬件。
> `guess_input_device()` 已经会优先挑名字含 "usb" 的真设备。

#### 推荐购买的声卡规格
**硬性要求**：

| 规格 | 要求 | 原因 |
|---|---|---|
| **免驱（USB Audio Class 1.0）** | 必须 | 板子是 **aarch64 + 内核 5.10**，厂商基本不出 arm64 Linux 驱动 |
| **独立麦克风输入** | 必须 | 输入（ASR）比输出（TTS）更关键；只带输出口的用不了 |
| **USB Type-A 公头** | 推荐 | 板子给的是 2 个 Type-A 口 |
| **16 kHz 兼容** | 兼容即可 | SenseVoice 是 16k 模型；常见 44.1k/48k 声卡会自动重采样 |
| **经典 UAC1 芯片** | 推荐 | `Cmedia CM108/CM119`、`SSS1629`、`PCM2902` 等，Linux 零配置 |

**三种买法**：

| 方案 | 内容 | 价格 | 说明 |
|---|---|---|---|
| **A. 声卡 + 手机耳机** | 声卡（带 3.5mm 耳麦口）+ 现有耳机 | ¥15–35 | 耳机上的麦即可录音，耳机当喇叭；**有耳机就够** |
| **B. 声卡 + 领夹麦** ⭐ | 声卡 + 3.5mm 领夹麦 | **约 ¥35** | 麦克风能夹在小车上朝向用户，**收音明显更好，推荐** |
| C. 一体件 | USB 全向麦 / 免驱 USB 麦克风音箱 | ¥50–120 | 一根线搞定，但体积大、不便装车 |

> ### ⚠️ 分清"输入"和"输出"，别买多了
>
> - **ASR（听你说话）→ 只需要麦克风输入。** 语音控车、KWS 急停**全程不需要喇叭**。
> - **TTS（让小车说话）→ 才需要喇叭**，这是后面的事。
>
> **所以：只想先跑通语音控车 →「声卡 + 麦克风」就够了，喇叭可以先不买。**
> 想验证输出，随便插副闲置耳机到声卡输出口就能听见。
>
> 将来买喇叭时：**必须买"有源"音箱（带 USB 供电或自带电池）**，
> 无源小喇叭没功放推不动，声音小到听不见。

**❌ 不要买**：
- **USB-C 耳机转接头** —— 内含 DAC 芯片，多数在 Linux 下不认
- **需要厂商驱动的专业录音声卡** —— 无 aarch64 驱动
- **"USB 声卡"却不带麦口的纯输出转接头** —— ASR 用不了

#### ⚠️ USB 口余量（实测拓扑）

板子**只有 1 个 USB 主控（`xhci-hcd/1p`，单端口）**，那 2 个 Type-A 口实际是
**一颗 Realtek RTS5411 hub 分出来的**：

```
Bus 03 →Port1→ Hub(4p) → Port1: 键鼠接收器
                       → Port2: 键鼠接收器
      （还剩 2 个空口）
```

所以 **声卡 1 口 + USB 摄像头 1 口 = 刚好用完**。建议顺手**买个带独立供电的 USB Hub（¥20–30）**。
这也是为什么视觉一定要走 **MIPI-CSI** —— 省的不是钱，是口。

插好后确认设备：

```bash
arecord -l                                   # ALSA 层面看设备
python3 asr_engine.py --list-devices         # sounddevice 层面看设备号
python3 asr_preflight.py                     # 一键复检（L1 应转为 ✅）
```

---

## 五、下载模型

模型：`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`

| 文件 | 大小 | 说明 |
|---|---|---|
| `model.int8.onnx` | 约 228 MB | 识别模型（int8 量化，4GB 内存必须用这个） |
| `tokens.txt` | 约 308 KB | 词表 |
| `silero_vad.onnx` | 约 0.6 MB | 断句模型（可选，但强烈建议） |
| `test_wavs/` | 几个 wav | 官方测试音频，用来验证识别是否正常 |

> **不要下 float32 版**：`model.onnx` 有 894 MB，4GB 内存的板子会很吃力。

### 5.1 方式 A：在板子上下载（板子能上网时）

```bash
cd /home/HwHiAiUser/car_gui
bash download_asr_model.sh
```

脚本会自动在 `github` / `ghfast.top` / `gh-proxy.com` / `ghproxy.net` 四个源之间依次重试，并校验文件大小。默认装到 `~/models/sense-voice/`。

指定别的源或目录：

```bash
bash download_asr_model.sh --via ghfast
bash download_asr_model.sh --dest /data/models/sense-voice
bash download_asr_model.sh --keep-tarball      # 保留压缩包，便于拷给队友
```

### 5.2 方式 B：在 PC 上下载再传（**板子网速慢时推荐**）

板子直连 GitHub 经常龟速。按《../2.环境配置/SSH连接与传文件.md》的原则：**先在 PC 上下好，再走千兆局域网传过去**。

在 PC 的 Git Bash 里：

```bash
cd "C:/Users/LinZeming/Desktop/大创小车项目/昇腾开发板-大创小车/car_gui"
bash download_asr_model.sh --dest /c/Users/LinZeming/Downloads/sense-voice
```

然后传过去（约 230 MB，千兆直连 3–5 秒）：

```bash
tar czf - -C "/c/Users/LinZeming/Downloads/sense-voice" . | ssh root@192.168.137.100 \
    "mkdir -p /home/HwHiAiUser/models/sense-voice && tar xzf - -C /home/HwHiAiUser/models/sense-voice"
```

### 5.3 装到哪：**优先 `/data`，别塞 SD 卡**

```bash
df -h / /data
```

系统盘通常只有 32 GB TF 卡，228 MB 模型 + 系统 + 代码很快就紧张。挂了 NVMe 的话（见文档 7 §8.3）：

```bash
bash download_asr_model.sh --dest /data/models/sense-voice
# 之后运行时用 --model-dir /data/models/sense-voice 指定
```

---

## 六、部署与验证（**分层验证，别一步到位**）

语音链路环节多（依赖 → 声卡 → 模型 → 识别 → 解析 → 串口），出错要一层层查。**按下面顺序走**，每一步失败都能立刻定位。

### ⭐ 第 −1 层：一键体检（**先用这个，再往下走**）

```bash
cd /home/HwHiAiUser/car_gui
python3 asr_preflight.py
```

它会一次跑完 L0~L6 全部检查，并且**每个失败项都直接给出修复命令**。
输出结尾的汇总里，只要「**硬失败 = 0**」就说明环境侧没问题。

```bash
python3 asr_preflight.py --no-model    # 快速版：不加载模型，约 1 秒
python3 asr_preflight.py --model-dir /data/models/sense-voice   # 指定模型目录
```

**实测输出（2026-09-26，声卡未到货时）**：

```
  通过 16 · 硬失败 0 · 待硬件 2
  ⏳ ALSA 声卡注册  无（no soundcards）
  ⏳ 录音设备数量   0 个
  ✅ 除声卡外，ASR 环境已全部就绪。
```

> 📌 **L1 那两项"待硬件"不是失败**，是板子没有音频硬件导致的，不会计入退出码。
> 声卡插上后重跑，它们会自动变成 ✅。

### 第 0 层：离线自测（不碰硬件、不碰声卡）

```bash
cd /home/HwHiAiUser/car_gui
python3 selftest_offline.py
```

期望结尾：

```
 通过 52 项，失败 0 项
```

其中第 [10] 组是本次新增的语音逻辑断言（文本清洗、重采样、端点检测、增量取音）。

### 第 1 层：依赖是否齐

```bash
python3 asr_engine.py --status
```

期望看到：

```
  numpy          OK
  sounddevice    OK
  sherpa_onnx    OK
  模型文件   : /home/HwHiAiUser/models/sense-voice/model.int8.onnx
  tokens.txt : .../tokens.txt
  silero_vad : .../silero_vad.onnx
```

哪一项是"缺失"，就回第四节补那一项。

### 第 2 层：麦克风能不能录到声音

```bash
python3 asr_engine.py --list-devices
```

期望列出形如：

```
  [0] USB PnP Sound Device: Audio (hw:0,0)    44100 Hz  1 ch
```

> **注意两点**：
> 1. USB 声卡的设备号会在插拔/重启后变化。每次部署前都用 `arecord -l` 或上面这条命令复核，不要写死设备号。
> 2. **只认第一个真设备**（名字带 "USB"、声道数为 1 的那种）；`sysdefault` / `spdif` / `upmix`
>    这些是 ALSA 软件插件虚拟节点，channel 数是 128/8/6 的假值，别选。

#### ⭐ 2.1 先修增益，再测电平（**最容易踩的坑**）

USB 免驱声卡的默认录音增益经常是 **0%** —— 设备认得到、能录音、但录下来全是静音。
**先做这一步，否则后面所有识别都会失败，而且看起来像"麦克风坏了"。**

```bash
bash setup_mic_gain.sh --install    # 一键：设 100% + 开 AGC + 持久化 + 装开机自启
```

手动版（想自己控制时）：

```bash
amixer -c 0 sget Mic                  # 看当前值，若是 0% 就是它的问题
amixer -c 0 sset Mic 100%
amixer -c 0 sset 'Auto Gain Control' on
alsactl store 0
```

**验证电平**（说话时要有明显跳动）：

```bash
python3 mic_check.py --seconds 5      # 录 5 秒，打印实时电平条 + 识别结果
```

期望看到电平条随说话大幅摆动，峰值 **> 0.05**：

```
  ▮▮▮▮▮▮▮▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯▯   2.35s
```

> **判据**：峰值 < 0.01 基本就是静音（增益没设或麦克风没插对孔）；
> 峰值 > 0.98 是过载削波，把增益降到 80%。

### 第 2.5 层：真机语音联调（**不接小车**）

```bash
python3 mic_check.py                   # 交互：按回车开始，说完再按回车
python3 mic_check.py --seconds 3       # 固定录 3 秒
python3 mic_check.py --auto            # 自动聆听：说一句停顿即识别
```

它会同时打印 **识别原文** 和 **解析出的动作**，一眼看出问题出在"没录到"、"识别错"还是"解析错"：

```
  识别：前进一米
  音频 1.82s · 识别 0.31s · 实时率 0.170
  解析：{'action': 'forward', 'speed': 300, 'duration': 3.33}
```

> **强烈建议先跑这个、再开 GUI**：命令行没有显示依赖，SSH 里就能用，
> 而且不会误触发小车动作。确认识别准了，再接串口。

### 第 3 层：模型能不能识别**已知音频**

**这一步最关键——它把"麦克风问题"和"模型问题"彻底分开。**

```bash
python3 asr_engine.py --model-dir ~/models/sense-voice \
    --file ~/models/sense-voice/test_wavs/zh.wav
```

首次加载模型需要 10–30 秒（228 MB），之后每次识别 1–3 秒。期望输出：

```
识别结果：开放时间早上9点至下午5点。
```

**如果这里能出正确文字，说明模型和引擎都没问题**，剩下要查的就只有麦克风了。

### 第 4 层：语音控车（命令行，**SSH 里就能跑**）

> **重要**：GUI 必须有显示器，但**语音识别本身在 SSH 里完全能跑**（音频走板子本地 ALSA，与显示无关）。所以调试语音链路一律先用命令行，比开 GUI 快得多。

**先只测识别、不下发运动指令**（最安全的调试方式）：

```bash
python3 asr_cli.py --no-exec
```

然后按提示操作：**按回车开始录音 → 说「前进一米」→ 再按回车结束**，看识别结果对不对。

识别准了，再接入小车：

```bash
python3 asr_cli.py --port /dev/ttyCAR
```

或者用自动聆听模式（说一句、停一下，自动断句识别）：

```bash
python3 asr_cli.py --port /dev/ttyCAR --auto
```

> **⚠️ 第一次接地测试前**：小车放地上、周围留 1 米以上空间、手放在物理电源开关上。

### 第 5 层：GUI

```bash
cd /home/HwHiAiUser/car_gui
bash run_gui.sh --model-dir ~/models/sense-voice
```

在 GUI 里点「加载模型」→ 等状态变成 **已就绪** → 然后：

- **按住「按住说话」按钮**（或按住 **F2**）说指令，**松开**即开始识别
- 或点「开始聆听」进入自动聆听模式

---

## 七、界面与操作说明

### 7.1 语音面板

```
┌─ 语音输入 ─────────────────────────────────────────────────────────────────────┐
│ 模型目录 [~/models/sense-voice                        ] [加载模型]  已就绪·断句:silero │
│ 麦克风 [USB PnP Sound Device ▾] [刷新]  ☑识别后自动执行  [按住说话（或F2）] [开始聆听] │
│ 输入电平 ▮▮▮▮▯▯▯▯▯▯    识别结果：前进一米                                        │
└───────────────────────────────────────────────────────────────────────────────┘
```

| 控件 | 说明 |
|---|---|
| **模型目录** | 模型所在文件夹，默认 `~/models/sense-voice` |
| **加载模型** | 点一次即可；加载中约 10–30 秒，界面不会卡（后台线程） |
| **状态文字** | `已就绪 · 断句:silero` 表示模型可用且用 Silero VAD 断句；`断句:energy` 表示退回了能量断句 |
| **麦克风** | 下拉选录音设备，点「刷新」重新枚举 |
| **识别后自动执行** | ☑ 识别到指令就直接下发（默认开）。**调试识别率时取消勾选**，只显示文字不动车 |
| **按住说话** | **按住 → 说 → 松开**，松开后 1–3 秒出结果 |
| **开始聆听** | 自动聆听：说一句、停顿约 0.8 秒自动结束并识别，无需按键 |
| **输入电平** | 实时音量条。**说话时它必须动**——不动就说明麦克风没采到声音，先解决这个再谈识别率 |

### 7.2 两种聆听模式怎么选

| 模式 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| **按住说话（PTT）** | 确定性强，不会误触发；不依赖断句算法 | 要按住按钮/键 | **默认用它**，安静环境下最稳 |
| **自动聆听** | 免按键，演示效果好 | 小车电机噪声可能被误判成说话 | 演示时用；开电机跑动时慎用 |

### 7.3 快捷键（在文档 7 的基础上新增）

| 按键 | 功能 |
|---|---|
| **F2（按住）** | **按住说话**，松开开始识别 |
| `空格` | 急停（任何时候都有效，优先级最高） |
| `W/A/S/D` 或方向键 | 移动（按住走、松开停） |
| `[` / `]` | 减速 / 加速 |
| `F11` | 全屏切换 |

### 7.4 能听懂的指令

沿用文档 7 §6.3 的规则解析（**已通过 18 项回归测试**）：

| 说法 | 效果 |
|---|---|
| `前进` / `往前走` | 以当前速度持续前进 |
| `前进一米` / `前进两米` | 按距离走，自动换算时长 |
| `前进 2 秒` | 走 2 秒自动停 |
| `后退` / `往后退一点` | 后退 |
| `左转` / `左转90度` / `右转45度` | 转向；带角度自动换算 |
| `左移` / `右移` | 麦轮平移 |
| `速度 200` / `速度调到三百` | 设定速度 |
| `以两百的速度后退两秒` | 指定速度 + 动作 + 时长 |
| `快一点` / `慢一点` | 每次 ±50 mm/s |
| **`停` / `停止` / `急停` / `别动` / `刹车`** | **立即停止** |
| 其他 | 不下发任何动作，日志里提示"没听懂" |

> **识别结果带标点也没关系**（`use_itn` 会输出「前进一米。」），解析用的是子串匹配，标点不影响。

---

## 八、参数调整

### 8.1 常用参数（`asr_engine.py` 文件头部）

```python
DEFAULT_THREADS = 2       # 识别线程数。板子只有 4 核，给 GUI 和串口留 2 核
DEFAULT_LANGUAGE = "zh"   # 锁定中文，避免 auto 把中文误判成日文/粤语
ASR_SAMPLE_RATE = 16000   # SenseVoice 的特征采样率

PTT_MIN_SECONDS = 0.3     # 短于此判为误按
AUTO_TIMEOUT = 8.0        # 自动聆听：一直没说话就退出
AUTO_MAX_SECONDS = 12.0   # 自动聆听：单句最长
AUTO_TAIL_SILENCE = 0.8   # 说完后静音多久算结束
```

命令行也能改：`--threads 3`、`--lang auto`。

### 8.2 调优建议

| 症状 | 调整 |
|---|---|
| 识别太慢、界面卡顿 | `--threads 1`（先减线程）；关掉不用的程序；跑识别时别传大文件 |
| 说话稍慢就被截断 | 提高 `AUTO_TAIL_SILENCE` 到 1.2 |
| 一句话太长被切 | 提高 `AUTO_MAX_SECONDS` 到 20 |
| 电机噪声误触发自动聆听 | **改用"按住说话"模式**（推荐）；或换 Silero VAD（见 8.3） |
| 识别经常错字 | 说慢一点、靠近麦克风；确认 `--lang zh`；把麦克风远离电机 |

### 8.3 断句：Silero VAD vs 能量断句

引擎会**优先用 Silero VAD**（`silero_vad.onnx` 存在且能加载时），失败才退回自研的能量端点检测。

- **Silero VAD**：神经网络判断，抗噪好，是首选
- **能量断句**：纯 numpy 算 RMS + 自适应噪声底噪，零额外依赖，安静环境够用

看当前用的是哪个：

```bash
python3 asr_engine.py --status     # 看 silero_vad 是否"未找到"
```

GUI 状态栏也会显示 `断句:silero` / `断句:energy`。

---

## 九、性能与实测数据

> ### ✅ **已实测（2026-09-26，Atlas 200I DK A2 实机）**
>
> | 指标 | 实测值 | 说明 |
> |---|---|---|
> | **模型加载** | **5.45 秒** | 228MB int8 模型，比预估的 10–30 秒**快得多** |
> | **实时率 RTF** | **≈ 0.157** | 约 **6.4 倍实时速度**，比预估的 0.4–0.8 **好 3~5 倍** |
> | 内存占用 | ~120 MB（GUI 全量）/ 模型本身约 400 MB 峰值 | 4GB 内存充裕 |
>
> **逐语种实测明细**（模型加载后，取多次平均）：
>
> | 语种 | 音频时长 | 推理耗时 | RTF |
> |---|---|---|---|
> | 中文 zh | 5.6 s | 0.88 s | **0.157** |
> | 英文 en | 7.2 s | 1.13 s | **0.158** |
> | 日文 ja | 7.2 s | 1.13 s | **0.157** |
> | 粤语 yue | 5.1 s | 0.80 s | **0.155** |
> | 韩语 ko | 4.6 s | 0.72 s | **0.157** |
>
> **识别结果**（与官方参考对比）：
>
> | 音频 | 识别结果 | 判定 |
> |---|---|---|
> | zh.wav | 「**开饭**时间早上9点至下午5点。」（参考为"开放"） | ⚠️ int8 量化正常误差 |
> | en.wav | "The tribal chieftain called for the boy..." | ✅ 完全一致 |
> | ja.wav | "うちの中学は弁当制で持っていきない場合は50円の..." | ✅ 完全一致 |
> | yue.wav | "呢几个字都表达唔到我想讲嘅意思。" | ✅ 完全一致 |
> | ko.wav | "조금만 생각을 하면서 살면 훨씬 편할 거야。" | ✅ 完全一致 |
>
> ### 📌 实测带来的三个结论
>
> 1. **ASR 在 CPU 上跑完全够用** —— 说 5 秒的话只需 0.8 秒识别，交互流畅。
> 2. **"ASR 上 NPU"的紧迫性大幅降低** —— 从"必须做"降级为"锦上添花的加分项"（二期）。需要 CANN 8.x + 源码编译 + 自行 ATC 导出 310B4 的 om，投入产出比现在更不划算。
> 3. **CPU 余量比预期充裕** —— 2 线程推理只用到 6 倍实时速度的约 1/6，`install_deps.sh` 默认线程数设置合理。
>
> **⚠️ 精度注意**：int8 模型有轻微字错（"开放"→"开饭"）。因此**指令解析器必须保持宽松匹配**，
> `parse_text_command()` 用的是 `in` 包含匹配而非精确相等，这个设计是**正确的、要保留**。
>
> **参照系**：上一代 RK3576（8 核 A72@2.2GHz）跑 KWS 实测 RTF 0.127；
> 本板 CPU 虽只有 4×1.0GHz，但 SenseVoice-Small 模型本身很小，**实测 RTF 反而只比它差一点点**。

### 9.1 当初的估算（保留作对照）

| 指标 | 当初估算 | 实测 | 偏差 |
|---|---|---|---|
| 模型加载 | 10–30 秒 | **5.45 秒** | 快了 2~5 倍 |
| 单次识别耗时 | 1–3 秒（3 秒语音） | **约 0.5 秒（3 秒语音）** | 快了 2~6 倍 |
| 实时率 RTF | 0.4–0.8 | **0.157** | 好 3~5 倍 |
| 内存占用 | 400–500 MB | 约 400 MB 峰值 | 基本吻合 |

> **如果实测 RTF 明显大于 1**（本板不会），说明 CPU 撑不住，这时应该：
> ① 先减 `--threads`；② 关掉 GUI 只用命令行；③ 走二期路线把 ASR 挪到 NPU。

### 9.2 预检复核数据（`asr_preflight.py`，2026-09-26 复跑）

声卡到货前跑预检的实测输出，作为**环境基线**留档：

```
L0 依赖      numpy 2.2.6 / sounddevice 0.5.6 / sherpa-onnx 1.13.8
             PortAudio V19.6.0-devel · Python 3.10.12
L1 声卡      ⏳ 无（no soundcards）· 录音设备 0 个 · ALSA 工具链齐备
L2 模型      model.int8.onnx 228.2 MB · tokens.txt 0.3 MB
             silero_vad.onnx 0.6 MB · test_wavs/ 五种语言齐
L3 加载      5.47 秒 · VAD = silero · 2 线程
L4 识别      RTF 0.155（音频 5.6s / 耗时 0.87s）
L5 合成闭环  生成 2.50s @ 16kHz → 录音路径识别 0.44s ✅
L6 指令解析  18/18 全对
汇总         通过 16 · 硬失败 0 · 待硬件 2
```

> **L5 的合成音频闭环**是本轮新增的关键验证：结果「开放时间早上9」是**正确**的 ——
> 它只是从原句开头裁出的 2.5 秒片段，只含"开放时间早上9"这部分，后面被截掉了。
> 有意思的是这里「**开放**」反而识别对了，而 L4 识别完整音频时是「开饭」——
> 说明 int8 的误差**与音频长度/上下文有关**，进一步印证了"解析器必须宽松匹配"的设计。

---

## 十、常见问题排查

> ### 💡 先跑这个，能省掉 80% 的排查
>
> ```bash
> python3 asr_preflight.py
> ```
>
> 它按 L0~L6 分层体检，**每个失败项都直接给出修复命令**。
> 看到「硬失败 = 0」就说明环境没问题，可以放心往下走。
>
> 如果报「缺 sounddevice / 缺 sherpa-onnx」，先别急着装 —— 大概率是
> **用错了 Python**（本板多套并存），跑 `python3 env_doctor.py` 确认：
>
> ```bash
> python3 env_doctor.py            # 列出所有解释器各自装了什么
> python3 env_doctor.py --write    # 固定最佳解释器
> bash run_py.sh <脚本> [参数]      # 之后统一这样跑
> ```

### 10.1 依赖与安装

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'sherpa_onnx'` | 没装 | `bash install_deps.sh` |
| **`缺少 sounddevice` / `No module named 'sherpa_onnx'` 但确认已装** | **用错了 Python**——本板同时有系统 3.10 / conda base 3.9 / conda 环境，依赖只装在其中一套 | **`python3 env_doctor.py`** 看哪个解释器可用；`python3 env_doctor.py --write` 固定；之后统一用 **`bash run_py.sh <脚本>`** |
| `OSError: PortAudio library not found` | 缺 `libportaudio2` | `sudo apt install -y libportaudio2` |
| `pip` 装 sherpa-onnx 卡住/失败 | 网络 | 换源：`-i https://pypi.tuna.tsinghua.edu.cn/simple`；或先在 PC 下 wheel 再传 |
| 装了但还是 `No module named` | **在 conda 环境里装的，跑却在另一套** | `conda deactivate` 后重装，或用 `env_doctor.py --write` 固定；**不要**只靠 `export PATH` |
| `No space left on device` | 模型装到 SD 卡系统盘了 | 用 `--dest /data/models/sense-voice` |

### 10.2 模型

| 现象 | 原因 | 解决 |
|---|---|---|
| `在 xxx 里找不到模型文件` | 模型没下或路径写错 | `bash download_asr_model.sh`，再 `python3 asr_engine.py --status` 确认 |
| 加载后识别出乱码/空 | 模型下坏了 | 检查 `model.int8.onnx` 是否约 228 MB；删了重下 |
| 解压报错 | 缺 `bzip2` | `sudo apt install -y bzip2`；或 PC 上用 7-Zip 解压后拷过去 |
| 加载很慢（>60 秒） | 模型在 SD 卡上 | 挪到 NVMe（`/data`） |
| 内存不足 / 被 OOM kill | 用了 float32 模型 | 换 int8 版（228 MB） |

### 10.3 麦克风与识别

| 现象 | 原因 | 解决 |
|---|---|---|
| `--list-devices` 列表为空 | USB 声卡没插好 / 缺 PortAudio | 重插 USB；`arecord -l` 复核；`sudo apt install -y libportaudio2` |
| 电平条**完全不动** | 麦克风没采到声音 | 检查选对设备号；`arecord -l` 看卡号；系统音量/输入增益（`alsamixer`，F4 切到 capture） |
| 电平条一动就录完 | 阈值太低，被噪声触发 | 改用"按住说话"模式 |
| 一直提示"没听到声音" | 设备选错，或麦克风静音 | 先用 `arecord -d 3 test.wav && aplay test.wav` 验证能不能录能放 |
| 识别结果总是"没听清" | 声音太小 / 太远 | 靠近麦克风 20–30 cm，正常音量说 |
| 中文被识别成日文/粤语 | `language` 没锁 | 已经是默认 `zh`；若改过就改回 `--lang zh` |
| **短指令识别不准**（"前进"听成"前进吧"） | 正常现象，模型没做领域适配 | **说完整一点**（"前进一米"比"前进"更稳）；或在解析层加同义词 |
| 电机一转就识别不到 | 电机噪声盖过人声 | 见下方"安全须知"，演示时先停车再说话 |

### 10.4 执行

| 现象 | 原因 | 解决 |
|---|---|---|
| 识别对了但小车不动 | 没连串口 / 未勾"识别后自动执行" / 未点连接 | 看顶部串口圆点是否绿；检查复选框 |
| 下发了但日志说"未识别" | 说法不在规则表里 | 换说法（"前进一米"），或看文档 7 §6.3 的指令表 |
| 自动执行了危险动作 | —— | **取消勾选"识别后自动执行"**，先只验证识别 |

---

## 十一、安全须知（**必读**）

1. **语音不能替代物理急停。** 急停必须保留三级：物理电源开关 → 键盘 `空格` → 界面红按钮。语音"急停"只是顺手的补充，**电机噪声下它可能听不到**。
2. **调试识别率时，一定先取消「识别后自动执行」。** 或直接用 `asr_cli.py --no-exec`，只识别不动车。
3. **自动聆听模式在电机运行时慎用**——电机噪声可能被误判成语音，导致误执行。安静环境、车静止时再用。
4. **说话前先停车。** 一边跑一边说，噪声会盖过人声。
5. **首次接地测试**：小车放地上、周围 1 米无遮挡、手放在电源开关上。
6. **拔线顺序**：先关 GUI/命令行程序（会自动发停止指令）→ 再关板子 → 最后断电。

---

## 十二、和上一代的关系 / 下一步

### 12.1 复用了什么

| 上一代（RK3576 / 麒麟） | 本次（Atlas / Ubuntu） |
|---|---|
| sherpa-onnx 框架、中文模型选型思路 | **完全沿用** |
| 采样率适配（设备 44100 → 模型 16000） | 沿用（本次优先直接开 16 kHz，失败才重采样） |
| 「USB 麦克风设备号会变，每次复核」的教训 | 沿用，并写成了 `--list-devices` 工具 |
| 单字"停"不能当一级急停词的结论 | 沿用 |
| KWS 急停方案（已验证） | **本次没做**，见下 |

### 12.2 下一步建议（按性价比排序）

> **进展更新（2026-09-26）**：原 P0「实测 RTF」**已完成**（RTF≈0.157，见第九节）；
> 依赖安装、模型下载、五语种识别、端到端闭环**均已在实机验证通过**。
>
> **并且：不依赖声卡的环境工作已全部做完** —— 新增 `asr_preflight.py` 一键体检
> （L0~L6 共 16 项通过、0 硬失败），`asr_engine.py` 新增合成音频闭环自测，
> `test_all_offline.py` 扩到 **5 套全过**。**声卡到货当天即可直接进第 4 层语音控车。**

| 优先级 | 事项 | 怎么做 |
|---|---|---|
| ~~**P0**~~ ✅ | ~~实测 RTF 并记录~~ | **已完成**：RTF **0.157**，模型加载 5.45s。已在第九节 |
| ~~**P0**~~ ✅ | ~~依赖安装 / 模型下载 / 基础识别验证~~ | **已完成**，5 语种识别与官方参考一致 |
| ~~**P0**~~ ✅ | ~~声卡到货前的环境预置与离线验证~~ | **已完成**：`asr_preflight.py` 16 项通过、合成音频闭环通过。见 §6 第 −1 层 |
| **P0**（待硬件） | **接上 USB 声卡做真机录音** | 采购规格见 §4.4。`CONFIG_SND_USB_AUDIO=y` 已内置，插上即用。**这是当前唯一的阻塞项** |
| **P0** | **语音唤醒 / 急停关键词（KWS）** | 直接复用《../6.历史资料/上一代RK3576-KWS方案.md》的整套方案：`sherpa-onnx-kws-zipformer-wenetspeech-3.3M` 模型 + 「急停/紧急停止/小车急停」关键词表。**它应该跑在独立线程里，且不经任何大模型**——说"急停"就停，不走识别→解析→下发这条长链路。**不依赖声卡也能先把代码写好、用 wav 文件验证** |
| P1 | **TTS 语音播报** | 两条路：① 高频短回复（"好的""正在前进""已停止"）**离线预合成 WAV 缓存**，运行时直接播放，延迟趋近 0；② 接轻量中文 TTS（Matcha-TTS 比 VITS 快数倍）。**需要 USB 声卡带扬声器输出** |
| P1 | 接入 **LLM 意图解析** | 见文档 7 §11：替换 `car_gui_core.parse_text_command()`，保持 `{"action","speed","duration"}` 契约，上层零改动。当前规则解析降级为"快路径" |
| **P2**（优先级下调） | ASR 上 **NPU** | 需升 CANN 8.x + 源码编译 sherpa-onnx + 自行 ATC 导出 310B4 的 `.om`。**⚠️ 实测 RTF 只有 0.157，CPU 完全够用，NPU 化的性价比进一步下降**，建议放在很后面再考虑 |

> **关于「识别误差」的提醒**：实测中 int8 模型把"开放"识别成"开饭"。
> 这**不是 bug**，是量化的正常代价。已确认 `parse_text_command()` 用 `in` 包含匹配（宽松），
> **这个设计要保留**，不要改成精确匹配。
>
> **建议做法**：语音控车时，可以在界面日志里同时显示识别原文与解析结果，
> 让人一眼看出是"识别错了"还是"解析错了"，便于现场调整。

---

## 相关文档

| 文档 | 关系 |
|---|---|
| 《GUI语音交互面板.md》 | 手动控制部分；本文是它的语音扩展 |
| 《../1.总览与架构/项目总览导读.md》 | 平台总纲；本文对应 §5.4「交互感知层」 |
| 《../6.历史资料/上一代RK3576-KWS方案.md》 | 上一代 KWS 急停方案，下一步直接复用 |
| 《../2.环境配置/SSH连接与传文件.md》 | 模型（230 MB）怎么传到板子 |
| 《../3.硬件与供电/硬件组装.md》 | 串口协议、动作映射、安全机制 |
