@echo off
chcp 65001 >nul
title Overmind - Autonomy Log
cd /d "%~dp0"

set "PY=C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

REM Autonomy actions only; heartbeat lines are folded by default.
REM Options: --all (show heartbeat)  --agent (agent ops)  --mail (mailbox)
REM Example: this_bat.bat --all   (double-click from Explorer, not editor)
"%PY%" -u "%~dp0tools\unified_watch.py" %*

echo.
echo   Window exited.
pause
