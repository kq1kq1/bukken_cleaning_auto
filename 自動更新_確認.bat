@echo off
chcp 65001 > nul
set "SCRIPT_DIR=%~dp0."
set "PYTHON=C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
cd /d "%SCRIPT_DIR%"
echo [DRY-RUN] Showing what would be updated. No actual changes.
"%PYTHON%" web_updater.py %*
pause
