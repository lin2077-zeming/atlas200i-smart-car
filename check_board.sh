#!/usr/bin/env bash
# Atlas 200I DK A2 连接自检脚本
# 在 Windows 的 Git Bash 里运行（已内置 ssh / scp / ssh-keygen）
#
# 用法：
#   bash check_board.sh                    # 默认 192.168.137.100
#   bash check_board.sh 192.168.1.27       # 指定 IP（接路由器时）
#
# 判读：
#   1-3 步全通过         -> 链路正常，问题多半在客户端软件（如 FinalShell 算法协商）
#   4 步看到 npu-smi 输出 -> 板子状态正常
#   5 步 10MB 上传 < 1 秒 -> 千兆链路正常；> 5 秒说明走的是 Type-C 或 WiFi

set -u

IP="${1:-192.168.137.100}"
USER_NAME="root"

say() { printf '\n=== %s ===\n' "$1"; }

say "1. 本机有线网卡 IP"
ipconfig 2>/dev/null | tr -d '\r' | grep -A6 -E "以太网|Ethernet" | grep -E "适配器|adapter|IPv4" \
  || echo "（未识别到网卡信息，请手动确认 PC 以太网口已设为 192.168.137.101/24）"

say "2. ping 板子 $IP"
if ping -n 2 "$IP" >/dev/null 2>&1; then
  echo "ping 通"
else
  echo "!! ping 不通。依次检查："
  echo "   1) PC 以太网口是否设了静态 IP 192.168.137.101 / 255.255.255.0"
  echo "   2) 网线是否插在板子的 eth1（换另一个 RJ45 口再试）"
  echo "   3) 板子是否已开机完成（首次开机需等约 3 分钟）"
  echo "   4) PC 防火墙是否拦了 ICMP（可直接跳过 ping 试第 3 步）"
fi

say "3. SSH 端口 22 是否可达"
if timeout 5 bash -c "cat < /dev/null > /dev/tcp/$IP/22" 2>/dev/null; then
  echo "22 端口可达"
else
  echo "!! 22 不可达。可能原因：sshd 未启动 / 端口被改 / 被防火墙拦截"
  echo "   在板子本地终端执行：sudo systemctl enable --now ssh && ss -tlnp | grep ssh"
fi

say "4. SSH 登录并读取板子信息（需要输密码）"
if ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$USER_NAME@$IP" '
  echo "主机名 : $(hostname)"
  echo "系统   : $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
  echo "内核   : $(uname -r)"
  echo "架构   : $(uname -m)"
  echo "--- 网口 ---"; ip -br addr 2>/dev/null | grep -v "^lo"
  echo "--- 磁盘 ---"; df -h / /data 2>/dev/null
  echo "--- 内存 ---"; free -h 2>/dev/null | head -2
  echo "--- NPU  ---"; npu-smi info 2>/dev/null | head -12 || echo "npu-smi 无输出，请检查驱动或权限"
'; then
  echo
  echo "SSH 登录成功"
else
  echo "!! SSH 登录失败。按《6.说明文档/2.环境配置/SSH连接与传文件.md》§9 故障排查对照表处理："
  echo "   - Algorithm negotiation fail  -> 升级 FinalShell，或放宽板子 sshd 算法"
  echo "   - Permission denied           -> 用户名试 root 或 HwHiAiUser，密码 Mind@123"
  echo "   - HOST IDENTIFICATION CHANGED -> ssh-keygen -R $IP"
fi

say "5. 传输测速（上传 10MB 随机文件并计时）"
TMPF="$(mktemp 2>/dev/null || echo /tmp/_spd.bin)"
head -c 10485760 /dev/urandom > "$TMPF" 2>/dev/null

START=$(date +%s)
if scp -q "$TMPF" "$USER_NAME@$IP:/tmp/_spd.bin" 2>/dev/null; then
  END=$(date +%s)
  COST=$((END - START))
  echo "10MB 上传耗时：${COST}s"
  if   [ "$COST" -le 1 ]; then echo "链路正常（千兆级），适合传大模型文件"
  elif [ "$COST" -le 5 ]; then echo "链路一般，可能经过路由器或链路抖动"
  else                          echo "链路偏慢（< 2MB/s），传大文件前建议改用网线直连 eth1"
  fi
  ssh -o ConnectTimeout=8 "$USER_NAME@$IP" "rm -f /tmp/_spd.bin" 2>/dev/null
else
  echo "!! 上传失败，请先解决第 3、4 步的问题"
fi
rm -f "$TMPF" 2>/dev/null

say "自检结束"
echo "详细说明见《6.说明文档/2.环境配置/SSH连接与传文件.md》"
