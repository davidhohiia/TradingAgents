@echo off
REM Start TRINITY in PAPER mode. Safe for autostart: it never starts live trading.
cd /d "%~dp0..\..\.."
:loop
"trinity\.venv\Scripts\python.exe" trinity\scripts\run_paper.py
REM exit code 2 = another instance is already running -> do not loop
if %ERRORLEVEL%==2 exit /b 2
echo TRINITY exited with code %ERRORLEVEL%. Restarting in 30 seconds (Ctrl+C to abort)...
timeout /t 30 /nobreak >nul
goto loop
