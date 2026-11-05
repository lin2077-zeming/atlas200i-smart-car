# 给 Agent 的新电脑部署说明：把「小车远程 Web 控制台」装到另一台电脑

> **你（Agent）的任务**：在一台全新电脑上，从 clone 项目开始，到最后成功用浏览器连上开发板控制台，**全流程做完并自检**。
> 这份文档是自包含的：不用读别的文档也能执行。遇到需要人给凭据/接线的步骤，明确停下来问，**不要猜、不要假装成功**。
>
> 版本：2026-11-05 · 维护者：lin2077-zeming

---

## 0. 先看：这套东西是什么（30 秒，避免白干）

```
老师的电脑（你正在配的）                开发板 Atlas 200I DK A2              小车底盘
┌──────────────────────┐   HTTP 8090   ┌──────────────────────────┐  串口   ┌─────────┐
│ 浏览器（遥控界面）    │ ────────────► │ car_web.py（真正在跑）    │ ──────► │ STM32   │
│ + 一个启动脚本        │   HTTP 8099   │  串口 / 语音 / TTS / 急停  │         │ M500    │
└──────────────────────┘ ◄──────────── └──────────────────────────┘         └─────────┘
```

**关键事实（别搞反）**：

1. **Web 控制台运行在开发板上，不在电脑上。** 电脑只负责「浏览器 + 用 SSH 把板子上的服务拉起来」。
   → **绝对不要在电脑上运行 `car_web.py` / `run_web.sh`**（电脑没有串口/语音硬件，而且会和板上服务抢串口）。
2. 板子主机名 `davinci-mini`，SSH 用户 `root`，控制台端口 **8090**，网络音箱端口 **8099**。
3. 板上服务名 `car-web.service`（已配开机自启）；服务目录 `/home/HwHiAiUser/car_gui`。
4. 电脑和板子**必须在同一个局域网**（同一个路由器/同一个手机热点）。跨网段、校园网隔离都不行。
5. 仓库是公开的，clone 不需要账号：
   `https://github.com/lin2077-zeming/atlas200i-smart-car.git`
6. ⚠️ 控制台**没有登录口令**，只适用于实验室/家庭局域网；**不要**把 8090 映射到公网。

**成功标准（§6 会逐条验）**：能打开 `http://<板子IP>:8090/`，`/api/status` 返回正常 JSON，浏览器已打开，并输出一份部署报告。

> 💡 **只是演示、且板子已开机自启时**：最短路径是 §3 找 IP → §4.1 测 8090 → §5.4 开浏览器 → §6 验收。
> **SSH 只在 8090 不通（服务没起来）时才需要**，别一上来就折腾 SSH。

---

## 1. 环境预检（逐条执行并记录）

### 1.1 判断操作系统

- Windows 10 1809+ / Windows 11 → 走 **Windows 路径（推荐，主路径）**
- Linux / macOS → 跳到 **附录 B**，主流程的验收标准相同

### 1.2 Windows 必备项

在 PowerShell 里执行：

```powershell
$PSVersionTable.PSVersion            # 需要 5.1+
git --version                        # 没有 git 也没关系，见 §2 的 ZIP 备用方案
ssh -V                               # 没有则：设置 → 应用 → 可选功能 → 添加 OpenSSH 客户端
curl.exe --version                   # Win10 1803+ 自带；没有就用 Invoke-RestMethod
```

浏览器：Edge 或 Chrome（Windows 自带 Edge 即可）。

> ⚠️ 全程使用 **PowerShell 5.1 语法**；不要用需要管理员权限的安装动作，除非人类同意。

### 1.3 网络预检（必须先过）

把电脑连到和板子**同一个网络**（同一 WiFi / 同一路由器 LAN / 同一手机热点），然后：

```powershell
# 看本机 IPv4（记下网段，后面扫板子要用）
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } |
  Select-Object IPAddress, PrefixLength, InterfaceAlias
```

如果电脑和板子不在同一网段 → 停下来，先解决网络（让人类把板子接到这个网络，或把电脑接到板子所在网络）。

---

## 2. 获取项目（clone 或 ZIP，二选一）

**目标目录必须是不含中文/空格的 ASCII 路径**，统一用 `C:\car-web`（避免后续脚本踩编码坑）。

### 2.1 首选：git clone

> ⚠️ 加 `-c core.autocrlf=false`：仓库里的 `.py/.sh/.md` 按 **LF** 存；Windows 版 git
> 默认可能把 checkout 转成 CRLF（功能不受影响，但和仓库字节不一致，`.sh` 拷到板子上
> 用 bash 跑会报错）。加上这个参数 = 原样落盘。

