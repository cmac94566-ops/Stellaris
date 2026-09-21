@echo off
chcp 65001 >nul
title Overmind - Mail Alert
cd /d "%~dp0"

set "PY=C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

REM Beep + title flash when a new message arrives (both inboxes).
"%PY%" -u "%~dp0tools\mail_alert.py" %*

echo.
echo   Window exited.
pause
