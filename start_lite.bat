@echo off
chcp 65001 > nul
title JRA解析ツール 簡易版
cd /d "%~dp0"

echo.
echo  JRA解析ツール 簡易版 を起動します...
echo  (AI機能・DB機能なし / インターネット接続が必要)
echo.

REM 初回のみ依存パッケージをインストール
python -c "import flask, flask_cors, requests, bs4" 2>nul
if errorlevel 1 (
    echo  必要なパッケージをインストール中...
    pip install flask flask-cors requests beautifulsoup4
    echo.
)

python jra_lite.py

echo.
echo  サーバーが停止しました。
pause
