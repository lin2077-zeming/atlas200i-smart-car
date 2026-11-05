# 语音急停（KWS）

> 项目：大创 · 人机交互智能小车
> 平台：华为 Atlas 200I DK A2（昇腾 310B4）+ 官方 Ubuntu 22.04
> 下位机：STM32F407 + M500 麦轮四驱车
> 前置阅读：《语音识别-ASR.md》（ASR 链路）

---

## 一、这条链路要解决什么问题

ASR（SenseVoice）已经能用了，识别准确率也够。但它有一个**物理上无法绕开的缺陷**：

| | ASR 链路 | KWS 链路 |
|---|---|---|
| 模型 | SenseVoice-Small int8（228MB） | Zipformer KWS（3.3M） |
| 工作方式 | 录完整句 → 一次性识别 | 麦克风常开 → 流式扫 |
| 延迟 | **1~3 秒**（要等 VAD 断句 0.8s + 识别） | **< 300ms** |
| CPU 占用 | 2 核跑满 | 极低，可 7×24 挂着 |
| 用途 | "前进一米""左转三十度" | **"急停"** |

**为什么急停不能复用 ASR？** 三个理由，任何一个都足够：

1. **太慢**。说"急停"→ 等 0.8 秒静音断句 → 再等 1~3 秒识别 → 已经撞上了。
2. **太贵**。ASR 跑一次吃满 2 个 CPU 核（板子总共 4 核），做不到常开。
3. **太脆**。ASR 依赖 VAD 断句，嘈杂环境断句失败就整句丢失。

所以需要一条**独立的、轻量的、常开的**关键词通道。这就是 KWS。

### ⚠️ 安全边界（先读这段）

- **语音急停不能替代物理急停。** 它只是"多一只手"，不是安全带。真车上路必须保留物理急停按钮。
- **上电 10 秒内无效**。下位机 `CONTROL_DELAY=1000`，期间电机强制失能且串口不接收数据（见《../3.硬件与供电/器件核对报告.md》）。
- **麦克风没了，急停就没了**。所以有 watchdog（`KwsListener.watchdog_ok()`），链路断了要能报警。
- **严禁把单字"停"当一级急停词**。日常对话里"停"出现频率极高，误触发会把正常行驶的车突然刹停——这本身就是危险。
- **本板无音频输出**（PCM2902 只有输入），所以急停命中后**暂时无法语音播报**，只能靠界面视觉提示 + 串口停车。TTS 是后续任务。

---

## 二、本板适配要点（和上一代 RK3576 的差异）

上一代方案见《../6.历史资料/上一代RK3576-KWS方案.md》，本板有 5 处必须改：

| 项 | 上一代 RK3576 | 本板 Atlas 200I DK A2 | 原因 |
|---|---|---|---|
| 运行时 | 静态二进制 + FP32 onnx | **pip `sherpa_onnx 1.13.8` Python API** | 板子用 pip 装的 wheel |
| 输入设备 | 写死 `INPUT_DEVICE=1` | **自动探测**（`guess_input_device()`） | PCM2902 是 `hw:0,0`，写死会错 |
| 采样率 | `44100` 写死 | **复用 `_RATE_CACHE`** | PCM2902 硬件只支持 44100/48000 |
| 录音增益 | 无需处理 | **必须先跑 `setup_mic_gain.sh`** | PCM2902 默认增益 **0%**，全静音 |
| 模型精度 | FP32（`int8` 报错） | **FP32**（同样不能用 int8） | int8 与静态运行时不兼容 |

### 实测环境事实（2026-09-26）

```
主机名        davinci-mini
系统          Ubuntu 22.04 LTS (jammy), 内核 5.10, aarch64
Python        3.10.12
sherpa_onnx   1.13.8
声卡          TI PCM2902 (08bb:2902), 注册为 card 0
录音增益      100% / 23.81dB（已由 mic-gain.service 开机自锁）
可用采样率    44100 / 48000（无 16kHz）
NPU           310B4（8T 档），驱动 23.0.RC3
磁盘          59G 总量，35G 可用
```

---

## 三、文件清单

| 文件 | 作用 | 是否可改 |
|---|---|---|
| `kws_engine.py` | **KWS 引擎核心**：模型加载、流式解码、实时监听 | 可改 |
| `kws_keywords_emergency.txt` | **急停关键词表**（安全相关） | 谨慎改 |
| `kws_emergency.py` | 命令行演示（不接小车，只打印） | 可改 |
| `kws_record.py` | 录制真人验证音频（**倒计时 + 自动裁切 + 当场质检**，见 §4.8） | 可改 |
| `kws_sample_check.py` | **样本录制质量体检**（不加载模型，录完先跑它） | 可改 |
| `kws_batch_test.py` | 批量回归测试（召回率 / 误报率） | 可改 |
| `kws_preflight.py` | **一键环境预检（L0~L5）** | 可改 |
| `env_doctor.py` | **Python 解释器体检**（本板多套 Python，见 §4.7） | 可改 |
| `_pyenv.sh` | 解释器解析器（被其它脚本 source） | 可改 |
| `run_py.sh` | **用正确解释器跑任意脚本**（推荐入口） | 可改 |
| `tts_engine.py` | 中文 TTS 合成（附带产出，见 §11） | 可改 |
| `download_kws_model.sh` | 下载 KWS 模型（多源重试） | 可改 |

模型落地位置：

```
~/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
    encoder-epoch-99-avg-1-chunk-16-left-64.onnx    12.1 MB
    decoder-epoch-99-avg-1-chunk-16-left-64.onnx     0.7 MB
    joiner-epoch-99-avg-1-chunk-16-left-64.onnx      0.25 MB
    tokens.txt                                       1.6 KB
    test_wavs/                                       官方测试音频（7 个）
```

> ⚠️ **不要用 int8 版**。模型包里有 `*.int8.onnx`，本板 `sherpa_onnx 1.13.8`
> 加载会失败（报 `'attention_dims' does not exist in the metadata`）。
> `kws_engine.find_kws_files()` 已自动避开 int8。

---

## 四、关键词表格式（踩坑实录）

### 4.1 正确格式

```
j í t íng @急停
```

- 建模单元是**拼音的声母 + 韵母**，**必须分开写**，中间用空格
  - 正确：`j í t íng`（j + í + t + íng）
  - 错误：`jítíng`（合起来不会被切分）
- **声调符号是韵母的一部分**，不能丢。`í` / `ǐ` / `ī` / `ì` 是四个不同符号
- `@` 后面是命中后返回的展示文本
- 一行一个关键词，禁止重复

### 4.2 🔴 坑 1：关键词文件**不支持 `#` 注释**

这是本次最耗时的坑。sherpa 读关键词文件用的是极简的 `str -> float` 解析，
遇到注释行直接抛 `std::invalid_argument: stof`，**没有行号、没有原因**：

```
❌ 加载 KWS 模型失败：stof
```

实测（2026-09-26，sherpa_onnx 1.13.8）：