```powershell
$dst = "C:\car-web"
if (Test-Path $dst) {
  # 目录已存在：远端是"快照式"提交（无共同历史），旧克隆可能 pull 不动
  # → 直接改名备份，重新 clone，最稳
  Rename-Item $dst "$dst.bak-$(Get-Date -Format yyyyMMddHHmm)"
}
git -c core.autocrlf=false clone https://github.com/lin2077-zeming/atlas200i-smart-car.git $dst
git -C $dst log -1 --oneline
```

### 2.2 没有 git：下载 ZIP（GitHub 自动打包）

```powershell
$dst = "C:\car-web"
$zip = "$env:TEMP\car-web.zip"
Invoke-WebRequest "https://github.com/lin2077-zeming/atlas200i-smart-car/archive/refs/heads/main.zip" -OutFile $zip
if (Test-Path $dst) { Rename-Item $dst "$dst.bak-$(Get-Date -Format yyyyMMddHHmm)" }
New-Item -ItemType Directory -Force $dst | Out-Null
# Win10 自带 bsdtar，能正确解压中文文件名；不要用 Expand-Archive
tar -xf $zip -C $dst --strip-components=1
```

### 2.3 克隆后自检（缺一个就停下，说明仓库/网络不对）

```powershell
$dst = "C:\car-web"
$must = @(
  "car_gui\car_web.py",
  "car_gui\web_ui.html",
  "car_gui\run_web.sh",
  "car_gui\test_web_console.py",
  "6.说明文档\2.环境配置\工具脚本\car_console.ps1",
  "6.说明文档\2.环境配置\工具脚本\启动小车控制台.cmd",
  "6.说明文档\2.环境配置\工具脚本\find_car.ps1"
)
$miss = $must | Where-Object { -not (Test-Path (Join-Path $dst $_)) }
if ($miss) { Write-Host "缺文件：`n$($miss -join "`n")" -ForegroundColor Red } else { Write-Host "项目文件齐全 OK" -ForegroundColor Green }
```

> 如果仓库里没有这些文件（说明远端仓库是旧快照），**停止**并报告「仓库不完整，需要维护者更新 GitHub 仓库」，不要尝试自己补代码。

---

## 3. 找到开发板 IP

板子 IP 由 DHCP 分配，会变。按下面顺序逐级尝试，**找到就不再往下**。

### 3.1 mDNS 主机名（最快）

```powershell
try {
  Resolve-DnsName davinci-mini.local -ErrorAction Stop |
    Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress
} catch { Write-Host "mDNS 无结果" }
# 备选：Windows 的 ping 也走 mDNS
ping -n 1 davinci-mini.local
```

### 3.2 历史/常见 IP 直连（按 8090 端口判断，比 ping 可靠，板子禁 ping 也适用）

```powershell
$known = @("192.168.137.100","192.168.31.128","10.36.121.127")
$board = $null
foreach ($ip in $known) {
  $ok8090 = (Test-NetConnection $ip -Port 8090 -WarningAction SilentlyContinue).TcpTestSucceeded
  $ok22   = (Test-NetConnection $ip -Port 22   -WarningAction SilentlyContinue).TcpTestSucceeded
  Write-Host ("{0,-16} 8090={1} 22={2}" -f $ip,$ok8090,$ok22)
  if ($ok8090 -or $ok22) { $board = $ip; break }
}
$board
```

### 3.3 扫网段（前两步都失败时）

```powershell
$tools = "C:\car-web\6.说明文档\2.环境配置\工具脚本"
# -NoProbe：只按 MAC/端口找，不依赖 SSH 凭据
powershell -NoProfile -ExecutionPolicy Bypass -File "$tools\find_car.ps1" -Sweep -NoProbe
```

板子两块网卡的 MAC 前缀（用于肉眼比对）：

| 接口 | MAC 前缀 | 说明 |
|---|---|---|
| `wlan0`（AX210 WiFi，主用） | `ac:45:ef` | 完整值 `ac:45:ef:a6:36:c3` |
| `eth0/eth1`（板载，网线） | `a0:70:b7` | 网线直连时 `192.168.137.100` |

### 3.4 兜底（需要人类配合）

- 板子接显示器键盘：在板子上执行 `hostname -I` 或 `bash find_ip.sh`
- 路由器管理页看 DHCP 客户端列表，找 `davinci-mini`
- 或者插网线直连电脑（电脑侧 ICS 静态 `192.168.137.1`，板子固定 `192.168.137.100`）

**找到后**，把 IP 记到变量里（后续命令都用它）：

```powershell
$board = "192.168.31.128"     # ← 换成你实际找到的 IP
```

> 把 IP 写进 `C:\car-web\6.说明文档\2.环境配置\工具脚本\.car_board_ip`，下次启动脚本会优先用它（可选）。

---

## 4. 连接测试（先看服务在不在，再决定要不要 SSH）

```powershell
# 4.1 网络层
Test-NetConnection $board -Port 8090 -WarningAction SilentlyContinue | Select-Object TcpTestSucceeded
Test-NetConnection $board -Port 22   -WarningAction SilentlyContinue | Select-Object TcpTestSucceeded
```

### 4.1 如果 8090 已经通了 → **很可能板子已开机自启，直接跳到 §5.4**

```powershell
curl.exe -s --noproxy "*" --max-time 5 "http://$board`:8090/api/status"
```

### 4.2 如果 8090 不通、22 通 → 需要 SSH 进去把服务拉起来

先测免密（**这一步决定成败**）：

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@$board "echo SSH_OK"
```

