# wake_prompts/ —— 语音唤醒的**预录提示音**资产

这个目录只放「语音唤醒」用的短提示音，以及生成它们的脚本。

## 为什么用预录音频

唤醒链路是：**说唤醒词 → 播提示音 → 立刻开录音 → ASR → 走现有指令链路**。

提示音如果用 TTS 现合成，本板要等 **0.5~2 秒**（NPU 后端 RTF≈0.9，CPU 后端 RTF≈5.4）
才出声，用户会以为"它没理我"，而且这段等待还会拉长"唤醒 → 开录音"的总时间。
预录成 wav 后直接 `aplay`，几十毫秒就出声。

## 文件清单

| 文件 | 内容 | 谁生成 |
|---|---|---|
| `wake_zh.wav` | NPU TTS 合成的人声「在听」 | `gen_wake_prompts.py`（TTS 成功时）**← car_gui_core 默认首选** |
| `wake_zh2.wav` | NPU TTS 合成的「等待指令」 | 同上（备选，改 `CAR_WAKE_PROMPT` 指到它即可） |
| `wake_beep.wav` | 标准库 `wave` 生成的短嘟声（0.26s，880Hz→1175Hz） | 同上（**永远生成，兜底**） |
| `gen_wake_prompts.py` | 生成脚本 | — |
| `README.md` | 本文件 | — |

格式要求：**16bit / 单声道 / PCM WAV**（`aplay` 与本项目 TTS 都按此处理）。
时长控制在 **2~4 字、≤1.6s**；再长会明显拖慢"唤醒 → 开录音"。

## 再生成步骤（板上）

```bash
# 板上代码在 /home/HwHiAiUser/car_gui
ssh root@192.168.137.100
cd /home/HwHiAiUser/car_gui
# ⚠️ 要用 NPU TTS 合成，必须先 source 昇腾环境（否则 pyACL 导入失败 → 退 CPU 后端）；
#    而且必须用 HwHiAiUser 跑（root 的 ~ 是 /root，会去找 /root/models/tts-npu 而扑空）
source /usr/local/Ascend/ascend-toolkit/set_env.sh
bash run_py.sh wake_prompts/gen_wake_prompts.py            # 嘟声 + NPU TTS 人声
bash run_py.sh wake_prompts/gen_wake_prompts.py --beep-only  # 只要兜底嘟声（秒级）
```

> ⚠️ 板上跑 Python **一律用 `bash run_py.sh`**，不要裸 `python3` ——
> `.bashrc` 里的 conda 会换成一套没有依赖的解释器。

TTS 失败也没关系：脚本**先**生成 `wake_beep.wav`，所以"一定有声音"；
`car_gui_core._wake_prompt_path()` 的挑选顺序是
`CAR_WAKE_PROMPT` → `wake_zh.wav` → `wake_beep.wav` → 目录里任意 `.wav`，
两者都没有时还会退回**极短 TTS**（`WAKE_PROMPT_TEXT = "在听"`），
再失败就跳过提示音直接录音（不报错中断、不阻塞安全链路）。

## 换提示音 / 换唤醒词

```bash
# 换一条提示音（文件名或绝对路径都行）
export CAR_WAKE_PROMPT=wake_zh2.wav
# 换唤醒词（逗号分隔）。⚠️ 不要用一级急停词（急停/停车/刹车…），会被拒收
export CAR_WAKE_WORDS="小车车"
# 关掉唤醒功能
export CAR_WAKE_ENABLE=0
```

新唤醒词如果要被 KWS 真正听到，必须能给出生母/韵母拼音
（见 `car_gui_core.WAKE_WORD_PINYIN`，或直接写整行
`CAR_WAKE_WORDS="x iǎo ch ē ch ē @小车车"`）——
`car_gui_core` 会把唤醒词并进 `kws_keywords_emergency.txt` 生成一份**临时合并词表**，
因为本板麦克风只有 1 个 capture subdevice，**只能有一个 KWS 监听器**。

合并词表的落盘位置（按 uid 区分，**别用固定名**）：

```
/tmp/car_wake_keywords_<uid>.txt      # 首选（普通用户就是 _1000.txt）
/tmp/car_wake_keywords.txt            # 同名兜底
<car_gui>/car_wake_keywords_<uid>.txt # /tmp 写不进去时的兜底
```

> ⚠️ **踩过的坑（2026-10-14 板上实测）**：固定名 `/tmp/car_wake_keywords.txt`
> 只要**被 root 建过一次**，普通用户就再也替换不掉它（`/tmp` 有 sticky 位，
> 只有文件属主能删/改名）→ `os.replace` 报 `PermissionError(1,
> 'Operation not permitted')` → **唤醒词被静默丢掉**（词表只剩 5 个急停词），
> 表现是"喊了完全没反应"，而且日志里只有一行警告，极难排查。
> 现在按 uid 起名 + 多路径重试 + 记住上次成功的路径；若仍全部失败，
> 会额外投递一条 `wake_error` 界面事件，**绝不会影响语音急停**（急停用原词表照常启动）。

排查"喊了没反应"的第一步，就是看 KWS 启动日志里的关键词个数：

```
[KWS] KWS 加载完成...KWS · 6 个关键词（急停、紧急停止、小车急停、停车、刹车、小车车）
                                                   ↑ 少了"小车车"就是没并进去
```

## 与本地工作区同步（md5 必须一致）

本地 `car_gui/` ↔ 板上 `/home/HwHiAiUser/car_gui/`：

```powershell
scp car_gui/wake_prompts/*.py car_gui/wake_prompts/*.wav car_gui/wake_prompts/README.md `
    root@192.168.137.100:/home/HwHiAiUser/car_gui/wake_prompts/
ssh root@192.168.137.100 "chown -R HwHiAiUser:HwHiAiUser /home/HwHiAiUser/car_gui/wake_prompts"
# 逐文件核对
Get-FileHash car_gui/wake_prompts/* -Algorithm MD5
ssh root@192.168.137.100 "md5sum /home/HwHiAiUser/car_gui/wake_prompts/*"
```

## 已实测的坑（别再踩）

1. **合成音送 KWS 不可用**：本项目实测把 TTS 合成音喂给 KWS 一个都命不中
   （WenetSpeech 1 万小时真人数据训练，声学分布差异太大）。
   → 提示音只是**给人听**的；唤醒词能否命中**必须用真人录音验证**。
2. `aplay` **不带 `-D` 在本板必失败**（ALSA `default` 指向只有采集端点的 card0）；
   `car_gui_core._play_wav_file()` 因此会先探测设备再 `-D plughw:N,M`。
3. 提示音必须在**麦克风让位之后**播（采集/播放双向互斥），且**播完才能开录音**。
