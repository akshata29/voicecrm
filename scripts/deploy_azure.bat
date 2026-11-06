@echo off
rem ====================================================================================================================
rem VoiceCRM -> Azure.  Wrapper around "azd up": provisions infra\main.bicep, builds the image remotely in ACR, deploys it.
rem
rem   scripts\deploy_azure.bat             deploy; asks for an explicit go-ahead first
rem   scripts\deploy_azure.bat /preview    what-if only (azd provision --preview); creates nothing
rem   scripts\deploy_azure.bat /yes        skip the prompt (automation, only after the owner has approved the spend)
rem   any other argument is passed to azd up, e.g.  scripts\deploy_azure.bat -e voicecrm-dev
rem
rem Exit codes: 0 ok, 1 prerequisite or sign-in problem, 2 not confirmed, otherwise the exit code of azd.
rem ====================================================================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."

set "PREVIEW="
set "ASSUME_YES="
set "AZD_ARGS="

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/?" goto usage
if /i "%~1"=="/help" goto usage
if /i "%~1"=="/preview" (
  set "PREVIEW=1"
  shift
  goto parse_args
)
if /i "%~1"=="/yes" (
  set "ASSUME_YES=1"
  shift
  goto parse_args
)
set AZD_ARGS=%AZD_ARGS% %1
shift
goto parse_args

:usage
echo Usage: scripts\deploy_azure.bat [/preview] [/yes] [azd up arguments]
echo   /preview   what-if only, creates nothing
echo   /yes       skip the confirmation prompt ^(automation, after approval^)
exit /b 0

:args_done
if not exist azure.yaml (
  echo ERROR: azure.yaml not found in %CD%. Run this script from a VoiceCRM checkout.
  exit /b 1
)

where azd >nul 2>nul
if errorlevel 1 (
  echo ERROR: the Azure Developer CLI ^(azd^) was not found.  Install it:  winget install microsoft.azd
  exit /b 1
)
where az >nul 2>nul
if errorlevel 1 (
  echo ERROR: the Azure CLI ^(az^) was not found.  Install it:  winget install Microsoft.AzureCLI
  exit /b 1
)

echo Checking Azure sign-in ^(az and azd^) - this can take a few seconds ...
set "AZ_SUB_NAME="
set "AZ_SUB_ID="
for /f "usebackq tokens=1,* delims=|" %%A in (`az account show --only-show-errors --query "join('^|', [id, name])" -o tsv 2^>nul`) do (
  set "AZ_SUB_ID=%%A"
  set "AZ_SUB_NAME=%%B"
)
if not defined AZ_SUB_ID (
  echo ERROR: the Azure CLI is not signed in.  Run:  az login
  exit /b 1
)
call azd auth login --check-status >nul 2>nul
if errorlevel 1 (
  echo ERROR: azd is not signed in.  Run:  azd auth login
  exit /b 1
)
echo   az subscription : !AZ_SUB_NAME! ^(!AZ_SUB_ID!^)
echo   azd uses the subscription stored in its environment and asks on first run; check it before you confirm.

if defined PREVIEW goto do_preview

echo.
echo  ====================================================================================
echo   WARNING: this deploys VoiceCRM to AZURE and creates BILLABLE resources:
echo     Container Apps environment and app, Container Registry, Azure AI Services with a
echo     model deployment, Azure AI Search, Storage, Log Analytics, Application Insights,
echo     and Cosmos DB only if VOICECRM_DEPLOY_COSMOS=true.
echo   Several of them accrue cost while idle ^(for example AI Search and the always-on
echo   Container App replica^). Do not run this without the owner's explicit go-ahead.
echo   Remove everything again with:  azd down --purge
echo  ====================================================================================
echo.

if defined ASSUME_YES goto do_deploy
set "CONFIRM="
set /p "CONFIRM=Type DEPLOY to create these resources, anything else cancels: "
if /i not "!CONFIRM!"=="DEPLOY" (
  echo Cancelled. Nothing was created.
  exit /b 2
)

:do_deploy
echo.
echo Running: azd up%AZD_ARGS%
call azd up%AZD_ARGS%
set "AZD_RC=%ERRORLEVEL%"
if not "%AZD_RC%"=="0" (
  echo.
  echo azd up failed with exit code %AZD_RC%. Resources created before the failure still bill: see docs\infrastructure.md ^(Teardown^).
  exit /b %AZD_RC%
)

set "APP_URL="
for /f "usebackq delims=" %%U in (`azd env get-value VOICECRM_APP_URL 2^>nul`) do set "APP_URL=%%U"
echo.
echo Deployed.
if defined APP_URL echo   App: !APP_URL!
echo   The app has no end-user sign-in; restrict access with VOICECRM_ALLOWED_IP_RANGES before sharing the URL.
echo   Remove everything and stop the charges:  azd down --purge
exit /b 0

:do_preview
echo.
echo Preview only: azd provision --preview ^(what-if^). Nothing is created.
call azd provision --preview%AZD_ARGS%
exit /b %ERRORLEVEL%
