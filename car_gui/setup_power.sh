#!/bin/bash
# setup_power.sh —— 允许网页控制台「重启 / 关机」，但只开放这两个动作
#
# 背景：car-web.service 以 HwHiAiUser 运行，普通用户没有关机权限。
# 这里装一个**只接受 reboot/shutdown/check** 的 root 脚本，并用 sudoers
# 只放行这一个脚本 —— 不把整个 systemctl 暴露给服务账号。
#
# 用法（板子上，root）：
#     sudo bash setup_power.sh
#
# 验证（不需要真的重启）：
#     su -s /bin/sh HwHiAiUser -c 'sudo -n /usr/local/sbin/car-power check'
#     → 输出 ok
#
# 撤销：
#     sudo rm -f /usr/local/sbin/car-power /etc/sudoers.d/car-power

set -e

if [ "$(id -u)" != "0" ]; then
  echo "❌ 需要 root：sudo bash $0" >&2
  exit 1
fi

HELPER=/usr/local/sbin/car-power
SUDOERS=/etc/sudoers.d/car-power

cat > "$HELPER" <<'EOF'
#!/bin/sh
# car-power —— 网页控制台的唯一提权入口
# 本脚本由 setup_power.sh 安装；只接受 reboot / shutdown / check。
case "$1" in
  reboot)   exec /bin/systemctl reboot ;;
  shutdown) exec /bin/systemctl poweroff ;;
  check)    echo ok ;;
  *) echo "usage: $0 reboot|shutdown|check" >&2; exit 2 ;;
esac
EOF
chmod 755 "$HELPER"
chown root:root "$HELPER"

cat > "$SUDOERS" <<'EOF'
# 网页控制台（HwHiAiUser）只能调用 car-power，且只允许这三个动作
HwHiAiUser ALL=(root) NOPASSWD: /usr/local/sbin/car-power reboot, /usr/local/sbin/car-power shutdown, /usr/local/sbin/car-power check
EOF
chmod 440 "$SUDOERS"

# 语法校验（写坏了会导致 sudo 全面失效，必须校验）
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$SUDOERS" >/dev/null || { echo "❌ sudoers 校验失败" >&2; rm -f "$SUDOERS"; exit 1; }
fi

echo "✅ 已安装："
echo "    $HELPER"
echo "    $SUDOERS"
echo
echo "验证："
echo "    su -s /bin/sh HwHiAiUser -c 'sudo -n /usr/local/sbin/car-power check'"
echo "    → 输出 ok 即成功（不会真的重启）"
echo
echo "然后重启网页服务让状态里出现 power=true："
echo "    sudo systemctl restart car-web"