@echo off
title JRA Performance Dashboard
cd /d "%~dp0"

rem Resolve real Python (avoid the Microsoft Store stub)
rem repo venv first (works on a laptop without the Python Install Manager shim)
set "PYEXE=%~dp0..\Scripts\python.exe"
if exist "%PYEXE%" goto found
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" -X utf8 jra_perf.py

pause
