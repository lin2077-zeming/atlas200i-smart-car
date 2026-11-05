# 语音合成（TTS）接入

> **本文对应的代码：项目根目录下的 `car_gui/tts_engine.py`**
> 目标：在 **Atlas 200I DK A2（昇腾 310B4 + Ubuntu 22.04）** 上实现**离线中文语音播报**——
> 小车自己开口说话："已急停"、"开始前进"、"电量不足，请充电"。
>
> 引擎：**sherpa-onnx + Kokoro（kokoro-multi-lang-v1_0）**，全程离线，不联网、不上传文本。
> （2026-09-27 从 `vits-zh-hf-fanchen-C` 换过来 —— 旧模型实测输出不可懂，见 §2.5）
>
> ⚠️ **当前状态：引擎与音质都已达标，但缺音频输出硬件。** 详见第二节。
> ✅ 音箱到手前不必干等：**§2.1 的网络音箱**能把声音送到 PC/手机浏览器，现在就能听。

---
> 🔵 **板子 IP 提示**：本文若出现 `192.168.137.100`，那是**网线直连 PC** 时的静态地址（一直有效）。
> 走 **WiFi** 时地址由路由器 DHCP 分配、**会变**。找不到就：
> ① PC 上双击 `../../2.环境配置/工具脚本\find_car.cmd`；② 板子上 `bash find_ip.sh`；
> ③ 详见《../../2.环境配置/快捷命令速查.md》**§0.1**。

## 一、这条路要解决什么问题

语音交互是**双向**的。之前的《../语音识别-ASR.md》只做了"听"（ASR），
《../语音急停-KWS.md》只做了"关键字监听"（KWS）。
小车现在能听懂话，但**回应只能是屏幕上的一行字** —— 而屏幕在你手上（VNC），
车在你脚下 3 米外，这个交互是断的。

TTS 补上另一半：**让小车用声音回答**。

```
你说话 → KWS 判断是否为急停
         ├── 是 → 停车 → 【TTS 播报"已急停"】← 本文要做的事
         └── 否 → ASR 识别 → 规则/LLM 解析 → 串口下发 → 小车动
                                              └→ 【TTS 播报"开始前进"】
```

### 典型播报场景

| 场景 | 播报内容 | 价值 |
|---|---|---|
| 急停命中 | "已急停" | **最重要**——不用看屏幕就知道刹住了 |
| 指令确认 | "开始前进" | 确认解析正确，避免误动作 |
| 拒绝执行 | "前方有障碍，已停止" | 说明为什么不动 |
| 状态提醒 | "电量不足，请充电" | 安全预警 |
| 启动自检 | "系统就绪" | 上电时确认链路通 |

> ⚠️ **安全边界**：TTS 是**提示**，不是**安全机制**。
> 急停必须依靠下位机物理开关 + KWS 直连串口，**绝不能等 TTS 播完再停车**。
> 播报永远是"停完之后"的附加动作（详见第七节）。

---

## 二、🔴 本板没有音频输出：已查实到硬件层，附两条解法

这是**开工前必须先知道的事**，否则会白折腾很久：**代码和模型都没问题，
是硬件层缺一个能发声的东西。**

### 根因：三条出路**全部**封死，而且都不是配置问题

2026-09-27 逐条复核过，结论比之前更硬 —— 这也意味着**别再花时间去试蓝牙或 HDMI**：

**① 板载没有任何音频硬件。**
官方内核把 `CONFIG_SND_DESIGNWARE_I2S` / `CONFIG_SND_SIMPLE_CARD` /
`CONFIG_SND_AUDIO_GRAPH_CARD` **全关了**，而且 `SND_SOC` 下除 I2C/SPI 粘合层外
**没有任何具体 codec 驱动**：

```bash
$ (zcat /proc/config.gz) | grep -E "^CONFIG_SND"
CONFIG_SND=y
CONFIG_SND_SOC=y
CONFIG_SND_SOC_I2C_AND_SPI=y     ← 只有粘合层，没有任何 codec 驱动
CONFIG_SND_USB_AUDIO=y           ← ✅ 但 USB 音频是完整的
```

**连板载 MIC 也用不了**（不是没插好，是没驱动），板子上也没有 3.5mm 耳机孔。
→ **HDMI 音频同样不可用**（没有 codec 驱动，HDMI 出不了声）。

**② 外接的 PCM2902 是「纯采集」设备。**
本板接了一个 **TI/Burr-Brown PCM2902**（`lsusb` 里 `08bb:2902`）：

```bash
$ arecord -l
card 0: Device [USB PnP Sound Device], device 0: USB Audio   ← ✅ 有输入
$ aplay -l
**** List of PLAYBACK Hardware Devices ****
                      ← ❌ 只有标题行，没有任何输出设备
```

**为什么它天生没有输出？看 USB 描述符就明白了：**

```bash
$ lsusb -v -d 08bb:2902 | grep -E "bInterfaceNumber|bNumEndpoints|bEndpointAddress"
      bInterfaceNumber        0     ← AudioControl（0 个端点）
      bInterfaceNumber        1     ← AudioStreaming
        bNumEndpoints           1
        bEndpointAddress     0x82  EP 2 IN     ← ★ 只有这一个端点，而且是 IN（采集）
      bInterfaceNumber        2     ← HID（音量按键）
        bEndpointAddress     0x87  EP 7 IN
```

**整颗芯片只有 IN 端点，没有任何 OUT 端点。** 一个全双工的 PCM2902 本该有
一路 IN（录音）+ 一路 OUT（放音），这个模块只引出了录音那路。
`/dev/snd/` 里也能对上：只有 `pcmC0D0c`，没有 `pcmC0D0p`。

> ⚠️ **别想靠它上面的 3.5mm 口解决问题** —— 那是 **MIC IN**，插耳机不会响。
> 它是给 #17 领夹麦用的（见《../../3.硬件与供电/采购清单.md》）。

**③ 蓝牙这条"无线"的路也封死了。**
AX210 的蓝牙模块在 `lsusb` 里看得到（`8087:0032`），但**内核根本没编蓝牙**：

```bash
$ (zcat /proc/config.gz) | grep -i bluetooth
1848:# CONFIG_BT is not set                              ← 蓝牙子系统整个没编
$ ls /lib/modules/$(uname -r)/kernel/drivers/bluetooth/
ls: cannot access '...': No such file or directory       ← 连目录都不存在
$ hciconfig -a
Can't open HCI socket.: Address family not supported by protocol
```

`# CONFIG_BT is not set` 意味着连蓝牙子系统都没有，`btusb` 模块不存在，
所以 **A2DP 音箱这条路直接断掉，不用再试**。

> 📌 顺带一条容易误判的经验：本板的 **`load average` 基线就是 17 左右**。
> 那是昇腾驱动的内核线程（`dcc_thread` / `hsm_proxy` / `ts_proxy` / `hi_vdec` /
> `hi_venc` / `bbox_main` 等 17 个）常驻 `D` 状态等硬件造成的。
> 看到 load 17 别慌，先看 `top` 里的空闲率：实测 **CPU 97.6% 空闲**，完全正常。

### 两条解法

