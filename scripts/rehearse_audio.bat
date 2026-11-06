@echo off
rem Scenario A rehearsal: sends the recorded calls through the real Azure path (speech-to-text, language service, model)
rem in-process, with a throwaway state folder, and prints word error rate, extraction accuracy and timings per call.
rem   scripts\rehearse_audio.bat                          every recorded call
rem   scripts\rehearse_audio.bat pb-teams-equity-class-c -v    one call, with what was extracted
rem   scripts\rehearse_audio.bat --meeting -v                 the eight Teams meeting recordings (data\audio\meeting), scored against their answer keys
rem   scripts\rehearse_audio.bat --meeting --via transcript   skip Speech: score the extraction on the script text (cheaper)
setlocal
cd /d "%~dp0..\backend"
..\.venv\Scripts\python.exe ..\tools\live_check.py %*
