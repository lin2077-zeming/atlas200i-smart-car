#!/usr/bin/env bash
# ============================================================================
#  setup_serial_udev.sh —— 给 USB-TTL 串口固定一个设备名 /dev/ttyCAR
#
#  为什么需要：
#      CH343/CH340 这类 USB 转串口在 Linux 上通常枚举为 /dev/ttyUSB0，
#      但如果同时插了别的 USB 串口设备（比如烧录器、单片机下载器），
#      设备号会漂移成 ttyUSB0/ttyUSB1/... ，导致程序里写死的路径失效。
#      固定成 /dev/ttyCAR 之后，插拔顺序变化也不会影响。
#
#  用法：
#      bash setup_serial_udev.sh            # 自动探测当前插着的串口并生成规则
#      bash setup_serial_udev.sh --remove   # 卸载规则
# ============================================================================
set -u

RULE_FILE="/etc/udev/rules.d/99-car-serial.rules"
LINK_NAME="ttyCAR"

C_OK="\033[32m"; C_WARN="\033[33m"; C_ERR="\033[31m"; C_OFF="\033[0m"
say()  { printf "\n${C_OK}==> %s${C_OFF}\n" "$1"; }
warn() { printf "${C_WARN}[警告] %s${C_OFF}\n" "$1"; }
err()  { printf "${C_ERR}[错误] %s${C_OFF}\n" "$1"; }

if [ "${1:-}" = "--remove" ]; then
    say "卸载 udev 规则"
    sudo rm -f "$RULE_FILE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "已删除 $RULE_FILE"
    exit 0
fi

say "1/4 探测当前 USB 串口设备"
mapfile -t DEVS < <(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | sort -V)
if [ "${#DEVS[@]}" -eq 0 ]; then
    err "没有发现 /dev/ttyUSB* 或 /dev/ttyACM*"
    echo "  请确认："
    echo "    1) USB-TTL 模块已插到开发板"
    echo "    2) 下位机（STM32）已上电，否则某些板子不枚举"
    echo "    3) 用 dmesg 看内核是否识别到：sudo dmesg | tail -20"
    exit 1
fi

printf "  发现设备：\n"
for d in "${DEVS[@]}"; do
    printf "    %s\n" "$d"
done

DEV="${DEVS[0]}"
if [ "${#DEVS[@]}" -gt 1 ]; then
    warn "有多个串口设备，默认取第一个：$DEV"
    echo "  如果取错了，请编辑脚本里的 DEV 变量，或先把其他串口设备拔掉再运行。"
fi

say "2/4 读取 $DEV 的 USB 厂商/产品 ID"
VID="$(udevadm info -q property -n "$DEV" 2>/dev/null | sed -n 's/^ID_VENDOR_ID=//p')"
PID="$(udevadm info -q property -n "$DEV" 2>/dev/null | sed -n 's/^ID_MODEL_ID=//p')"
DRIVER="$(udevadm info -q property -n "$DEV" 2>/dev/null | sed -n 's/^ID_USB_DRIVER=//p')"

if [ -z "$VID" ] || [ -z "$PID" ]; then
    err "无法从 $DEV 读取 USB ID，可能它不是 USB 串口（而是板载串口）"
    udevadm info -q property -n "$DEV" 2>/dev/null | grep -E "ID_VENDOR|ID_MODEL|ID_USB" | sed 's/^/    /'
    exit 1
fi

echo "  VID=$VID  PID=$PID  driver=${DRIVER:-unknown}"
case "$VID" in
    1a86) echo "  → WCH 芯片（CH340/CH341/CH343），本项目使用的就是这一类" ;;
    10c4) echo "  → Silicon Labs CP210x" ;;
    0403) echo "  → FTDI" ;;
    *)    echo "  → 其他厂商，规则按 VID/PID 精确匹配，同样有效" ;;
esac

say "3/4 写入 udev 规则 $RULE_FILE"
sudo tee "$RULE_FILE" >/dev/null <<EOF
# 智能小车 USB-TTL 串口固定别名（由 setup_serial_udev.sh 生成）
# 生成时探测到的设备：$DEV  (VID=$VID PID=$PID)
SUBSYSTEM=="tty", ATTRS{idVendor}=="$VID", ATTRS{idProduct}=="$PID", \\
    SYMLINK+="$LINK_NAME", MODE="0666", GROUP="dialout"
EOF

cat "$RULE_FILE" | sed 's/^/    /'

say "4/4 重新加载 udev 并验证"
sudo udevadm control --reload-rules
sudo udevadm trigger
sleep 1

# 拔插一次才会重建符号链接，这里先手工试一次
if [ -e "/dev/$LINK_NAME" ]; then
    echo "  /dev/$LINK_NAME -> $(readlink -f /dev/$LINK_NAME)"
else
    warn "/dev/$LINK_NAME 还没出现"
    echo "  请把 USB-TTL 拔下再插上（或执行 sudo udevadm trigger 后重插），然后运行："
    echo "      ls -l /dev/$LINK_NAME"
fi

echo
echo "  之后程序里统一使用：  /dev/$LINK_NAME"
echo "  直接测试连通性：      python3 ../car_gui_core.py --port /dev/$LINK_NAME"