- 输出 `SSH_OK` → 凭据 OK，跳到 §5.1。
- 输出 `Permission denied` / 卡住 → 走 §4.3。

### 4.3 配 SSH 凭据（需要人类配合一次，二选一）

**方案 A（推荐）：把维护者电脑的密钥拷过来**

1. 在维护者电脑（Windows）上，把 SSH 私钥文件拷到 U 盘/网盘：
   - 私钥：`C:\Users\<维护者用户名>\.ssh\id_ed25519`（**默认文件名，以实际为准**）
   - 公钥：同目录 `id_ed25519.pub`
2. 放到新电脑 `%USERPROFILE%\.ssh\` 下（`C:\Users\<当前用户名>\.ssh\`），然后收紧权限（Windows 必须做，否则 ssh 拒绝使用）：

```powershell
$k = "$env:USERPROFILE\.ssh\id_ed25519"
icacls $k /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$board "echo SSH_OK"
```

  公钥指纹（用于核对，**不是秘密**）：
  `SHA256:iNv3ZZtHrvpVdXQT02Wa4Dk/hJ1WfWIRivGs07W6gyw`

**方案 B：现场生成新密钥 + 人类输一次板子密码**

```powershell
# 生成密钥：这一步交给人类执行（提示 passphrase 时直接回车两次，留空）
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\id_ed25519"

# 把公钥装到板子：会提示输入板子 root 密码，必须由人类在终端里输入一次
type "$env:USERPROFILE\.ssh\id_ed25519.pub" | ssh root@$board "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

# 复核
ssh -o BatchMode=yes root@$board "echo SSH_OK"
```

> 若板子**禁用了密码登录**（`PasswordAuthentication no`），方案 B 走不通 → 只能用方案 A（拷维护者密钥）。

> 🔒 不要向人类索要并记录密码；不要写进任何文件/日志。人类不在场时，方案 B 做不了 → 若 8090 也不通，就停下来报告「缺板子 SSH 凭据」。

---

## 5. 启动控制台并打开浏览器

### 5.1 Windows 一键启动（推荐）

```powershell
$tools = "C:\car-web\6.说明文档\2.环境配置\工具脚本"
powershell -NoProfile -ExecutionPolicy Bypass -File "$tools\car_console.ps1" -IP $board
```

它做四件事：找板子 → SSH 在板上启动服务（已在跑就跳过）→ 等 8090 应答 → 用默认浏览器打开 `http://<ip>:8090/`。首次启动要加载 TTS/LLM，**可能 10~30 秒**，脚本会等。

> 也可以直接双击 `启动小车控制台.cmd`；但给 Agent 用上面的 PowerShell 命令更可控。

### 5.2 Windows 手动等价（一键脚本失败时）

