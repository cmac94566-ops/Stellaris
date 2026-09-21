@echo off
REM ==========================================================
REM  Stellaris Overmind - one-click launcher
REM  NOTE: keep this file ASCII-only (Windows .bat encoding)
REM ==========================================================
setlocal

set OLLAMA_MODELS=D:\Ollama\models
set OLLAMA_HOST=127.0.0.1:11434
set OLLAMA_KEEP_ALIVE=10m

set OLLAMA_DIR=C:\Users\<user>\AppData\Local\Programs\Ollama
set PATH=%PATH%;%OLLAMA_DIR%

REM --- the bundled GUI app fails to bring up its server and then holds
REM     port 11434 hostage; always remove it before starting our own ---
taskkill /F /IM "ollama app.exe" >nul 2>nul

REM --- make sure the Ollama server is running -----------------
curl -s -m 3 http://localhost:11434/api/version >nul 2>nul
if errorlevel 1 (
    echo [overmind] Starting Ollama server ^(minimized^)...
    start "Ollama Server" /MIN "%OLLAMA_DIR%\ollama.exe" serve
    echo [overmind] Waiting for the server to come up...
    timeout /t 12 /nobreak >nul
) else (
    echo [overmind] Ollama server already running.
)

REM --- start the engine --------------------------------------
cd /d C:\Users\<user>\.zcode\workspace\default\Stellaris_Overmind
echo [overmind] Launching engine TUI...
py -3.12 -m engine.main --console

endlocal
