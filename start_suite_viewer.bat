@echo off
title JRA Suite (Viewer Mode / 閲覧モード, port 5005)
cd /d "%~dp0"

rem SPEC-T83: 閲覧モード起動。監視ループ・通知・prospective書き込みは行わない。
rem 2台目PC (ノート) でデスクトップと同時にアプリを動かすための専用起動スクリプト。
rem 通常モードの start_suite.bat はこのファイルの追加に伴っても変更していない。
set "JRA_VIEWER_MODE=1"

echo.
echo  Starting JRA suite in VIEWER MODE (monitor loops and notifications are OFF).
echo  閲覧モードで起動します。監視ループと通知は停止しています。
echo  Single process, single port (5005). Do NOT also run start_ev.bat /
echo  start_win5.bat / start_perf.bat / start_suite.bat at the same time.
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

"%PYEXE%" -c "import flask, flask_cors, requests, bs4, pandas, numpy" 2>nul
if errorlevel 1 (
    echo  Installing required packages...
    "%PYEXE%" -m pip install flask flask-cors requests beautifulsoup4 pandas numpy
    echo.
)

"%PYEXE%" jra_suite.py --viewer

echo.
echo  Server stopped. (Check messages above if there was an error)
pause
