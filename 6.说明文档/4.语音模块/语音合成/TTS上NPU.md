# TTS 上昇腾 NPU（FastSpeech2）

> **结论先行**：TTS 已跑在板子的 **昇腾 NPU** 上，**平均 RTF 0.92（达到实时）**，
> 比原来的 Kokoro（CPU，RTF 5.43）**快约 5.9 倍**。
>
> 本文覆盖：为什么换 → 怎么装的 → 实测数据 → **怎么用** → 6 个坑 → 已知限制。

---

> 🔵 **板子 IP 提示**：本文若出现 `192.168.137.100`，那是**网线直连 PC** 时的静态地址（一直有效）。
> 走 **WiFi** 时地址由路由器 DHCP 分配、**会变**。找不到就：
> ① PC 上双击 `../../2.环境配置/工具脚本\find_car.cmd`；② 板子上 `bash find_ip.sh`；
> ③ 详见《../../2.环境配置/快捷命令速查.md》**§0.1**。

## 一、为什么要换

原方案是 **sherpa-onnx + Kokoro-82M**，跑在 **CPU** 上：

| 项 | Kokoro（CPU）| FastSpeech2（NPU）|
|---|---|---|
| RTF（越小越快）| **5.43** | **0.92** |
| 单句耗时 | 0.5 ~ 13 秒（看句子长度）| **恒定约 1.11 秒** |
| 「已急停」这种短句 | ~4.7 秒 | **1.11 秒** |
| 模型加载 | ~5.4 秒 | ~2.6 秒 |
| 采样率 | 24000 Hz | 22050 Hz |
| 说话人 | 54 个音色 | 1 个（女声）|

**RTF 5.43 的含义**：合成 1 秒语音要花 5.43 秒。播报「检测到障碍物，请小心」
（2.28 秒语音）要等 **12.95 秒** —— 车早开走了语音才出来。这对**交互**是致命的。

换到 NPU 后，**1.11 秒就出结果**，播报才真正跟得上动作。

---

## 二、怎么跑的（原理）

用的是 **昇腾社区官方为 Atlas 200I DK A2 适配的 FastSpeech2 工程**
（Gitee `ascend/ascend_community_projects` 的 **310B 分支**），
5 个 ONNX 模型用 ATC 转成 `.om`，再用 **pyACL** 推理。

```
文本 → 拼音/音素 → encoder.om → variance_adaptor.om → decoder.om
                                                      → postnet.om
                                                      → hifigan.om（声码器）
                                                      → 22.05kHz 音频
```

**关键：310B 芯片只支持静态 shape**，所以模型永远按固定长度算满 ——
这也是「耗时恒定、与句子长短无关」的原因（见 §5）。

---

## 三、怎么用（日常只需要知道这些）

### 3.1 正常启动 GUI —— 自动就用 NPU

```bash
cd ~/car_gui
bash run_gui.sh                    # 默认 auto：NPU 优先，不可用自动回退 CPU
```

启动日志里会明确写着用了哪个后端：

```
[TTS] NPU TTS：模型加载完成 2.61s（.../models/tts-npu；音素上限 64，梅尔帧上限 250≈2.9 秒音频）
TTS 后端：NPU（NPU TTS 就绪（FastSpeech2, 22050Hz, 2.61 秒加载；单次上限 250 帧≈2.9 秒））
TTS 就绪（tts-npu，说话人 0，后端 NPU，输出：板载喇叭）
```

### 3.2 想强制用 CPU（对照 / NPU 出问题时）

```bash
CAR_TTS_BACKEND=cpu bash run_gui.sh
```

### 3.3 单独测 TTS（不开 GUI）

```bash
cd ~/car_gui
bash run_py.sh npu_tts_engine.py --text "大家好"          # 合成一句
bash run_py.sh npu_tts_engine.py --bench                  # 跑 4 句测 RTF
bash run_py.sh npu_tts_engine.py --status                 # 只看可用性
```

### 3.4 完全关掉 NPU（回退到 Kokoro）

```bash
mv ~/models/tts-npu ~/models/tts-npu.off     # 改名即可
```

`npu_tts_engine.available()` 会返回 False，`load_best()` 自动回退 CPU。

---

## 四、实测数据

