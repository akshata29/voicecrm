@echo off
rem Opens the public door for the phone line: a Microsoft dev tunnel to the phone INGRESS port only (default 8011), which serves
rem nothing but the Event Grid and Call Automation webhooks. Never tunnel the API port (8010): the API has no sign-in.
rem The tunnel is persistent, so its https address stays the same between runs; put it in .env as VOICECRM_PHONE_PUBLIC_BASE_URL.
rem   first time:  devtunnel user login   (done below when needed)
setlocal
set TUNNEL=voicecrm-phone
set PORT=8011
if not "%VOICECRM_PHONE_INGRESS_PORT%"=="" set PORT=%VOICECRM_PHONE_INGRESS_PORT%
where devtunnel >nul 2>&1
if errorlevel 1 (
  echo The dev tunnel CLI is not installed: winget install Microsoft.devtunnel
  exit /b 1
)
devtunnel user show >nul 2>&1
if errorlevel 1 devtunnel user login
devtunnel show %TUNNEL% >nul 2>&1
if errorlevel 1 (
  echo Creating the tunnel %TUNNEL% ^(anonymous access: Azure cannot sign in^) for port %PORT% only
  devtunnel create %TUNNEL% --allow-anonymous
  devtunnel port create %TUNNEL% -p %PORT% --protocol http
)
echo Hosting %TUNNEL%: copy the https address that ends in -%PORT%... into VOICECRM_PHONE_PUBLIC_BASE_URL, then scripts\phone_check.bat subscribe
devtunnel host %TUNNEL%
