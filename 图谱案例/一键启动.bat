@echo off
setlocal
title Knowledge Graph System
cd /d "%~dp0"

echo ================================================
echo   Knowledge Graph Display System
echo   Server: http://127.0.0.1:6006
echo ================================================
echo.

REM ---------- Kill leftover server instances (by command line + by port, double guard) ----------
echo [CLEAN] Stopping leftover server instances if any...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":6006" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

REM ---------- Verify / create virtual environment ----------
REM IMPORTANT: a venv folder is NOT portable. Its pyvenv.cfg hard-codes the
REM absolute path of the Python interpreter it was created with, so a venv
REM copied from another machine (or a project folder that has been moved) is
REM broken even though all files are present. We therefore verify the venv can
REM actually run AND has the required dependencies, then rebuild it if not.
set "VENV_MISSING=1"
set "VENV_BROKEN=0"
set "DEPS_MISSING=0"
if exist "venv\Scripts\python.exe" (
    set "VENV_MISSING=0"
    "venv\Scripts\python.exe" -c "import sys" >nul 2>&1 || set "VENV_BROKEN=1"
    "venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 || set "VENV_BROKEN=1"
    "venv\Scripts\python.exe" -c "import flask, openpyxl, requests" >nul 2>&1 || set "DEPS_MISSING=1"
)

if "%VENV_MISSING%"=="1" goto :rebuild_venv
if "%VENV_BROKEN%"=="1" goto :rebuild_venv
if "%DEPS_MISSING%"=="1" goto :install_deps
goto :venv_ready

:rebuild_venv
if exist "venv" echo [SETUP] Found a broken venv (copied from another machine or corrupted). Recreating...
echo [SETUP] Creating virtual environment...
py -3 -m venv venv --clear 2>nul || python -m venv venv --clear
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Failed to create virtual environment.
    echo         Please install Python 3.10+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during installation, then retry.
    pause
    exit /b 1
)

:install_deps
echo [SETUP] Installing dependencies, please wait...
set "REQ=requirements.txt"
if not exist "%REQ%" set "REQ=..\requirements.txt"
"venv\Scripts\python.exe" -m pip install -r "%REQ%" -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Check your network and retry.
    pause
    exit /b 1
)

:venv_ready

REM ---------- Open browser automatically once server is ready (curl probe bypasses system proxy) ----------
start "" powershell -NoProfile -WindowStyle Hidden -Command "$ok=$false;for($i=0;$i -lt 450;$i++){$c=& curl.exe -s --noproxy '*' --max-time 5 -o NUL -w '%%{http_code}' 'http://127.0.0.1:6006/api/folders' 2>$null;if($c -eq '200'){$ok=$true;break};Start-Sleep -Milliseconds 2000};if($ok){Start-Process 'http://127.0.0.1:6006'}"

echo [RUN] Starting server...
echo       The browser will open automatically in a few seconds.
echo       Data import continues in background (progress shown below).
echo       Please KEEP THIS WINDOW OPEN. Press Ctrl+C to stop.
echo.
"venv\Scripts\python.exe" app.py

echo.
echo [INFO] Server stopped.
pause
