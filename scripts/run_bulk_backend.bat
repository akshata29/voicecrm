@echo off
rem Starts the API on http://127.0.0.1:8010 in the "bulk" profile for the 800-call scenario:
rem   - its own state folder (.state-bulk), so the inbox you prepared in .state is untouched and the audience sees an empty inbox fill up
rem   - more calls processed in parallel (VOICECRM_MAX_CONCURRENCY, default 32) and more privacy-filter requests in flight
rem     (a quota-bound deployment gains nothing from more: ~280 calls/min measured at 32 on the 800K-TPM gpt-5.4-mini one;
rem     ~370 calls/min at 64 on a 5M-TPM deployment - set VOICECRM_MAX_CONCURRENCY=64 first when you have that much quota)
rem Stop the normal backend first (same port). Everything else comes from .env. Delete .state-bulk afterwards to reset.
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Run scripts\setup_backend.bat first.
  exit /b 1
)
if "%VOICECRM_STATE_DIR%"=="" set VOICECRM_STATE_DIR=%CD%\.state-bulk
if "%VOICECRM_MAX_CONCURRENCY%"=="" set VOICECRM_MAX_CONCURRENCY=32
if "%VOICECRM_PII_CONCURRENCY%"=="" set VOICECRM_PII_CONCURRENCY=32
rem The model is the deployment named in .env (VOICECRM_FOUNDRY_DEPLOYMENT): pick a high-quota, low-cost one for a bulk run.
rem A deployment with less quota than the run needs is throttled and retried, so the run finishes later but still finishes.
echo Bulk profile: state=%VOICECRM_STATE_DIR%  parallel calls=%VOICECRM_MAX_CONCURRENCY%  privacy requests=%VOICECRM_PII_CONCURRENCY%
.venv\Scripts\python.exe -m voicecrm.cli doctor
.venv\Scripts\python.exe -m voicecrm.cli serve --port 8010
