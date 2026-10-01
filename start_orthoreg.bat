@echo off
title Orthopedic Radiology Registry (ORTHOREG)
cd /d "%~dp0"

echo =========================================================================
echo    ORTHOPEDIC RADIOLOGY REGISTRY - LAUNCHER
echo =========================================================================

if not defined NGROK_AUTHTOKEN (
    set "NGROK_AUTHTOKEN=3JzZPYwP2A7gb9Q6xyBGbNtsDBP_2fpuxh8kAvYS4rok9ke7D"
)

REM Priority 1: Check for isolated portable environment in python_local
if exist "python_local\python.exe" (
    echo [INFO] Detected isolated Python environment at .\python_local\python.exe
    if "%~1"=="app" (
        echo [INFO] Launching Flask application directly: app.py
        .\python_local\python.exe app.py
    ) else if exist "runner.py" (
        echo [INFO] Starting OrthoReg application and pyngrok tunnel: runner.py
        .\python_local\python.exe runner.py
    ) else (
        echo [INFO] Starting OrthoReg application: app.py
        .\python_local\python.exe app.py
    )
    goto :check_exit
)

REM Priority 2: Check for virtual environment venv
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    python runner.py %*
    goto :check_exit
)

echo [ERROR] Neither .\python_local\python.exe nor .\venv was found!
echo Please run setup_orthoreg.ps1 to configure the isolated environment.

:check_exit
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ALERT] Application stopped with exit code %ERRORLEVEL%.
    pause
)
