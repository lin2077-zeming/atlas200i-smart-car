# push_via_api.ps1 —— 通过 GitHub API 推送（绕过 Steam++ 代理对 git-receive-pack 的 502）
# 思路：
#   1. Contents API 放一个 README 让仓库"非空"（空仓库不能用 Git Data API）
#   2. Git Data API 为每个文件建 blob
#   3. 建一棵包含全部文件的 tree
#   4. 建一个**无父提交**的 root commit（这样最终历史只有 1 个提交）
#   5. 强制把 refs/heads/main 指向它，覆盖掉第 1 步的临时提交
$ErrorActionPreference = "Continue"   # ⚠️ 不能设 Stop：原生命令(gh)写 stderr 时
                                       #    PowerShell 会把它当成终止性异常，
                                       #    导致下面的重试逻辑根本跑不到。
$Owner = "lin2077-zeming"
$Repo  = "atlas200i-smart-car"
$Work  = (Get-Location).Path

function TryGhApi($method, $path) {
    $a = @("api", "-X", $method, $path)
    $out = & gh @a 2>&1
    if ($LASTEXITCODE -eq 0) { return ($out -join "`n") }
    return $null
}

function GhApi($method, $path, $bodyFile) {
    # ★ 带重试：本机走 Steam++ 代理，偶发 TLS handshake timeout / 502
    $last = ""
    for ($try = 1; $try -le 6; $try++) {
        $a = @("api", "-X", $method, $path)
        if ($bodyFile) { $a += @("--input", $bodyFile) }
        $out = & gh @a 2>&1
        $code = $LASTEXITCODE
        if ($code -eq 0) { return ($out -join "`n") }
        $last = ($out | Out-String).Trim()
        if ($last -match "wasn't supplied|already exists|Reference already exists") {
            throw "ALREADY_EXISTS: $last"
        }
        Write-Host ("        [重试 {0}/6] {1}" -f $try, $last.Substring(0, [Math]::Min(60, $last.Length)))
        Start-Sleep -Milliseconds (1500 * $try)
    }
    throw "gh api $method $path 失败: $last"
}

function WriteJsonBody($path, $obj) {
    # ⚠️ 必须写成 **UTF-8 无 BOM**：仓库里有中文路径（6.说明文档/…），
    #    用 Out-File -Encoding ascii 会把中文变 "?"，推上去的树就坏了。
    $json = $obj | ConvertTo-Json -Compress -Depth 10
    [IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function PostJson($path, $obj) {
    $f = Join-Path $env:TEMP ("ghbody_" + [guid]::NewGuid().ToString("N") + ".json")
    WriteJsonBody $f $obj
    try { return (GhApi "POST" $path $f) } finally { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}

function PatchJson($path, $obj) {
    $f = Join-Path $env:TEMP ("ghbody_" + [guid]::NewGuid().ToString("N") + ".json")
    WriteJsonBody $f $obj
    try { return (GhApi "PATCH" $path $f) } finally { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}

function PutJson($path, $obj) {
    $f = Join-Path $env:TEMP ("ghbody_" + [guid]::NewGuid().ToString("N") + ".json")
    WriteJsonBody $f $obj
    try { return (GhApi "PUT" $path $f) } finally { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}

# ── 取要提交的文件清单：**工作区**（已跟踪 + 未跟踪未忽略）─────
# ⚠️ 不用 `git ls-tree HEAD`：那样只能推"已 commit"的内容。改用
#    `git ls-files --cached --others --exclude-standard`，GitHub 快照才等于
#    本地磁盘上的项目文件（含还没 commit 的文档/代码），做到"完全一致"。
#    被 .gitignore 排除的（.workbuddy/、__pycache__/ 等）不推 —— 本来就不该进仓库。
$files = @(& git -c core.quotepath=false ls-files --cached --others --exclude-standard |
           Sort-Object -Unique)
if (-not $files) { throw "工作区里没有文件" }
Write-Host ("  待提交文件: {0} 个（含未跟踪/未提交）" -f $files.Count)

# ── 1. 让仓库非空（可重入：已有 main 分支就跳过）──────────────
Write-Host "  [1/5] 确认仓库非空…"
$branch = "main"
$refJson = TryGhApi "GET" "repos/$Owner/$Repo/git/ref/heads/main"
if ($refJson) {
    Write-Host "        仓库已有 main 分支，跳过初始化"
} else {
    Write-Host "        空仓库 → 用 Contents API 建初始提交"
    $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $Work "../../1.总览与架构/系统架构与安全设计.md")))
    $r = PutJson "repos/$Owner/$Repo/contents/../../1.总览与架构/系统架构与安全设计.md" @{
        message = "chore: 初始化仓库（临时，稍后被覆盖）"
        content = $b64
    }
    Write-Host "        初始提交: $(($r | ConvertFrom-Json).commit.sha)"
}
Write-Host "        默认分支: $branch"

# ── 2. 每个文件建 blob ────────────────────────────────────────
Write-Host "  [2/5] 创建 blob…"
$tree = @()
$i = 0
foreach ($f in $files) {
    $i++
    $full = Join-Path $Work $f
    if (-not (Test-Path $full)) { Write-Host "        跳过（不存在）: $f"; continue }
    $c = [Convert]::ToBase64String([IO.File]::ReadAllBytes($full))
    $resp = PostJson "repos/$Owner/$Repo/git/blobs" @{ content = $c; encoding = "base64" }
    $sha = ($resp | ConvertFrom-Json).sha
    if (-not $sha) { throw "blob 创建失败: $f" }
    # git 路径统一用 /；.sh 保留可执行位（否则 clone 后 chmod 要重做）
    $mode = if ($f -match '\.sh$') { "100755" } else { "100644" }
    $tree += @{ path = ($f -replace '\\','/'); mode = $mode; type = "blob"; sha = $sha }
    if ($i % 10 -eq 0) { Write-Host ("        {0}/{1}" -f $i, $files.Count) }
}
Write-Host ("        完成 {0} 个 blob" -f $tree.Count)

# ── 3. 建 tree ────────────────────────────────────────────────
Write-Host "  [3/5] 创建 tree…"
$resp = PostJson "repos/$Owner/$Repo/git/trees" @{ tree = $tree }
$treeSha = ($resp | ConvertFrom-Json).sha
Write-Host "        tree: $treeSha"

# ── 4. 建 root commit（无父提交 → 最终历史只有 1 个提交）──────
Write-Host "  [4/5] 创建提交…"
$msg = (& git log -1 --pretty=%B) -join "`n"
$resp = PostJson "repos/$Owner/$Repo/git/commits" @{ message = $msg; tree = $treeSha }
$newCommit = ($resp | ConvertFrom-Json).sha
Write-Host "        commit: $newCommit"

# ── 5. 强制更新分支（分支已由第 1 步创建 → 必须用 PATCH）───────
Write-Host "  [5/5] 更新分支 $branch …"
PatchJson "repos/$Owner/$Repo/git/refs/heads/$branch" @{ sha = $newCommit; force = $true } | Out-Null
Write-Host "  ✅ 完成"
