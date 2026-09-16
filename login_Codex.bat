@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo MIMI has not been installed in this folder yet.
    echo Double-click setup_MIMI.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "login_Codex.py"
set "MIMI_LOGIN_EXIT=%errorlevel%"
echo.
if not "%MIMI_LOGIN_EXIT%"=="0" echo Codex sign-in did not complete successfully.
pause
exit /b %MIMI_LOGIN_EXIT%