| 方案 | 成本 | 效果 | 说明 |
|---|---|---|---|
| **① USB 音箱 / USB 声卡（带输出）** | ¥20~80 | ⭐⭐⭐⭐⭐ | **正规解法**。内核 `CONFIG_SND_USB_AUDIO=y` 已内置，符合 UAC 标准的设备**免驱即插即用**，插上 `aplay -l` 立刻出现新设备，代码一行都不用改 |
| **② 网络音箱（`tts_serve.py`）** | **¥0** | ⭐⭐⭐⭐ | **现在就能用**。推理仍在板子上跑，声音经网络送到 PC/手机浏览器播放。见下方 §2.1 |
| ③ 用 PCM2902 上的 3.5mm 口 | — | ❌ | **不行**。那是 MIC IN |
| ④ 蓝牙音箱 | — | ❌ | **不行**。内核 `# CONFIG_BT is not set` |

> 💡 **买之前确认这件事**：板子上只有 **2×USB-A + 1×Type-C**，
> 现在键鼠的 2.4G 接收器已占了 2 个口 —— 买**带 USB Hub 的音箱**，
> 或配一个**带独立供电的 Hub**，否则声卡没口插（见《../../3.硬件与供电/采购清单.md》）。
>
> ⚠️ **买到"只有输入的 USB 声卡"就白买了** —— 手上这个 PCM2902 就是活教材：
> 它看起来是个"USB 声卡"，其实只有麦口。
> **选"USB 音箱"（自带喇叭，必然有输出）比选"USB 声卡"更不容易踩这个坑。**

### 2.1 现在就能出声：`tts_serve.py` 网络音箱

音箱还没到手，但**语音这条路不必停着**。原理很简单：

> **声音不必从板子出来。板子只负责"算"，把算好的音频经网络送到一台
> 本来就有喇叭的设备上播放** —— 你的 PC 浏览器、手机、平板都行。

```
板子（Atlas 200I DK A2）                     播放端（PC / 手机浏览器）
┌─────────────────────────┐                 ┌──────────────────────┐
│  http://ip:8099/        │   ← 打字试听      │  直接出声             │
│  TtsEngine 现场合成      │                 │                      │
│  http://ip:8099/listen  │   ← 长轮询取音频   │  当"车的喇叭"常开      │
└─────────────────────────┘                 └──────────────────────┘
```

```bash
# 板子上（会常驻，Ctrl+C 停止）
cd ~/car_gui
bash run_py.sh tts_serve.py --port 8099

# PC / 手机浏览器
http://192.168.137.100:8099/          # 手动试听：打字 → 点一下 → 出声
http://192.168.137.100:8099/listen    # 自动收听：挂着当喇叭
```

| 接口 | 用途 |
|---|---|
| `GET /` | **手动试听页**。挑音色、试文案，改了说话人编号立刻能听 |
| `GET /listen` | **自动收听页**。常开挂着，车要说话时自动播出 —— 这就是"车的喇叭" |
| `POST /say` | **程序接口**：`{"text":"已急停"}` → 投入播报队列（GUI / KWS 回调用它） |
| `GET /say.wav?text=...` | 直接拉 wav，curl / 任意播放器都能用 |
| `GET /next` | 长轮询取待播条目（`/listen` 页面在用） |
| `GET /healthz` | 健康检查 JSON |

**为什么 `/say` 是"投递"而不是"同步播放"**：
合成要 1~4 秒，如果让调用方等，急停回调就被卡住了。
所以 `POST /say` **立刻返回序号**，合成交给后台线程串行做，
播放端用长轮询（`/next`）取走。这样**投递方永远不会被合成拖住**。

**实测结果（2026-09-27，板子 + PC 全程真跑）**：

```
POST /say {"text":"已急停"}  → 立即返回 {"seq":1}
GET  /next?since=0&wait=30  → 等 2 秒（含合成）返回 {"seq":1,"url":"/audio/1.wav"}
GET  /audio/1.wav           → 200, 23274 字节, audio/wav
                              校验：16000Hz 单声道 16bit, 0.73s,
                                    峰值 4445/32767, 87% 样本非零 → 确实有声音
```

> ⚠️ **在 PC 上 curl 板子时记得加 `--noproxy '*'`**。
> PC 上常设着 `http_proxy`（本机实测 `http://127.0.0.1:7896`），
> curl 会**把发往局域网地址的请求也丢给代理** —— 结果要么绕远、要么超时，
> 看起来像"服务没起来"，其实服务好得很。正确写法：
> ```bash
> curl --noproxy '*' -o out.wav \
>      "http://192.168.137.100:8099/say.wav?text=%E5%B7%B2%E6%80%A5%E5%81%9C"
> ```

**这个方案的正确定位**：它是**过渡方案 + 演示利器**，不是最终形态。

- ✅ 现在就能验证文案、音色、语速，不必等硬件
- ✅ 演示时可以拿手机当"车的喇叭"，隔一张桌子也听得见
- ❌ 车真跑起来脱离局域网就没用了 —— 所以 **USB 音箱还是要买**

### 新设备到手后怎么验证

```bash
# 1. 看系统认没认到
aplay -l
#    ✅ 期望：出现 "card 1: Device [USB Audio Device], device 0: USB Audio [USB Audio]"
#    ❌ 还是只有标题行 → 换一个 USB 口再试；仍不行说明音箱没被识别

# 2. 直接放一声测试音
speaker-test -t sine -f 1000 -l 1     # 响 1 秒 1kHz 正弦（Ctrl+C 可提前停）

# 3. 用板子自带 wav 试（若 /usr/share/sounds 里有）
aplay /usr/share/sounds/alsa/Front_Center.wav
```

上面三步都听不到声音时的排查顺序：

```bash
lsusb                       # 音箱的 USB 设备在不在
cat /proc/asound/cards      # ALSA 认到几张卡（新卡应是 card 1）
cat /proc/asound/devices    # 有没有 playback 设备
fuser -v /dev/snd/*         # 有没有别的进程占着声卡（PulseAudio 常见）
pactl list short sinks      # PulseAudio 层面的输出设备
```

> ⚠️ **注意和录音那个坑区分开**：
> 录音时会遇到 "PulseAudio 抢声卡"（见《12.KWS…》§4.5），那是**占用**问题，绕开即可。
> 本文这个是**根本不存在输出设备**的问题，绕不开，只能加硬件。

---

## 二点五、🎯 模型选型：实测对比与换模型（2026-09-27）

> **起因**：第一版用 `vits-zh-hf-fanchen-C` 接进网络音箱后，实测**根本听不懂**。
> 本节把"音质差"这件事量成数字，并给出换模型的结论。

### 2.5.1 先造一把尺子：`tts_quality_test.py`

"音质差"是主观的，凭感觉换模型会一直试错。所以先做一个**客观测量工具**：

> **核心思路：把 TTS 合成出来的音频，用板上的 SenseVoice 再听一遍。**

ASR 听得越准，说明音频越清楚。SenseVoice 和真人听的是同一段声学信息，
这是最贴近"人能不能听懂"的客观代理指标。