```powershell
# 在板上启动（幂等：systemd 在跑就跳过；否则后台拉起）
$remote = @"
cd /home/HwHiAiUser/car_gui || exit 1
if systemctl is-active --quiet car-web 2>/dev/null; then echo MANAGED; exit 0; fi
if pgrep -f '[c]ar_web.py' >/dev/null; then echo ALREADY; exit 0; fi
setsid nohup sudo -u HwHiAiUser env HOME=/home/HwHiAiUser bash run_web.sh >/tmp/car_web.log 2>&1 </dev/null &
sleep 2
pgrep -f '[c]ar_web.py' >/dev/null && echo STARTED || echo FAILED
"@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
ssh -o StrictHostKeyChecking=no root@$board "echo $b64 | base64 -d | bash"

# 等就绪（最多 60 秒）
1..60 | ForEach-Object {
  try { $s = curl.exe -s --noproxy "*" --max-time 3 "http://$board`:8090/api/status" | ConvertFrom-Json; if ($s.ok) { "READY"; break } } catch {}
  Start-Sleep -Seconds 1
}
```

失败时看板上日志：

```powershell
ssh root@$board "tail -40 /tmp/car_web.log"
```

### 5.3 Linux / macOS 手动等价

```bash
# 已在跑就跳过；否则后台拉起
ssh root@$BOARD "cd /home/HwHiAiUser/car_gui && (systemctl is-active --quiet car-web || setsid nohup sudo -u HwHiAiUser env HOME=/home/HwHiAiUser bash run_web.sh >/tmp/car_web.log 2>&1 </dev/null &)"
# 等就绪
for i in $(seq 1 60); do curl -s --noproxy '*' --max-time 3 "http://$BOARD:8090/api/status" && break; sleep 1; done
# 打开浏览器
xdg-open "http://$BOARD:8090/" 2>/dev/null || open "http://$BOARD:8090/"
```

### 5.4 打开浏览器

```powershell
Start-Process "http://$board`:8090/"
```

> 如果电脑上跑的是无头/远程会话（没有桌面），不要强行开浏览器；把 URL 交给人类即可，验收以 §6 的 HTTP 检查为准。

---

## 6. 验收（必须逐条做，只读，不碰车）

> 🚫 **严禁自动下发任何运动指令**（`/api/action`、`/api/text` 里带"前进/转向"等），
> 严禁点急停/重启/关机。车可能正架着或在地上，误动会出事。
> 遥控动作请交给人类在浏览器里操作。

```powershell
$base = "http://$board`:8090"

# 6.1 服务活着 + 关键状态
$s = curl.exe -s --noproxy "*" --max-time 5 "$base/api/status" | ConvertFrom-Json
$s.ok
$s | Select-Object connected, voltage, tts_state, asr_state, speaker, power, demo_mode, console_online

# 6.2 页面是控制台（不是 404/旧页面）
$html = curl.exe -s --noproxy "*" --max-time 5 "$base/"
if ($html -match 'id="estop"' -and $html -match '小车远程控制台') { "PAGE_OK" } else { "PAGE_BAD" }

