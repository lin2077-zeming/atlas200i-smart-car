#!/usr/bin/env bash
# ============================================================================
#  install_deps.sh —— 在 Atlas 200I DK A2（Ubuntu 22.04 aarch64）上一键装好
#                     小车 GUI 所需的全部依赖
#
#  用法：
#      bash install_deps.sh            # 装全部（含 PySide6 与语音识别）
#      bash install_deps.sh --no-qt    # 不装 PySide6（省 200MB+ 磁盘），用 Tkinter 界面
#      bash install_deps.sh --no-asr   # 不装语音识别（sherpa-onnx 等，省 300MB+ 磁盘）
#      bash install_deps.sh --minimal  # 只装手动控制所需（--no-qt + --no-asr）
#
#  说明：
#      脚本可重复执行，已装的包会被 apt/pip 自动跳过。
#      本板多套 Python 并存（系统 3.10 / conda base 3.9 / conda 环境），
#      脚本会先解析出「该用哪一套」（见 _pyenv.sh），装完固定到 .python_for_car。
# ============================================================================
set -u

SKIP_QT=0
SKIP_ASR=0
for a in "$@"; do
    case "$a" in
        --no-qt) SKIP_QT=1 ;;
        --no-asr) SKIP_ASR=1 ;;
        --minimal) SKIP_QT=1; SKIP_ASR=1 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    esac
done

C_OK="\033[32m"; C_WARN="\033[33m"; C_ERR="\033[31m"; C_OFF="\033[0m"
say()  { printf "\n${C_OK}==> %s${C_OFF}\n" "$1"; }
warn() { printf "${C_WARN}[警告] %s${C_OFF}\n" "$1"; }
err()  { printf "${C_ERR}[错误] %s${C_OFF}\n" "$1"; }

# ── 0. 环境体检 ─────────────────────────────────────────────────────────
say "0/6 环境体检"

# 解析「用哪个 python3」。本板有多套 Python（系统 3.10 / conda base 3.9 /
# 其它 conda 环境），依赖只装在其中一套里。后面统一用 $CAR_PY，不要再写 python3。
# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"
echo "  解释器 : $CAR_PY"

