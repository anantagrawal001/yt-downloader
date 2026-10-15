@echo off
setlocal enabledelayedexpansion
title OmniStream YouTube Downloader

echo ==========================================================
echo  OmniStream - YouTube Video ^& Playlist Downloader
echo ==========================================================
echo.

:: Set current directory to the script's directory
cd /d "%~dp0"

:: Locate Python executable
set "PY_CMD="

if exist "C:\Users\Anant\AppData\Local\Programs\Python\Python311\python.exe" (
    set "PY_CMD=C:\Users\Anant\AppData\Local\Programs\Python\Python311\python.exe"
) else if exist "C:\Users\Anant\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PY_CMD=C:\Users\Anant\AppData\Local\Programs\Python\Python312\python.exe"
) else (
    where python >nul 2>nul
    if !errorlevel! equ 0 (
        set "PY_CMD=python"
    ) else (
        where py >nul 2>nul
        if !errorlevel! equ 0 (
            set "PY_CMD=py"
        )
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python was not found on your system!
    echo Please install Python or add it to PATH.
    echo.
    pause
    exit /b 1
)

echo Using Python: "%PY_CMD%"
echo.

echo Checking requirements...
"%PY_CMD%" -c "import yt_dlp, flask" >nul 2>&1
if %errorlevel% neq 0 (
    echo Dependencies missing. Installing yt-dlp and flask...
    "%PY_CMD%" -m pip install yt-dlp flask
)

echo Starting OmniStream Downloader Web App...
echo.
"%PY_CMD%" app.py

echo.
echo Application stopped.
pause
