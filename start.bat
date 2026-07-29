@echo off
cd /d D:\study-monitor

echo [1/2] Cleaning up old go2rtc processes...
taskkill /F /IM go2rtc.exe 2>nul
timeout /t 2 /nobreak >nul

echo [1/2] Starting Go2rtc...
start /B "" "D:\study-monitor\go2rtc_bin\go2rtc.exe" -config "D:\study-monitor\go2rtc.yaml"
timeout /t 3 /nobreak >nul
echo     Go2rtc started (http://localhost:1984)

echo [2/2] Starting monitor (Ctrl+C to stop)...
echo.
python study_monitor.py --debug
pause