@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

:: Kill existing instance via PID file (tree kill)
if exist "data\nanoclaw.pid" (
    set /p OLD_PID=<"data\nanoclaw.pid"
    echo [nanoclaw] Killing existing instance PID !OLD_PID! + children...
    taskkill /F /T /PID !OLD_PID! >nul 2>&1
    del "data\nanoclaw.pid" >nul 2>&1
    timeout /t 3 /nobreak >nul
)

title nanoclaw-py [Ape]

echo [nanoclaw] Starting...
nanoclaw.exe
pause