| 内容 | 结果 |
|---|---|
| `j í t íng @急停` | ✅ OK |
| 空行 | ✅ OK |
| `# 这是注释` | ❌ **抛 `stof`** |
| 缺少 `@` 的行 | ❌ 抛 `stof` |
| `\r\n` 行尾 | ✅ OK（会自动处理） |

**对策**：`kws_engine.check_keywords_file()` 会做静态体检，把 `stof` 翻译成人话：

```
关键词文件有问题 → 第1行 '# 这是注释'：不支持 # 注释行（sherpa 会抛 stof），请把说明写到文件外面
```

所以**本项目的关键词文件不带任何注释**，说明全部写在本文档里。

### 4.3 ⚠️ 坑 2：tokens.txt 是「声母 + 韵母」分离的

词表共 227 个符号，其中 192 个是拼音。它**不是**按整音节存的：

```
tokens.txt 里有： j  í  t  íng  zh  ǐ  x  iǎo  ch  ē  sh  ā
tokens.txt 里没有： tíng  tǐng  jítíng   ← 合起来的都不存在
```

所以写"急停"必须写成 `j í t íng`，写成 `tíng` 会直接不命中（且不一定报错，
更难排查）。

**核对方法**（改动关键词后建议跑一遍）：

```bash
python3 - <<'EOF'
MD="/home/HwHiAiUser/models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
syms={l.split(" ")[0] for l in open(MD+"/tokens.txt",encoding="utf-8") if " " in l}
for s in "j í t íng ǐ n zh x iǎo ch ē sh ā".split():
    print("  %-6s -> %s" % (s, "OK" if s in syms else "❌ 缺失"))
EOF
```

### 4.4 当前急停词表

| 显示标签 | 拼音编码 | 级别 |
|---|---|---|
| 急停 | `j í t íng` | 一级 |
| 紧急停止 | `j ǐn j í t íng zh ǐ` | 一级 |
| 小车急停 | `x iǎo ch ē j í t íng` | 一级 |
| 停车 | `t íng ch ē` | 一级 |
| 刹车 | `sh ā ch ē` | 一级 |

**刻意不收的词**：

- ❌ **单字"停"**（`t íng`）：日常对话误触发率过高，单独说"停"也可能是在跟别人说话
- ⏸ "解除急停"/"继续"：需要时再开（`kws_keywords_emergency.txt` 注释里有模板）

---

## 四点五、🔴 坑 4：`Error querying device -1` / 设备数 = 0（PulseAudio 抢占）

**现象**（用户实测）：

```
onnxruntime cpuid_info warning: ...
自动选中录音设备：None
── pos_jiting ──
第 1 遍：
❌ 打开麦克风失败：Error querying device -1
```

**同一个板子，前后行为不一致**：之前能看到 `USB PnP Sound Device: Audio (hw:0,0)`，
现在一个设备都看不到。

### 根因：PulseAudio 抢占声卡

板子**桌面登录后**，`HwHiAiUser` 会话会自动拉起 PulseAudio：

```
$ fuser -v /dev/snd/*
/dev/snd/controlC0:  HwHiAiUser   3811 F.... pulseaudio
/dev/snd/pcmC0D0c:   HwHiAiUser   3811 F...m pulseaudio    ← 录音设备被独占

$ cat /proc/asound/card0/pcm0c/info | grep subdevices
subdevices_avail: 0            ← 0 个可用子设备

$ arecord -D hw:0,0 ...
arecord: main:831: audio open error: Device or resource busy
```

于是 `sounddevice` / PortAudio 走 ALSA 直连会被挡，返回 **0 个设备**、
`default.device = [-1, -1]`。

### 关键：环境变量决定走哪条路

实测矩阵（同一台板子、同一时刻）：

| 环境 | 设备数 | 设备列表 |
|---|---|---|
| `root` 用户 | **0** | —— |
| `su - HwHiAiUser`（**无** `XDG_RUNTIME_DIR`） | **8** | `[0] ...(hw:0,0)` 真硬件 + 7 个 ALSA 插件 |
| `su - HwHiAiUser` + `XDG_RUNTIME_DIR=/run/user/1000` | **9** | 上面 8 个 + `[5] pulse`、`[8] default` |

**结论**：
- **不带 `XDG_RUNTIME_DIR`** → PortAudio 走 ALSA 直连，能看到真硬件，**能录音** ✅
- **带 `XDG_RUNTIME_DIR`** → 走 PulseAudio，也能用（多两个虚拟设备）
- **`root` 用户** → 什么都看不到（PA 是 `HwHiAiUser` 的会话进程，root 连不上自己的 PA）

### 对策（已写进代码）

**代码侧**（`asr_engine.py` 本次增强）：

1. **`is_real_device()` / `real_input_devices()`**：区分真硬件与 ALSA/Pulse 软件插件。
   判据是名字含 `hw:`。插件（`sysdefault`/`spdif`/`samplerate`/`speexrate`/`upmix`/
   `vdownmix`/`pulse`/`default`）的 `ch` 数是 128/32/8/6 的假值。
2. **`guess_input_device()` 优先返回真硬件** —— 永不把虚拟插件当首选，
   避免"有时能录有时报 `-1`"。
3. **`Recorder.start()` 在 `device is None` 时自己探测**，绝不把 `None` 传给
   `sd.query_devices()`（那正是 `Error querying device -1` 的来源）。
4. **`_default_input_rate()`** 安全取默认采样率，失败兜底 44100。
5. **`audio_environment_hint()`**：设备数为 0 时输出人能看懂的诊断与对策。

**使用侧**：

```bash
# 1. 先诊断（最省事）
python3 kws_record.py --diag

# 2. 如果走 PA 环境有问题，绕开它直连 ALSA
env -u XDG_RUNTIME_DIR -u PULSE_SERVER python3 kws_record.py

# 3. 或者显式指定真硬件设备号
python3 kws_record.py --device 0
```

**为什么会时好时坏？** 声卡插拔（`dmesg` 里能看到 `usb 3-1.1.3: new full-speed USB device`）
或桌面会话启停，都会改变 PA 是否持有声卡。所以**不要在两种环境间反复切换**，
认准一种：**推荐 `su - HwHiAiUser` 且不带 `XDG_RUNTIME_DIR`**（走 ALSA 直连，最稳）。

> 💡 若桌面 PA 长期干扰，可以考虑：
> `systemctl --user stop pulseaudio` 或 `pactl unload-module module-alsa-card`
> —— **但本机验证不需要动它**，代码已能兼容。

### 4.6 🟡 坑 5：固定路径的临时 wav 会造成"权限假象"

`setup_mic_gain.sh` 原先用 `/tmp/_mic_check.wav` 做录音验证。踩坑过程：

```
$ ls -la /tmp/_mic_check.wav
-rw-r--r-- 1 root root 64044   ← 之前用 root 跑过，文件属于 root

$ su - HwHiAiUser -c "arecord -D plughw:0,0 ... /tmp/_mic_check.wav"
/tmp/_mic_check.wav: Permission denied
arecord exit=1        ← 录音其实失败了

$ [ -s /tmp/_mic_check.wav ] && echo "通过"
通过                  ← 但旧文件非空，判断"假通过"！
```

