@echo off
echo Stopping SENTINEL VISION...

rem Primary: close by the window titles start.bat launched.
taskkill /FI "WINDOWTITLE eq SENTINEL-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq SENTINEL-Frontend*" /T /F >nul 2>&1

rem Fallback: kill whatever is actually bound to the app ports, in case a
rem window was renamed/detached from the batch launcher.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1

echo Done. Ports 8000 and 3000 should be free.
pause
