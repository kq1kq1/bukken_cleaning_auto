@echo off
chcp 65001 > nul
set "SCRIPT_DIR=%~dp0."
set "PYTHON=C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
cd /d "%SCRIPT_DIR%"
echo 要確認物件の印刷用HTMLをPDF化（A4・モノクロ）して既定プリンタへ送信します。
"%PYTHON%" print_review.py
if %errorlevel% neq 0 pause
