@echo off
setlocal enabledelayedexpansion
echo Stopping SENTINEL VISION...

rem Primary: close by the window titles start.bat launched.
taskkill /FI "WINDOWTITLE eq SENTINEL-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq SENTINEL-Frontend*" /T /F >nul 2>&1

rem Fallback: kill whatever is actually bound to the app ports, in case a
rem window was renamed/detached from the batch launcher.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1

rem Confirm rather than just claim — check the ports are actually free now.
rem ping as a ~1s wait instead of `timeout`, which needs a real console
rem input handle and fails ("Input redirection is not supported") when
rem run non-interactively; ping works everywhere.
ping -n 2 127.0.0.1 >nul
set STILL_UP=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do set STILL_UP=1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do set STILL_UP=1

if !STILL_UP! == 1 (
    echo [WARNING] Something is still listening on 8000 or 3000 after stopping.
    echo Check manually: netstat -ano ^| findstr ":8000 :3000"
) else (
    echo Done. Ports 8000 and 3000 are free.
)
pause
