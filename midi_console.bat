@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title DARAVE - MIDI-пульт

rem Пульт шлёт MIDI напрямую в Mixxx, поэтому ему нужен rtmidi. На Python
rem 3.13/3.14 колёс rtmidi нет (сборка из исходников требует компилятора),
rem поэтому ищем тот же Python, которым запускается companion.

cd /d "%~dp0"

set "PY="

rem 1) путь из darave_config.ps1, если диджей его туда вписал
if exist "darave_config.ps1" (
    for /f "usebackq tokens=* delims=" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $c = & '.\darave_config.ps1'; if ($c.PythonExe) { $c.PythonExe }"`) do (
        if not "%%L"=="" set "PY=%%L"
    )
)
if defined PY if exist "!PY!" (
    "!PY!" -c "import rtmidi" 2>nul && goto :run
    set "PY="
)

rem 2) типовые места установки Python 3.12/3.11/3.10
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%P (
        %%P -c "import rtmidi" 2>nul && ( set "PY=%%~P" & goto :run )
    )
)

rem 3) через py-лаунчер
for %%V in (3.12 3.11 3.10) do (
    py -%%V -c "import rtmidi" 2>nul && ( set "PY=py -%%V" & goto :run )
)

rem 4) просто python из PATH
python -c "import rtmidi" 2>nul && ( set "PY=python" & goto :run )

echo.
echo Не нашёл Python с модулем rtmidi.
echo.
echo Пульту нужен rtmidi, чтобы слать MIDI в Mixxx. Колёс rtmidi нет для
echo Python 3.13/3.14 - нужен Python 3.12 или старше.
echo.
echo Установите rtmidi в Python 3.12:
echo    "%%LOCALAPPDATA%%\Programs\Python\Python312\python.exe" -m pip install python-rtmidi
echo.
pause
exit /b 1

:run
echo Python: %PY%
echo.
%PY% midi_console.py %*
echo.
pause
