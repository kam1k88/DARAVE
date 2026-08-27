@echo off
rem start_darave.bat - double-click this file to launch DARAVE.
rem It only lifts the PowerShell script-execution restriction for THIS run
rem and changes nothing system-wide. All logic lives in start_darave.ps1,
rem which also writes darave_start.log next to itself.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_darave.ps1"
if errorlevel 1 (
  echo.
  echo PowerShell exited with code %errorlevel%.
  echo See darave_start.log next to this file.
  pause
)
endlocal
