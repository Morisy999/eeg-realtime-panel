@echo off
cd /d "%~dp0"
title EEG Panel

set "PYEXE="
if exist "E:\tools\python.exe" (
    "E:\tools\python.exe" -c "import numpy,scipy,bleak" >nul 2>nul
    if not errorlevel 1 set "PYEXE=E:\tools\python.exe"
)

if not defined PYEXE (
    python -c "import numpy,scipy,bleak" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    echo.
    echo [ERROR] No usable Python found.
    echo Need Python 3.10+ with: bleak numpy scipy
    echo.
    echo Run this first:
    echo   pip install bleak numpy scipy -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo Using: %PYEXE%
echo Starting EEG panel...
echo.
"%PYEXE%" eeg_gui.py
set RC=%errorlevel%

echo.
if not "%RC%"=="0" (
    echo ==========================================
    echo Exit code: %RC%
    echo Please send a screenshot of the messages above.
    echo ==========================================
) else (
    echo Closed normally.
)
pause
