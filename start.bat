@echo off
setlocal enabledelayedexpansion
set ROOT=%~dp0

if not exist "%ROOT%backend\.venv\Scripts\python.exe" (
    echo [ERROR] Backend venv not found at backend\.venv
    echo Run: cd backend ^&^& uv venv --python 3.11 .venv ^&^& uv pip install --python .venv -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
    pause
    exit /b 1
)
if not exist "%ROOT%frontend\node_modules" (
    echo [ERROR] Frontend deps not found. Run: cd frontend ^&^& npm install
    pause
    exit /b 1
)

rem Port pre-flight: catches "it's already running" / a stale process left
rem over from a crashed previous run before silently colliding with it.
set PORT_BUSY=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do set PORT_BUSY=1
if !PORT_BUSY! == 1 (
    echo [ERROR] Port 8000 is already in use — SENTINEL VISION backend may already be running.
    echo Run stop.bat first, or check what's using it: netstat -ano ^| findstr ":8000"
    pause
    exit /b 1
)
set PORT_BUSY=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do set PORT_BUSY=1
if !PORT_BUSY! == 1 (
    echo [ERROR] Port 3000 is already in use — SENTINEL VISION frontend may already be running.
    echo Run stop.bat first, or check what's using it: netstat -ano ^| findstr ":3000"
    pause
    exit /b 1
)

if not exist "%ROOT%backend\.env" (
    echo [NOTE] backend\.env not found — running with defaults only ^(DEMO_MODE, no
    echo real Sentinel Grid camera credentials^). Real-camera auto-connect stays off
    echo until SENTINEL_GRID_EMAIL/PASSWORD are set in backend\.env.
    echo.
)

echo Starting SENTINEL VISION backend on :8000 ...
start "SENTINEL-Backend" cmd /k "cd /d "%ROOT%backend" && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

echo Waiting for the backend to come up...
set BACKEND_READY=0
for /l %%i in (1,1,30) do (
    curl -s -o nul -m 1 http://localhost:8000/api/health
    if !errorlevel! == 0 (
        set BACKEND_READY=1
        goto :backend_up
    )
    rem ping as a ~1s wait instead of `timeout` — timeout needs a real
    rem console input handle and fails immediately ("Input redirection is
    rem not supported") when run non-interactively; ping works everywhere.
    ping -n 2 127.0.0.1 >nul
)
:backend_up
if !BACKEND_READY! == 0 (
    echo [WARNING] Backend did not respond within 30s — check the SENTINEL-Backend
    echo window for errors. Starting the frontend anyway; it will show a connection
    echo error until the backend comes up.
) else (
    echo Backend is up.
)

echo Starting SENTINEL VISION frontend on :3000 ...
start "SENTINEL-Frontend" cmd /k "cd /d "%ROOT%frontend" && npm run dev"

echo.
echo ----------------------------------------
echo Backend:  http://localhost:8000/docs
echo Frontend: http://localhost:3000
echo Login:    admin / sentinel123
echo ----------------------------------------
echo Two windows opened. Closing a window stops that service.
echo Or run stop.bat to stop both from here.
pause
