@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows.ps1" setup
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows.ps1" demo
if errorlevel 1 goto failed
echo.
echo Demo completed. Read README.md and docs\project-guide.md next.
pause
exit /b 0
:failed
echo.
echo Setup or demo failed. Keep this window open and see docs\windows.md.
pause
exit /b 1

