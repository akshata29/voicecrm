@echo off
rem Starts the API on http://127.0.0.1:8010 (also serves the built frontend when frontend\dist exists).
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Run scripts\setup_backend.bat first.
  exit /b 1
)
.venv\Scripts\python.exe -m voicecrm.cli doctor
.venv\Scripts\python.exe -m voicecrm.cli serve --port 8010
