@echo off
rem Scenario D check: tests the phone line (Communication Services access, the public URL, the seller and the recording notice)
rem and manages the Event Grid subscriptions. Every failure prints the exact fix. Needs VOICECRM_PHONE_* in .env (docs\phone-line.md).
rem   scripts\phone_check.bat                 check the line (the app and the tunnel should be running)
rem   scripts\phone_check.bat line            show the identity the browser dialer calls
rem   scripts\phone_check.bat subscribe       create the Event Grid subscriptions (the app and the tunnel must be running)
rem   scripts\phone_check.bat subscriptions   list them
rem   scripts\phone_check.bat unsubscribe     delete them
setlocal
cd /d "%~dp0..\backend"
if "%~1"=="" (
  ..\.venv\Scripts\python.exe -m voicecrm.cli phone check
) else (
  ..\.venv\Scripts\python.exe -m voicecrm.cli phone %*
)
