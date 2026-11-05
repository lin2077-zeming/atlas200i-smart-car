# 上一代方案：RK3576 + Sherpa Zipformer KWS 急停识别

> 目标：在 RK3576 CPU 上运行 Sherpa\-ONNX 中文 Zipformer KWS，持续监听 USB 麦克风，识别急停关键词时只打印 JSON 事件。本教程不包含串口、小车或 GPIO 控制。
> 
> 

## 1\. 最终效果与安全边界

成功命中后应输出：

```JSON
{"event":"EMERGENCY_STOP","source":"local_kws_cpu","keyword":"急停","timestamp_unix_ms":1784370000000,"mode":"print_only_no_vehicle_control"}
```

本教程中的程序不会导入小车代码，不会打开 /dev/ttyACM0。只有完成语料测试和安全审查后，才允许把该事件接入停车接口。

## 2\. 已验证环境

|项目|已验证结果|
|---|---|
|处理器架构|aarch64|
|系统|银河麒麟桌面操作系统（工业版）V10 SP1 2503|
|内核|Linux 6\.1\.75|
|当前 Conda 环境|base，Python 3\.13\.12|
|Python 包|sherpa\_onnx 1\.12\.34、sounddevice 0\.5\.5、onnxruntime 1\.24\.4|
|静态运行时|sherpa\-onnx 1\.12\.32，Linux aarch64 static|
|USB 麦克风|PortAudio 设备 1，44100 Hz，单声道|

登录开发板。请使用 SSH 密钥或自己的安全认证方式，切勿把密码写入脚本或版本库。

```Bash
ssh kylin@<开发板IP>
```

检查环境：

```Bash
uname -a
python --version
python -m pip list | grep -Ei 'sherpa|sounddevice|onnx'
arecord -l
```

关键预期输出：

```Plain Text
Linux ... aarch64 ... GNU/Linux
Python 3.13.12
sherpa_onnx  1.12.34
sounddevice  0.5.5
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
```

## 3\. 下载 KWS 模型

模型名称为 sherpa\-onnx\-kws\-zipformer\-wenetspeech\-3\.3M\-2024\-01\-01。它是中文开放词表流式 KWS 模型，关键词使用带声调拼音配置，无需为急停词先训练模型。



```Bash
mkdir -p /home/kylin/modelplayer/kws
cd /home/kylin/modelplayer/kws
curl -fL --retry 3   -o sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2   https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
tar -xjf sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
```

确认模型：

```Bash
MODEL_DIR=/home/kylin/modelplayer/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
find "$MODEL_DIR" -maxdepth 1 -type f -printf '%f\n' | sort
```

关键预期输出：

```Plain Text
decoder-epoch-12-avg-2-chunk-16-left-64.onnx
encoder-epoch-12-avg-2-chunk-16-left-64.onnx
joiner-epoch-12-avg-2-chunk-16-left-64.onnx
keywords.txt
keywords_raw.txt
tokens.txt
```

## 4\. 配置急停关键词

```Bash
cd "$MODEL_DIR"
nano keywords_emergency.txt
```

将下列内容写入文件并保存：

```Plain Text
j í t íng @急停
j ǐn j í t íng zh ǐ @紧急停止
x iǎo ch ē j í t íng @小车急停
```

检查：

```Bash
cat keywords_emergency.txt
```

格式为“拼音 token \+ @ \+ 命中后返回的中文标签”。不要把单个“停”作为一级急停词，它会显著增加误触发风险。

## 5\. 验证静态运行时和模型

```Bash
RUNTIME=/home/kylin/modelplayer/sherpa-onnx-v1.12.32-linux-aarch64-static/bin
"$RUNTIME/sherpa-onnx-version"
```

预期：

```Plain Text
sherpa-onnx version : 1.12.32
```

先用模型自带音频验证 KWS。当前运行时与模型的 FP32 ONNX 文件兼容，故以下命令明确使用不带 int8 后缀的模型文件：

```Bash
cd "$MODEL_DIR"
"$RUNTIME/sherpa-onnx-keyword-spotter"   --tokens=tokens.txt   --encoder=encoder-epoch-12-avg-2-chunk-16-left-64.onnx   --decoder=decoder-epoch-12-avg-2-chunk-16-left-64.onnx   --joiner=joiner-epoch-12-avg-2-chunk-16-left-64.onnx   --provider=cpu   --num-threads=2   --keywords-file=test_wavs/test_keywords.txt   test_wavs/3.wav
```

预期至少出现两条关键词检测 JSON：

