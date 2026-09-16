@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo MIMI has not been installed in this folder yet.
    echo Double-click setup_MIMI.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "lauch_MIMI.py" %*
if errorlevel 1 pause
