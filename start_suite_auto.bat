@echo off
title JRA Suite (auto, port 5005)
cd /d "%~dp0"

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

rem Weekend auto-run for the integrated suite: starts the single-port server
rem (EV monitor + WIN5 + Perf) AND kicks off EV analysis automatically.
rem Replaces start_ev_auto.bat after the T38 cutover (SPEC-T38 section 5).
rem Do NOT keep both this and start_ev_auto.bat registered in Task Scheduler.
"%PYEXE%" -X utf8 jra_suite.py --auto-start >> "%~dp0suite_monitor.log" 2>&1
