@echo off
cd /d "%~dp0"
start "" /min ollama serve
timeout /t 3 >nul
start "" http://localhost:7860
.venv\Scripts\python -m uvicorn app.server:app --host 127.0.0.1 --port 7860
pause
