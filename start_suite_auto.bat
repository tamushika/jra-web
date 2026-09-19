@echo off
title JRA Suite (auto, port 5005)
cd /d "%~dp0"

rem Resolve a real CPython. Order: project venv (plain exe, works headless and
rem has every dependency incl. psycopg2) -> Python Install Manager shim.
rem Never fall back to bare "python": the Microsoft Store stub just prints
rem "Python" and exits, which is exactly what suite_monitor.log showed on
rem 2026-09-19 08:30 (nothing was analysed until the manual start at 10:28).
set "PYEXE=%~dp0..\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" (
    echo [%date% %time%] [auto-start] ERROR: no Python executable found >> "%~dp0suite_monitor.log"
    exit /b 1
)

rem Weekend auto-run for the integrated suite: starts the single-port server
rem (EV monitor + WIN5 + Perf) AND kicks off EV analysis automatically.
rem Replaces start_ev_auto.bat after the T38 cutover (SPEC-T38 section 5).
rem Do NOT keep both this and start_ev_auto.bat registered in Task Scheduler.
rem If the suite is already running, jra_suite.py --auto-start asks it over
rem HTTP to start the analysis and exits 0 instead of dying on the port guard
rem (SPEC-T78).
echo [%date% %time%] [auto-start] launching %PYEXE% >> "%~dp0suite_monitor.log"
"%PYEXE%" -X utf8 jra_suite.py --auto-start >> "%~dp0suite_monitor.log" 2>&1
echo [%date% %time%] [auto-start] exited with code %ERRORLEVEL% >> "%~dp0suite_monitor.log"
