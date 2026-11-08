param([string]$Action = 'start')

$ErrorActionPreference = 'Continue'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT

function Get-Token {
    if ($env:COMPANION_TOKEN) { return $env:COMPANION_TOKEN }
    $f = Join-Path $ROOT 'data\token.txt'
    if (Test-Path $f) { return (Get-Content $f -Raw).Trim() }
    return ''
}

function Show-Status {
    $t = Get-Token
    $port = '8787'
    if ($env:COMPANION_PORT) { $port = $env:COMPANION_PORT }
    $base = 'http://127.0.0.1:' + $port

    Write-Host ''
    Write-Host '=== 状态 ===' -ForegroundColor Cyan

    $url = $base + '/api/health'
    if ($t) { $url = $url + '?t=' + $t }
    try {
        $h = Invoke-RestMethod $url -TimeoutSec 3
        Write-Host ('服务端  : 运行中   最大 id=' + $h.max_id + '   大脑=' + $h.brain) -ForegroundColor Green
    } catch {
        Write-Host '服务端  : 未运行' -ForegroundColor Red
    }

    $envFile = Join-Path $ROOT '.env'
    if (Test-Path $envFile) {
        $lines = Get-Content $envFile -Encoding UTF8
        $hasKey = $false
        foreach ($line in $lines) {
            if ($line -match '^\s*COMPANION_API_KEY\s*=\s*\S') { $hasKey = $true }
        }
        Write-Host ('配置    : .env 有大脑key=' + $hasKey)
    } else {
        Write-Host '配置    : 没有 .env' -ForegroundColor Yellow
    }

    Write-Host ''
}

$act = $Action.ToLower()
if ($act -eq 'start') {
    & (Join-Path $ROOT 'start.ps1')
} elseif ($act -eq 'stop') {
    & (Join-Path $ROOT 'stop.ps1')
} elseif ($act -eq 'restart') {
    & (Join-Path $ROOT 'stop.ps1')
    Start-Sleep -Seconds 2
    & (Join-Path $ROOT 'start.ps1')
} elseif ($act -eq 'status') {
    Show-Status
} else {
    Write-Host '用法：.\run.ps1 [start|stop|status|restart]'
}