```
大家好            合成 1.11s  音频 0.81s  RTF 1.37
已急停            合成 1.11s  音频 0.81s  RTF 1.36
你好，我是你的智能小车   合成 1.10s  音频 2.38s  RTF 0.46
检测到障碍物，请小心    合成 1.11s  音频 2.28s  RTF 0.49

平均合成耗时 1.11s    平均 RTF 0.92
★ 对照 Kokoro(CPU) RTF 5.43 → 加速 5.89 倍
```

集成到 `SpeakQueue` 后（走生产路径）3 句连续播报 **7.6 秒全部完成**；
同样 3 句用 Kokoro 光前两句就要 **17.7 秒**。

回归验证：`test_all_offline.py` **通过 10 · 失败 0 · 跳过 1**（与换 NPU 前一致）。

---

## 五、⚠️ 已知限制（用之前必须知道）

### 5.1 单次播报有**时长上限**（会**静默丢字**）

`max_mel_len=250` 帧 ≈ **2.9 秒音频** ≈ **约 18 个汉字**。
超了官方代码会**静默截断**（只 print 一句 `Truncate the input.`，不报错）。

实测比例约 **13.5 帧/汉字**：

| 文本 | 帧数 | 250 帧下 |
|---|---|---|
| 大家好（3 字）| 70 | ✅ |
| 检测到障碍物，请小心（10 字）| 196 | ✅ |
| 我还没连上小车呢，听不见你的指令哦（21 字）| **283** | ❌ 丢最后两字 |

**已处理**：`npu_tts_engine.py` 里 `DEFAULT_SENT_LEN = 14` 主动分句规避，
并且 `synth()` 会**检测截断并打日志**：

```
[TTS] ⚠ NPU TTS 截断：「...」需要 284 帧 > 上限 250 帧，**末尾会被丢掉**。请拆成更短的句子
```

看到这条日志就说明还有句子没拆干净，把文本发出来针对性调整阈值。

### 5.2 耗时恒定 —— 短句不会更快

因为静态 shape 永远算满 250 帧，**「已急停」也要 1.11 秒**。
好处是**可预测**（不会像 Kokoro 那样从 0.5 秒到 13 秒剧烈波动）；
坏处是短句 RTF > 1。**别在代码里假设「短句会更快」。**

### 5.3 只有单说话人

baker 女声。`speaker` 参数被忽略。需要多音色就用 CPU 的 Kokoro。

### 5.4 长句靠分句，不是靠调大上限

把 `max_mel_len` 提到 500 试过 —— **decoder 一推理就段错误**（见 §6.6），
已回滚。目前靠**分句**解决，够用。

---

## 六、踩过的 6 个坑（都有普适性）

### 6.1 ATC 参数：`Ascend310B1`，**必须静态 shape**
```bash
atc --framework=5 --input_format=ND --soc_version=Ascend310B1 \
    --model=decoder_sim.onnx --output=decoder_bs1 \
    --input_shape="output:1,250,256;mel_masks:1,250"
```
- soc 写 **`Ascend310B1`**，**不是** `Ascend310B4`（写错会得到误导性的算子报错）。
- **310B 只支持静态 shape**，不要试 `--dynamic_dims`。

### 6.2 ATC 需要 numpy < 2（且不能动系统 numpy）
CANN 7.0.RC1 的 tbe 用了 `np.float_`，NumPy 2.0 已移除。
但本板系统装的是 numpy 2.2.6，`sherpa_onnx` 依赖它，**不能降级**。
→ 隔离一套 numpy 1.26 到 `car_gui/npu_libs/`，在 `npu_tts_engine.py`
**import numpy 之前**插到 `sys.path` 最前。

### 6.3 ★ 干脆不要 scipy
官方 `synthesize.py` 只有一行用 scipy（`wavfile.write`），
但装真 scipy 会引出**无法调和的 numpy 冲突**（scipy 用 NumPy 1.x 编译，
链路里 numpy 早就被加载成 2.2.6 → `_ARRAY_API not found`）。
试过 `.pth` 前置、`sys.path.insert(0)` **都压不过已加载的 numpy**。
→ 用标准库 `wave` 写了个 `scipy.io.wavfile` 替身塞进 `sys.modules`，
**官方代码一行不改**照样跑。

