@echo off
title DARAVE - stems
cd /d "%~dp0"

rem Vsyo odnoy komandoy: snachala dolozhit rowformer-vokal k trekam, u
rem kotorykh sloi uzhe poschitany, potom doschitat ostalnye.
rem Papku mozhno peredat argumentom: stems_all.bat "D:\Music"

set "MUSIC=%~1"
if "%MUSIC%"=="" set "MUSIC=%USERPROFILE%\Music"

if not exist "%MUSIC%" (
  echo Папка не найдена: %MUSIC%
  echo Передайте её аргументом:  stems_all.bat "D:\Music"
  echo.
  pause
  exit /b 1
)

echo == DARAVE: стемы для "%MUSIC%" ==
echo.
python stems.py --info
echo.
python stems.py --dir "%MUSIC%" --all
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
  echo Завершилось с ошибкой, код %RC% - смотрите сообщение выше.
) else (
  echo Готово. Файлы .stem.mp4 лежат рядом с треками, слои - в DARAVE\stems.
)
echo.
pause
