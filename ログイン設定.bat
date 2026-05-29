@echo off
chcp 65001 > nul
set "SCRIPT_DIR=%~dp0."
set "PYTHON=C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
cd /d "%SCRIPT_DIR%"
echo Login setup for SKYHRS and PITAT.
echo A browser will open. Log in manually, then press Enter in this window.
"%PYTHON%" login_setup.py
pause
