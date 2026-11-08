# 开机自启：安装 / 卸载 / 查看
#
# 它是干什么的：让电脑登录后自动把服务端拉起来，不用每次手动跑 run.ps1。
# 为什么用计划任务而不是启动文件夹：可设延时（等网络就绪）、可设"失败后重启"、无窗口闪现。
# 做完会得到什么：重启电脑、登录后约 1 分钟，"它"自动上线。
#
# 用法：
#   .\autostart.ps1            安装（登录时启动，延时 1 分钟）
#   .\autostart.ps1 -Remove    卸载
#   .\autostart.ps1 -Status    查看当前状态

param(
    [switch]$Remove,
    [switch]$Status,
    [switch]$RunNow
)

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$TASK = 'CompanionAutoStart'
$VBS = Join-Path $ROOT '_boot.vbs'

if ($Status) {
    $r = schtasks /query /tn $TASK /fo LIST 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host '未安装开机自启' -ForegroundColor Yellow }
    else { Write-Host '已安装：' -ForegroundColor Green; $r }
    exit
}

if ($Remove) {
    schtasks /delete /tn $TASK /f 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host '已卸载开机自启' -ForegroundColor Green }
    else { Write-Host '卸载失败（可能本来就没装）' -ForegroundColor Yellow }
    exit
}

if ($RunNow) {
    & (Join-Path $ROOT 'run.ps1') start
    exit
}

if (-not (Test-Path $VBS)) { Write-Host "缺少 $_boot.vbs" -ForegroundColor Red; exit 1 }

# wscript 执行 vbs（隐藏窗口），登录后延时 1 分钟
$action = 'wscript.exe "' + $VBS + '"'
schtasks /create /tn $TASK /tr $action /sc onlogon /delay 0001:00 /f 2>&1 | Out-Null

if ($LASTEXITCODE -eq 0) {
    Write-Host '已安装开机自启（登录后约 1 分钟启动，无窗口）' -ForegroundColor Green
    Write-Host "任务名：$TASK"
    Write-Host '卸载：.\autostart.ps1 -Remove'
} else {
    Write-Host '安装失败。可改用启动文件夹方式：' -ForegroundColor Yellow
    Write-Host '  1. Win+R 输入 shell:startup'
    Write-Host "  2. 把 $ROOT\_boot.cmd 的快捷方式放进去"
}
