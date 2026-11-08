# 本地一键启动：只起服务端
#
# 它是干什么的：把"开两个终端、手输一堆环境变量"变成一条命令。
# 为什么需要：环境变量漏一个就报错，而报错信息（比如拿 token 401）不直观。
# 做完会得到什么：两个进程常驻、日志落到 logs/，并且先做健康检查再报"就绪"。

$ErrorActionPreference = 'Continue'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT
$LOG = Join-Path $ROOT 'logs'
New-Item -ItemType Directory -Force -Path $LOG | Out-Null

Write-Host ''
Write-Host '=== 同伴 · 本地启动 ===' -ForegroundColor Cyan

# ---------- 1. 载入 .env ----------
$envFile = Join-Path $ROOT '.env'
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $k, $v = $line -split '=', 2
            if ($v.Trim()) { [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process') }
        }
    }
    Write-Host '[env] 已载入 .env'
} else {
    Write-Host '[env] 没有 .env（可复制 .env.example 后填写）' -ForegroundColor Yellow
}

# 口令：优先环境变量，否则读 data\token.txt
$tokenFile = Join-Path $ROOT 'data\token.txt'
if (-not $env:COMPANION_TOKEN -and (Test-Path $tokenFile)) {
    $env:COMPANION_TOKEN = (Get-Content $tokenFile -Raw).Trim()
}
if (-not $env:COMPANION_PORT) { $env:COMPANION_PORT = '8787' }
$PORT = $env:COMPANION_PORT
$BASE = "http://127.0.0.1:$PORT"

# ---------- 2. 找解释器 ----------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = 'python' }
if (-not (Test-Path $python)) { Write-Host '[错误] 找不到 python' -ForegroundColor Red; exit 1 }

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { $node = 'D:\nodejs\node.exe' }

# ---------- 3. 服务端 ----------
$serverPid = $null
$healthy = $false
try {
    $h = Invoke-RestMethod "$BASE/api/health$(if ($env:COMPANION_TOKEN) { "?t=$($env:COMPANION_TOKEN)" })" -TimeoutSec 3
    if ($h.ok) { $healthy = $true; Write-Host "[服务端] 已在运行（最大 id=$($h.max_id)，大脑=$($h.brain)）" -ForegroundColor Green; Write-Host "         （改了 .env 要生效：先 .\stop.ps1 再 .\start.ps1）" -ForegroundColor DarkGray }
} catch { }

if (-not $healthy) {
    $out = Join-Path $LOG 'server.out.log'
    $err = Join-Path $LOG 'server.err.log'
    $p = Start-Process -FilePath $python -ArgumentList 'server.py' -WorkingDirectory $ROOT `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -WindowStyle Hidden
    $serverPid = $p.Id
    Write-Host "[服务端] 启动中 pid=$serverPid ..."

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $h = Invoke-RestMethod "$BASE/api/health$(if ($env:COMPANION_TOKEN) { "?t=$($env:COMPANION_TOKEN)" })" -TimeoutSec 2
            if ($h.ok) { $healthy = $true; break }
        } catch { }
    }
    if ($healthy) {
        Write-Host "[服务端] 就绪  $BASE  （大脑=$($h.brain)）" -ForegroundColor Green
    } else {
        Write-Host '[服务端] 启动失败，看 logs\server.err.log' -ForegroundColor Red
        Get-Content $err -Tail 20 -ErrorAction SilentlyContinue
        exit 1
    }
}

# ---------- 5. 记录 pid ----------
$pidFile = Join-Path $LOG 'pids.json'
$pids = @{}
if (Test-Path $pidFile) { try { (Get-Content $pidFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $pids[$_.Name] = $_.Value } } catch { } }
if ($serverPid) { $pids['server'] = $serverPid }
if ($gwPid) { $pids['gateway'] = $gwPid }
$pids | ConvertTo-Json | Set-Content $pidFile -Encoding UTF8

Write-Host ''
Write-Host '=== 就绪 ===' -ForegroundColor Cyan
if ($env:COMPANION_TOKEN) {
    $ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.PrefixOrigin -ne 'WellKnown' }).IPAddress
    Write-Host "本机网页： $BASE/?t=$($env:COMPANION_TOKEN)"
    foreach ($ip in $ips) { Write-Host "手机网页： http://$ip`:$PORT/?t=$($env:COMPANION_TOKEN)" }
}
Write-Host "日志目录： $LOG"
Write-Host '停止：     .\stop.ps1'
Write-Host ''