```JSON
{"start_time":0.00,"keyword":"文森特卡索","timestamps":[...],"tokens":["w","én",...]}
{"start_time":0.00,"keyword":"法国","timestamps":[...],"tokens":["f","ǎ","g","uó"]}
```

可测量离线性能：

```Bash
/usr/bin/time -f 'elapsed=%e cpu=%P'   "$RUNTIME/sherpa-onnx-keyword-spotter"   --tokens=tokens.txt   --encoder=encoder-epoch-12-avg-2-chunk-16-left-64.onnx   --decoder=decoder-epoch-12-avg-2-chunk-16-left-64.onnx   --joiner=joiner-epoch-12-avg-2-chunk-16-left-64.onnx   --provider=cpu   --num-threads=2   --keywords-file=test_wavs/test_keywords.txt   test_wavs/3.wav
```

本板参考输出：

```Plain Text
Audio duration: 8.030 s
Elapsed seconds: 1.017
RTF = 1.017/8.030 = 0.127
```

RTF 小于 1 表示解码快于实时音频；这只是性能参考，不能替代现场急停测试。

## 6\. 确认 USB 麦克风设备号

```Bash
"$RUNTIME/sherpa-onnx-pa-devs" 2>/dev/null | grep -E 'device #|Name|Default sample rate' | head -36
```

关键预期输出：

```Plain Text
device #1
Name = USB PnP Sound Device: Audio (hw:1,0)
Default sample rate = 44100.00
```

也可用 Python 检查：

```Bash
python - <<'PY'
import sounddevice as sd
for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels']:
        print(i, d['name'], int(d['default_samplerate']))
PY
```

预期包含：

```Plain Text
1 USB PnP Sound Device: Audio (hw:1,0) 44100
```

设备号可能因插拔或重启变化；每次部署前都按名称复核。

## 7\. 创建连续监听程序

```Bash
cd /home/kylin/asr_llm
nano emergency_kws_monitor.py
```

粘贴完整代码：

```Python
#!/usr/bin/env python3
# CPU-only emergency KWS monitor. No serial port and no vehicle control.

import json
import queue
import signal
import sys
import time
import numpy as np
import sherpa_onnx
import sounddevice as sd

MODEL_DIR = "/home/kylin/modelplayer/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KEYWORDS = f"{MODEL_DIR}/keywords_emergency.txt"
INPUT_DEVICE = 1
INPUT_SAMPLE_RATE = 44100
BLOCK_SECONDS = 0.20
NUM_THREADS = 2
KEYWORDS_THRESHOLD = 0.25
COOLDOWN_SECONDS = 1.0

def create_spotter():
    return sherpa_onnx.KeywordSpotter(
        tokens=f"{MODEL_DIR}/tokens.txt",
        encoder=f"{MODEL_DIR}/encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        decoder=f"{MODEL_DIR}/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        joiner=f"{MODEL_DIR}/joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        keywords_file=KEYWORDS,
        num_threads=NUM_THREADS,
        sample_rate=16000,
        feature_dim=80,
        keywords_threshold=KEYWORDS_THRESHOLD,
        provider="cpu",
    )

def main():
    audio_queue = queue.Queue(maxsize=50)
    running = True
    last_trigger_time = 0.0

    def stop_handler(_signum, _frame):
        nonlocal running
        running = False

    def audio_callback(indata, _frames, _time_info, status):
        if status:
            print(json.dumps(
                {"event": "AUDIO_STATUS", "detail": str(status)},
                ensure_ascii=False,
            ), flush=True)
        try:
            audio_queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            try:
                audio_queue.get_nowait()
                audio_queue.put_nowait(indata[:, 0].copy())
            except queue.Empty:
                pass

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    spotter = create_spotter()
    stream = spotter.create_stream()
    blocksize = int(INPUT_SAMPLE_RATE * BLOCK_SECONDS)

    print(json.dumps({
        "event": "KWS_READY",
        "device": INPUT_DEVICE,
        "input_sample_rate": INPUT_SAMPLE_RATE,
        "keywords": ["急停", "紧急停止", "小车急停"],
        "threshold": KEYWORDS_THRESHOLD,
        "mode": "print_only_no_vehicle_control",
    }, ensure_ascii=False), flush=True)

    with sd.InputStream(
        device=INPUT_DEVICE,
        samplerate=INPUT_SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        latency="high",
        callback=audio_callback,
    ):
        while running:
            try:
                samples = audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            stream.accept_waveform(
                INPUT_SAMPLE_RATE,
                np.asarray(samples, dtype=np.float32),
            )

            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                keyword = spotter.get_result(stream)
                if not keyword:
                    continue

                now = time.monotonic()
                if now - last_trigger_time >= COOLDOWN_SECONDS:
                    print(json.dumps({
                        "event": "EMERGENCY_STOP",
                        "source": "local_kws_cpu",
                        "keyword": keyword,
                        "timestamp_unix_ms": int(time.time() * 1000),
                        "mode": "print_only_no_vehicle_control",
                    }, ensure_ascii=False), flush=True)
                    last_trigger_time = now

                spotter.reset_stream(stream)

    print(json.dumps({"event": "KWS_STOPPED"}, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps(
            {"event": "KWS_ERROR", "detail": str(exc)},
            ensure_ascii=False,
        ), file=sys.stderr, flush=True)
        raise
```

