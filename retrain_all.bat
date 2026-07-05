@echo off
title WIN5 model retrain pipeline
cd /d "%~dp0"

rem ================================================================
rem  Semiannual retrain pipeline (TARGET export NOT required)
rem  Training data is extended automatically from Neon (weekly
rem  registrations with horse names). Requires DATABASE_URL in .env.
rem
rem  Standard-time window: slide forward by 1 year every January by
rem  editing STD_FROM/STD_TO below and uncommenting the rebuild step.
rem ================================================================
set STD_FROM=2016
set STD_TO=2023

rem Resolve real Python (avoid the Microsoft Store stub)
set "PYEXE=%LocalAppData%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo.
echo  [1/5] Backfilling final win odds from netkeiba (all runners) ...
"%PYEXE%" backfill_odds_netkeiba.py || goto :fail

echo.
echo  [2/5] Extending ability.db from Neon (2026+ rows with horse names) ...
"%PYEXE%" extend_ability_from_neon.py || goto :fail

rem -- Optional: full rebuild to slide the standard-time window (January).
rem    Uses the frozen local 1980.csv; run extend step again afterwards.
rem "%PYEXE%" build_ability_db.py --std-from %STD_FROM% --std-to %STD_TO% || goto :fail
rem "%PYEXE%" extend_ability_from_neon.py || goto :fail

echo.
echo  [3/5] Regenerating track variants (full) ...
"%PYEXE%" gen_track_variants.py || goto :fail

echo.
echo  [4/5] Retraining ML model (check the OOS validation output!) ...
"%PYEXE%" backtest_ml.py --write || goto :fail

echo.
echo  [5/5] Re-measuring coverage + WIN5 simulation with the new model ...
"%PYEXE%" backtest_win5.py --from 20210101 --ml --write || goto :fail

echo.
echo  Done. Review the OOS results above, then commit and push:
echo    git add -A ^&^& git commit -m "Retrain ML model" ^&^& git push
echo.
pause
exit /b 0

:fail
echo.
echo  [ERROR] Pipeline failed. Check messages above.
pause
exit /b 1
