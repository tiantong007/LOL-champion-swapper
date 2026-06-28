@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  ARAM 秒换英雄 - 打包工具
echo ============================================
echo.

pip install pyinstaller pywebview 2>nul
if %errorlevel% neq 0 (
    echo [Error] pip install failed. Make sure Python and pip are installed.
    pause
    exit /b 1
)

echo [Build] Packaging with PyInstaller...
pyinstaller --onefile --name "ARAM秒换英雄" --noconsole --add-data "LICENSE;." swapper.py

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo  Success! dist\ARAM秒换英雄.exe
    echo ============================================
) else (
    echo [Error] Build failed.
)
pause
