#!/usr/bin/env bash
# ============================================================================
#  run_py.sh —— 用「正确的那个 python3」跑任意小车脚本
#
#  为什么需要它
#  -----------
#  本板有多套 Python，语音依赖只装在其中一套里。直接敲 `python3 xxx.py`
#  很容易撞上「缺少 sounddevice」这类假故障。本包装脚本自动挑对解释器。
#
#  用法
#  ----
#      bash run_py.sh kws_record.py --diag
#      bash run_py.sh kws_record.py
#      bash run_py.sh kws_batch_test.py --sweep
#      bash run_py.sh env_doctor.py --write
#      bash run_py.sh --which                 # 只看解析到哪个解释器
#      bash run_py.sh --list                  # 列出所有解释器及依赖
#
#  等价的等价写法（不想记脚本时）：
#      source _pyenv.sh && "$CAR_PY" kws_record.py
# ============================================================================
set -u

cd "$(dirname "$0")"

# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"

case "${1:-}" in
    ""|-h|--help)
        sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    --which)
        echo "CAR_PY = $CAR_PY"
        [ -x "$CAR_PY" ] && "$CAR_PY" -c 'import sys;print("版本   = %d.%d.%d"%sys.version_info[:3])'
        echo "来源   = $CAR_PY_SOURCE"
        exit 0
        ;;
    --list)
        exec "$CAR_PY" env_doctor.py
        ;;
esac

SCRIPT="$1"; shift
if [ ! -f "$SCRIPT" ]; then
    echo "[错误] 找不到脚本：$SCRIPT"
    echo "       当前目录：$CAR_GUI_DIR"
    exit 1
fi

# ⚠️ PYTHONUNBUFFERED=1：Python 的 stdout 在**重定向到文件时是块缓冲的**
#    （只有接终端才是行缓冲）。本项目经常 `nohup ... > log &` 跑长任务，
#    不加这个的话日志会长时间一片空白，让人误以为"卡死了" ——
#    2026-09-27 跑音色扫描时就因此白等了两分钟。
export PYTHONUNBUFFERED=1

exec "$CAR_PY" "$SCRIPT" "$@"
