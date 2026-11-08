@echo off
rem 开机自启用：切到项目目录，无窗口启动服务
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    exit /b 1
)
start "" /b pythonw server.py 2>> "logs\boot.err.log"