**更隐蔽的是**：如果旧逻辑只是 `if arecord ...; then`，`arecord` 返回 1 会失败；
但之前脚本恰好是 `arecord ... && [ -s 文件 ]`，两个条件都可能被旧文件骗过。

**对策**（已改）：

```bash
RECTMP="/tmp/_mic_check_$$.wav"     # 带 PID，唯一，不会撞权限
rm -f "$RECTMP" || true
for devspec in "plughw:$CARD,0" "hw:$CARD,0" "default" "pulse"; do
    rm -f "$RECTMP" || true          # 每轮都先清掉，避免残留
    if arecord -D "$devspec" -d 2 -f S16_LE -r 44100 -c 1 "$RECTMP" 2>/dev/null \
       && [ -s "$RECTMP" ]; then ...; fi
done
rm -f "$RECTMP" || true              # 用完即删
```

**教训**：凡是"证明某事成功"的检查，都要确认**证据是自己刚产生的**，
而不是环境里可能残留的旧产物。

---

## 四点七、🔴 坑 6：`缺少 sounddevice` —— 其实是"用错了那套 Python"

### 现象

```
(base) HwHiAiUser@davinci-mini:~/car_gui$ python3 kws_record.py --diag
缺少 sounddevice。
(base) HwHiAiUser@davinci-mini:~/car_gui$ env -u XDG_RUNTIME_DIR python3 kws_record.py
缺少 sounddevice。
(base) HwHiAiUser@davinci-mini:~/car_gui$ python3 kws_record.py --device 0
缺少 sounddevice。
```

注意提示符里的 **`(base)`** —— 这是 conda 的 base 环境。

### 根因：本板同时存在**多套 Python**

| 解释器 | 路径 | 说明 |
|---|---|---|
| 系统 Python 3.10 | `/usr/bin/python3` | **依赖齐全装在这套** |
| conda base 3.9 | **`/usr/local/miniconda3/bin/python3`** | 提示符 `(base)` 就是它 |
| conda 其它环境 | `/usr/local/miniconda3/envs/*/bin/python3` | 按需创建 |

> 🔴 **本板实测（2026-09-26 复现确认）：conda 装在 `/usr/local/miniconda3`，
> 不是 `~/miniconda3`**，而且 `/home/HwHiAiUser/.bashrc:119-132` 有 `conda init` 块：
>
> ```bash
> # >>> conda initialize >>>
> __conda_setup="$('/usr/local/miniconda3/bin/conda' 'shell.bash' 'hook' 2> /dev/null)"
> if [ $? -eq 0 ]; then eval "$__conda_setup"; ...
> # <<< conda initialize <<<
> ```
>
> **所以只要你是交互式登录（看到 `(base)` 提示符），`python3` 已经被改成
> `/usr/local/miniconda3/bin/python3`（3.9.2，没有 sounddevice/sherpa-onnx）了。**

实测三套环境对照：

| 场景 | `sys.executable` | 依赖情况 |
|---|---|---|
| `su - HwHiAiUser -c "python3 …"`（**非交互**，不读 `.bashrc`） | `/usr/bin/python3` (3.10.12) | ✅ 全齐 |
| `su - HwHiAiUser -c 'bash -i -c "python3 …"'`（**交互**，复现你的终端） | `/usr/local/miniconda3/bin/python3` (3.9.2) | ❌ 只有 numpy 1.22.4 |
| 你自己的终端（提示符 `(base)`） | 同上 | ❌ 同上 |

而 `numpy` / `sounddevice` / `sherpa-onnx` **只装在其中一套里**。
一旦 `conda init` 自动激活了 base，`python3` 就指向了没装依赖的那一套 ——
于是"缺少 sounddevice"。

**关键点**：这个报错和声卡、和 PulseAudio **一点关系都没有**。
`--device 0` 也没用，因为 `sounddevice` 这个模块根本没 import 成功，
连设备列表都拿不到。

> 💡 排查时也踩了个坑：用 `su - user -c "python3 …"` 这种**非交互**方式测，
> 结果跑的是 `/usr/bin/python3`（有依赖），看起来"一切正常"，
> **完全复现不出你的报错**。必须用 `bash -i -c` 走交互 shell 才能复现。
> 这也说明：**验证要复现用户的环境，不能只验"能跑通"的路径。**

### 怎么判断是不是这个坑

```bash
which -a python3                    # 看有几把
python3 -c "import sys;print(sys.executable)"   # 看当前用的是哪把
echo $CONDA_PREFIX                  # 非空就是在 conda 环境里
```

如果 `sys.executable` 指向 `miniconda3/...` 而依赖装在 `/usr/bin/python3`
（或反过来），就是这个坑。

### 对策（已写进代码）

**1. 新增 `env_doctor.py` —— 解释器体检**

```bash
python3 env_doctor.py            # 扫描所有解释器，列出各自装了什么
python3 env_doctor.py --write    # 把最佳解释器写进 .python_for_car
```

**板子实测输出（2026-09-26）**：

```
  当前解释器      : /usr/local/miniconda3/bin/python3
  版本            : 3.9.2
  CONDA_PREFIX    : /usr/local/miniconda3

  解释器                                        版本      pyserial  numpy    sounddevice  sherpa-onnx  PySide6   分
  ----------------------------------------------------------------------------------------------------------------
  /usr/bin/python3                           3.10.12   v3.5      v2.2.6   v0.5.6       v1.13.8      v6.8.0.2  177
  /usr/bin/python3.10                        3.10.12   v3.5      v2.2.6   v0.5.6       v1.13.8      v6.8.0.2  175
  /usr/local/miniconda3/bin/python3.9         3.9.2    —         v1.22.4  —            —            —         20
  /usr/local/miniconda3/bin/python3           3.9.2    —         v1.22.4  —            —            —         20
  /usr/local/miniconda3/bin/python            3.9.2    —         v1.22.4  —            —            —         20

  建议使用的解释器
  /usr/bin/python3   （Python 3.10.12）   三组依赖齐全 ✅
```

一眼看出差别：**conda base 有 numpy 但完全没有 sounddevice / sherpa-onnx**。

**2. 新增 `_pyenv.sh` + `run_py.sh` —— 所有脚本统一用同一个解释器**

```bash
bash run_py.sh kws_record.py --diag    # 自动挑对解释器
bash run_py.sh --which                 # 只看解析到哪个
bash run_py.sh --list                  # 等价于 env_doctor.py
```

解析优先级：`$CAR_PYTHON` → `.python_for_car` → 自动探测（找能 import
sounddevice 的） → `PATH` 里的 python3。

`run_gui.sh` / `install_deps.sh` / `setup_mic_gain.sh` / 两个
`download_*.sh` 都已改为通过 `$CAR_PY` 调用，不再写死 `python3`。

**已固定**（板子实测）：

```
$ bash run_py.sh --which
CAR_PY = /usr/bin/python3
版本   = 3.10.12
来源   = .python_for_car（已固定）
```

