@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -c "import webview" 2>nul
if %errorlevel% neq 0 (
    echo [Setup] Installing pywebview (one-time)...
    pip install pywebview
)

python swapper.py
if %errorlevel% neq 0 (
    echo [Error] Make sure Python 3.7+ is installed.
    pause
)