```bash
bash run_py.sh tts_quality_test.py                     # 测当前自动选中的模型
bash run_py.sh tts_quality_test.py --speakers 0,47,52  # 试几个音色
bash run_py.sh tts_quality_test.py --dir ~/models/tts/<模型名>
bash run_py.sh tts_quality_test.py --text-file texts.txt   # 中文文本走文件，避开 shell 转义
```

输出的指标：

| 指标 | 说明 |
|---|---|
| **拼音准确率** | ★ 核心。同音容忍（认"音"不认"字"） |
| 字符准确率 | 严格比对，同音字算错 |
| RTF | 合成秒数 ÷ 音频秒数，>1 表示慢于实时 |
| 峰值 / 语音占比 / 削顶 | 响度与波形质量 |

> ⚠️ **为什么要两个准确率**：ASR 对合成音经常"**音听对了、字选错了**"。
> 实测 Kokoro 说「已急停」，ASR 写成「一级停」—— 字符比对判 29%，
> 拼音比对却是 **100%**。只看字符会误判成"音质差"。
> **判据**：字符低 + 拼音高 = 音频没问题；两个都低 = 音频真的糊了。

### 2.5.2 基线：旧模型真的不行

```
模型  vits-zh-hf-fanchen-C（说话人 0）
  「已急停」            ASR 反听 →「爷爷。」        拼音 0%  字符 0%
  「前方有障碍，已停止」  ASR 反听 →「要爪子。」      拼音 0%  字符 0%
  「开始前进，请注意安全」ASR 反听 →「只一一有也。」  拼音 0%  字符 0%
  「电量不足，请充电」    ASR 反听 →「耶无有你。」    拼音 0%  字符 0%
  RTF 平均 2.82   峰值 平均 0.149
```

**4/4 条全错，一个字都没听出来。** 排除了以下可能：

- **ASR 没问题**：同一套 ASR 听官方真人样本（英/日/韩/中）**全部正确**。
- **不是速度/采样率错位**：把同一段音频拉伸 0.5 / 0.75 / 1.5 / 2 / 3 倍分别送 ASR，没有任何一个倍速能听懂。
- **模型文件完整**：`vits-zh-hf-fanchen-C.onnx` = 121MB，未截断。
- 音符集正常：`tokens.txt` 是**注音符号**（`ㄅㄆㄇㄈ`），`lexicon.txt` 形如 `已 → ㄧˇ`，自洽。

**结论**：这个模型的输出客观不可懂，弃用。（顺带说明：早期文档只量了"加载 3.7s /
合成 4.7s"这类**时间**指标，从没验过**内容**，所以问题一直没被发现。）

### 2.5.3 换 Kokoro：拼音准确率 96.4%

换上 **`kokoro-multi-lang-v1_0`**（Kokoro-82M）：

```
模型  kokoro-multi-lang-v1_0（说话人 47 / 52）
  「已急停」            ASR 反听 →「一级停。」       **拼音 100%**  字符 29%
  「前方有障碍，已停止」  ASR 反听 →「前方有障碍已停止。」 **拼音 100%**  字符 89%
  汇总：拼音准确率 平均 96.4%  最低 85.7%   听得清 4/4 条
  RTF 平均 5.43   峰值 平均 0.506
```

**「一级停」不是错** —— 那是 ASR 选了同音字，拼音 100% 说明发音完全正确。

### 2.5.4 两个模型对比

| 指标 | vits-zh-hf-fanchen-C | **kokoro-multi-lang-v1_0** |
|---|---|---|
| **拼音准确率（听得清吗）** | **0%** ❌ | **96.4%** ✅ |
| 字符准确率 | 0% | 57.8% |
| 采样率 | 16000 Hz | 24000 Hz |
| 说话人 | 187 | 54 |
| **RTF（越小越快）** | **2.82** ✅ | 5.43 ⚠️ |
| 峰值（响度） | 0.149 | **0.506** |
| 模型体积 | 121 MB | 325 MB + 28 MB 音色表 |

**结论：音质优先选 Kokoro；但要接受 RTF 从 2.8 涨到 5.4。**

一次"已急停"（约 1.2 秒音频）合成耗时从 ~2 秒变成 **~6 秒**。
对车上播报**可以接受**（本来就必须"先停车、后播报"），但不能用于实时对话。

### 2.5.5 🔴 换模型必须注意：**编号 0 是英语音色**

`kokoro-multi-lang-v1_0` 是**多语言**模型，54 个音色里混着英/日/韩/西等语言。
**编号 0（`af_alloy`）是英语音色** —— 拿它念中文会带很重的英文口音。

实测能正常读中文的是 **45 ~ 52**（8 个中文音色）。
所以默认说话人**不能用 0**，代码里已经写死：

```python
# tts_engine.py
PREFERRED_SPEAKER = {"kokoro-multi-lang": 47}      # 实测拼音 100%
ZH_SPEAKER_RANGE  = {"kokoro-multi-lang": (45, 52)}
```

`tts_engine.py` / `tts_serve.py` 的 `--speaker` 默认值现在是 `None`，
加载模型后自动取 `default_speaker()`；试听页的输入框也会预填这个值。

> ⚠️ 这个坑很隐蔽：不指定说话人时 sherpa-onnx 默认用 0，
> 结果就是"换了个更强的模型，声音反而带口音了"。

### 2.5.6 模型是**自动发现**的

`tts_engine.guess_tts_dir()` 会扫 `~/models/tts/` 下所有可用模型，
按 `MODEL_PREFERENCE = ("kokoro", "melo", "matcha", "vits-...")` **自动选音质最好的**。

**所以装上新模型后，所有脚本不用改任何参数就自动用上。** 查看现状：

```bash
bash run_py.sh tts_engine.py --list-models
```

### 2.5.7 下载 Kokoro

```bash
cd ~/models/tts
curl -L --retry 3 -o /tmp/kokoro.tar.bz2 \
  "https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_0.tar.bz2"
tar xjf /tmp/kokoro.tar.bz2 -C ~/models/tts/
```

约 **334 MB**（解压后 384 MB）。板子上实测下载 ~1 分钟、解压 ~3 分钟
（bz2 解压是单线程，4 核 1GHz 偏慢，耐心等）。

### 2.5.8 还没做的：Matcha（速度备选）

`matcha-icefall-zh-baker`（非自回归，**比 Kokoro 快得多**）已经下载并解压到
`~/models/tts/matcha-icefall-zh-baker/`，但**这个压缩包里没有声码器**
（只有 `model-steps-3.onnx` 声学模型，缺 `hifigan_v2.onnx`），所以暂时加载不了。

如果后续嫌 Kokoro 慢，值得花时间补上声码器再测一轮 —— 它是"速度 vs 音质"的折中点。

### 2.5.9 附带的两个教训

> **① 别让指标说谎。** 第一版拼音指标在 `pypinyin` 没装进正确解释器时
> **静默显示成 0%**，看起来像"音质极差"，其实是指标不可用。
> 已改成明确打印"拼音 ——（不可用）"+ 给出安装命令。
> **一个说谎的指标比没有指标更危险。**

