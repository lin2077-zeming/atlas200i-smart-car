#!/usr/bin/env bash
# ============================================================================
#  setup_qxmic_udev.sh —— 给声源定位麦克风（HK-QXMIC）的 USB-TTL 固定设备名 /dev/ttyMIC
#
#  为什么需要：
#      HK-QXMIC-V3.0 模组本身不带 USB 转串口，坐标要从「接口3 串口 TTL」
#      用自备的 USB-TTL（CH340/CH343）接出来。这类设备在 Linux 上通常枚举成
#      /dev/ttyUSB0、ttyUSB1 …，会随插拔顺序漂移；更麻烦的是**小车下位机的
#      CH9102 也走 ch341 驱动、也枚举成 /dev/ttyUSB0**，两个设备会抢同一个名字。
#      本脚本按 WCH 的 VID/PID 把声源麦克风固定成 /dev/ttyMIC（小车仍是
#      /dev/ttyCAR，由 setup_serial_udev.sh 负责），两边互不干扰：
#          · VID 1a86 = 沁恒 WCH（CH340/CH341/CH343）
#          · 明确排除 PID 55d4（小车 CH9102），绝不碰小车串口
#
#  用法：
#      bash setup_qxmic_udev.sh --dry-run     # 只看会做什么，不改系统（建议先看）
#      sudo bash setup_qxmic_udev.sh          # 真正安装 udev 规则
#      sudo bash setup_qxmic_udev.sh --remove # 卸载规则
#      bash setup_qxmic_udev.sh --help        # 查看帮助
#
#  可选参数：
#      --device /dev/ttyUSB1   手动指定要固定的是哪个串口（默认自动探测 CH34x）
#      --pid 7523              手动指定 PID（硬件没插、想先写规则时用）
#      --link ttyMIC           自定义别名（默认 ttyMIC，一般不用改）
#
#  ⚠️ 本脚本**不自动 sudo**：写 /etc/udev 时要你自己按提示加 sudo；
#     --dry-run 永远只打印、不碰系统。
#
#  对应文档：6.说明文档/4.语音模块/声源定位麦克风.md
# ============================================================================
set -u

ORIG_ARGS="$*"

RULE_FILE="/etc/udev/rules.d/99-qxmic-serial.rules"
LINK_NAME="ttyMIC"
VID="1a86"
PID="7523"
PID_OVERRIDDEN=0
DEV_ARG=""
DRY_RUN=0
REMOVE=0

C_OK="\033[32m"; C_WARN="\033[33m"; C_ERR="\033[31m"; C_OFF="\033[0m"
say()  { printf "\n${C_OK}==> %s${C_OFF}\n" "$1"; }
warn() { printf "${C_WARN}[警告] %s${C_OFF}\n" "$1"; }
err()  { printf "${C_ERR}[错误] %s${C_OFF}\n" "$1"; }

usage() {
    cat <<EOF
用法：
    bash setup_qxmic_udev.sh --dry-run     # 只看会做什么（推荐先看）
    sudo bash setup_qxmic_udev.sh          # 真正安装规则（固定成 /dev/$LINK_NAME）
    sudo bash setup_qxmic_udev.sh --remove # 卸载规则

可选：
    --device /dev/ttyUSB1   手动指定要固定的串口（默认自动探测 CH34x）
    --pid 7523              手动指定 PID（硬件没插时也能先生成规则）
    --link ttyMIC           自定义别名（默认 $LINK_NAME）
EOF
}

# ── 参数解析 ────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --remove)  REMOVE=1 ;;
        --device)  DEV_ARG="${2:-}"; shift ;;
        --device=*) DEV_ARG="${1#*=}" ;;
        --pid)     PID="${2:-}"; PID_OVERRIDDEN=1; shift ;;
        --pid=*)   PID="${1#*=}"; PID_OVERRIDDEN=1 ;;
        --link)    LINK_NAME="${2:-}"; shift ;;
        --link=*)  LINK_NAME="${1#*=}" ;;
        -h|--help) usage; exit 0 ;;
        *) err "未知参数：$1"; usage; exit 2 ;;
    esac
    shift
done

if [ "$LINK_NAME" = "ttyCAR" ]; then
    err "--link 不能是 ttyCAR（那是小车下位机的固定名，会被抢）"
    exit 2
fi