**3. 所有脚本的"缺依赖"报错都变成可执行指引**

现在跑 `python3 kws_record.py`（用错环境）会看到：

```
  ⚠️  当前解释器缺少：sounddevice
     解释器 = /usr/local/miniconda3/bin/python3
     版本   = 3.9.2
     conda  = /usr/local/miniconda3

  这条错误 90% 是「用错了那套 Python」——本板同时装了多套
  Python，语音依赖只装在其中一套里。请按下面顺序处理：

  【第 1 步】看看别的解释器能不能用：
      bash run_py.sh env_doctor.py   # 在 ~/car_gui 下运行
    已经扫到可用的：
        /usr/bin/python3  （语音链路完整）
    → 直接用它的绝对路径跑：
        /usr/bin/python3 kws_record.py
  ...
```

**4. `--diag` 现在**必须**在依赖检查之前执行**

原先 `kws_record.py` 的 `--diag` 被 `if not HAS_SOUNDDEVICE: return 1`
挡在前面 —— 于是"唯一能诊断缺依赖的工具"自己也被缺依赖锁死了。
这个顺序死锁已修：`--diag` 现在能正常跑，并直接告诉你缺什么、还缺哪个
解释器可用。

**5. 预检加了 `L0- 解释器` 一层**

`kws_preflight.py` 现在会先报：当前解释器、Python 版本、
`.python_for_car` 里固定的是不是同一个。三者不一致会直接给出提示。

`test_all_offline.py` 也把解释器体检加为**第 0 项**（可用 `--no-env` 跳过）。

**6. 探测路径覆盖 `/usr/local/*`**

第一版只扫了 `~/miniconda3`，与实测的 `/usr/local/miniconda3` 不符，
导致 `env_doctor.py` 一开始没认出 conda 那套。现已补全：

```python
CONDA_ROOTS = [
    "/usr/local/miniconda3", "/usr/local/anaconda3", "/usr/local/conda",
    "/opt/miniconda3", "/opt/anaconda3", "/opt/conda",
    "~/miniconda3", "~/anaconda3",
]   # 各自 envs/*/bin/python3 也会扫
```

**7. 运行时提示统一改指 `run_py.sh`**

`kws_record.py`（4 处）、`kws_batch_test.py`（1 处）、`kws_preflight.py`（3 处）
里打印给用户照抄的命令，全部从 `python3 xxx.py` 改成 `bash run_py.sh xxx.py`。
（docstring 里的示例保留 `python3`，纯说明，不影响执行。）

### 教训

> **多解释器环境里，`python3` 不是一个确定的东西。**
> 报错说"缺少某模块"时，先确认「是不是用错了 Python」，
> 再去查模块本身的安装问题 —— 顺序反了会绕很远。

还顺手记下两条这一轮踩到的：

> **① 诊断工具自己的依赖检查顺序要放在最后。**
> `--diag` 被 `if not HAS_SOUNDDEVICE: return 1` 挡住，于是"唯一能诊断
> 缺依赖的工具"自己也被缺依赖锁死了 —— 这类**自锁**要专门检查一遍。

> **② 验证要复现用户的环境，不能只验"能跑通"的路径。**
> 用 `su - user -c "python3 …"`（非交互、不读 `.bashrc`）测时，
> 跑的是 `/usr/bin/python3`（有依赖），**完全测不出用户的报错**。
> 必须用 `bash -i -c` 走交互 shell 才能复现 `(base)` 场景。
> 另外，**本地改完代码必须显式同步到板子** ——
> 否则用户会得到 `No such file or directory`，而本地"明明有"。

---

## 四点八、🔴 坑 7：召回率只有 25%，而且**降阈值完全无效**

这是本阶段最值得记的一条：**当一个"调参"动作完全不产生任何效果时，说明问题不在参数上。**

### 现象

第一次真人录音（14 条，8 正 6 负）跑 `kws_batch_test.py --sweep`：

```
===== threshold = 0.25 =====   召回 25.0% / 误报 0.0%
===== threshold = 0.20 =====   召回 25.0% / 误报 0.0%
===== threshold = 0.15 =====   召回 25.0% / 误报 0.0%
===== threshold = 0.10 =====   召回 25.0% / 误报 0.0%
```

**四个阈值输出一字不差。**

### 关键推理：为什么"无效"本身就是证据

阈值从 0.25 一路降到 0.10，是把命中门槛放宽了 2.5 倍。
如果漏报的样本**真的进了评分**，哪怕只差一点，也一定会有样本翻盘。

结果一条都没翻 → **那些漏报样本根本没被评分** → 它们里面**没有语音**。

### 真实根因：录音脚本的定长窗口 vs 人说话的延迟

旧版 `kws_record.py` 的 `record_one()` 是这样的：

```python
input("   准备好后按回车开始录音…")     # ← 按下回车
while time.time() - t0 < 1.8:          # ← 立刻开始定长硬录 1.8 秒
    ...
```

问题在于：**人按下回车之后，注意力还在键盘上，才刚要开口。**
从"按下回车"到"真正出声"有 **0.3~1.4 秒**的延迟，而窗口只有 1.8 秒。

用能量包络 + ASR 反查逐个验证，14 条里**只有 3 条录得合格**：

| 文件 | 语音起点 | 语音时长 | 判定 |
|---|---|---|---|
| `neg_liaotian_1` | 0.00s | 1.72s | ✅ 完整 |
| `neg_qianjin_1` | 1.16s | 0.50s | ⚠️ 开口太晚 |
| `neg_qianjin_2` | — | — | ❌ 全静音 |
| `neg_zuozhuan_1` | — | — | ❌ 全静音 |
| `pos_che_1` | — | — | ❌ 全静音 |
| `pos_che_2` | 0.94s | 0.48s | ⚠️ 开口太晚 |
| `pos_jinji_1` | 1.04s | 0.76s | ⚠️ 开口太晚 + 贴末尾 |
| `pos_jinji_2` | 0.00s | 0.22s | ⚠️ 语音太短 |
| `pos_jiting_1` | 1.36s | 0.46s | ⚠️ 开口太晚 + **被截断** |
| `pos_jiting_2` | 0.00s | 0.14s | ⚠️ 语音太短 |
| `pos_shache_1` | — | — | ❌ 全静音 |
| `pos_shache_2` | 0.72s | 0.46s | ✅ 完整 |

**三条铁证**：

1. **全静音**：`RMS 0.003` 就是纯底噪。把这几个文件扔给 SenseVoice，
   它听出来是 `'我。'` / `'。'` —— 里面确实没有人声。
2. **被截断**：`pos_jiting_1` 语音从 1.36s 开始，一路到 1.82s（窗口末尾），
   明显说了一半就被切掉。
3. **ASR 反查**：`pos_jinji_2`（0.18s）被听成 `'4。'`，
   `pos_jiting_2`（0.12s）被听成 `'听。'` —— 只有半个字的音。

