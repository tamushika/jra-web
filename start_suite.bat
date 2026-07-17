@echo off
title JRA Suite (EV + WIN5 + Perf, port 5005)
cd /d "%~dp0"

echo.
echo  Starting integrated JRA suite (EV monitor + WIN5 + Perf dashboard)...
echo  Single process, single port (5005). Do NOT also run start_ev.bat /
echo  start_win5.bat / start_perf.bat at the same time (double notifications).
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

"%PYEXE%" jra_suite.py

echo.
echo  Server stopped. (Check messages above if there was an error)
pause