if ! command -v udevadm >/dev/null 2>&1; then
    if [ "$DRY_RUN" = 1 ]; then
        warn "本机没有 udevadm（不是 Linux？）—— --dry-run 仍可查看规则内容"
    else
        err "找不到 udevadm：本脚本只能在开发板 / Linux 上运行"
        exit 1
    fi
fi

# ── 卸载 ────────────────────────────────────────────────────────────────
if [ "$REMOVE" = 1 ]; then
    say "卸载 udev 规则：$RULE_FILE"
    if [ "$DRY_RUN" = 1 ]; then
        echo "  [--dry-run] 真正执行会运行："
        echo "    sudo rm -f $RULE_FILE"
        echo "    sudo udevadm control --reload-rules"
        echo "    sudo udevadm trigger"
        exit 0
    fi
    if [ "$(id -u)" != 0 ]; then
        err "需要 root 才能删除 /etc/udev 规则"
        echo "  请执行：sudo bash $0 --remove"
        exit 1
    fi
    rm -f "$RULE_FILE"
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
    echo "  已删除 $RULE_FILE"
    echo "  若 /dev/$LINK_NAME 还在，拔插一次 USB-TTL 即可消失。"
    exit 0
fi

# ── 1/5 探测设备 ────────────────────────────────────────────────────────
say "1/5 探测 CH34x USB-TTL 设备（声源麦克风）"

CHOSEN_DEV=""
DETECTED_PID=""
if [ -n "$DEV_ARG" ]; then
    if [ ! -e "$DEV_ARG" ]; then
        err "指定的设备不存在：$DEV_ARG"
        echo "  先看看有哪些串口：ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null"
        exit 1
    fi
    CHOSEN_DEV="$DEV_ARG"
else
    for dev in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$dev" ] || continue
        v="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_VENDOR_ID=//p')"
        p="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_MODEL_ID=//p')"
        [ "$v" = "1a86" ] || continue
        if [ "$p" = "55d4" ]; then
            warn "跳过 $dev：1a86:55d4 是小车下位机 CH9102（保留给 /dev/ttyCAR）"
            continue
        fi
        if [ -z "$CHOSEN_DEV" ]; then
            CHOSEN_DEV="$dev"
            DETECTED_PID="$p"
        else
            warn "还有其它 CH34x 设备：$dev（用 --device 手动指定要固定哪个）"
        fi
    done
fi

if [ -n "$CHOSEN_DEV" ]; then
    echo "  选中设备：$CHOSEN_DEV"
    if [ -z "$DEV_ARG" ]; then
        VID="$(udevadm info -q property -n "$CHOSEN_DEV" 2>/dev/null | sed -n 's/^ID_VENDOR_ID=//p')"
        if [ "$PID_OVERRIDDEN" = 0 ] && [ -n "$DETECTED_PID" ]; then
            PID="$DETECTED_PID"
        fi
        DRIVER="$(udevadm info -q property -n "$CHOSEN_DEV" 2>/dev/null | sed -n 's/^ID_USB_DRIVER=//p')"
        echo "  探测到：VID=$VID  PID=$PID  driver=${DRIVER:-unknown}"
    fi
else
    warn "没有检测到 CH34x 设备（可能麦克风还没插 / 只插了供电没接 TTL）"
    if [ "$PID_OVERRIDDEN" = 0 ]; then
        echo "  → 将按默认值生成规则：VID=$VID PID=$PID（CH340 最常见）"
        echo "    等设备插好后再跑一遍本脚本，可自动探测实际 PID；"
        echo "    硬件不在手边时也可以用 --pid 55d3（CH343）等手动指定。"
    else
        echo "  → 将按你指定的 VID=$VID PID=$PID 生成规则。"
    fi
fi

# ── 2/5 核对 VID/PID ────────────────────────────────────────────────────
say "2/5 核对 VID/PID（该规则只匹配 VID=$VID PID=$PID，不会误伤其它设备）"
case "$VID" in
    1a86) echo "  VID 1a86 = 沁恒 WCH（CH340/CH341/CH343），符合预期" ;;
    55d4) err "VID 写成 55d4 不对：55d4 是 PID，不是 VID"; exit 1 ;;
    *)    warn "VID=$VID 不是常见的沁恒 1a86，仍会按你给的值生成规则" ;;
