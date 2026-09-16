@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 setup_MIMI.py
set "MIMI_SETUP_EXIT=%errorlevel%"
goto setup_finished

:try_python
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During installation, select "Add Python to PATH".
    pause
    exit /b 1
)
python setup_MIMI.py
set "MIMI_SETUP_EXIT=%errorlevel%"

:setup_finished
echo.
if not "%MIMI_SETUP_EXIT%"=="0" echo MIMI setup did not complete successfully.
pause
exit /b %MIMI_SETUP_EXIT%