> **② 多解释器的坑又踩了一次。** `pip install pypinyin` 装进了 conda 的 Python，
> 而 `run_py.sh` 用的是 `/usr/bin/python3`，于是"装了却 import 不到"。
> **装包时必须用 `$CAR_PY -m pip install`，而不是裸 `python3 -m pip`。**

> **③ 长任务一定要 `PYTHONUNBUFFERED=1`。** Python 的 stdout 重定向到文件时是
> **块缓冲**，日志会长时间一片空白，让人误以为卡死（本次白等了两分钟）。
> 已写进 `run_py.sh`。

---

## 三、为什么用 sherpa-onnx（而不是别的框架）

| 候选 | 结论 | 理由 |
|---|---|---|
| **sherpa-onnx + Kokoro（2026-09-27 起正式采用）** | ✅ | **实测拼音准确率 96.4%**（详见 §2.5）。与 ASR/KWS **共用同一个 `sherpa_onnx` Python 包**，不用多装框架；纯 CPU 离线推理。代价是 RTF 5.4（比 VITS 慢一倍） |
| ~~sherpa-onnx + VITS `vits-zh-hf-fanchen-C`~~ | ❌ **已弃用** | 曾是第一版选择，但**实测输出客观不可懂**（ASR 反听 4/4 全错，拼音 0%）。原因未完全定位（文件完整、音符集自洽、非采样率错位），**建议直接用 Kokoro 替代** —— 见 §2.5 |
| 云端 TTS（讯飞/百度） | ❌ | 断网即失效，且和"离线"的项目卖点冲突 |
| pyttsx3 / espeak-ng | ❌ | 音质是机器人念经，中文还是"字正腔圆地在念拼音"，实验报告里放不出去 |
| Matcha（`matcha-icefall-zh-baker`） | ⏸ 备选 | 非自回归、**比 Kokoro 快得多**，是"速度 vs 音质"的折中点。但官方压缩包里**缺声码器**，暂未跑通 —— 见 §2.5.8 |
| 昇腾 NPU 加速 TTS | ⏸ 二期 | 同 ASR：需 CANN 8.x + 源码编译 + 自行 ATC 导出 310B4 的 `.om` |

> **一句话**：一期把 TTS 放在 **CPU** 上，复用已有的 `sherpa_onnx` 依赖，
> 先让它"能说话"，音质和速度优化留到后续。

---

## 四、需要安装什么

### 4.1 依赖总表

| 类别 | 包 | 用途 | 是否已有 |
|---|---|---|---|
| Python 包 | `numpy` | 音频数组处理 | ✅ ASR 时已装 |
| Python 包 | `sherpa-onnx` | VITS 推理 | ✅ ASR 时已装 |
| 系统包 | `alsa-utils` | `aplay` / `speaker-test` | ✅ 系统自带（但无设备可用） |
| 硬件 | **USB 音箱** | **唯一缺的东西** | ❌ **要买** |

> 💡 **好消息**：`sherpa-onnx` 是 ASR / KWS / TTS **三者共用**的。
> 所以装 TTS **不需要 pip 装任何新东西**，只要插上音箱就行。

### 4.2 模型

```
名称    vits-zh-hf-fanchen-C
大小    113 MB
采样率  16000 Hz
说话人  187 个（编号 0 ~ 186）
位置    ~/models/tts/vits-zh-hf-fanchen-C/
```

目录内容：

```
~/models/tts/vits-zh-hf-fanchen-C/
├── vits-zh-hf-fanchen-C.onnx     主模型
├── lexicon.txt                   发音词典
├── tokens.txt                    符号表
├── dict/                         分词词典
├── speakers.txt                  说话人列表（187 行）
├── number.fst / date.fst / phone.fst   文本正则化（数字/日期/电话读法）
└── test_wavs/                    官方测试音频
```

`sherpa-onnx` 会自动加载 `rule_fsts`（`number.fst`/`date.fst`/`phone.fst`），
所以「**剩余电量 85%**」里的 `85` 会被正确读成"八十五"而不是"八五"。

### 4.3 模型下载（如果还没下）

模型来自 sherpa-onnx 的 GitHub release（tag `tts-models`）。

> ⚠️ **本机现状**：模型**已经在板子上了**（`~/models/tts/vits-zh-hf-fanchen-C/`）。
> 卸载了、误删了、或换新板子时才需要重新下。

```bash
sudo su - HwHiAiUser
cd ~/car_gui

# 下载（走代理，直连 GitHub 基本不通）
source ./_pyenv.sh                       # 载入 $CAR_PY

MODEL=vits-zh-hf-fanchen-C
curl -L --retry 3 -o /tmp/$MODEL.tar.bz2 \
  "https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/$MODEL.tar.bz2"

mkdir -p ~/models/tts && tar xjf /tmp/$MODEL.tar.bz2 -C ~/models/tts/
```

**可用代理**（按实测速度排序，与 KWS/ASR 模型下载一致）：

| 代理 | 前缀 |
|---|---|
| ghfast.top | `https://ghfast.top/` |
| gh-proxy.com | `https://gh-proxy.com/` |
| ghproxy.net | `https://ghproxy.net/` |

> 💡 板子直连下载慢时，**建议在 PC 上先下好**（Git Bash 里同样能跑 curl），
> 再用 `scp -r` 把整个目录传过去。见《../../2.环境配置/SSH连接与传文件.md》。

---

## 五、🔴 踩坑记录：`vits-icefall-zh-aishell3` 不能用

这是选型时踩过的坑，**记下来避免重复试**。

最初想用 `vits-icefall-zh-aishell3`（icefall 系，看起来更"官方"），结果：

```
Error when reading tokens at Line sil 0. size: 3
```

**根因**：它的 `tokens.txt` 第一行是 `sil`（3 个字符），
而新版 sherpa-onnx API 期望的是 `<sil>` 这种带尖括号的格式。
这是**模型发布时的格式**问题，不是代码问题，改不了。

| 模型 | 结果 |
|---|---|
| `vits-icefall-zh-aishell3` | ❌ `Error when reading tokens at Line sil 0. size: 3` |
| **`vits-zh-hf-fanchen-C`** | ✅ 正常，`tokens.txt` 是标准格式 |

> **教训**：sherpa-onnx 的模型仓库里，**不同来源的模型格式并不统一**。
> 遇到"tokens 解析失败"这类报错，第一反应应该是**换一个模型**，
> 而不是去改 `tokens.txt`（改了通常还有别的格式问题等着）。

---

## 六、代码结构

`tts_engine.py` 分三层，**引擎层不依赖 GUI**（和 `asr_engine.py` 一样的思路）：

| 层 | 类/函数 | 作用 |
|---|---|---|
| 模型定位 | `guess_tts_dir()` / `find_tts_files()` | 找模型目录，**不用手写路径** |
| 引擎 | `TtsEngine.load()` / `.synth()` / `.save()` / `.play()` | 加载、合成、存盘、播放 |
| 命令行 | `_main()` | SSH 里直接跑 |

配套脚本：

