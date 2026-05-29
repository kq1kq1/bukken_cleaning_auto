@echo off
chcp 65001 > nul

set "SCRIPT_DIR=%~dp0."
set "PYTHON=C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

cd /d "%SCRIPT_DIR%"

if "%~1"=="" (
    echo CSV file not specified.
    echo Please drag and drop a CSV file onto this bat file.
    pause
    exit /b 1
)

"%PYTHON%" check_csv.py "%~1" 2>> error.log
if %errorlevel% neq 0 (
    echo.
    echo Error occurred:
    type error.log
    echo.
    pause
    exit /b 1
)
