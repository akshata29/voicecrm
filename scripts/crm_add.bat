@echo off
rem Adds a person to the local CRM simulator so a LIVE participant (e.g. the guest in your Teams demo call) resolves to an account.
rem   scripts\crm_add.bat --email guest@example.com --name "Dana Whitfield" --account "Harborview Pension Fund"
rem The account may be an existing one (name or id) or a new name. Only the local simulator is touched, never Dataverse.
setlocal
cd /d "%~dp0..\backend"
..\.venv\Scripts\python.exe -m voicecrm.cli crm-add %*
