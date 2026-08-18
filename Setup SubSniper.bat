@echo off
REM Double-click this the first time you install SubSniper.
REM See "Update SubSniper.bat" for why this wrapper exists.

cd /d "%~dp0"
title SubSniper Setup

echo Preparing...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path '%~dp0' -Recurse -Include *.ps1,*.bat -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"

echo.
echo ----------------------------------------
echo Finished. Press any key to close.
pause >nul
