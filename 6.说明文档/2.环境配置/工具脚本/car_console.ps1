<#
================================================================================
 car_console.ps1 —— 一键连上开发板并打开「小车远程 Web 控制台」

 它做四件事：
   1. 找板子（缓存 → mDNS 主机名 → 已知 IP → 可选扫网段）
   2. 通过 SSH 在板子上**把 Web 控制台跑起来**（已经在跑就不重复起）
   3. 等 8090 端口真正能应答（TTS/语音模型加载需要时间）
   4. 用默认浏览器打开控制台

 用法：
   .\car_console.ps1                # 一键连接
   .\car_console.ps1 -Stop          # 停止板子上的控制台
   .\car_console.ps1 -IP 192.168.31.128
   .\car_console.ps1 -Sweep         # 候选都失败时扫本机所在 /24
   .\car_console.ps1 -Asr           # 启动时预加载语音识别（否则用网页上按需加载）

 为什么用 SSH 去启动：串口是独占资源，Web 服务必须和 CarController 同进程，
 所以只能在板子上把它拉起来 —— 这个脚本把这一步自动化了。
================================================================================
#>
param(
    [switch]$Stop,
    [switch]$Sweep,
    [switch]$Asr,
    [string]$IP = "",
    [int]$Port = 8090
)

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$CacheFile = Join-Path $Here ".car_board_ip"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Ok($msg)   { Say "  [OK]   $msg" "Green" }
function Warn($msg) { Say "  [警告] $msg" "Yellow" }
function Bad($msg)  { Say "  [失败] $msg" "Red" }

function Test-Port {
    param([string]$Target, [int]$TcpPort, [int]$TimeoutMs = 900)
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $t = $c.ConnectAsync($Target, $TcpPort)
        if ($t.Wait($TimeoutMs) -and $c.Connected) { return $true }
    } catch { }
    finally { try { $c.Close() } catch { } }
    return $false
}

function Resolve-Host([string]$Name) {
    try {
        $a = [System.Net.Dns]::GetHostAddresses($Name) |
             Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
             Select-Object -First 1
        if ($a) { return $a.IPAddressToString }
    } catch { }
    return $null
}

function Sweep-LocalSubnet {
    $found = $null
    $mine = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -match '^(192\.168|10)\.' -and
                           $_.IPAddress -notmatch '^169\.254' }
    foreach ($me in $mine) {
        $base = ($me.IPAddress -split '\.')[0..2] -join '.'
        Say "  扫描 $base.0/24（每秒查 22 端口）…" DarkGray
        $clients = @{}
        $tasks = @()
        foreach ($i in 1..254) {
            $ip = "$base.$i"
            try {
                $c = New-Object System.Net.Sockets.TcpClient
                $clients[$ip] = $c
                $tasks += [pscustomobject]@{ IP = $ip; Task = $c.ConnectAsync($ip, 22) }
            } catch { }
        }
        $deadline = (Get-Date).AddSeconds(3)
        foreach ($t in $tasks) {
            $remain = [int][Math]::Max(40, ($deadline - (Get-Date)).TotalMilliseconds)
            try {
                if ($t.Task.Wait($remain) -and $clients[$t.IP].Connected) { $found = $t.IP; break }
            } catch { }
            if ((Get-Date) -gt $deadline) { break }
        }
        foreach ($k in @($clients.Keys)) { try { $clients[$k].Close() } catch { } }
        if ($found) { break }
    }
    return $found
}

# ── 1. 找板子 ────────────────────────────────────────────────────────────
Say ""
Say "════════════════════════════════════════════════════" Cyan
Say "  小车远程控制台 · 一键连接" Cyan
Say "════════════════════════════════════════════════════" Cyan
Say ""
Say "[1/4] 找开发板…"

$cands = @()
if ($IP) { $cands += $IP }
if (Test-Path $CacheFile) {
    $cached = (Get-Content $CacheFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($cached) { $cands += $cached }
}
Say "  试 mDNS 主机名 davinci-mini.local …" DarkGray
$mdns = Resolve-Host "davinci-mini.local"
if (-not $mdns) { $mdns = Resolve-Host "davinci-mini" }
if ($mdns) { $cands += $mdns; Say "    解析到 $mdns" DarkGray }
$cands += @("192.168.137.100", "192.168.31.128", "10.36.121.127")
$cands = $cands | Where-Object { $_ } | Select-Object -Unique

$board = $null
foreach ($c in $cands) {
    Write-Host "  试 $c … " -NoNewline -ForegroundColor DarkGray
    if (Test-Port $c 22) { $board = $c; Write-Host "通" -ForegroundColor Green; break }
    Write-Host "不通" -ForegroundColor DarkGray
}

if (-not $board -and $Sweep) { $board = Sweep-LocalSubnet }
if (-not $board -and -not $Sweep) {
    Warn "常见地址都没通。如果板子换了 IP，加 -Sweep 扫一遍："
    Warn "    .\car_console.ps1 -Sweep"
}

if (-not $board) {
    Bad "找不到开发板。请检查："
    Say "     1. 板子是否开机、是否连上同一个 WiFi/热点" Yellow
    Say "     2. 板子 IP：在板子上执行 hostname -I" Yellow
    Say "     3. 或手动指定：.\car_console.ps1 -IP 192.168.x.x" Yellow
    Say "     4. 也可以先跑同目录的 find_car.cmd 找 IP" Yellow
    exit 1
}
Ok "板子：$board"
try { Set-Content -Path $CacheFile -Value $board -Encoding ascii -ErrorAction SilentlyContinue } catch { }

$SshOpts = @("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5")
$sshTarget = "root@$board"

# ── 2. 在板子上把服务跑起来 ──────────────────────────────────────────────
Say ""
Say "[2/4] 在板子上启动控制台…"
if ($Stop) {
    # ⚠️ 远程脚本走 stdin（bash -s）：PowerShell→ssh→bash 三层引号会把
    #    `"[c]ar_web.py"` 这类引号搞坏（踩过）
    $stopScript = "pkill -f '[c]ar_web.py' && echo STOPPED || echo NOTRUNNING`n" +
                  "systemctl is-active --quiet car-web 2>/dev/null && systemctl stop car-web || true"
    $stopB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($stopScript))
    & ssh @SshOpts $sshTarget "echo $stopB64 | base64 -d | bash" 2>&1 |
        ForEach-Object { Say "  $_" DarkGray }
    Ok "已请求停止"
    exit 0
}