关键配置说明：

|配置|值|原因|
|---|---|---|
|INPUT\_DEVICE|1|当前 USB 麦克风|
|INPUT\_SAMPLE\_RATE|44100|已实测的设备采样率|
|模型采样率|16000|KWS 特征配置；Sherpa 内部重采样|
|BLOCK\_SECONDS|0\.20|降低输入溢出风险|
|latency|high|优先连续稳定采集|
|NUM\_THREADS|2|已完成离线基准|
|KEYWORDS\_THRESHOLD|0\.25|初始值，必须现场调参|
|COOLDOWN\_SECONDS|1\.0|避免同一句话反复触发|

## 8\. 启动和人工测试

```Bash
cd /home/kylin/asr_llm
python emergency_kws_monitor.py
```

启动成功：

```JSON
{"event":"KWS_READY","device":1,"input_sample_rate":44100,"keywords":["急停","紧急停止","小车急停"],"threshold":0.25,"mode":"print_only_no_vehicle_control"}
```

首次输入音频时可能出现正常提示：

```Plain Text
Creating a resampler:
  in_sample_rate: 44100
  output_sample_rate: 16000
```

面对 USB 麦克风依次说“急停”“紧急停止”“小车急停”。成功命中时预期：

```JSON
{"event":"EMERGENCY_STOP","source":"local_kws_cpu","keyword":"急停","timestamp_unix_ms":...,"mode":"print_only_no_vehicle_control"}
```

按 Ctrl\+C 退出：

```JSON
{"event":"KWS_STOPPED"}
```

## 9\. 已知问题与排障

### 9\.1 INT8 模型与旧静态运行时不兼容

若使用带 int8 后缀的 encoder、decoder、joiner，当前静态运行时可能报：

```Plain Text
'attention_dims' does not exist in the metadata
```

并以退出码 255 结束。处理方式是当前 CPU 阶段继续使用 FP32 ONNX。后续 RKNN 阶段应单独匹配 RKNN Runtime、RKNN Toolkit 与 Sherpa RKNN 版本，不要直接将该 CPU 兼容结论外推到 NPU。

### 9\.2 出现 AUDIO\_STATUS input overflow

早期在 100 ms 音频块与默认低延迟下出现过：

```JSON
{"event":"AUDIO_STATUS","detail":"input overflow"}
```

已将脚本调整为：

```Python
BLOCK_SECONDS = 0.20
latency = "high"
```

12 秒连续启动测试未再出现溢出。若现场仍出现：

1. 先停止 SenseVoice、Ollama 等占用 CPU 的程序。

2. 将 NUM\_THREADS 从 2 改为 1，重新测试。

3. 确认采样率为 44100 Hz，不要复用旧的 48000 Hz 到 16000 Hz 的 3:1 简化降采样。

4. 记录溢出次数、CPU 占用与识别延迟，再决定是否切换到 C\+\+ 服务或 RKNN。

## 10\. 阈值和质量测试

阈值 0\.25 只是起点。请建立本地测试集：

|数据类别|最小建议|
|---|---|
|每个应触发词|50 条|
|不应触发普通语音|100 条以上|
|每类噪声场景|20 条以上|
|不同说话人|至少 5 人|

必须覆盖电机声、轮胎声、音乐、他人讲话、回声、远近距离和不同音量。每条记录真实标签、是否触发、触发词、触发延迟、CPU 占用和溢出状态。

调参原则：

- 一级急停优先防漏检；漏检比误停车更危险。

- 不要使用单字“停”作为一级急停词。

- 先调关键词集合和阈值，再考虑微调训练。

- 语音急停不能替代物理急停按钮和底盘看门狗。

