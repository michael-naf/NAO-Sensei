@echo off
REM Double-click this to bring up the full NAO Sensei system: Ollama, the
REM NAO bridge (redeployed + restarted fresh each time), then the main app
REM in its own window. All the actual logic lives in start_nao_sensei.ps1
REM (PowerShell handles the SSH/quoting far more reliably than plain
REM cmd.exe batch syntax does) - this file is just the entry point.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_nao_sensei.ps1"
echo.
pause
