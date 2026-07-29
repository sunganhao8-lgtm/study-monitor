@echo off
REM ============================================
REM  Study Monitor - 独立启动脚本
REM  不依赖 Hermes，关机或休眠也不会断
REM ============================================
cd /d D:\study-monitor

echo ============================================
echo   Study Monitor Startup
echo ============================================

REM 清理旧进程
taskkill /F /IM go2rtc.exe 2>nul
taskkill /F /IM python.exe 2>nul
timeout /t 2 /nobreak >nul

REM 1. 启动 go2rtc
echo [1/2] Starting go2rtc...
start "go2rtc" /MIN go2rtc_bin\go2rtc.exe -config go2rtc.yaml
timeout /t 3 /nobreak >nul
echo        go2rtc started (http://localhost:1984)

REM 2. 启动 Web UI
echo [2/2] Starting Web UI...
start "StudyMonitor-WebUI" /MIN python web_ui.py
timeout /t 2 /nobreak >nul
echo        Web UI started (http://localhost:8765)

echo.
echo ============================================
echo   All services running!
echo   Dashboard: http://localhost:8765
echo   Go2rtc:    http://localhost:1984
echo ============================================
echo.
echo Close this window to stop, or leave it open.
pause
