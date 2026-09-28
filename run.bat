@echo off
title Orthopedic Radiology Registry (ORTHOREG)
cd /d "%~dp0"

echo =========================================================================
echo    ORTHOPEDIC RADIOLOGY REGISTRY - LAUNCHER
echo =========================================================================

REM Check if virtual environment exists
if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment 'venv' not found in %CD%
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

REM Start Flask application and pyngrok tunnel via runner.py
python runner.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ALERT] Application stopped with an exit code %ERRORLEVEL%.
    pause
)
