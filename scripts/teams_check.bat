@echo off
rem Scenario B check: signs in to Microsoft Graph as the Entra app and tests everything live Teams ingestion needs
rem (app sign-in, transcript listing for each organiser, transcript download + speaker names, meeting details).
rem Every failure prints the exact fix. Needs VOICECRM_TEAMS_* in .env (see docs\live-demo.md, scenario B).
rem   scripts\teams_check.bat                 check access
rem   scripts\teams_check.bat subscribe       webhook mode: subscribe Graph to this API (needs VOICECRM_TEAMS_PUBLIC_BASE_URL)
rem   scripts\teams_check.bat subscriptions   list the app's Graph subscriptions
setlocal
cd /d "%~dp0..\backend"
if "%~1"=="" (
  ..\.venv\Scripts\python.exe -m voicecrm.cli teams check
) else (
  ..\.venv\Scripts\python.exe -m voicecrm.cli teams %*
)
