#!/usr/bin/env bash
# ============================================================================
#  run_web.sh —— 启动「无界面远程 Web 控制台」
#
#  在**板子上**跑（不需要显示器/键鼠）；然后在**笔记本浏览器**里打开：
#      http://<板子IP>:8090/
#
#  用法：
#      bash run_web.sh                        # 默认 8090，自动连串口
#      bash run_web.sh --serial /dev/ttyCAR   # 指定串口
#      bash run_web.sh --no-voice             # 不占用麦克风
#      bash run_web.sh --asr                  # 启动就预加载语音识别（否则网页按需加载）
#      bash run_web.sh --demo                 # 离线演示（不发串口，只看界面）
#      bash run_web.sh --port 8091            # 换端口（默认 8090）
#
#  通常不用手敲 —— 电脑上双击 `工具脚本/启动小车控制台.cmd` 会自动 SSH 过来执行本脚本。
#
#  为什么用 `run_py.sh` 那套解释器解析：
#      本板有多套 Python，依赖只装在其中一套里（见 _pyenv.sh）。
# ============================================================================
set -u

cd "$(dirname "$0")"

# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"

# ── 昇腾（NPU）TTS 环境 ─────────────────────────────────────────────
# ASCEND_TTS_ENV: 让 TTS 能用 NPU 后端（FastSpeech2 的 5 个 om）。
# 动态链接器只在**进程启动时**读 LD_LIBRARY_PATH，所以必须在这里 source，
# 光在 Python 里设 os.environ 是没用的（见 npu_tts_engine.ensure_runtime 的说明）。
# 找不到 set_env.sh 也没关系 —— npu_tts_engine 会自动回退 CPU(sherpa)，
# 但那样 RTF 会从 0.92 掉到 ~5，所以**能 source 就一定要 source**。
# （2026-10-27 实测：漏了这段，网页启动的 TTS 回退到 CPU。）
ASCEND_SET_ENV="/usr/local/Ascend/ascend-toolkit/set_env.sh"
if [ -f "$ASCEND_SET_ENV" ]; then
    # ⚠️ 本脚本开头有 `set -u`（未定义变量即报错），而华为的 set_env.sh
    #    里面**多处**直接引用裸变量（$LD_LIBRARY_PATH / $PYTHONPATH / $PATH …），
    #    逐个补默认值既啰嗦又容易漏 → 干脆在 source 期间**临时关掉 set -u**，
    #    之后再恢复。否则会 "unbound variable" 直接退出。
    case "$-" in
        *u*) _had_u=1; set +u ;;
        *)   _had_u=0 ;;
    esac
    . "$ASCEND_SET_ENV"
    [ "$_had_u" = "1" ] && set -u
    unset _had_u
fi
# tbe 依赖（numpy<2 等）跟着工程走，HwHiAiUser 可读
if [ -d "$(dirname "$0")/npu_libs" ]; then
    PYTHONPATH="$(dirname "$0")/npu_libs:${PYTHONPATH:-}"
    export PYTHONPATH
fi
export CAR_TTS_BACKEND="${CAR_TTS_BACKEND:-auto}"
# ────────────────────────────────────────────────────────────────────

echo "使用解释器：$CAR_PY"

# ── 依赖提示（不阻断启动：网页控制不依赖语音）────────────────────────────
if ! "$CAR_PY" -c "import serial" 2>/dev/null; then
    echo "[警告] 缺少 pyserial → 无法连小车串口。安装： bash install_deps.sh"
fi
if ! "$CAR_PY" -c "import sherpa_onnx" 2>/dev/null; then
    echo "[提示] 未检测到 sherpa-onnx → 语音急停/语音输入不可用（网页控制不受影响）。"
fi

exec "$CAR_PY" car_web.py "$@"