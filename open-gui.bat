@echo off
setlocal
cd /d "%~dp0"
if not exist ".gui-token" goto not_ready
set /p "TG_GUI_TOKEN="<".gui-token"
if not defined TG_GUI_TOKEN goto not_ready
if /i "%~1"=="--dry-run" goto print_url
start "" "http://127.0.0.1:8765/?token=%TG_GUI_TOKEN%"
if errorlevel 1 goto open_failed
exit /b 0

:print_url
echo http://127.0.0.1:8765/?token=%TG_GUI_TOKEN%
exit /b 0

:not_ready
echo GUI is not ready. Run start.bat first.
pause
exit /b 1

:open_failed
echo Could not open the default browser.
echo Open http://127.0.0.1:8765/ using the token in .gui-token.
pause
exit /b 1