echo "  系统   : $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
echo "  内核   : $(uname -r)"
echo "  架构   : $(uname -m)"
PYV=$("$CAR_PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo "缺失")
echo "  Python : $PYV"
echo "  磁盘   :"; df -h / | tail -1 | sed 's/^/    /'

if [ "$(uname -m)" != "aarch64" ]; then
    warn "当前不是 aarch64。本脚本针对 Atlas 200I DK A2（aarch64）编写，"
    warn "在 x86 上跑只用于语法/逻辑自测，PySide6 也能装，但不是目标平台。"
fi

if ! command -v python3 >/dev/null 2>&1; then
    err "没有 python3，请先 sudo apt install -y python3"
    exit 1
fi

# ── 1. 系统依赖 ─────────────────────────────────────────────────────────
say "1/6 安装系统依赖（apt）"
sudo apt update

# Tkinter 版 + 串口 + 基础的音频/字体工具
BASE_PKGS="python3-pip python3-tk python3-serial alsa-utils fonts-noto-cjk curl bzip2"

# 语音识别需要：PortAudio（sounddevice 的底层）+ 语音相关基础库
ASR_PKGS="libportaudio2 libasound2-plugins"

# Qt6(xcb 平台插件) 运行库 —— 这是 PySide6 在 Linux 上最常见的缺库来源
QT_PKGS="libgl1 libegl1 libgles2 libglib2.0-0 \
libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 \
libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xfixes0 \
libxcb-image0 libxcb-sync1 libxcb-shm0 libxcb-util1 libxcb1 \
libfontconfig1 libfreetype6 libdbus-1-3 libx11-xcb1 \
libxi6 libsm6 libice6 libxext6 libxrender1 libx11-6"

if [ "$SKIP_QT" = "1" ]; then
    echo "  （--no-qt：跳过 Qt 运行库）"
    sudo apt install -y $BASE_PKGS
else
    # 拆成两次装：即使个别包名在某个小版本上不存在，也不会整体失败
    sudo apt install -y $BASE_PKGS || err "基础依赖安装有失败项，请查看上面的 apt 输出"
    for p in $QT_PKGS; do
        sudo apt install -y "$p" 2>/dev/null || warn "跳过无法安装的包：$p"
    done
fi

# ── 2. 串口库 ───────────────────────────────────────────────────────────
say "2/6 校验串口库（pyserial）"
if "$CAR_PY" -c "import serial; print('    pyserial', serial.__version__)" 2>/dev/null; then
    :
else
    warn "apt 版 pyserial 不可用，改用 pip 安装"
    "$CAR_PY" -m pip install pyserial -i https://pypi.tuna.tsinghua.edu.cn/simple
fi
"$CAR_PY" -c "import serial, serial.tools.list_ports; print('    pyserial OK')" \
    || { err "pyserial 安装失败，请检查网络"; exit 1; }

# ── 3. Tkinter ──────────────────────────────────────────────────────────
say "3/6 校验 Tkinter"
if "$CAR_PY" -c "import tkinter" 2>/dev/null; then
    echo "    tkinter OK"
else
    err "tkinter 不可用，请执行：sudo apt install -y python3-tk"
    exit 1
fi

# ── 4. PySide6 ──────────────────────────────────────────────────────────
if [ "$SKIP_QT" = "1" ]; then
    say "4/6 跳过 PySide6（--no-qt）"
else
    say "4/6 安装 PySide6"
    if "$CAR_PY" -c "import PySide6; print('    PySide6 已安装')" 2>/dev/null; then
        :
    else
        "$CAR_PY" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple || true
        # Essentials 版只含 QtCore/QtGui/QtWidgets 等核心模块，体积比完整版小很多，
        # 本 GUI 只需要核心模块，优先装这个。
        echo "  尝试安装 PySide6-Essentials（体积较小）…"
        "$CAR_PY" -m pip install PySide6-Essentials -i https://pypi.tuna.tsinghua.edu.cn/simple \
            || "$CAR_PY" -m pip install PySide6 -i https://pypi.tuna.tsinghua.edu.cn/simple \
            || warn "PySide6 安装失败。不影响使用：改用 Tkinter 版 car_gui_tk.py"
    fi

    if "$CAR_PY" -c "import PySide6; from PySide6 import QtWidgets" 2>/dev/null; then
        echo "    PySide6 OK"
        # 真正能跑起来才算数（会加载 xcb 平台插件）
        "$CAR_PY" - <<'PY' 2>/dev/null && echo "    Qt 平台插件 xcb 加载正常" \
            || warn "Qt 平台插件加载失败，通常是缺 libxcb-cursor0 等库；\
若无显示器（纯 SSH）也会失败，属于正常"
import os, sys
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    sys.exit(1)
from PySide6.QtWidgets import QApplication
app = QApplication([])
PY
    fi
fi

# ── 5. 语音识别依赖 ─────────────────────────────────────────────────────
if [ "$SKIP_ASR" = "1" ]; then
    say "5/6 跳过语音识别依赖（--no-asr）"
else
    say "5/6 安装语音识别依赖（sherpa-onnx / sounddevice）"

    # PortAudio 是 sounddevice 的底层依赖
    for p in $ASR_PKGS; do
        sudo apt install -y "$p" 2>/dev/null || warn "跳过无法安装的包：$p"
    done

    # sherpa-onnx 官方在 PyPI 上提供 manylinux aarch64 的 wheel，
    # 板子的 Python 3.9 可以直接 pip 装，**不需要源码编译**。
    PIPS="numpy sounddevice sherpa-onnx"
    "$CAR_PY" -m pip install --upgrade $PIPS -i https://pypi.tuna.tsinghua.edu.cn/simple \
        || "$CAR_PY" -m pip install --upgrade $PIPS \
        || warn "语音识别依赖安装失败（不影响手动控车），可稍后重跑本脚本"

    "$CAR_PY" - <<'PY'
import importlib
for mod, label in (("numpy", "numpy"), ("sounddevice", "sounddevice"),
                   ("sherpa_onnx", "sherpa-onnx")):
    try:
        m = importlib.import_module(mod)
        print("    %-13s OK   %s" % (label, getattr(m, "__version__", "")))
    except Exception as e:
        print("    %-13s 缺失 (%s)" % (label, e))
PY

    echo "    录音设备："
    "$CAR_PY" - <<'PY' 2>/dev/null || echo "      （查询失败，插好 USB 声卡后重跑本脚本）"
try:
    import sounddevice as sd
    found = False
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            print("      [%d] %s  %d Hz"
                  % (i, str(d["name"])[:44], int(d["default_samplerate"])))
            found = True
    if not found:
        print("      （没有输入设备：USB 声卡 / 麦克风没插好？）")
except Exception as e:
    print("      （查询失败：%s）" % e)
PY

    MD="$HOME/models/sense-voice"
    if [ -f "$MD/model.int8.onnx" ] || [ -f "$MD/model.onnx" ]; then
        echo "    识别模型：$MD 已就绪"
    else
        warn "识别模型还没下载，装完后执行： bash download_asr_model.sh"
    fi
fi

# ── 6. 串口权限 ─────────────────────────────────────────────────────────
say "6/6 配置串口权限"
if id -nG "$USER" | grep -qw dialout; then
    echo "    当前用户 $USER 已在 dialout 组"
else
    warn "把 $USER 加入 dialout 组（需要 sudo 密码）"
    sudo usermod -aG dialout "$USER" || true
    warn "【重要】必须重新登录（或重启）后才生效！"
fi

# ── 汇总 ────────────────────────────────────────────────────────────────
say "自检汇总"
"$CAR_PY" - <<'PY'
import importlib, os
def chk(mod, label):
    try:
        importlib.import_module(mod)
        print("  [OK]   %s" % label)
        return True
    except Exception as e:
        print("  [缺失] %s  (%s)" % (label, e))
        return False

chk("serial", "pyserial")
chk("tkinter", "tkinter")
has_qt = chk("PySide6", "PySide6")
chk("numpy", "numpy")
chk("sounddevice", "sounddevice")
has_asr = chk("sherpa_onnx", "sherpa-onnx")
print("\n  串口设备：")
found = False
for p in ("/dev/ttyCAR", "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0"):
    if os.path.exists(p):
        print("    %s  (存在)" % p)
        found = True
if not found:
    print("    （没发现串口设备，请插好 USB-TTL 并确认下位机已上电）")

print("\n  识别模型：")
md = os.path.expanduser("~/models/sense-voice")
hit = None
for c in ("model.int8.onnx", "model.onnx"):
    if os.path.isfile(os.path.join(md, c)):
        hit = c
        break
print("    %s" % (os.path.join(md, hit) if hit else "未下载 → bash download_asr_model.sh"))

print("\n  推荐启动命令：")
print("    %s" % ("bash run_gui.sh          # PySide6 界面（含语音面板）"
                     if has_qt else
                     "python3 car_gui_tk.py    # Tkinter 界面"))
if has_asr:
    print("    python3 asr_cli.py --no-exec    # 先只测识别，不下发运动指令")
    print("    python3 asr_cli.py --port /dev/ttyCAR")
if has_asr:
    print("\n  建议下一步（装完先体检一遍）：")
    print("    python3 asr_preflight.py                # ASR 全链路体检")
    print("    python3 asr_preflight.py --no-model     # 快速版（不加载模型）")
PY

say "完成。若串口打不开，请先注销重新登录（dialout 组生效）"

# ── 7. 固定解释器 ───────────────────────────────────────────────────────
# 本板多套 Python 并存是「缺少 sounddevice」类假故障的根源。
# 这里把本次使用的解释器写进 .python_for_car，之后 run_gui.sh / run_py.sh
# 都会自动用它，不必再记路径，也不受 conda 激活状态影响。
say "固定 Python 解释器"
if [ -x "$CAR_PY" ]; then
    printf '%s\n' "$CAR_PY" > "$(dirname "$0")/.python_for_car"
    echo "  已写入 .python_for_car → $CAR_PY"
    echo "  之后请用下面两种方式跑脚本（自动用同一解释器）："
    echo "     bash run_py.sh <脚本> [参数]"
    echo "     bash run_gui.sh"
else
    warn "解析出的解释器不可执行（$CAR_PY），未固定"
fi