### 6.4 ★★ ACL 的 device context 是**线程局部**的（最坑的一个）
主线程里连续推理完全正常，一放进 `SpeakQueue` 的**后台线程**就报
`malloc(512) 失败，错误码 107002`（**连内存都申请不到**）。
→ `create_context` 只对创建它的线程生效，必须**每个线程** `acl.rt.set_context(ctx)`。
**单线程测试永远发现不了这个问题。**

### 6.5 设备内存必须用完即释放
`infer()` 早期把每次推理的 buffer 都登记进 `self._bufs` 且从不释放
→ 连跑几次就 `malloc` 失败。单次测试看不出。
→ 改成**局部 buffer 列表 + `finally` 里 `acl.rt.free`**。

### 6.6 ★ 标量输入的 dtype（导致过「音频为空」）
`variance_adaptor` 的 `p_control/e_control/d_control` 是**标量**（`shape=[]`，8 字节），
无法从大小判断是 int64 还是 float64，被误判成 **int64** →
`1.0` 被转成整数 `1` → **时长预测输出全 0** → 音频为空、RTF = inf。
→ **dtype 以"用户实际传入数组的 dtype"为准**（浮点就按浮点喂）。

### 6.7 把 `max_mel_len` 提到 500：转换成功但**推理段错误**
decoder/postnet/hifigan 的 500 帧版本 ATC **转换全部成功**，但部署后
**decoder 一推理就 SIGSEGV**（encoder/variance 正常）。
已回滚到 250。旧版备份在 `~/models/tts-npu/mel250_bak/`，
500 版留在 `~/models/tts-npu/mel500_keep/` 供以后排查。

---

## 七、文件清单

**板子上**

| 路径 | 大小 | 说明 |
|---|---|---|
| `~/models/tts-npu/*.om` | 99 MB | 5 个模型（encoder / variance_adaptor / decoder / postnet / hifigan）|
| `~/models/tts-npu/preprocess.yaml` | — | 官方前处理配置（词典路径已改绝对路径）|
| `~/models/tts-npu/lexicon/` | — | `pinyin-lexicon-r.txt` |
| `~/car_gui/npu_tts_engine.py` | 31 KB | **引擎**（与 `TtsEngine` 同接口）+ 截断检测 + scipy 替身 |
| `~/car_gui/tts_engine.py` | — | 加了 `enable_npu()` / `load_best()` / `npu_status` |
| `~/car_gui/acl_infer_session.py` | 18 KB | pyACL 版 `InferSession` |
| `~/car_gui/torch.py` | 4 KB | numpy 替身（官方代码只用了几个张量操作）|
| `~/car_gui/ais_bench/` | — | 转发到上面的 `InferSession`，让官方 import 零改动 |
| `~/car_gui/npu_tts/` | 88 KB | **官方** `synthesize.py` / `utils.py` / `text/` |
| `~/car_gui/npu_libs/` | 248 MB | tbe 依赖（numpy 1.26 / scipy / sympy…）|

**复现入口**：`/root/runfs2.sh`（已 source 昇腾环境并设好 PYTHONPATH）

---

## 八、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `pyACL 不可用：libascendcl.so ...` | `LD_LIBRARY_PATH` 没有昇腾库 | 正常启动 `run_gui.sh` 会自动 source；裸跑会自动 `execve` 重启一次 |
| 日志出现 `⚠ NPU TTS 截断` | 单句超过 250 帧（约 18 汉字）| 句子太长，需分句（`DEFAULT_SENT_LEN`）|
| `malloc 失败，错误码 107002` | 非主线程没绑 context | 已修（`bind_thread`）；若复现请查是否绕过了 `infer()` |
| 末尾少字 | 单句超 2.9 秒音频 | 同上，看截断告警日志 |
| `npu-smi` 显示 `Alarm` | 强杀过跑着 NPU 的进程（`dmesg` 有 `Res leak`）| **不影响功能**；重启可清。别用 `kill -9` |

---

## 九、下一步可做

- ⏸ **真机联调**：接上小车跑一轮，确认「先停车 → 再播报」的时序在 1.11 秒延迟下正确
- ⏸ **GUI 显示当前后端**：`tts_status()` 已暴露 `backend` 字段，面板还没接
- ⏸ **KWS 真人重录**（独立任务，见《../语音急停-KWS.md》）
- ⏸ 可选：排查 §6.7 的 500 帧段错误（试 `--op_select_implmode` / 换精度模式 / 分段转）
