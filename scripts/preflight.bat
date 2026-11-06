@echo off
rem Checks .env and then calls every live Azure service for real (speech, language, model, ...), reporting what is ready
rem for each demo scenario. Add --burst 16 to also test concurrent load. Costs a few cents.
setlocal
cd /d "%~dp0..\backend"
..\.venv\Scripts\python.exe -m voicecrm.cli preflight %*