# 6.3 网络音箱端口（浏览器听声音用）
Test-NetConnection $board -Port 8099 -WarningAction SilentlyContinue | Select-Object TcpTestSucceeded
```

**判定标准**：

| 检查 | 通过条件 |
|---|---|
| `/api/status` | 返回 JSON 且 `ok: true` |
| 页面 | 含 `id="estop"`（急停按钮）和「小车远程控制台」字样 |
| 8099 | TCP 可达（`speaker: true` 更直接） |
| 浏览器 | 已打开 URL（无桌面环境则跳过，给人类 URL） |
| 小车串口 | `connected: true` 才算"能遥控车"；`false` 时提示人类：把小车 USB 串口线插到开发板，3 秒内自动连上 |

**功能说明（写进报告即可，不用操作）**：方向键按住走/松开停、整宽急停、速度档位、文本指令、远程语音（板子麦克风，首次点「加载语音识别」要 20~30 秒）、浏览器当喇叭（需点一下开关，浏览器要求用户手势）、高级设置里有语音急停/唤醒/离线演示/远程电源。

---

## 7. 故障排查表

| 症状 | 先检查 | 处理 |
|---|---|---|
| mDNS 解析不到 | `Resolve-DnsName davinci-mini.local` | 用 §3.2 已知 IP / §3.3 扫网段 / 路由器 DHCP 列表 |
| 找不到板子 | `Test-NetConnection <ip> -Port 22` | 板子没开机 / 没连同一网络 / 换手机热点重试；板子屏幕 `hostname -I` |
| 22 通、8090 不通 | `curl $base/api/status` | 服务没起 → §5.1/§5.2；或看 `/tmp/car_web.log` |
| 22、8090 都不通 | 本机 IP 与板子是否同网段 | 网络隔离：路由器开了 AP 隔离 / 校园网 / 不同 VLAN → 换同一路由器/热点 |
| SSH `Permission denied` | `ssh -o BatchMode=yes root@$board true` | §4.3 配密钥；人类不在场且 8090 也不通 → 停止报告 |
| 页面打开但状态不动 | 浏览器 F12 Network | 确认请求打到板子 IP；刷新页面；看 `console_online` |
| 没声音 | 是否点了「浏览器当喇叭」 | 必须点一下开关（自动播放策略）；确认 8099 通 |
| 语音没反应 | 板上麦克风是否被占用 | 高级里关「自动聆听」/「语音急停」再试；详细见板上日志 |
| PowerShell 报执行策略 | `Get-ExecutionPolicy` | 用 `-ExecutionPolicy Bypass -File`（只影响本次） |
| 中文路径/编码报错 | 项目是否在 `C:\car-web` | 移到 ASCII 路径重新跑 |
| 控制台显示「未连接」 | 小车 USB 串口线 | 让人类把线插到板子 USB 口，3 秒后状态自动变绿 |

---

## 8. 最后必须输出一份部署报告（直接贴给人类）

```markdown
## 部署报告
- 电脑系统 / PowerShell 版本：
- 项目目录 / 仓库提交：
- 板子 IP（发现方式）：
- 控制台 URL：
- /api/status 摘要：ok=..., connected=..., voltage=..., tts_state=..., asr_state=..., speaker=..., power=...
- 页面检查：PAGE_OK / PAGE_BAD
- 8099 可达：是/否
- 浏览器已打开：是/否（无桌面环境时给出 URL）
- SSH 凭据方式：已有密钥 / 现场生成 / 无（仅浏览器直连）
- 遗留问题与需要人类做的事：
```

---

## 9. 禁止事项（重要）

1. 🚫 **不要在电脑上运行 `car_web.py` / `run_web.sh`**（它们属于开发板）。
2. 🚫 **不要自动发运动指令、不要点急停/重启/关机**；车的动作交给人类。
3. 🚫 **不要修改** `car_gui/` 代码、板子上的 systemd 服务、网络配置（除非人类明确要求）。
4. 🚫 **不要把 SSH 私钥、板子密码写进**任何文件、日志、聊天记录、git 仓库。
5. 🚫 **不要 `git commit` / `git push`**；只读使用仓库。
6. 🚫 **不要安装需要管理员权限的软件**（Git/浏览器已由用户提供；ZIP 方案不需要装任何东西）。
7. ✅ 卡住超过 10 分钟或需要人类提供凭据/接线时：**停下来，写清楚缺什么**，不要伪造"部署成功"。

---

## 附录 A：Windows 最短路径（人类只做两件事）

人类只需要：
1. 让电脑和板子连同一个网络，板子开机；
2. 若 Agent 要 SSH，把维护者的 `id_ed25519` 拷到新电脑 `%USERPROFILE%\.ssh\`（或当场给板子密码）。

Agent 执行：

```powershell
# ① 拿项目
git clone https://github.com/lin2077-zeming/atlas200i-smart-car.git C:\car-web   # 没 git 用 §2.2 ZIP
# ② 找板子（mDNS → 已知 IP → 扫网段）
# ③ 一键连
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\car-web\6.说明文档\2.环境配置\工具脚本\car_console.ps1" -IP <板子IP>
# ④ 验收（§6）：/api/status、页面 estop、8099、浏览器
```

## 附录 B：Linux / macOS 路径

```bash
# 依赖：git（或 unzip/curl）、ssh、curl、浏览器
git clone https://github.com/lin2077-zeming/atlas200i-smart-car.git ~/car-web
cd ~/car-web
# 找板子
getent hosts davinci-mini.local || ping -c1 davinci-mini.local
for ip in 192.168.137.100 192.168.31.128 10.36.121.127; do
  curl -s --noproxy '*' --max-time 2 "http://$ip:8090/api/status" >/dev/null && echo "FOUND $ip"
done
# 扫网段（可选）：arp-scan --localnet | grep -i 'ac:45:ef\|a0:70:b7'
# 启动 + 打开
BOARD=<板子IP>
ssh root@$BOARD "cd /home/HwHiAiUser/car_gui && (systemctl is-active --quiet car-web || setsid nohup sudo -u HwHiAiUser env HOME=/home/HwHiAiUser bash run_web.sh >/tmp/car_web.log 2>&1 </dev/null &)"
for i in $(seq 1 60); do curl -s --noproxy '*' "http://$BOARD:8090/api/status" && break; sleep 1; done
xdg-open "http://$BOARD:8090/" 2>/dev/null || open "http://$BOARD:8090/"
```

## 附录 C：出问题时可查的项目文档（在 `C:\car-web` 里）

- `6.说明文档/2.环境配置/远程Web控制台.md`（架构、API、安全设计）
- `6.说明文档/2.环境配置/工具脚本/README.md`（找 IP 的原理与坑）
- `6.说明文档/2.环境配置/SSH连接与传文件.md`（SSH/传文件 18 项排查）
- `6.说明文档/2.环境配置/快捷命令速查.md`（板上常用命令）