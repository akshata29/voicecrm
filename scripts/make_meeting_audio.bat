@echo off
rem Scenario B prep: speaks the eight sales calls (Aisha West and Dana Whitfield) into data\audio\meeting, to play into a Teams meeting.
rem   scripts\make_meeting_audio.bat                         every script in data\meeting: <name>.wav, .txt (script with times), .vtt (Teams-style transcript)
rem   scripts\make_meeting_audio.bat --list                  the play list: order, length and what each call shows
rem   scripts\make_meeting_audio.bat competitor-call --force    just that one, overwriting what is there
rem   scripts\make_meeting_audio.bat class-c-call --stems       also each voice alone on the same timeline (two devices, two participants)
rem Existing recordings are skipped unless you pass --force. Check them against the real services with: scripts\rehearse_audio.bat --meeting
rem See docs\live-demo.md, section 3.4.
setlocal
cd /d "%~dp0..\backend"
..\.venv\Scripts\python.exe ..\tools\gen_meeting_audio.py %*
