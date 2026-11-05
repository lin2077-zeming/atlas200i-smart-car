<#
.SYNOPSIS
    找出小车的 IP 地址（用于 SSH 连接）。

.DESCRIPTION
    板子是 Atlas 200I DK A2（主机名 davinci-mini），可能同时通过 WiFi 和网线接入。
    本脚本用「MAC 地址」识别板子 —— 这比盲扫端口可靠得多。

    识别用的网卡 MAC 前缀：
        ac:45:ef  →  wlan0（AX210 WiFi 网卡）
        a0:70:b7  →  eth0/eth1（华为板载网卡）

    流程（②③④ 是 fallback，① 命中就够）：
        ① 已知/历史 IP（网线 ICS 固定 + 上次的 WiFi 地址），按优先级全部测一遍
        ② 扫本机 ARP 缓存
        ③ 扫目标网段的 ARP 缓存
        ④ 并行 ping 填充 ARP 后重扫（最慢，兜底）

    输出会区分「WiFi（主用）」和「网线（备用）」，并给出推荐命令。

.PARAMETER Subnet
    要扫描的网段前缀，如 192.168.31。不给则自动从本机 IPv4 推断。

.PARAMETER Sweep
    强制执行 ping 扫描（默认只在前面几级都没命中时才扫）。

.PARAMETER NoProbe
    跳过 SSH 复核（只找 MAC，快很多）。

.PARAMETER TimeoutMs
    ping 超时毫秒数，默认 900。

.EXAMPLE
    .\find_car.ps1
    .\find_car.ps1 -Subnet 192.168.31
    .\find_car.ps1 -Sweep -NoProbe
#>
[CmdletBinding()]
param(
    [string] $Subnet,
    [switch] $Sweep,
    [switch] $NoProbe,
    [int]    $TimeoutMs = 900
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

# ── 识别板子的网卡 MAC 前缀 ─────────────────────────────────────
$BoardOuis = @(
    @{ Prefix = 'ac:45:ef'; Nic = 'wlan0 (AX210 WiFi)' },
    @{ Prefix = 'a0:70:b7'; Nic = 'eth0/eth1 (华为板载)' }
)

# ── 已知/历史 IP：Sort=小的优先（WiFi 主用，网线备用）──────────
$Known = @(
    [pscustomobject]@{ IP = '192.168.31.128';  How = 'WiFi';   Sort = 0 },
    [pscustomobject]@{ IP = '192.168.137.100'; How = '网线';   Sort = 1 }
)
$BoardHost = 'davinci-mini'

function Write-Head($t) {
    Write-Host ""
    Write-Host "── $t " -ForegroundColor Cyan -NoNewline
    Write-Host ("─" * [Math]::Max(1, 58 - $t.Length)) -ForegroundColor DarkGray
}
function Write-Ok($t)   { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Write-Bad($t)  { Write-Host "  [--]   $t" -ForegroundColor DarkGray }
function Write-Warn2($t){ Write-Host "  [!!]   $t" -ForegroundColor Yellow }

# ══ 取 ARP 表 ════════════════════════════════════════════════════
function Get-ArpTable {
    $rows = @()
    foreach ($line in ((arp -a) 2>$null)) {
        if ($line -match '^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})') {
            $rows += [pscustomobject]@{
                IP  = $Matches[1]
                MAC = ($Matches[2] -replace '-', ':').ToLower()
            }
        }
    }
    return $rows
}

# ══ TCP 端口探测（比 Test-NetConnection 快）═════════════════════
function Test-TcpPort([string]$Ip, [int]$Port, [int]$Ms = 1200) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $t = $c.ConnectAsync($Ip, $Port)
        return ($t.Wait($Ms) -and $c.Connected)
    } catch { return $false } finally { $c.Close() }
}