$asrFlag = if ($Asr) { "--asr" } else { "" }
# ⚠️ 远程脚本用 **stdin** 送过去（`bash -s`），不要再拼成一行参数 ——
#    PowerShell → ssh → bash 三层引号会把 `"[c]ar_web.py"` 这类引号搞坏，
#    报 "unexpected EOF while looking for matching"（真踩过）。
$remote = @"
cd /home/HwHiAiUser/car_gui || exit 1
if systemctl is-active --quiet car-web 2>/dev/null; then
    echo MANAGED
    exit 0
fi
if pgrep -f '[c]ar_web.py' >/dev/null; then
    echo ALREADY
    exit 0
fi
setsid nohup sudo -u HwHiAiUser env HOME=/home/HwHiAiUser bash run_web.sh $asrFlag >/tmp/car_web.log 2>&1 </dev/null &
sleep 2
if pgrep -f '[c]ar_web.py' >/dev/null; then
    echo STARTED
else
    echo FAILED
fi
"@
# 用 base64 送脚本：命令里只有 [A-Za-z0-9+/=]，
# **没有引号/换行/BOM**，彻底避开 PowerShell→ssh→bash
# 三层解析的坑（引号被吃、stdin 带 BOM 都踩过）
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
$rc = & ssh @SshOpts $sshTarget "echo $b64 | base64 -d | bash" 2>&1
$state = ($rc | Select-Object -Last 1)
switch -Regex ($state) {
    "STARTED" { Ok "已在板子上启动（后台运行）" }
    "ALREADY" { Ok "板子上已经在跑了，直接连" }
    "MANAGED" { Ok "由 systemd 管理，已在运行" }
    "FAILED"  {
        Bad "启动失败。板子上的日志："
        & ssh @SshOpts $sshTarget 'tail -25 /tmp/car_web.log' 2>&1 | ForEach-Object { Say "    $_" DarkGray }
        exit 1
    }
    default {
        Warn "SSH 返回不明确（可能需要密码）。原始输出："
        $rc | ForEach-Object { Say "    $_" DarkGray }
        Warn "若一直卡在密码输入，请先做免密："
        Say "     ssh-keygen -t ed25519  然后  ssh-copy-id root@$board" Yellow
        exit 1
    }
}

# ── 3. 等它真的能应答 ───────────────────────────────────────────────────
Say ""
Say "[3/4] 等控制台就绪（首次启动要加载 TTS/LLM，约 5~20 秒）…"
$url = "http://${board}:$Port/"
$ready = $false
$status = $null
for ($i = 1; $i -le 60; $i++) {
    try {
        $status = Invoke-RestMethod -Uri "http://${board}:$Port/api/status" -TimeoutSec 3
        $ready = $true; break
    } catch { Start-Sleep -Milliseconds 1000 }
    if ($i % 5 -eq 0) { Say "  …已等 $i 秒" DarkGray }
}
if (-not $ready) {
    Bad "端口 $Port 一直没有应答。板子上的日志："
    & ssh @SshOpts $sshTarget 'tail -30 /tmp/car_web.log' 2>&1 | ForEach-Object { Say "    $_" DarkGray }
    exit 1
}
Ok "控制台已就绪"
Say ("      串口：{0}    电压：{1} V    TTS：{2}    语音识别：{3}" -f `
     $(if ($status.connected) { $status.port } else { "未连接（插好 USB 会自动连）" }), `
     $status.voltage, $status.tts_state, $status.asr_state) DarkGray

# ── 4. 开浏览器 ─────────────────────────────────────────────────────────
Say ""
Say "[4/4] 打开浏览器…"
try { Start-Process $url; Ok "已打开 $url" }
catch { Warn "没能自动开浏览器，请手动访问：$url" }

Say ""
Say "════════════════════════════════════════════════════" Cyan
Say "  控制台地址： $url" White
Say "  停止服务：   .\car_console.ps1 -Stop" DarkGray
Say "  下次直接双击「启动小车控制台.cmd」即可" DarkGray
Say "════════════════════════════════════════════════════" Cyan
Say ""
exit 0