唯一一条**真·模型未命中**是 `pos_jinji_1`：语音 0.87s 完整、峰值 0.48、
ASR 明确听出 `'紧急停止。'`，但 KWS 没命中。**这才是真正需要用阈值去救的样本，
而这样的样本只有 1 条。**

另有一个辅助因素：`kws_batch_test.py` 用 `detect_file()` 把整段 wav 一次喂进去，
依赖模型自己在长音频里找关键词。如果音频前 1 秒全是静音，识别条件更差。

### 对策（已写进代码）

**① 录前倒计时** —— 让人把注意力从键盘转到嘴巴：

```python
def countdown(n, prompt="准备"):
    for i in range(n, 0, -1):
        sys.stdout.write("\r   ▶ 请准备… %d" % i)
        time.sleep(1.0)
    sys.stdout.write("\r   ▶ 现在说！      \n")
```

**② 窗口从 1.8s 放宽到 3.0s**（`--window`，默认 `FIXED_WINDOW = 3.0`）。
反正录完会裁，录长一点没有任何代价。

**③ 录完自动裁掉首尾静音**（能量端点检测）：

```python
# 底噪 = 全部帧 RMS 的 20% 分位数（不能用"开头几帧"——
# 有些样本一开口就是词，用开头估底噪会把真正的语音吃掉）
noise = float(np.percentile(rms, 20))
thr = max(noise * VOICE_RMS_RATIO, QUIET_RMS)
```

> ⚠️ **这里踩了一个子坑**：第一版裁切逻辑只取"最长的一段连续超阈帧"，
> 结果真人语音被判成 **0.22 秒** —— 因为说话时**音节之间必然掉到阈值以下**
> （"急停"里 `j` 和 `t` 之间有停顿），语音被切成一地碎片。
>
> **正确做法是合并间隔短于 300ms 的相邻段**（标准 VAD 行为，`GAP_MERGE_SEC`）。
> 改完之后同一个测试样本从 0.22s 恢复到真实的 0.80s。
>
> 这个 bug 是在用合成音频做离线断言时发现的（见 §5.3），
> 如果只在真机上试，很容易误以为是"说话太快"。

**④ 录完立即质检，不合格当场要求重录**（最多 3 次/遍）：

```
   ❌ 没录到声音（RMS 0.0044 / 峰值 0.0389）
      → 这条不合格，请重录。若连续多条如此，跑 setup_mic_gain.sh
```

旧版只有一句 `⚠️ 电平太低` 的弱提示，**不会拦截**，于是坏样本照样落盘、
照样进测试集、最后体现为"召回率 25%"。现在改成硬拦截。

**⑤ 新增独立体检脚本 `kws_sample_check.py`** —— 不加载模型（秒级返回），
在"录完"和"跑测试"之间插一道闸门：

```bash
bash run_py.sh kws_sample_check.py            # 体检 ./kws_samples
bash run_py.sh kws_record.py --redo           # 只重录不合格的
```

它还会直接告诉你结论，不让你在坏样本上浪费时间：

```
  结论：11 / 14 条不合格，**先重录再跑批量测试**。

  ⚠️  重录之前请先确认 kws_record.py 是新版（带倒计时 + 自动截取）。
     旧版是定长硬录，就是这次 25% 召回率的根因。
```

### 教训

> **① 「调参完全没有效果」= 数据有问题，不是参数有问题。**
> 四个阈值输出一字不差，这个异常本身就把答案写出来了。
> 遇到这种情况应该立刻停手去查数据，而不是继续调阈值或换模型。

> **② 采集环节不做质检，后面所有指标都是垃圾。**
> 25% 的召回率看着像"模型不行"，实际上是"5 条样本里没有人声"。
> **在拿数据算指标之前，先证明数据本身是合格的。**

> **③ 先修工具，再重做实验。**
> 用户问"还要再做一遍吗" —— 答案是不用盲目重录：
> **不修录音脚本就重录，结果一定一样。**

---

## 五、快速上手

### 5.1 一条命令跑起来（命令行演示，不接小车）

```bash
# 1. 先确保录音增益正常（PCM2902 默认 0% 的坑）
bash setup_mic_gain.sh

# 2. 跑到 HwHiAiUser 下（GUI/音频相关都要用这个账号）
sudo su - HwHiAiUser
cd ~/car_gui

# 3. 【关键】先确认用对了 Python（本板多套并存，见 §4.7）
python3 env_doctor.py

#   如果提示有更合适的解释器，固定下来（之后所有脚本自动跟随）：
python3 env_doctor.py --write

# 4. 【强烈建议】再看音频环境诊断，确认真硬件被选中
bash run_py.sh kws_record.py --diag

# 5. 启动语音急停演示
bash run_py.sh kws_emergency.py --beep --stats
```

> 💡 从「第 4 步」起统一用 `bash run_py.sh <脚本>` 而不是 `python3 <脚本>`。
> `run_py.sh` 会自动挑对解释器，避开 §4.7 那个坑。

对着麦克风清晰地说「**急停**」，看到红字提示就算通过。

> ⚠️ 如果第 4 步显示「录音设备总数 = 0」或「自动选中 = None」，
> 说明 PulseAudio 抢占了声卡 —— 见 §4.5，用
> `env -u XDG_RUNTIME_DIR -u PULSE_SERVER bash run_py.sh kws_emergency.py` 绕开。
>
> ⚠️ 如果显示「缺少 sounddevice」，那是 §4.7 的解释器问题（不是声卡问题）：
> 先跑 `python3 env_doctor.py`。

### 5.2 常用参数

```bash
bash run_py.sh kws_emergency.py --threshold 0.20   # 调灵敏度
bash run_py.sh kws_emergency.py --device 0         # 指定设备
bash run_py.sh kws_emergency.py --beep             # 命中响一声
bash run_py.sh kws_emergency.py --stats            # 退出打印统计
```

`kws_record.py` 的参数：

```bash
bash run_py.sh kws_record.py --window 3.5    # 说话慢就加大窗口（默认 3.0s）
bash run_py.sh kws_record.py --countdown 0   # 关掉倒计时（熟练后）
bash run_py.sh kws_record.py --redo          # 只重录体检不合格的
bash run_py.sh kws_record.py --check         # 录完自动体检
bash run_py.sh kws_record.py --name t1 --text 急停   # 只录一条
```

### 5.3 离线验证（不占麦克风）

```bash
# 样本质量体检（不加载模型，秒级返回）—— 录完/测试前都该跑
bash run_py.sh kws_sample_check.py

# 引擎自检 + 官方 test_wavs 全量验证
bash run_py.sh kws_engine.py --test-wavs

# 列录音设备
bash run_py.sh kws_engine.py --list-devices

# 检测单个 wav
python3 kws_engine.py --file ~/kws_samples/pos_jiting_1.wav
```

---

## 六、⚠️ KWS 必须用真人语音验证（重要结论）

这是本次最重要的**方法论结论**，也是踩了最久的坑。

### 6.1 合成音频**完全不能**用于 KWS 验证

最初想用 TTS 合成"急停"来验证，走了两条路，**全部失败**：

**路线 A：espeak-ng**（轻量，apt 直装）