# ══ 并行 ping 扫一个网段，填充 ARP ══════════════════════════════
function Invoke-PingSweep([string]$Prefix, [int]$Timeout = 900) {
    Write-Host "  正在 ping $Prefix.1-254 …" -NoNewline
    $tasks = New-Object System.Collections.ArrayList
    foreach ($i in 1..254) {
        $p = New-Object System.Net.NetworkInformation.Ping
        [void]$tasks.Add($p.SendPingAsync("$Prefix.$i", $Timeout))
    }
    try { [System.Threading.Tasks.Task]::WaitAll($tasks.ToArray(), 8000) } catch {}
    $alive = 0
    foreach ($t in $tasks) {
        try { if ($t.Status -eq 'RanToCompletion' -and $t.Result.Status -eq 'Success') { $alive++ } } catch {}
    }
    Write-Host ("  完成（{0} 台响应 ping）" -f $alive)
    return $alive
}

# ══ 从 ARP 表挑板子 ═════════════════════════════════════════════
function Find-BoardInArp([string]$Prefix = '') {
    $hits = @()
    foreach ($r in (Get-ArpTable)) {
        if ($Prefix -and -not $r.IP.StartsWith("$Prefix.")) { continue }
        foreach ($oui in $BoardOuis) {
            if ($r.MAC.StartsWith($oui.Prefix)) {
                $hits += [pscustomobject]@{ IP = $r.IP; MAC = $r.MAC; Nic = $oui.Nic }
            }
        }
    }
    return $hits
}

# ══ SSH/TCP 复核 ════════════════════════════════════════════════
$script:RemoteHost = ''
function Confirm-Board([string]$Ip) {
    if ($NoProbe) { return (Test-TcpPort $Ip 22) }
    if (Get-Command ssh -ErrorAction SilentlyContinue) {
        $o = & ssh -o BatchMode=yes -o StrictHostKeyChecking=no `
                   -o ConnectTimeout=5 -o LogLevel=ERROR `
                   "root@$Ip" "hostname" 2>$null
        if ($LASTEXITCODE -eq 0 -and $o) {
            $h = ($o | Select-Object -First 1).Trim()
            if ($h) { $script:RemoteHost = $h }
            return $true
        }
    }
    return (Test-TcpPort $Ip 22)
}

# ══ 主流程 ══════════════════════════════════════════════════════
Write-Host ""
Write-Host "════════════════════════════════════════════════════════════" -ForegroundColor White
Write-Host " 找小车（Atlas 200I DK A2 / $BoardHost）" -ForegroundColor White
Write-Host "════════════════════════════════════════════════════════════" -ForegroundColor White

Write-Head "本机网络"
$myIps = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' }
foreach ($a in $myIps) { Write-Host ("  {0,-16} {1}" -f $a.IPAddress, $a.InterfaceAlias) }

# 推断网段（优先 WLAN）
if (-not $Subnet) {
    $cand = $myIps |
        Where-Object { $_.IPAddress -like '192.168.*' -or $_.IPAddress -like '10.*' } |
        Sort-Object { if ($_.InterfaceAlias -match 'WLAN|Wi-?Fi|无线') { 0 } else { 1 } }
    if ($cand) { $Subnet = ($cand[0].IPAddress -split '\.')[0..2] -join '.' }
}
if ($Subnet) { Write-Host ""; Write-Host "  目标网段: $Subnet.0/24" -ForegroundColor Yellow }

$found = @()      # 收集所有可达的板子地址

# ── ① 已知/历史 IP（全测，不提前 break）────────────────────────
Write-Head "① 已知/历史 IP"
foreach ($k in ($Known | Sort-Object Sort)) {
    if (Confirm-Board $k.IP) {
        Write-Ok ("{0,-18} [{1}]  {2}" -f $k.IP, $k.How, $script:RemoteHost)
        $found += [pscustomobject]@{ IP = $k.IP; How = $k.How; Sort = $k.Sort; Src = '已知' }
    } else {
        Write-Bad ("{0,-18} [{1}]  不可达" -f $k.IP, $k.How)
    }
}

# ── ②③④ ARP 查找（只在①一无所获时才做）─────────────────────
if ($found.Count -eq 0) {
    Write-Head "② 本机 ARP 缓存"
    $hits = Find-BoardInArp
    if ($hits) {
        foreach ($h in $hits) {
            Write-Ok "$($h.IP)  MAC $($h.MAC)  [$($h.Nic)]"
            if (Confirm-Board $h.IP) {
                $found += [pscustomobject]@{ IP = $h.IP; How = 'ARP'; Sort = 1; Src = $h.Nic }
            }
        }
    } else { Write-Bad "没有板子的 MAC（a0:70:b7 / ac:45:ef）" }
}

