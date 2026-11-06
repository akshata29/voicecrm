@echo off
rem Starts the Vite dev server on http://localhost:5173 (proxies /api to the backend on :8010).
rem Use "run_frontend.bat mock" to run against canned data without the backend.
setlocal
cd /d "%~dp0..\frontend"
if not exist node_modules (
  call npm install || exit /b 1
)
if /i "%1"=="mock" (
  call npm run dev:mock
) else (
  call npm run dev
)