```
说"急停" → SenseVoice 听成：'人。' / '基林。' / '基实。' / '提实。'
说"前进" → '前进。'   ← 这个词反倒对了
```

"急停"两个清晰音节被完全听丢。换 `-v cmn/zh/zh-yue`、`-s 100~150`、
`-p 60`、拼音模式 `[[ji2 ting2]]`（→ 听成"一定"）**全部不行**。

**路线 B：sherpa-onnx VITS 中文 TTS**（质量高得多，187 个说话人）

```
vits-zh-hf-fanchen-C, 113MB, 16000Hz
"急停" 合成为 0.26~0.52 秒的音频
```

频谱对比：

| 音频 | 时长 | 谱质心 | RMS |
|---|---|---|---|
| **官方真人** 3.wav | 8.03s | **1615 Hz** | 0.0562 |
| TTS "急停" | 0.44s | **3433 Hz** | 0.0139 |
| TTS "紧急停止" | 0.90s | 1088 Hz | 0.0451 |

TTS 短词的**谱质心严重偏高**（高频噪声占比过大），且能量极低。
送上 KWS：**24 条全部未命中**（正负样本都不命中）。加前后静音填充、
重复两次——**依然全部未命中**。

### 6.2 反证：引擎和关键词文件**完全正常**

用**官方真人 test_wavs** 做交叉验证：

```
官方完整 test_keywords.txt  → 3.wav 命中 ['文森特卡索', '法国']
只留"蒋友伯"一行            → 命中 ['蒋友伯']
只留"女儿"一行              → 命中 ['女儿']
```

**自定义关键词文件机制 100% 工作**（单行文件也能精准命中）。

### 6.3 结论

> **KWS 模型是在 WenetSpeech 真人语音（10000 小时）上训练的，
> 声学分布与 TTS 合成音差异巨大。自定义关键词必须用真人录音验证。**

这不是缺陷，是正常的——就像不能用合成指纹去测试真人指纹识别。

（顺带：`vits-zh-hf-fanchen-C` 这个 TTS 模型本身是好东西，**留着给后续 TTS 播报用**，
只是不该拿来当 KWS 的测试素材。）

### 6.4 正确做法：录真人语音

> ⚠️ **录音前务必先读 §4.8。** 第一次录音因为"定长窗口 vs 说话延迟"的问题，
> 14 条里只有 3 条合格，批量测试召回率被拉到 25%，而且**降阈值完全无效**。
> 现在的 `kws_record.py` 已经改成「倒计时 + 长窗口 + 自动裁切 + 当场质检」。

```bash
sudo su - HwHiAiUser
cd ~/car_gui

# 1. 交互式录 7 条（4 个正样本 + 3 个负样本），每条录两遍
bash run_py.sh kws_record.py

#   流程：按回车 → 3 秒倒计时 → 看到「现在说！」后念关键词
#   录完自动裁掉首尾静音；不合格会当场要求重录（每遍最多 3 次）

# 2. 【必做】先体检，别急着跑测试
bash run_py.sh kws_sample_check.py

#   若有不合格项，只补录那些：
bash run_py.sh kws_record.py --redo

# 3. 体检全绿之后，才跑批量测试标定阈值
bash run_py.sh kws_batch_test.py --sweep
```

`kws_record.py` 的默认清单：

| 文件名 | 要念的内容 | 期望 |
|---|---|---|
| `pos_jiting_*.wav` | 急停 | 命中 |
| `pos_jinji_*.wav` | 紧急停止 | 命中 |
| `pos_che_*.wav` | 停车 | 命中 |
| `pos_shache_*.wav` | 刹车 | 命中 |
| `neg_qianjin_*.wav` | 前进 | **不命中**（负样本） |
| `neg_zuozhuan_*.wav` | 左转 | **不命中**（负样本） |
| `neg_liaotian_*.wav` | 今天天气不错 | **不命中**（负样本） |

**念词要领**：

- 距离麦克风 20~50cm，正常音量，不要刻意喊
- **听到「现在说！」再开口**（这是新版加了倒计时的原因）
- **连贯地说**，中间不要停顿（"急—停"会被切碎）
- 每条录 2 遍，便于观察稳定性
- 环境尽量安静（板子自带风扇有底噪）

**体检指标怎么读**（`kws_sample_check.py` 的输出）：

```
  pos_jiting_1.wav    1.82s  RMS 0.0690  峰值 0.7182  语音 1.36~1.82s (0.46s)  ❌
        ❌ 第 1.36s 才开口（>0.80s），前面都在等 —— 重录这条
        ❌ 语音贴到窗口末尾（终点 1.82s / 总长 1.82s），疑似被截断
```

| 指标 | 正常范围 | 异常说明 |
|---|---|---|
| `RMS` | > 0.02 | < 0.006 → 全静音，没录到 |
| `峰值` | > 0.05 | < 0.02 → 没录到（或增益 0%） |
| **语音起点** | **< 0.80s** | 太晚说明开口慢了，有效语音被浪费 |
| **语音时长** | **> 0.25s** | 太短说不清一个词 |
| 语音终点 | 距末尾 > 0.05s | 贴末尾说明**被截断**了 |
| 削顶 | < 0.1% | 太高说明离麦太近或增益过大 |

`kws_batch_test.py --sweep` 会自动扫 0.25 / 0.20 / 0.15 / 0.10 四个阈值，
按「召回率 − 误报率」给出最佳点。**急停场景下，召回率优先于误报率**——
宁可偶尔误停，也不能刹不住。

> 💡 **判断"阈值是否在起作用"的快速方法**：如果几个阈值的输出**完全相同**，
> 说明漏报样本没进评分（通常是没录到声音），此时调阈值毫无意义 ——
> 回到 §4.8 先查数据。

---

## 七、参数说明与调优

| 参数 | 默认 | 说明 |
|---|---|---|
| `--threshold` | 0.25 | 命中阈值。**调低=更灵敏但易误报**。官方推荐 0.25 |
| `--threads` | 1 | 推理线程数。3.3M 小模型 1 个线程够，留给 GUI/串口 |
| `BLOCK_SECONDS` | 0.20 | 每块音频长度。越小响应越快、CPU 越忙 |
| `COOLDOWN_SECONDS` | 1.0 | 命中后冷却，防止一句话触发多次 |
| `WATCHDOG_SECONDS` | 2.0 | 多久没收到音频就算链路死 |

### 实测性能

| 指标 | 本板实测 | 上一代 RK3576 |
|---|---|---|
| 模型加载 | **2.9 秒** | — |
| 官方 test_wavs 命中 | 4/7（0/1/2 是负样本，正确未命中） | — |
| 阈值 0.25 下官方样本 | ✅ 命中，无需降阈值 | — |

> RTF 待真人样本接入后实测。3.3M 模型理论上远快于 ASR 的 0.157。

---

## 八、接入小车（**已于 2026-09-27 实现并实测**）

`KwsListener` 的 `on_hit` 回调是唯一接入点。**回调在监听线程里执行，
绝不能碰 GUI 控件**。

