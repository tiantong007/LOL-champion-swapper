@echo off
chcp 65001 >nul
cd /d "%~dp0"

pip install -r requirements.txt >nul 2>&1

python swapper.py
pause
