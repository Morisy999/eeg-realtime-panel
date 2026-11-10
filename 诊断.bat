@echo off
cd /d "%~dp0"
title EEG Panel - Diagnostics

echo ==========================================
echo  1. Python candidates
echo ==========================================
where python 2>nul
where pythonw 2>nul
where py 2>nul
echo.

set "PYEXE="
if exist "E:\tools\python.exe" set "PYEXE=E:\tools\python.exe"
if not defined PYEXE (
    python -c "import numpy,scipy,bleak" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] No Python with numpy/scipy/bleak.
    pause
    exit /b 1
)

echo ==========================================
echo  2. Version
echo ==========================================
"%PYEXE%" -V
echo.

echo ==========================================
echo  3. Packages
echo ==========================================
"%PYEXE%" -c "import importlib.util as u; [print(n, 'OK' if u.find_spec(n) else 'MISSING') for n in ('numpy','scipy','bleak','tkinter')]"
echo.

echo ==========================================
echo  4. Files
echo ==========================================
if exist eeg_gui.py (echo   eeg_gui.py OK) else (echo   eeg_gui.py MISSING)
if exist eye_events.py (echo   eye_events.py OK) else (echo   eye_events.py MISSING)
echo.

echo ==========================================
echo  5. Demo run (close the window to finish)
echo ==========================================
"%PYEXE%" eeg_gui.py --demo --warmup 3
echo.
echo Exit code: %errorlevel%
echo.
pause
