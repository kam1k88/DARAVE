@echo off
chcp 65001 >nul
title DARAVE - починка темпа в библиотеке Mixxx
cd /d "%~dp0"

echo.
echo ============================================================
echo  Починка половинного темпа в библиотеке Mixxx
echo ============================================================
echo.
echo  Анализатор Mixxx определяет драм-н-бейс как 86 BPM вместо 172.
echo  Из-за этого sync между таким треком и правильно определённым
echo  требует изменить скорость ВДВОЕ, а питч-фейдер даёт лишь +-8%%.
echo  Свести их невозможно - деки разъезжаются.
echo.
echo  ВАЖНО: Mixxx должен быть ПОЛНОСТЬЮ ЗАКРЫТ.
echo  Резервная копия базы делается автоматически.
echo.
pause

set "PY=python"
where python >nul 2>nul || set "PY=py"

echo.
echo --- ЧТО БУДЕТ ИЗМЕНЕНО (пока ничего не трогаю) ---
echo.
%PY% fix_mixxx_bpm.py --assume-dnb
if errorlevel 1 goto :done

echo.
echo ============================================================
set /p ANSWER="Применить эти изменения? [y/N]: "
if /i not "%ANSWER%"=="y" (
    echo Отменено, ничего не изменено.
    goto :done
)

echo.
%PY% fix_mixxx_bpm.py --assume-dnb --apply

:done
echo.
pause