if ($found.Count -eq 0 -and $Subnet) {
    Write-Head "③ 网段 $Subnet 的 ARP 缓存"
    $hits = Find-BoardInArp $Subnet
    if ($hits) {
        foreach ($h in $hits) {
            Write-Ok "$($h.IP)  MAC $($h.MAC)"
            if (Confirm-Board $h.IP) {
                $found += [pscustomobject]@{ IP = $h.IP; How = 'ARP'; Sort = 1; Src = $h.Nic }
            }
        }
    } else { Write-Bad "没有" }
}

if ($found.Count -eq 0 -and $Subnet) {
    Write-Head "④ 扫描 $Subnet.0/24（填充 ARP）"
    Invoke-PingSweep $Subnet $TimeoutMs | Out-Null
    Start-Sleep -Milliseconds 800
    $hits = Find-BoardInArp $Subnet
    if ($hits) {
        foreach ($h in $hits) {
            Write-Ok "$($h.IP)  MAC $($h.MAC)"
            if (Confirm-Board $h.IP) {
                $found += [pscustomobject]@{ IP = $h.IP; How = '扫描'; Sort = 1; Src = $h.Nic }
            }
        }
    } else { Write-Warn2 "扫完仍没找到板子" }
}

# ── 结果 ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "════════════════════════════════════════════════════════════" -ForegroundColor White

if ($found.Count -gt 0) {
    $found = @($found | Sort-Object Sort, IP -Unique)

    # 按网段判断接入方式：192.168.137.x 是 PC 的 ICS 网线；其余按 WiFi 算
    function Get-How($ip) { if ($ip -like '192.168.137.*') { '网线/ICS' } else { 'WiFi' } }
    $wifi = @($found | Where-Object { (Get-How $_.IP) -eq 'WiFi' })
    $best = if ($wifi.Count -gt 0) { $wifi[0] } else { $found[0] }

    $n = $found.Count
    if ($n -eq 1) {
        Write-Host " ★ 找到小车" -ForegroundColor Green
    } else {
        Write-Host " ★ 找到小车（$n 个地址可达）" -ForegroundColor Green
    }
    Write-Host ""
    foreach ($f in $found) {
        $how  = Get-How $f.IP
        # 只有一个地址时不必标"推荐"（没得选）
        $mark = if ($n -eq 1 -or $f.IP -eq $best.IP) { '← 用这个' } else { '备用' }
        $col  = if ($f.IP -eq $best.IP) { 'Yellow' } else { 'DarkGray' }
        Write-Host ("   {0,-18} {1,-9} {2}" -f $f.IP, $how, $mark) -ForegroundColor $col
    }
    Write-Host ""
    Write-Host "   SSH 命令：" -ForegroundColor White
    Write-Host "       ssh root@$($best.IP)" -ForegroundColor Yellow
    if ($script:RemoteHost) {
        Write-Host "   （远端主机名: $script:RemoteHost）" -ForegroundColor DarkGray
    }
    if ($n -gt 1) {
        Write-Host "   两条路都通；拔掉网线后用 WiFi 那条。" -ForegroundColor DarkGray
    }
} else {
    Write-Host " ✗ 没找到小车" -ForegroundColor Red
    Write-Host ""
    Write-Host "   排查建议：" -ForegroundColor White
    Write-Host "     1. 板子有屏幕的话最可靠：在板上开终端跑  hostname -I"
    Write-Host "     2. 确认同 WiFi：板上跑  nmcli -t -f active,ssid dev wifi | grep '^yes'"
    Write-Host "     3. 指定网段强扫：  .\find_car.ps1 -Subnet 192.168.31 -Sweep"
    Write-Host "     4. 插网线走 ICS：  ssh root@192.168.137.100"
    Write-Host ""
    Write-Host "   注意：ARP 缓存会过期，一次扫描没找到 ≠ 板子没联网。" -ForegroundColor Yellow
}
Write-Host "════════════════════════════════════════════════════════════" -ForegroundColor White
Write-Host ""

if ($found.Count -gt 0) { exit 0 } else { exit 1 }
