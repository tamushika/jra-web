@echo off
title EV Race Monitor
cd /d "%~dp0"

echo.
echo  Starting EV race monitor...
echo  (Notifies at T-15min / T-5min before each race)
echo.

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if exist "%PYEXE%" goto found

where py >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py"
    goto found
)
set "PYEXE=python"

:found
echo  Python: %PYEXE%
"%PYEXE%" --version
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found. Install from https://www.python.org/
    pause
    exit /b 1
)

"%PYEXE%" -c "import flask, flask_cors, requests, bs4, pandas, numpy" 2>nul
if errorlevel 1 (
    echo  Installing required packages...
    "%PYEXE%" -m pip install flask flask-cors requests beautifulsoup4 pandas numpy
    echo.
)

"%PYEXE%" jra_ev.py

echo.
echo  Server stopped. (Check messages above if there was an error)
pause
