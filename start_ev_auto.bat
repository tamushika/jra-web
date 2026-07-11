@echo off
title EV Race Monitor (auto)
cd /d "%~dp0"

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

rem Weekend auto-run: starts the server AND kicks off analysis automatically.
"%PYEXE%" -X utf8 jra_ev.py --auto-start >> "%~dp0ev_monitor.log" 2>&1