| 文件 | 作用 |
|---|---|
| `tts_engine.py` | **主引擎**（加载 / 合成 / 存盘 / 播放 / 输出设备探测 / 播报队列 / **多架构自动识别**） |
| `tts_speaker_compare.py` | **挑音色用**：批量合成多个说话人，拼成一个对比 wav |
| `tts_serve.py` | **网络音箱**：把板载 TTS 变成 HTTP 服务，声音送到浏览器播放（见 §2.1） |
| `tts_quality_test.py` | **音质客观测量**：合成 → 用板载 ASR 反听 → 拼音/字符准确率 + RTF（见 §2.5.1） |
| `test_voice_estop.py` | **安全断言**：假车 + 假队列，锁死"先停车→再锁定→后播报"（见 §8.1） |
| （`car_gui_core.py`） | 接入点：`init_tts()` / `speak()` / `enable_voice_emergency()` / `trigger_emergency()`（见第八节） |

> `find_tts_files()` 实际要求的文件：`model.onnx`（或目录下任意 `.onnx`）、
> `lexicon.txt`、`tokens.txt`；`dict/` 目录可选（找不到就用模型目录本身）。
> `rule_fsts` 里的 `number.fst`/`date.fst`/`phone.fst` **有就加载、没有就跳过**，
> 不会因为缺这三个文件而失败。

### 关键接口

```python
from tts_engine import TtsEngine, SpeakQueue, output_probe

# ① 先看有没有输出设备（不加载模型，很快）
probe = output_probe()
#   {"ok": False, "alsa": [], "sinks": [], "players": ["paplay","aplay","ffplay"]}
#   ok=False → 本板现状，别去调 --play，先看 §2

eng = TtsEngine()
ok, msg = eng.load()                   # 加载模型（约 3.7 秒）
#   ok=True, msg="加载完成 3.70 秒，187 个说话人，16000 Hz"

sr, data = eng.synth("已急停")          # 合成，返回 (采样率, float32 数组)
                                        # ⚠️ 内部加锁，全局串行（4 核别并发合成）
ok, msg = eng.save("已急停", "/tmp/a.wav")   # 合成并存 16bit wav
ok, msg = eng.play("已急停")            # 合成整段并播放（需输出设备）
ok, msg = eng.say("已急停")             # 分句合成 + 边合成边播，首句出声更快
ok, msg = eng.say("已急停", device="plughw:1,0")   # 指定 aplay 设备
ok, msg = eng.play_samples(sr, data)    # 播已在内存里的音频（不重新合成）

TtsEngine.set_volume(85)                # 对所有播放卡片设 85% 音量

# ② 接到小车里：用后台串行队列，绝不阻塞主线程
sq = SpeakQueue(eng, on_done=lambda ok, msg, t: print(t, ok, msg))
sq.put("已急停")                         # 立即返回，不等待合成
sq.put("开始前进")                        # 会排队，不会和上一句重叠
sq.close()
```

### 为什么 `load()` 要单独调用

模型加载要 **3.7 秒**，而合成只要 **1~4 秒**。
如果每次播报都重新加载，一次播报就变成 7 秒 —— **在急停场景完全不可接受**。

**正确做法**：程序启动时 `load()` 一次，之后反复调 `synth()`/`play()`。

> ⚠️ 这也意味着：**TTS 引擎应在应用启动时就初始化好**，
> 而不是等到"要播报了"才临时加载。接到 GUI 里时注意这一点（见第七节）。

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--text` | `已急停` | 要合成的文本 |
| `--out` | `/tmp/tts_<时间戳>.wav` | 输出路径（**默认带时间戳，不覆盖**） |
| `--speaker` | `0` | 说话人编号 **0~186** |
| `--speed` | `1.0` | 语速。`<1` 变慢更清晰，`>1` 变快更急 |
| `--threads` | `2` | 推理线程。**4 核 CPU 上最多给 2**，留核给 GUI 和串口 |
| `--dir` | 自动 | 模型目录 |
| `--play` | — | 直接播放（需输出设备） |
| `--list-speakers` | — | 列出全部 187 个说话人 |

---

## 七、使用方法

### 7.1 基本命令

> 💡 **统一用 `bash run_py.sh <脚本>` 而不是 `python3 <脚本>`。**
> 本板多套 Python 并存，直接敲 `python3` 很容易撞上"缺依赖"的假故障
> （详见《12.KWS…》§4.7）。

```bash
sudo su - HwHiAiUser
cd ~/car_gui

# 0.【先跑这个】环境诊断：依赖 / 模型 / 有没有输出设备（快，不加载模型）
bash run_py.sh tts_engine.py --check
bash run_py.sh tts_engine.py --list-devices      # 只看输出设备
bash run_py.sh tts_engine.py --list-models       # 看装了哪些模型、自动选了哪个

# 0b.【想知道音质好不好】客观测量（合成 → 板载 ASR 反听 → 拼音准确率）
bash run_py.sh tts_quality_test.py
bash run_py.sh tts_quality_test.py --speakers 45,47,52   # 挑音色

# 合成一句话存成 wav
bash run_py.sh tts_engine.py --text "已急停" --out /tmp/stop.wav

# 列出全部说话人，挑一个音色好的
bash run_py.sh tts_engine.py --list-speakers

# 换说话人 + 放慢语速（播报类建议 0.9~1.0，太快听不清）
bash run_py.sh tts_engine.py --text "前方有障碍，已停止" --speaker 3 --speed 0.95

# 直接播放（🔴 需要先接上 USB 音箱，否则必然失败）
bash run_py.sh tts_engine.py --text "已急停" --play
bash run_py.sh tts_engine.py --text "已急停" --play --device plughw:1,0   # 指定设备
bash run_py.sh tts_engine.py --text "已急停" --play --volume 85           # 先设音量

# 【没有音箱时】起网络音箱，用 PC/手机浏览器当喇叭（见 §2.1）
bash run_py.sh tts_serve.py --port 8099
```

### 7.2 现在就能做的：生成 wav 传回 PC 听

在音箱到手之前，**跑通引擎、挑音色**这件事现在就能做：

```bash
# 板子上：批量合成 6 个候选音色，拼成【一个】对比文件
bash run_py.sh tts_speaker_compare.py --text "已急停，请注意"

# 或指定编号 / 随机挑
bash run_py.sh tts_speaker_compare.py --speakers 0,3,5,18,42,99
bash run_py.sh tts_speaker_compare.py --random 8
```

然后**在 PC 上**（Windows Git Bash）：

```bash
scp root@192.168.137.100:/tmp/tts_compare.wav ./
# 打开听一遍 —— 每段前会先念出编号，听到喜欢的记下那个数字
```

> 💡 **`tts_speaker_compare.py` 就是为这件事写的**。
> 187 个说话人逐个 scp 不现实，它把 N 个候选**拼成一个 wav**
> （中间插 0.6 秒静音），**只需传一个文件**就能一次听完。
>
> 它还会在每段前先合成一句「编号 N」，你听到哪段好听，
> 直接就知道对应编号是多少，不用数。

### 7.3 说话人怎么选

187 个说话人全是同一个女声模型（fanchen-C）的不同**音色/韵律变体**，
不是 187 个不同性别年龄的人。

建议：

- 播报类选**平稳、偏低**的音色（听起来像"系统提示"而不是"在朗读"）
- 用 §7.2 的方法**一次听完 6~8 个候选**，比逐个试快得多
- 定下来之后写进代码默认值，不要每次手输

### 7.4 语速建议

| 场景 | `--speed` | 理由 |
|---|---|---|
| 急停播报 | `0.9 ~ 1.0` | 要听清，不能快 |
| 状态提醒 | `1.0` | 正常 |
| 连续播报多条 | `1.05 ~ 1.1` | 稍微加快，避免拖沓 |

---

## 八、接入小车（**已于 2026-09-27 实现并实测**）

### 8.1 设计原则：**播报永远在动作之后**

```
KWS 命中「急停」
   ↓
   ├─ ① 立即下发零速度帧（同步、最高优先级、不排队）  ← 这一步不等任何东西
   ├─ ② 进入急停锁定态
   └─ ③ 【之后】再 TTS 播报「已急停」（异步投递，不等待）
