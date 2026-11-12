@echo off
REM Activates the emergency stop (no new orders). Options: --status  --clear  --confirm-drawdown
cd /d "%~dp0..\..\.."
"trinity\.venv\Scripts\python.exe" trinity\scripts\emergency_stop.py %*
pause
