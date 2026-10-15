@echo off
rem ====================================================================
rem  Void OS installer for Windows - double-click this file.
rem  Options can be passed too, e.g.:   install.cmd -Yes
rem                                     install.cmd -Uninstall
rem  (The real work is done by install.ps1, next to this file.)
rem ====================================================================
setlocal
cd /d "%~dp0"
title Void OS installer

where powershell >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Windows PowerShell wasn't found. It's part of every Windows 10/11 install;
  echo   if it was removed, reinstall it, then run this again.
  echo.
  pause
  exit /b 1
)

rem -ExecutionPolicy Bypass applies to this one run only; it doesn't change
rem your system's PowerShell settings.
set "VOIDOS_LAUNCHED_BY_CMD=1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
