@echo off
rem Runs the backend test suite and the frontend type-check + build.
setlocal
cd /d "%~dp0..\backend"
..\.venv\Scripts\python.exe -m pytest -q || exit /b 1
cd /d "%~dp0..\frontend"
call npm run typecheck || exit /b 1
call npm run build || exit /b 1
echo All checks passed.
