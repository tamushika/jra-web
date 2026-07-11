@echo off
title JRA Performance Dashboard
cd /d "%~dp0"

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" -X utf8 jra_perf.py

pause