```

> ⚠️ **绝不能**写成"先播报再停车"。合成要好几秒，那几秒里车还在跑 ——
> 这是**安全问题**，不是性能问题。
>
> ✅ 这条顺序**已经被断言锁死**：`test_voice_estop.py` 用假车记录时间线，
> 断言"零速度帧的下标 < 播报投递的下标"。以后改动代码跑一遍就知道有没有被破坏
> （实测输出：`✓ 停车发生在播报之前（安全顺序正确）  stop@0 speak@5`）。

### 8.2 实现的接口（全在 `car_gui_core.py`）

| 方法 | 作用 |
|---|---|
| `init_tts(speaker, enabled, background, web_port)` | **启动时**预加载 TTS + 起播报队列。**默认后台加载**（要 5~7 秒，同步会冻住界面） |
| `speak(text, drop_pending, force)` | 投递一句播报，**立即返回**。未就绪/关掉/冷却中返回 `False`（**允许失败**） |
| `set_speak_enabled(on)` / `set_announce_actions(on)` | 播报总开关 / 动作播报开关 |
| `enable_voice_emergency(...)` | 开启 KWS 常开监听，命中走下面的链路 |
| `trigger_emergency(reason, announce)` | **急停完整语义**：① 停车 → ② 锁定 → ③ 播报 |
| `emergency_stop()` | 纯停车原语（安全链路最后一环，特意保持最"薄"） |
| `release_estop()` | 人工解除锁定（**必须人工** —— 误触发后车不会自己又跑起来） |
| `voice_status()` / `tts_status()` / `poll_voice_events()` | 给界面用的状态与事件 |
| `shutdown_voice()` | 退出时收尾 |

默认播报模板（`SPEAK_TEMPLATES`）：前进 →「开始前进」、左转 →「向左转」、
停止 →「已停止」……急停固定「已急停」。
**文本是写死的模板，不让大模型现编**（播报不能依赖 LLM，也不能因为它变慢）。

### 8.3 三个必须遵守的约束

**① 播报走后台队列，绝不阻塞主线程**
用 `tts_engine.SpeakQueue`：常驻一个后台线程串行合成 + 播放。
合成要好几秒，直接调会把 GUI 冻死（和 ASR 是同一个教训）。

**② 动作播报带 `drop_pending`（过期作废）**
本板 RTF ≈ 5，排队滞后的播报会**说反话** —— 车都已经停了，
还在慢慢说「开始前进」。所以投递新动作播报时**丢掉排队里的旧条目**。
急停播报同样带 `drop_pending`，用来**顶掉**所有排队的动作播报。

> ⚠️ 顺带一个很容易写错的点：
> `SpeakQueue.pending() == 0` 只说明"排队里没有新条目"，
> 此刻很可能**正在合成/播放上一条**。要判断"说完没有"必须看
> `pending()==0 and not speaking` —— 已封装成 `idle()`。

**③ 播报失败绝不能影响动作**
`speak()` 未就绪时返回 `False`，调用方当它"可以失败"。
`test_voice_estop.py` 专门断言了这件事：
**没有播报队列时，急停照样连发 5 帧停止、照样进入锁定态。**

### 8.4 🔴 本板特有问题：合成完了没地方放

本板**没有本地播放设备**（见第二节）。如果播报只能在本地放，
那接进小车就是"合成完直接丢掉"，**永远听不到**。

解法是给引擎加了**额外输出端（sink）**机制：

```python
eng.add_sink(fn)        # fn(rate, data, text) -> None
```

`say()` 的语义因此变成：**本地能放就放，同时把音频送给所有 sink**。

`init_tts()` 检测到"没有本地输出设备"时会**自动**在 8099 起一个后台网络音箱
（`tts_serve.start_background(engine)`，**复用同一个引擎、不重复加载模型**），
并把它注册成 sink。于是：

```
小车播报「已急停」
   → SpeakQueue 合成
   → sink 把 wav 塞进广播缓冲
   → 浏览器的 /listen 页面轮询取走 → 出声
```

```bash
# 启动控车程序后，浏览器打开这个地址当车的喇叭：
http://192.168.137.100:8099/listen
```

`web_port` 参数：`None`（默认）= 自动（没本地喇叭才起）/ `0` = 不起 / 具体端口 = 用它。

> 💡 **买了 USB 音箱之后**：`output_probe()` 会检测到本地设备，
> 网络音箱**自动不再启动**，代码一行都不用改。

### 8.5 实测（2026-09-27，板子 + PC 真跑）

```
[13:18:56] 🔊 网络音箱已启动：http://192.168.137.100:8099/listen
[13:18:56] TTS 就绪（kokoro-multi-lang-v1_0，说话人 47，输出：网络音箱 …）
让小车说「已急停」… 播报完成，用时 11.4 秒
/next -> {'seq': 1, 'text': '系统就绪', 'url': '/audio/1.wav'}
音频 60346 字节，类型 audio/wav → 24000 Hz 单声道 16bit 1.26 秒
/next(since=1) -> {'seq': 2, 'text': '已急停'}
✅ 链路通：控制器播报 → 网络音箱 → HTTP 可取到合法 wav
```

离线断言（`test_voice_estop.py`，**不需要硬件、不需要麦克风、不加载模型**）：
**通过 44 · 失败 0**；已挂进 `test_all_offline.py` 第 3 项（`--quick` 也会跑）。

板子上的完整回归：
```
通过 6 · 失败 0 · 跳过 2      # 解释器体检 / 纯逻辑 / 串口 / 语音急停 / GUI / 端到端
```

### 8.6 命令行自测（不需要小车硬件）

```bash
# 只播报一句，验证 TTS 链路（不需要串口/小车）
bash run_py.sh car_gui_core.py --say "已急停"

# 预加载 TTS 后只跑语音链路，不连串口
bash run_py.sh car_gui_core.py --no-connect --tts --seconds 30

# 全开：TTS 播报 + 语音急停（不连串口也能验急停链路）
bash run_py.sh car_gui_core.py --no-connect --tts --voice --seconds 60

