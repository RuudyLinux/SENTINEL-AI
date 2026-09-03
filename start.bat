@echo off
setlocal
set ROOT=%~dp0

if not exist "%ROOT%backend\.venv\Scripts\python.exe" (
    echo [ERROR] Backend venv not found at backend\.venv
    echo Run: cd backend ^&^& uv venv --python 3.11 .venv ^&^& uv pip install --python .venv -r requirements.txt
    pause
    exit /b 1
)
if not exist "%ROOT%frontend\node_modules" (
    echo [ERROR] Frontend deps not found. Run: cd frontend ^&^& npm install
    pause
    exit /b 1
)

echo Starting SENTINEL VISION backend on :8000 ...
start "SENTINEL-Backend" cmd /k "cd /d "%ROOT%backend" && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

timeout /t 2 /nobreak >nul

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
