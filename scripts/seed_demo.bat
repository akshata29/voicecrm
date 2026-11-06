@echo off
rem Processes a representative set of scripted calls so the inbox has items to review.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe -m voicecrm.cli seed-demo