# 安全断言（1 秒跑完）
bash run_py.sh test_voice_estop.py
```

### 8.7 GUI 已接好（2026-09-27）

`car_gui_pyside.py`（主界面）和 `car_gui_tk.py`（备用）都加了：

| 控件 | 作用 |
|---|---|
| **播报状态灯 + 文字** | 就绪/加载中/不可用/未启用；文字里带模型名、说话人、输出方式 |
| **☑ 播报** | 播报总开关（急停播报不受影响） |
| **☑ 动作播报** | 「开始前进」这类动作确认的开关 |
| **语音急停灯 + [开启语音急停]** | KWS 常开监听；**麦克风链路断了会变红并提示"急停已失效"** |
| **麦克风电平条 + 命中次数** | 判断关键词好不好使 |
| **声音输出** | 有音箱显示「板载喇叭」；没有则给出 `http://IP:8099/listen` 供浏览器打开 |
| **急停锁定警示条** | **只在急停后出现**：红底 + 原因 + 「解除急停锁定」按钮；窗口标题也带 `🔒` |
| **`--no-tts` 启动参数** | 不预加载播报（省 5~7 秒启动时间和约 600MB 内存） |

**两个必须知道的点**：

1. **急停后必须人工点「解除急停锁定」** —— 这是刻意的设计：误触发后车不会自己又跑起来。
2. **麦克风断链报警**：`voice_status()["watchdog_ok"]` 为 False 时灯变红、
   日志提示"语音急停已失效"。**这时候语音急停其实已经名存实亡，必须处理**，别以为还开着。

界面细节见《../GUI语音交互面板.md》§5.1 / §5.2。

### 8.8 还没做的

| 项 | 说明 |
|---|---|
| **播报语定稿** | 目前是 `SPEAK_TEMPLATES` 里的默认文案，可按演示需要调整 |
| **音量** | 有本地喇叭后可用 `amixer -c N sset Speaker 80%`；`TtsEngine.set_volume()` 已就绪 |
| **与录音的冲突** | 用独立 USB 音箱时录音/放音天然不冲突，无需处理 |

---

## 九、故障排查

| 现象 | 原因 | 对策 |
|---|---|---|
| **合成出来听不懂 / 完全不像人话** | 用了 `vits-zh-hf-fanchen-C`（**实测输出客观不可懂**） | **换 Kokoro**；并用 `tts_quality_test.py` 客观确认。见 §2.5 |
| **中文带很重的英文口音** | 多语言模型的**编号 0 是英语音色** | **指定中文音色**（kokoro 用 45~52；默认已自动设为 47）。见 §2.5.5 |
| **ASR 反听把「已急停」写成「一级停」** | **这不是故障** —— ASR 选了同音字 | 看**拼音准确率**，不看字符。见 §2.5.1 |
| `pip install` 了却 `import` 不到 | **装进了另一套 Python** | 用 `$CAR_PY -m pip install …`，别用裸 `python3`。见 §2.5.9 |
| `nohup … > log &` 后日志长时间空白 | Python stdout 重定向时**块缓冲** | 已修（`run_py.sh` 加了 `PYTHONUNBUFFERED=1`）。见 §2.5.9 |
| **`aplay -l` 只有标题行** | **本板无输出设备**（PCM2902 只有输入，板上也没 codec） | **接 USB 音箱；或先跑 `tts_serve.py`。见第二节** |
| **想用蓝牙音箱** | 内核 `# CONFIG_BT is not set`，没编蓝牙 | **此路不通**，不要浪费时间。见 §2 根因③ |
| **`uptime` 里 load average 一直 17** | 昇腾驱动的 17 个内核线程常驻 `D` 状态等硬件 | **正常现象**。看 `top` 的 `%id`：实测 97.6% 空闲 |
| **PC 上 curl 板子超时 / 连不上** | PC 设了 `http_proxy`，curl 把局域网请求也丢给代理 | **加 `--noproxy '*'`**。见 §2.1 |
| **`/listen` 页面不出声** | 浏览器自动播放限制，必须有用户手势 | **点一下「开始收听」按钮**（这是刻意的设计） |
| `--play` 报 `找不到 aplay/paplay/play` | 未装 `alsa-utils` | `sudo apt install alsa-utils` |
| `--play` 报 `Device or resource busy` | 声卡被别的进程占着 | `fuser -v /dev/snd/*` 看是谁；PulseAudio 可用 `pactl` 管理 |
| **`Error when reading tokens at Line sil 0. size: 3`** | **模型格式不匹配**（`vits-icefall-zh-aishell3`） | **换 `vits-zh-hf-fanchen-C`。见第五节** |
| `模型目录缺少 model/lexicon/tokens` | 模型没下全或路径不对 | 检查 `~/models/tts/vits-zh-hf-fanchen-C/`；跑 `--dir` 显式指定 |
| 合成出来是静音 wav | 文本含模型不认识的字符 | 换成常见汉字；检查 `tokens.txt` 里有没有该字 |
| 数字读成单个数字（"8 5"） | `rule_fsts` 没加载 | 确认目录里有 `number.fst` 等文件 |
| 加载报 `No module named sherpa_onnx` | **用错了 Python** | 跑 `python3 env_doctor.py`，统一用 `bash run_py.sh` |
| 合成很慢（> 6 秒） | 线程给多了，CPU 争抢 | `--threads 2`（4 核板子别给 4） |
| 播报把 GUI 卡住 | 合成在主线程做了 | 改成后台线程 + 队列，见 §8.2 |

---

## 十、实测数据（2026-09-26 / 27 板子实测）

### Kokoro（当前使用）

| 指标 | 实测值 | 备注 |
|---|---|---|
| 模型加载 | **5.4 ~ 7.1 秒** | 比 VITS 慢；**必须启动时预加载** |
| **拼音准确率** | **96.4%（最低 85.7%）** | ★ 听得清；旧模型是 0% |
| 合成「已急停」（1.2 秒音频） | **约 6 秒** | RTF ≈ 5.4 |
| 合成「前方有障碍，已停止」（1.8 秒） | **约 7.8 秒** | RTF ≈ 4.3 |
| 采样率 | **24000 Hz** | 比 VITS 的 16k 高 |
| 说话人 | 54 个，**其中 45~52 是中文音色** | 编号 0 是英语音色 |
| 峰值（响度） | **0.506** | 旧模型只有 0.136 |
| 模型体积 | 325 MB + 音色表 28 MB | 解压后 384 MB |
| 输出设备 | **0 个** | 🔴 阻塞项，见第二节 |

### VITS `vits-zh-hf-fanchen-C`（**已弃用**，留档对照）

| 指标 | 实测值 | 备注 |
|---|---|---|
| 模型加载 | 3.7 ~ 5.0 秒 | |
| **拼音准确率** | **0%** | ❌ ASR 反听 4/4 全错 |
| 合成 1.48 秒的句子 | 约 4.7 秒 | RTF ≈ 3.2（比 Kokoro 快，但听不清） |
| 采样率 / 说话人 | 16000 Hz / 187 个 | 音符集是注音符号 |

### 板子健康基线（别误判成故障）

| 项 | 正常值 | 说明 |
|---|---|---|
| `load average` | **~17** | 昇腾驱动 17 个内核线程常驻 `D` 状态等硬件所致，**不是过载** |
| CPU 空闲率 | **>95%** | 拿这个判断忙不忙，别用 load |
| 内存 | 3.5 GB 总 / 可用 ~1.9 GB | 4GB LPDDR4X 被系统留了一部分 |

