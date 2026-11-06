@echo off
rem Scenario A prep: speaks the scripted calls with Azure text-to-speech into data\audio\live (one WAV per call).
rem   scripts\make_audio.bat                    the recommended 8-call demo set
rem   scripts\make_audio.bat --all              every call
rem   scripts\make_audio.bat --list             what exists
rem Then run scripts\rehearse_audio.bat to check the whole live path before the demo.
setlocal
cd /d "%~dp0..\backend"
..\.venv\Scripts\python.exe ..\tools\gen_call_audio.py %*
