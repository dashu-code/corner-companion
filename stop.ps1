# 停止：按优先级找进程，尽量杀掉
# 顺序：data\*.pid → logs\pids.json → 端口占用者 → 命令行扫描
# 说明：提权启动的进程，普通会话杀不掉（会提示 Access is denied），需在该提权窗口里跑。

$ErrorActionPreference = 'Continue'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT

$script:killed = @()

function Try-Kill($id, $label) {
    if (-not $id) { return }
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $p) { return }
    try {
        Stop-Process -Id $id -Force -ErrorAction Stop
        Write-Host "[已停止] $label pid=$id" -ForegroundColor Green
        $script:killed += "$label=$id"
    } catch {
        Write-Host "[失败] $label pid=$id —— $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "        这个进程可能是提权启动的，请在那个终端里执行：" -ForegroundColor Yellow
        Write-Host "        Stop-Process -Id $id -Force" -ForegroundColor Yellow
    }
}

# 1) 各自的 pid 文件（最可靠）
Try-Kill (Get-Content (Join-Path $ROOT 'data\gateway.pid') -ErrorAction SilentlyContinue | Select-Object -First 1) 'gateway'
Try-Kill (Get-Content (Join-Path $ROOT 'data\server.pid')  -ErrorAction SilentlyContinue | Select-Object -First 1) 'server'

# 2) logs\pids.json
$pidFile = Join-Path $ROOT 'logs\pids.json'
if (Test-Path $pidFile) {
    try {
        $pids = Get-Content $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        Try-Kill $pids.gateway 'gateway'
        Try-Kill $pids.server  'server'
    } catch { }
}

# 3) 端口占用者（服务端一定占 8787）
$port = '8787'
if ($env:COMPANION_PORT) { $port = $env:COMPANION_PORT }
Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    Try-Kill $_.OwningProcess 'server(端口)'
}

# 4) 兜底：命令行扫描
    ForEach-Object { Try-Kill $_.ProcessId 'gateway(扫描)' }
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*server.py*' } |
    ForEach-Object { Try-Kill $_.ProcessId 'server(扫描)' }

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $ROOT 'data\gateway.pid') -Force -ErrorAction SilentlyContinue

if (-not $script:killed) { Write-Host '没有找到在运行的进程' -ForegroundColor Yellow }
