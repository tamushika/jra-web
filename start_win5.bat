@echo off
title WIN5 Prediction App
cd /d "%~dp0"

echo.
echo  Starting WIN5 prediction app...
echo.

rem Resolve real Python (avoid the Microsoft Store stub)
rem repo venv first (works on a laptop without the Python Install Manager shim)
set "PYEXE=%~dp0..\Scripts\python.exe"
if exist "%PYEXE%" goto found
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

"%PYEXE%" -c "import flask, flask_cors, requests, bs4, pandas" 2>nul
if errorlevel 1 (
    echo  Installing required packages...
    "%PYEXE%" -m pip install flask flask-cors requests beautifulsoup4 pandas
    echo.
)

"%PYEXE%" jra_win5.py

echo.
echo  Server stopped. (Check messages above if there was an error)
pause
