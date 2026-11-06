@echo off
rem Pushes a batch of calls through the running API and reports throughput, latency, tokens, cost and review load.
rem   scripts\bulk_load.bat --count 800                 800 calls as one burst
rem   scripts\bulk_load.bat --count 40 --via audio      real recordings through Azure Speech
rem   scripts\bulk_load.bat --dry-run --count 800       sizing only, sends nothing
rem Run scripts\run_bulk_backend.bat first (or any backend on :8010). See tools\bulk_load.py for all options.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe tools\bulk_load.py %*
