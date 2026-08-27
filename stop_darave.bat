@echo off
rem stop_darave.bat - stops the DARAVE backend, companion and the window-title
rem helper. Needed since the consoles became hidden: there are no windows left
rem to close. Mixxx and loopMIDI keep running; to close them too:
rem   stop_darave.bat -All
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_darave.ps1" %*
endlocal
