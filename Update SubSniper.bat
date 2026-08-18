@echo off
REM Double-click this to update SubSniper.
REM
REM Why a .bat instead of running update.ps1 directly: Windows blocks
REM PowerShell scripts that came out of a downloaded zip (Mark of the Web),
REM and Home editions default to a Restricted execution policy. Either one
REM makes the window close instantly with no message. This clears the flag
REM and runs the script explicitly, and always pauses so you can read what
REM happened.

cd /d "%~dp0"
title SubSniper Updater

echo Preparing...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path '%~dp0' -Recurse -Include *.ps1,*.bat -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*

echo.
echo ----------------------------------------
echo Finished. Press any key to close.
pause >nul
