@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title DARAVE - уточнение BPM библиотеки

cd /d "%~dp0"

rem Нужен тот же Python, которым сканируется библиотека (librosa/numpy/scipy),
rem а не первый попавшийся из PATH.
set "PY="

if exist "darave_config.ps1" (
    for /f "usebackq tokens=* delims=" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $c = ^& '.\darave_config.ps1'; if ($c.PythonExe) { $c.PythonExe }"`) do (
        if not "%%L"=="" set "PY=%%L"
    )
)
if defined PY if exist "!PY!" (
    "!PY!" -c "import librosa" 2>nul && goto :run
    set "PY="
)

for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%P (
        %%P -c "import librosa" 2>nul && ( set "PY=%%~P" & goto :run )
    )
)

for %%V in (3.12 3.11 3.10) do (
    py -%%V -c "import librosa" 2>nul && ( set "PY=py -%%V" & goto :run )
)

python -c "import librosa" 2>nul && ( set "PY=python" & goto :run )

echo.
echo Не нашёл Python с librosa - тем же, которым сканируется библиотека.
echo Установите: python -m pip install -r requirements-analysis.txt
echo.
pause
exit /b 1

:run
echo.
echo  DARAVE - уточнение BPM библиотеки
echo  ---------------------------------
echo  После сканирования BPM в базе стоит с сетки-приора librosa, а не
echo  измеренный: у части треков ровно 172.30 вместо реальных 171/173/174,
echo  у части - 2/3 темпа (117.5 вместо ~176). Из-за этого такты в сведении
echo  разъезжаются, а треки с неверной долей вообще не попадают в план.
echo.
echo  Перемеряю темп по 90 секундам из середины каждого трека.
echo  Пересканирования не будет - вся библиотека чинится за минуту-две.
echo.
echo  Python: %PY%
echo.
echo  --- сначала показываю, что изменится (в базу пока не пишу) ---
echo.
%PY% fix_library_bpm.py --dry-run %*
echo.
set /p ANSWER="Записать эти изменения в базу? (y/n): "
if /i not "%ANSWER%"=="y" goto :end
echo.
%PY% fix_library_bpm.py %*
echo.
echo  Готово. Перестройте план в веб-интерфейсе - BPM обновится.
:end
echo.
pause
