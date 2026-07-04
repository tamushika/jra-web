@echo off
title WIN5 model retrain pipeline
cd /d "%~dp0"

rem ================================================================
rem  Semiannual retrain pipeline (run after replacing 1980.csv)
rem  Standard-time window: slide forward by 1 year every January.
rem ================================================================
set STD_FROM=2016
set STD_TO=2023

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo.
echo  [0/5] Checking 1980.csv ...
if not exist "1980.csv" (
    echo  [ERROR] 1980.csv not found. Export from TARGET first.
    pause
    exit /b 1
)
for %%F in (1980.csv) do echo         1980.csv last modified: %%~tF
echo         (make sure this is the NEW export before continuing)
pause

echo.
echo  [1/5] Rebuilding ability.db + standard times (%STD_FROM%-%STD_TO%) ...
"%PYEXE%" build_ability_db.py --std-from %STD_FROM% --std-to %STD_TO% || goto :fail

echo.
echo  [2/5] Regenerating track variants (full) ...
"%PYEXE%" gen_track_variants.py || goto :fail

echo.
echo  [3/5] Retraining ML model (check the OOS validation output!) ...
"%PYEXE%" backtest_ml.py --write || goto :fail

echo.
echo  [4/5] Re-measuring coverage + WIN5 simulation with the new model ...
"%PYEXE%" backtest_win5.py --from 20210101 --ml --write || goto :fail

echo.
echo  [5/5] Done. Review the OOS results above, then commit and push:
echo         git add -A ^&^& git commit -m "Retrain ML model" ^&^& git push
echo.
pause
exit /b 0

:fail
echo.
echo  [ERROR] Pipeline failed. Check messages above.
pause
exit /b 1