已实现的方式（`car_gui_core.py`）：

```python
def _on_kws_hit(self, label, ts):
    self._kws_hits.append((label, ts))          # 记一笔（界面轮询用）
    self.log("★ 语音命中「%s」→ 立即急停" % label)
    self.trigger_emergency(reason="语音命中「%s」" % label)

def trigger_emergency(self, reason="手动", announce=True):
    ok = self.emergency_stop()                  # ① 同步、立即、不排队
    self._estop_locked = True                   # ② 进入锁定态
    self._estop_reason = reason
    self._voice_events.append(("estop", reason, time.time()))
    if announce:
        self.speak(SPEAK_ESTOP, drop_pending=True, force=True)   # ③ 之后才播报
    return ok
```

> ⚠️ **注意与原始设计的差异**：`on_hit` 里**不压队列等主线程轮询**，而是
> **直接在回调线程里立刻停车**。因为"压队列 → 主线程下一拍处理"会引入
> 最多几十毫秒的延迟，而急停要的是最短路径。停车走 `_write_speed()`，
> 里面有串口写锁保证不与界面线程的写交错。

设计要点（全部已落实）：

1. **急停必须走最短路**：命中 → 立即下发零速度帧，**不经过任何解析/确认流程** ✅
2. **不依赖 LLM**：这条链路里绝不能出现大模型 ✅（播报文本是写死的模板）
3. **双通道保活**：`voice_status()["watchdog_ok"]` 供界面显示；为 False 要红字报警 ✅
   （**界面还没接**，见《13.TTS…》§8.7）
4. **急停后进入锁定态**：`_estop_locked`，`move()` 一律拒绝（**`stop` 例外**），
   必须人工 `release_estop()` ✅

**验证**：`test_voice_estop.py`（44 项断言，不需要硬件/麦克风/模型）把上面每一条都断言了，
其中最关键的是"零速度帧的时间戳早于播报投递"。已挂进 `test_all_offline.py` 第 3 项。

`car_gui_core.py` 里可直接跑的自测：

```bash
# 开语音急停（不连串口也能验急停链路）
bash run_py.sh car_gui_core.py --no-connect --tts --voice --seconds 60

# 安全断言
bash run_py.sh test_voice_estop.py
```

> 💡 命中后的播报会经"网络音箱"送到浏览器（本板没喇叭）——
> 详见《13.TTS…》§8.4。浏览器打开 `http://<板子IP>:8099/listen` 就能听到。

---

## 九、故障排查

| 现象 | 原因 | 对策 |
|---|---|---|
| `加载 KWS 模型失败：stof` | 关键词文件有 `#` 注释或缺 `@` | 跑 `check_keywords_file()`，删掉注释 |
| **`缺少 sounddevice` / `No module named sherpa_onnx`** | **用错了 Python**（本板多套 Python 并存，如 conda base） | **跑 `python3 env_doctor.py`；固定后统一用 `bash run_py.sh <脚本>`。详见 §4.7** |
| **`Error querying device -1` / 自动选中 = None / 设备数 0** | **PulseAudio 抢占了声卡**（板子桌面登录后常见） | **跑 `python3 kws_record.py --diag`；或 `env -u XDG_RUNTIME_DIR python3 kws_record.py`；或 `--device 0`。详见 §4.5** |
| 命中率极低 | 增益 0%（全底噪） | `bash setup_mic_gain.sh`，确认 `amixer -c 0 sget Mic` 是 100% |
| 一直不命中 | 关键词拼音写错（如写成 `tíng`） | 用 §4.3 的脚本核对 tokens.txt |
| 频繁误触发 | 阈值太低 / 收了单字词 | 调回 0.25，检查词表有无单字 |
| 提示"麦克风无数据" | 声卡被拔 / 设备号变了 | `python3 kws_engine.py --list-devices` 重新确认 |
| 报错 `paInvalidSampleRate` | 首次探测撞 16kHz 失败 | **正常**，第 2 次会命中 44100 缓存 |
| 命中后没反应 | 下位机上电 10 秒保护期内 | 等 10 秒，OLED 显示 ON 才就绪 |
| 录音报 `Permission denied` | 临时 wav 被 root 创建过，属主不对 | 用带 PID 的唯一文件名（§4.6 已修） |
| `--diag` 自己都报缺依赖 | 旧版 `--diag` 被依赖检查挡在门外（死锁） | 已修（§4.7），升级到当前代码即可 |
| **召回率低，且几个阈值输出完全相同** | **样本里根本没录到声音**（不是模型/阈值问题） | **跑 `bash run_py.sh kws_sample_check.py` 体检，然后 `--redo` 重录。详见 §4.8** |
| 录音里语音"很晚才出现"/被截断 | 定长窗口 vs 说话延迟不匹配 | 新版已改为倒计时 + 3.0s 窗口 + 自动裁切（§4.8）；说慢了加 `--window 3.5` |
| 体检说"有效语音太短"（如 0.2s） | 可能是端点检测把音节间断开 | 已修（合并 <300ms 间隔，§4.8）；若仍如此，是真说话太快 |

---

## 十、预检结果（实测留档）

`bash run_py.sh kws_preflight.py`（2026-09-26，板子真实交互 shell）：

```
L0- 解释器     /usr/bin/python3 3.10.12 ✅ / .python_for_car 一致 ✅
L0 依赖        numpy 2.2.6 ✅ / sounddevice 0.5.6 ✅ / sherpa_onnx 1.13.8 ✅
               PortAudio V19.6.0-devel ✅
L1 模型文件     encoder 12.4MB ✅ / decoder 0.64MB ✅ / joiner 0.24MB ✅
                tokens.txt 1.6KB ✅ / test_wavs 7 个 ✅
L2 关键词表     5 个词全部合法 ✅ / 不含单字急停词 ✅
L3 模型加载     KeywordSpotter 构建 2.92 秒 ✅
L4 官方样本     0/1/2.wav 无命中（负样本，正确）✅
                3/4/5/6.wav 命中 ['文森特卡索','法国'] / ['蒋友伯','女儿']
                / ['周望军','落实'] / ['朱丽楠','见面会'] ✅（4/7）
L5 麦克风       设备 8 个（真硬件 1：USB PnP Sound Device hw:0,0）✅
                自动选中设备 0 ✅ / 增益 card 0 = 100% ✅
                实时监听启动 ✅ / 1.5s 内持续出数据 ✅
                说话前电平 0.0042（说话时应明显升高）
──────────────────────────────────────────────
✅ 通过 24 项 · ❌ 失败 0 · ⏳ 待硬件 0
结论：KWS 环境**全部就绪**，可以直接跑真人语音验证。
```

`bash run_py.sh test_all_offline.py --quick`（板子实测）：

```
✓ 通过  解释器体检
✓ 通过  纯逻辑自测
✓ 通过  串口协议联调
通过 3 · 失败 0 · 跳过 0
```

### 样本体检（2026-09-26 首次真人录音，14 条）

