@echo off

REM Try standard Chrome paths
SET CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
IF EXIST "%CHROME_PATH%" GOTO FOUND

SET CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
IF EXIST "%CHROME_PATH%" GOTO FOUND

REM Search registry for Chrome path
FOR /F "tokens=2*" %%A IN ('REG QUERY "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul') DO SET CHROME_PATH=%%B
IF EXIST "%CHROME_PATH%" GOTO FOUND

FOR /F "tokens=2*" %%A IN ('REG QUERY "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul') DO SET CHROME_PATH=%%B
IF EXIST "%CHROME_PATH%" GOTO FOUND

echo Chrome not found.
echo Please check your Chrome installation path.
pause
exit /b 1

:FOUND
echo Chrome found: %CHROME_PATH%
echo Starting Chrome with debug port 9222...
SET PROFILE_DIR=%LOCALAPPDATA%\bukken_cleaning_chrome
start "" "%CHROME_PATH%" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%" ^
    "https://sys.arcs.jp/login.html" ^
    "https://www.pitat-cloud.com/login/"
echo Done.
