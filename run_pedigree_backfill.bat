@echo off
title Pedigree backfill (netkeiba)
cd /d "%~dp0"

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo.
echo  Pedigree backfill: one daily chunk (3800 requests, ~2h).
echo  Safe to close and rerun tomorrow - progress is saved.
echo.

"%PYEXE%" -X utf8 backfill_pedigree_netkeiba.py --phase all --limit 3800 --sleep 1.8

echo.
pause
