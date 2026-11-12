@echo off
cd /d "%~dp0..\..\.."
"trinity\.venv\Scripts\python.exe" -m streamlit run trinity\dashboard\app.py --server.address=127.0.0.1 --server.port=8501
