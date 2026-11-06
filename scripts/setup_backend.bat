@echo off
rem Creates the Python virtual environment and installs the backend (editable) with dev tools.
setlocal
cd /d "%~dp0.."
if not exist .venv (
  python -m venv .venv || goto :fail
)
call .venv\Scripts\python.exe -m pip install --upgrade pip || goto :fail
call .venv\Scripts\python.exe -m pip install -e "backend[dev]" || goto :fail
if not exist .env copy .env.example .env >nul
echo.
echo Backend ready. Next: scripts\run_backend.bat  (and scripts\run_frontend.bat in a second window)
exit /b 0
:fail
echo Setup failed.
exit /b 1
