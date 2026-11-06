@echo off
rem Opens the backend and the frontend in two windows.
setlocal
cd /d "%~dp0.."
start "VoiceCRM backend" cmd /k scripts\run_backend.bat
start "VoiceCRM frontend" cmd /k scripts\run_frontend.bat
echo Backend: http://127.0.0.1:8010/docs    Frontend: http://localhost:5173
