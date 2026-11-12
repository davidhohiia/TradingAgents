@echo off
REM LIVE trading: all gates + interactive confirmation on every start. Never use in autostart.
cd /d "%~dp0..\..\.."
"trinity\.venv\Scripts\python.exe" trinity\scripts\run_live.py
pause
