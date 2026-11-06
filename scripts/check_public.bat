@echo off
rem Run before every push to the public repository: fails if a customer name, tenant id, internal host or secret is about to be published.
rem   scripts\check_public.bat
rem It scans what git would publish (tracked files plus untracked files that are not ignored). Your own deny-list lives in
rem .deny-terms.local (git-ignored: one "label: regex" per line, "label!: regex" for case-sensitive); see tools\check_public.py.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe tools\check_public.py %*
