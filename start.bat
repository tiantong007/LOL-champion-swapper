@echo off
chcp 65001 >nul
cd /d "%~dp0"
python swapper.py
if %errorlevel% neq 0 (
    echo Error starting. Make sure Python 3.7+ is installed.
    pause
)
