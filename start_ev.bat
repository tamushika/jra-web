@echo off
chcp 65001 > nul
title 期待値レース監視
cd /d "%~dp0"

echo.
echo  期待値レース監視 を起動します...
echo  (ローカル専用 / 発走15分前・5分前にブラウザ通知)
echo.

REM 必要パッケージ確認（初回のみインストール）
python -c "import flask, flask_cors, requests, bs4, pandas, numpy" 2>nul
if errorlevel 1 (
    echo  必要なパッケージをインストール中...
    pip install flask flask-cors requests beautifulsoup4 pandas numpy
    echo.
)

python jra_ev.py

echo.
echo  サーバーが停止しました。
pause