esac
case "$PID" in
    7523) echo "  PID 7523 = CH340（最常见）" ;;
    5523) echo "  PID 5523 = CH341" ;;
    55d3) echo "  PID 55d3 = CH343" ;;
    55d4) err "PID 55d4 是小车下位机 CH9102：本脚本不能用它固定成 /dev/$LINK_NAME"; exit 1 ;;
    *)    warn "PID=$PID 不是典型 CH34x PID，仍会按 VID/PID 精确匹配" ;;
esac

# 和小车别名做冲突检查（只提示，不阻断）
if [ -e "/dev/ttyCAR" ]; then
    CAR_TARGET="$(readlink -f /dev/ttyCAR 2>/dev/null || true)"
    echo "  当前 /dev/ttyCAR -> ${CAR_TARGET:-未知}"
    if [ -n "$CHOSEN_DEV" ] && [ "$CAR_TARGET" = "$(readlink -f "$CHOSEN_DEV" 2>/dev/null || true)" ]; then
        warn "选中的 $CHOSEN_DEV 就是 /dev/ttyCAR 指向的设备！"
        echo "  这很可能是小车下位机，不是声源麦克风 —— 别用本脚本固定它。"
        echo "  确认麦克风在哪个串口后再用 --device 指定。"
    fi
fi

# ── 3/5 生成规则内容 ────────────────────────────────────────────────────
say "3/5 生成 udev 规则内容"
RULE_CONTENT="$(cat <<EOF
# 声源定位麦克风 HK-QXMIC 的 USB-TTL 固定别名 /dev/$LINK_NAME
# 由 car_gui/setup_qxmic_udev.sh 生成；设备：${CHOSEN_DEV:-（未检测到，按指定 VID/PID 生成）}
# ⚠️ 不要和小车下位机 CH9102（1a86:55d4，/dev/ttyCAR）混用
SUBSYSTEM=="tty", ATTRS{idVendor}=="$VID", ATTRS{idProduct}=="$PID", \\
    SYMLINK+="$LINK_NAME", MODE="0666", GROUP="dialout"
EOF
)"
printf '%s\n' "$RULE_CONTENT" | sed 's/^/    /'

# ── 4/5 dry-run 或写规则 ───────────────────────────────────────────────
if [ "$DRY_RUN" = 1 ]; then
    say "4/5 [--dry-run] 不写系统，到此为止"
    echo "  真正安装会执行："
    echo "    sudo tee $RULE_FILE <<'EOF'"
    printf '%s\n' "$RULE_CONTENT" | sed 's/^/      /'
    echo "    EOF"
    echo "    sudo udevadm control --reload-rules"
    echo "    sudo udevadm trigger"
    echo
    echo "  确认无误后执行（去掉 --dry-run）："
    echo "    sudo bash $0"
    exit 0
fi

if [ "$(id -u)" != 0 ]; then
    err "写 /etc/udev/rules.d 需要 root"
    echo "  请重新执行（会自动带上你刚才的参数）："
    echo "    sudo bash $0${ORIG_ARGS:+ $ORIG_ARGS}"
    echo "  只想先看看会改什么：bash $0 --dry-run"
    exit 1
fi

say "4/5 写入 udev 规则 $RULE_FILE"
printf '%s\n' "$RULE_CONTENT" > "$RULE_FILE"
chmod 644 "$RULE_FILE"
echo "  已写入（$(wc -l < "$RULE_FILE") 行）"

# ── 5/5 重新加载并验证 ─────────────────────────────────────────────────
say "5/5 重新加载 udev 并验证"
udevadm control --reload-rules
udevadm trigger
sleep 1

if [ -e "/dev/$LINK_NAME" ]; then
    echo "  /dev/$LINK_NAME -> $(readlink -f "/dev/$LINK_NAME")"
else
    warn "/dev/$LINK_NAME 还没出现"
    echo "  把 USB-TTL 拔下再插上（或执行 sudo udevadm trigger 后重插），然后运行："
    echo "      ls -l /dev/$LINK_NAME"
fi

echo
echo "  之后程序里统一用：  /dev/$LINK_NAME"
echo "  自检：   ls -l /dev/$LINK_NAME"
echo "  列串口： python3 qxmic_locator.py --list-ports"
echo "  真机跑： python3 qxmic_locator.py --port /dev/$LINK_NAME"
echo "  无硬件： python3 qxmic_locator.py --simulate --duration 3"
echo "  卸载：   sudo bash $0 --remove"