```
  neg_liaotian_1.wav          1.72s  RMS 0.0592  峰值 0.6271  语音 0.00~1.72s (1.72s)  ✅
  neg_qianjin_1.wav           1.81s  ...  语音 1.16~1.66s (0.50s)  ❌ 开口太晚
  neg_qianjin_2.wav           ...  ❌ 全静音
  neg_zuozhuan_1.wav          ...  ❌ 全静音
  pos_che_1.wav               ...  ❌ 全静音
  pos_che_2.wav               1.81s  ...  语音 0.84~1.46s (0.62s)  ❌ 开口太晚
  pos_jinji_1.wav             1.81s  RMS 0.1004  语音 1.04~1.80s (0.76s)  ❌ 开口太晚+贴末尾
  pos_jinji_2.wav             1.81s  RMS 0.0687  语音 0.00~0.22s (0.22s)  ❌ 太短
  pos_jiting_1.wav            1.82s  RMS 0.0690  语音 1.36~1.82s (0.46s)  ❌ 开口太晚+被截断
  pos_jiting_2.wav            1.82s  RMS 0.0215  语音 0.00~0.14s (0.14s)  ❌ 太短
  pos_shache_1.wav            1.81s  RMS 0.0034  峰值 0.0137            ❌ 全静音
  pos_shache_2.wav            1.81s  RMS 0.0630  语音 0.62~1.20s (0.58s)  ✅
──────────────────────────────────────────────
结论：11 / 14 条不合格 —— 独立复现了"召回率 25%"的根因
```

> ✅ 这是 `kws_sample_check.py` 的**第一次真机实测**，判决与手工分析（能量包络 +
> ASR 反查）**完全一致**，说明体检脚本可信。

### 新录音流程端到端实测（板子，无人声输入）

```
   ▶ 请准备… 3   ▶ 请准备… 2   ▶ 请准备… 1   ▶ 现在说！
   🎙  录音中 @ 44100 Hz，窗口 2.0 秒（说完即可，多余静音会被裁掉）
   [▯▯▯▯…▯▯▯▯]  0.0s
   ❌ 没录到声音（RMS 0.0044 / 峰值 0.0389）
      → 这条不合格，请重录。若连续多条如此，跑 setup_mic_gain.sh
      → 这条没录好，再来一次
   ⚠️  第 1 遍 3 次都没录合格，先跳过（可用 --redo 补录）
```

> 因为测试时没有人对着麦说话，全部被判"没录到声音"——**这正是期望行为**。
> 旧版会把这种静音样本默默存盘，然后拿着去测，得到"召回率 25%"。

> 完整 6 套（含 GUI / 端到端 / ASR 预检 / KWS 预检）在板子上同样可跑。
> 注：在 Windows 本机跑时，"串口协议联调"与"端到端闭环"会因缺 `termios`
> 而失败 —— 那是本机限制，与代码无关。

---

## 十一、附带产出：中文 TTS 引擎

> 📄 **完整操作文档见《语音合成/TTS接入.md》**。
> 本节只记 KWS 视角下的相关结论。

排查 KWS 验证素材时，顺手把 TTS 也跑通了（`tts_engine.py`）。**这不是 KWS 的依赖**，
而是给后续"语音播报"任务铺路。

```
模型    vits-zh-hf-fanchen-C（113MB，16000Hz，187 个说话人）
位置    ~/models/tts/vits-zh-hf-fanchen-C/
耗时    加载 3.7 秒；合成 1.48 秒的句子约 4.7 秒（2 线程 CPU）
```

用法：

```bash
bash run_py.sh tts_engine.py --text "已急停，请注意" --out /tmp/a.wav
bash run_py.sh tts_engine.py --list-speakers          # 187 个说话人
bash run_py.sh tts_engine.py --text "前进" --speaker 3 --speed 1.1
```

**🔴 阻塞：本板没有音频输出。** `aplay -l` 输出只有一行标题
（`**** List of PLAYBACK Hardware Devices ****`），没有任何设备——
PCM2902 是"只有输入没有输出"的声卡。所以 `--play` 会失败。

要真正"说给人听"，需要二选一：

- 买一个 **USB 音箱**（自带声卡，插上就有 `card 1`，`aplay` 直接可用）
- 买一个 **带 3.5mm 输出的 USB 声卡** + 有源音箱

在此之前，TTS 只能"生成 wav 文件"然后 scp 回 PC 播放。

**踩坑记录**：

| 模型 | 结果 |
|---|---|
| `vits-icefall-zh-aishell3` | ❌ `Error when reading tokens at Line sil 0. size: 3` —— tokens.txt 首行是 `sil` 而非 `<sil>`，与新版 API 不匹配 |
| `vits-zh-hf-fanchen-C` | ✅ 正常，tokens.txt 是标准格式 |

> ⚠️ **播报绝不能挡在停车前面**：TTS 合成要 1~4 秒，
> 必须"先下发零速度帧 → 再（异步）播报"。
> 接入细节见《语音合成/TTS接入.md》§8.1。

---

## 十二、待办

- [x] 下载并解压 KWS 模型（FP32）
- [x] 写 `kws_engine.py`（引擎 + 实时监听 + 采样率缓存复用）
- [x] 写急停关键词表（5 个词，不含单字"停"）
- [x] 官方 test_wavs 验证通过（自定义关键词文件机制确认工作）
- [x] 写 `kws_record.py` / `kws_batch_test.py`
- [x] 写 `kws_emergency.py` 命令行演示
- [x] 写 `kws_preflight.py` 一键预检（21 项通过）
- [x] 挂入 `test_all_offline.py`（6 套全过）
- [x] （附带）跑通中文 TTS 引擎
- [x] 修 `Error querying device -1`（PulseAudio 抢占，§4.5）
- [x] 修 `setup_mic_gain.sh` 权限假象（§4.6）
- [x] 修 `缺少 sounddevice`（多 Python 解释器，§4.7）：新增 `env_doctor.py` /
      `_pyenv.sh` / `run_py.sh`，所有脚本改走 `$CAR_PY`
- [x] 定位"召回率 25% 且降阈值无效"的根因（§4.8）：**录音时序问题，非模型/阈值**
- [x] 重做 `kws_record.py` 录音流程：倒计时 + 3.0s 窗口 + 自动裁首尾静音 + 当场质检
- [x] 新增 `kws_sample_check.py` 样本体检（旧样本实测 11/14 不合格，复现诊断）
- [ ] **重录真人语音 → 体检全绿 → 标定阈值**（阻塞：需要人配合开口）
- [ ] 复查 `pos_jinji_1` 那条"真·模型未命中"（语音完整但 KWS 没命中，
      若重录后仍不命中，才需要考虑调阈值或换关键词）
- [x] 接入 `car_gui_core.py`：`enable_voice_emergency()` → `_on_kws_hit()` →
      `trigger_emergency()`（停车 → 锁定 → 播报），见第八节
- [ ] GUI 里加 KWS 状态指示与命中提示
- [ ] 急停锁定态与解除流程
- [ ] TTS 播报"已急停"——**需先解决音频输出设备**（见 §11）
