@echo off
chcp 65001 >nul
cd /d "%~dp0"
python watch.py %*
echo.
pause
