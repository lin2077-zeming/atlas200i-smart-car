#!/usr/bin/env bash
# ============================================================================
#  _pyenv.sh —— 统一的「用哪个 python3」解析器（被其它脚本 source）
#
#  背景
#  ----
#  本板同时装着多套 Python：
#      /usr/bin/python3                          系统（3.10）
#      ~/miniconda3/bin/python3                  conda base（3.9）
#      ~/miniconda3/envs/*/bin/python3           其它 conda 环境
#  而 numpy / sounddevice / sherpa-onnx 只装在其中一套里。
#  一旦 conda 激活了错的环境，就会看到「缺少 sounddevice」这类假故障。
#
#  解析优先级
#  ----------
#    1. 环境变量 CAR_PYTHON（临时指定，最高优先级）
#    2. .python_for_car 文件（env_doctor.py --write 生成，持久固定）
#    3. 自动探测：找第一个装了 sounddevice 的解释器
#    4. 兜底：PATH 里的 python3
#
#  用法
#  ----
#      source "$(dirname "$0")/_pyenv.sh"
#      "$CAR_PY" kws_record.py --diag
#
#  也可以通过直接执行来查看解析结果：
#      bash _pyenv.sh
# ============================================================================

# 不 set -e / set -u，避免影响调用方
# 把函数改成「设置全局 CAR_PY / CAR_PY_SOURCE」而不是 echo，
# 避免 $(...) 子 shell 把变量赋值吃掉（这是 bash 的经典陷阱：
# 命令替换里的赋值只作用于子 shell，外面看不到）。
_car_pyenv_resolve() {
    local here pin cand p

    # 1) 环境变量
    if [ -n "${CAR_PYTHON:-}" ] && [ -x "${CAR_PYTHON:-}" ]; then
        CAR_PY="$CAR_PYTHON"
        CAR_PY_SOURCE="CAR_PYTHON 环境变量"
        return 0
    fi

    # 2) 固定文件
    if [ -n "${CAR_GUI_DIR:-}" ]; then
        here="$CAR_GUI_DIR"
    else
        here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    fi
    pin="$here/.python_for_car"
    if [ -f "$pin" ]; then
        cand="$(head -1 "$pin" 2>/dev/null | tr -d '\r\n ')"
        if [ -n "$cand" ] && [ -x "$cand" ]; then
            CAR_PY="$cand"
            CAR_PY_SOURCE=".python_for_car（已固定）"
            return 0
        fi
    fi

    # 3) 自动探测：谁能 import sounddevice 就用谁
    #    ⚠️ 本板实测 conda 装在 /usr/local/miniconda3（不是 ~/miniconda3），
    #       且 .bashrc 有 conda init → 交互登录自动激活 base（3.9，无依赖）。
    #       所以 /usr/local 与 /opt 两个位置都要覆盖。
    local probe='/usr/bin/python3
/usr/local/bin/python3'
    local g
    for g in /usr/local/miniconda3/bin/python3 /usr/local/anaconda3/bin/python3 \
             /usr/local/conda/bin/python3 /opt/miniconda3/bin/python3 \
             /opt/anaconda3/bin/python3 /opt/conda/bin/python3 \
             "$HOME"/miniconda3/bin/python3 "$HOME"/anaconda3/bin/python3; do
        [ -x "$g" ] && probe="$probe
$g"
    done
    for g in /usr/local/miniconda3/envs/*/bin/python3 \
             /usr/local/anaconda3/envs/*/bin/python3 \
             /opt/miniconda3/envs/*/bin/python3 \
             /opt/anaconda3/envs/*/bin/python3 \
             /opt/conda/envs/*/bin/python3 \
             "$HOME"/miniconda3/envs/*/bin/python3 \
             "$HOME"/anaconda3/envs/*/bin/python3; do
        [ -x "$g" ] && probe="$probe
$g"
    done
    probe="$probe
$(command -v python3 2>/dev/null)"

    while IFS= read -r p; do
        [ -n "$p" ] || continue
        [ -x "$p" ] || continue
        if "$p" -c "import sounddevice" 2>/dev/null; then
            CAR_PY="$p"
            CAR_PY_SOURCE="自动探测（装有 sounddevice）"
            return 0
        fi
    done <<EOF
$probe
EOF

    # 4) 兜底
    CAR_PY="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"
    CAR_PY_SOURCE="兜底（PATH 里的 python3）"
    return 0
}

if [ -n "${CAR_GUI_DIR:-}" ]; then
    :
else
    CAR_GUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    export CAR_GUI_DIR
fi

CAR_PY=""
CAR_PY_SOURCE=""
_car_pyenv_resolve
export CAR_PY
export CAR_PY_SOURCE="${CAR_PY_SOURCE:-未知}"

# 支持直接执行查看结果
if [ "${BASH_SOURCE[0]:-}" = "${0:-}" ]; then
    echo "CAR_GUI_DIR = $CAR_GUI_DIR"
    echo "CAR_PY      = $CAR_PY"
    if [ -x "$CAR_PY" ]; then
        echo -n "  python 版本 = "
        "$CAR_PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo "（不可执行）"
        for m in numpy sounddevice sherpa_onnx serial PySide6; do
            if "$CAR_PY" -c "import $m" 2>/dev/null; then
                echo "  [OK]   $m"
            else
                echo "  [缺失] $m"
            fi
        done
    else
        echo "  ⚠️ 解析出来的路径不可执行"
    fi
    if [ -f "$CAR_GUI_DIR/.python_for_car" ]; then
        echo "  固定文件 = $(cat "$CAR_GUI_DIR/.python_for_car" 2>/dev/null)"
    else
        echo "  固定文件 = （无，可用 env_doctor.py --write 生成）"
    fi
fi