### RTF 说明

**RTF（Real-Time Factor）= 合成耗时 / 音频时长**。

实测 4.7 / 1.48 ≈ **3.2**，意思是"**要花 3.2 秒的运算，才能产出 1 秒的语音**"。

**这意味着什么**：

- ✅ **做播报完全够用**：一句"已急停"约 0.8 秒音频 → 合成约 2.6 秒 → 能接受
- ❌ **做实时对话不够**：不能说一句、等 3 倍时间才能听到回应
- 💡 **优化空间**：这是 **CPU 推理**（4 核 TaishanV200M @ 1.0GHz，
  本身是这块板最大的瓶颈）。二期上 NPU 可显著改善，但要 CANN 8.x。

> ⚠️ **播报文本要短**。"已急停" 三个字合成约 2 秒；
> 换成 "检测到您的紧急停止指令，车辆已经安全停稳，请确认周围环境后再继续操作"
> 这句要合成十几秒 —— **播报语要像系统提示音，一句话说清**。

---

## 十一、与 ASR / KWS 的关系

三者共用 `sherpa_onnx` 这一个 Python 包，但**互不依赖**：

| 能力 | 文件 | 模型 | 方向 |
|---|---|---|---|
| ASR（听懂） | `asr_engine.py` | SenseVoice-Small int8（228MB） | 听 |
| KWS（关键词） | `kws_engine.py` | zipformer-3.3M（32MB） | 听 |
| **TTS（说话）** | **`tts_engine.py`** | **vits-zh-hf-fanchen-C（113MB）** | **说** |

**资源占用**（4GB 内存要算着用）：

| 组合 | 内存 | 说明 |
|---|---|---|
| 只跑 ASR | ~600 MB | 当前 GUI 的默认 |
| ASR + TTS | ~1.1 GB | 常规语音交互 |
| ASR + KWS + TTS | ~1.3 GB | **全量**，建议观察内存占用 |

> 💡 **三者同时开时注意 CPU**：ASR 识别、KWS 常开监听、TTS 合成都吃 CPU，
> 而本板只有 4 核 1.0GHz。**KWS 必须常开**（安全），
> 所以 TTS 的 `--threads` 建议压到 **1~2**，别抢 KWS 的份额。

---

## 十二、待办

- [x] 下载 `vits-zh-hf-fanchen-C` 模型（113MB）
- [x] 写 `tts_engine.py`（加载 / 合成 / 存 wav / 播放，含 `rule_fsts` 自动加载）
- [x] 写 `tts_speaker_compare.py`（批量挑音色，拼成单个对比 wav）
- [x] 踩坑并排除 `vits-icefall-zh-aishell3`（tokens 格式不匹配）
- [x] 板子实测：加载 3.7s、合成 4.7s/1.48s 音频
- [x] **逐条查清"没有输出"的三条原因**（USB 描述符无 OUT 端点 / 内核无 codec /
      `# CONFIG_BT is not set`），确认不是配置问题 —— §2
- [x] `tts_engine.py` 补齐：输出设备探测 `output_probe()`、`--check` 诊断、
      `--list-devices`、`--device` / `--sink` / `--volume`、分句播报 `say()`、
      后台串行播报队列 `SpeakQueue`、合成加锁全局串行
- [x] **写 `tts_serve.py` 网络音箱**（零额外依赖）：手动试听页 + 自动收听页 +
      `POST /say` 投递接口 + `/healthz`，并**端到端实测通过** —— §2.1
- [x] **查实旧模型输出客观不可懂**（ASR 反听 4/4 全错），并排除 ASR / 采样率 / 文件完整性
- [x] 写 `tts_quality_test.py`（**ASR 反听 + 拼音/字符双层准确率 + RTF**），把"音质"量成数字
- [x] 下载并跑通 **Kokoro**：拼音准确率 **96.4%**（旧模型 0%）
- [x] `tts_engine.py` 支持 **vits / kokoro / matcha 自动识别**；`guess_tts_dir()` 按音质优先级自动选模型
- [x] 修掉"多语言模型默认用编号 0（英语音色）"的坑，默认说话人改为实测值
- [x] 修 `run_py.sh` 加 `PYTHONUNBUFFERED=1`（重定向日志不再一片空白）
- [x] **音色定稿**：说话人 **47**（用户确认）
- [x] **接入 `car_gui_core.py`**：`init_tts()` / `speak()` / `trigger_emergency()` /
      `enable_voice_emergency()` / 急停锁定态 —— 见第八节
- [x] **引擎加 sink 机制**，把播报接到网络音箱（本板没喇叭，否则合成完只能丢掉）
- [x] `init_tts()` 检测到无本地输出时**自动**起后台网络音箱（8099），复用同一引擎
- [x] 串口写锁（`_write_speed`）—— 现在界面线程和 KWS 线程都会写串口，必须串行化
- [x] 写 `test_voice_estop.py`（44 项断言）并挂进 `test_all_offline.py` 第 3 项
- [x] 板子实测：控制器播报 → 网络音箱 → HTTP 取到合法 wav；完整回归 6 通过 0 失败
- [ ] **买 USB 音箱**（仍是最终形态的必需品）—— 买回来自动生效，代码不用改
- [ ] 音箱到手后：`speaker-test` 验证 → 用 `tts_quality_test.py` 挑定音色与语速
- [ ] **GUI 暴露**（PySide / Tk）：播报开关、急停锁定态 + 解除按钮、语音状态指示灯
- [ ] **补 Matcha 的声码器**（`hifigan_v2.onnx`）再测一轮 —— 它是"比 Kokoro 快得多"的备选
- [ ] 播报模板按演示需要微调（`SPEAK_TEMPLATES`）
- [ ] 可选：合成输出做**峰值归一化**（Kokoro 峰值 0.506 已经不错，旧模型只有 0.136）
- [ ] 二期：TTS 上 NPU（需 CANN 8.x + 源码编译 + 自行 ATC 导出 310B4 的 `.om`）

---

## 附：一句话总结

> **模型已换成 Kokoro，音质实测达标（拼音准确率 96.4%，旧模型 0%）。**
> **"没有声音"是硬件问题**（板载无 codec、PCM2902 只有 IN 端点、内核没编蓝牙），
> 配置解决不了 —— 但**不必等硬件才能听到**。
>
> ```bash
> # ① 现在就能听到（¥0）：把浏览器当喇叭
> bash run_py.sh tts_serve.py --port 8099
> #    然后 PC/手机打开 http://192.168.137.100:8099/
>
> # ② 最终形态（需买 USB 音箱）
> bash run_py.sh tts_engine.py --text "已急停" --play
>
> # ③ 想知道音质到底好不好（客观测量，不靠耳朵）
> bash run_py.sh tts_quality_test.py
> ```
>
> 拿不准从哪开始，先跑 `bash run_py.sh tts_engine.py --check`：
> 它会报依赖、可用模型、有没有输出设备，并把上面几条路都摆出来。
