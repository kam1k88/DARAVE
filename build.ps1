# build.ps1 — собирает установщик DARAVE одной командой.
#
#   .\build.ps1                 полная сборка: venv, exe, ffmpeg, установщик
#   .\build.ps1 -SkipInstaller  только папку dist\DARAVE (проверить руками)
#   .\build.ps1 -StemsPack      ещё и пакет слоёв (~2,5 ГБ, долго)
#
# Что нужно на машине сборщика:
#   Python 3.12 (не 3.13+: под них ещё нет части колёс)
#   Inno Setup 6  — https://jrsoftware.org/isdl.php  (бесплатно)
#   интернет      — качаются пакеты и ffmpeg
#
# Пользователю установщика не нужно устанавливать Python или зависимости.

[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$StemsPack,
    [switch]$Clean,
    [string]$Version = "1.0.1"
)

$ErrorActionPreference = "Stop"
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Say([string]$m, [string]$c = "Gray") {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -ForegroundColor $c
}

function Fail([string]$m) {
    Write-Host ""
    Write-Host "СБОРКА ОСТАНОВЛЕНА: $m" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- Python
# Нужен именно 3.12. На 3.13/3.14 нет колёс под часть зависимостей, и pip
# начинает собирать их из исходников — это либо час, либо ошибка компилятора.
function Find-Python {
    $cands = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $p = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $cands += $p.Trim() }
    }
    $cands += @(
        "C:\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c)) {
            $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])"
            if ($v -eq "3.12") { return $c }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Fail "не нашёл Python 3.12. Поставьте его (winget install Python.Python.3.12) и повторите."
}
Say "Python сборки: $python" Green

# ---------------------------------------------------------------- venv
$venv = Join-Path $root ".build-venv"
if ($Clean -and (Test-Path $venv)) {
    Say "чищу окружение сборки"
    Remove-Item -Recurse -Force $venv
}
if (-not (Test-Path $venv)) {
    Say "создаю окружение сборки (.build-venv)"
    & $python -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) { Fail "venv создался без python.exe — удалите .build-venv и повторите" }

Say "ставлю зависимости (первый раз это 3-6 минут)"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt --quiet
& $vpy -m pip install -r requirements-dev.txt --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip не смог поставить зависимости — смотрите вывод выше" }

# Проверка ДО сборки: если torch затесался в окружение сборки, PyInstaller
# утащит его в дистрибутив, и вместо 200 МБ выйдет 3 ГБ. В .spec он в
# excludes, но лишние 2,5 ГБ на диске сборщика всё равно ни к чему.
$hasTorch = & $vpy -c "import importlib.util as u; print(1 if u.find_spec('torch') else 0)"
if ($hasTorch -eq "1") {
    Say "внимание: в окружении сборки есть torch. В сборку он не попадёт (excludes), но окружение раздуто." Yellow
}

# ---------------------------------------------------------------- ffmpeg
# Нужен для .stem.mp4 и для честных 320 кбит/с на экспорте. Кладём рядом с
# exe — иначе программа зависит от того, что пользователь сам поставил
# ffmpeg в PATH, а он не поставил.
$ffDir = Join-Path $root "build\ffmpeg"
$ffExe = Join-Path $ffDir "bin\ffmpeg.exe"
if (-not (Test-Path $ffExe)) {
    Say "качаю ffmpeg (около 80 МБ)"
    $zip = Join-Path $env:TEMP "ffmpeg-darave.zip"
    $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    } catch {
        Fail "не скачался ffmpeg ($url). Скачайте вручную и распакуйте так, чтобы вышло build\ffmpeg\bin\ffmpeg.exe"
    }
    $tmp = Join-Path $env:TEMP "ffmpeg-darave-unzip"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
    Copy-Item (Join-Path $inner.FullName "bin") -Destination $ffDir -Recurse -Force
    Remove-Item -Recurse -Force $tmp, $zip
}
if (-not (Test-Path $ffExe)) { Fail "ffmpeg так и не появился в build\ffmpeg\bin" }
Say "ffmpeg на месте" Green

# ------------------------------------------------- версия внутри программы
#
# APP_VERSION в server.py — это то, что программа показывает в панели
# аккаунта и сообщает сайту при входе. Раньше сюда не заглядывали, и
# установщик 1.0.2 ставил exe, который всем представлялся как 1.0.1.
# Источник правды один — параметр -Version, поэтому приводим файл к нему.
$srvPath = Join-Path $root "server.py"
$srv     = Get-Content -Raw -Encoding UTF8 $srvPath
if ($srv -notmatch '(?m)^APP_VERSION\s*=\s*"[^"]*"') {
    Fail "в server.py не нашёл строку APP_VERSION — версия внутри программы осталась бы старой"
}
$srvNew = [regex]::Replace($srv, '(?m)^APP_VERSION\s*=\s*"[^"]*"', "APP_VERSION = `"$Version`"")
if ($srvNew -ne $srv) {
    # Без BOM: PyInstaller и git одинаково не любят его в .py.
    [System.IO.File]::WriteAllText($srvPath, $srvNew, (New-Object System.Text.UTF8Encoding $false))
    Say "APP_VERSION в server.py приведён к $Version (это правка исходника — она попадёт в коммит)" Yellow
} else {
    Say "APP_VERSION в server.py уже $Version" Green
}

# ---------------------------------------------------------------- сборка
if (Test-Path (Join-Path $root "dist\DARAVE")) {
    Remove-Item -Recurse -Force (Join-Path $root "dist\DARAVE")
}
Say "собираю exe (5-10 минут)"
& $vpy -m PyInstaller DARAVE.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller завершился с ошибкой" }

$distDir = Join-Path $root "dist\DARAVE"
if (-not (Test-Path (Join-Path $distDir "DARAVE.exe"))) { Fail "DARAVE.exe не собрался" }

Copy-Item $ffDir -Destination $distDir -Recurse -Force

# Дымовая проверка. Собранный exe ловил три разные поломки ИМЕННО здесь —
# отсутствие данных librosa, отсутствие расширений scipy и неработающий
# режим --child. Все три видны за пару секунд и все три невидимы без этой
# проверки: они проявляются не при старте, а на первом анализе трека.
Say "проверяю сборку"
$exe = Join-Path $distDir "DARAVE.exe"
& $exe --selftest
if ($LASTEXITCODE -ne 0) {
    Fail @"
собранный exe не прошёл проверку (см. вывод выше).

Три обычные причины:
  - в .spec пропала строка collect_data_files('librosa') или collect_all('scipy');
  - пакета нет в requirements.txt, и в чистом окружении сборки его не оказалось
    (так было с python-multipart: FastAPI требует его для приёма файлов и
    проверяет это в декораторе маршрута, поэтому падал импорт server.py целиком);
  - пакет ставится под одним именем, а импортируется под другим, и PyInstaller
    его не увидел — тогда нужен hiddenimports в .spec.

Проверка 'бэкенд собирается целиком' ловит вторую и третью: она реально строит
приложение FastAPI, а не просто импортирует библиотеки.
"@
}
& $exe --child stems --info 2>&1 | Out-Null
if ($LASTEXITCODE -gt 1) {
    Say "режим --child отвечает кодом $LASTEXITCODE — проверьте руками: $exe --child stems --info" Yellow
}
$size = "{0:N0}" -f ((Get-ChildItem $distDir -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Say "готово: dist\DARAVE, $size МБ" Green

# ---------------------------------------------------------- пакет слоёв
if ($StemsPack) {
    Say "собираю пакет слоёв (это 2,5-3 ГБ и 15-30 минут)"
    & (Join-Path $root "tools\make_stems_pack.ps1") -Version $Version
    if ($LASTEXITCODE -ne 0) { Fail "пакет слоёв не собрался" }
}

# ---------------------------------------------------------- установщик
if ($SkipInstaller) {
    Say "установщик пропущен (-SkipInstaller). Папку dist\DARAVE можно запускать как есть."
    exit 0
}

# Поиск ISCC.exe.
#
# Здесь был баг, из-за которого установщик не собирался НИКОГДА, а скрипт
# говорил «Inno Setup не найден» даже когда он стоял:
#
#     "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe"
#
# В двойных кавычках PowerShell берёт за имя переменной $env:ProgramFiles,
# а "(x86)" считает обычным текстом. Получается C:\Program Files(x86)\...
# — БЕЗ пробела, и такой папки нет. Имена переменных со скобками нужно
# писать в фигурных: ${env:ProgramFiles(x86)}.
#
# Заодно ищем не только "Inno Setup 6" — версии меняются. В PATH Inno себя
# не прописывает, поэтому на Get-Command полагаться нельзя. Последняя
# надежда — реестр, куда установщик пишет путь всегда.
$iscc = $null
$roots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, "$env:LOCALAPPDATA\Programs") |
         Where-Object { $_ -and (Test-Path $_) }
# $base, а НЕ $root: $root — корень проекта, он задан в начале скрипта и
# нужен ниже, чтобы найти DARAVE.iss и папку installer. Цикл с именем
# $root затирал его, и ISCC потом искал .iss в C:\Program Files (x86).
foreach ($base in $roots) {
    $found = Get-ChildItem -Path $base -Filter "Inno Setup*" -Directory -ErrorAction SilentlyContinue |
             Sort-Object Name -Descending |
             ForEach-Object { Join-Path $_.FullName "ISCC.exe" } |
             Where-Object { Test-Path $_ } |
             Select-Object -First 1
    if ($found) { $iscc = $found; break }
}
if (-not $iscc) {
    foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*_is1",
                       "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*_is1")) {
        $loc = Get-ItemProperty $key -ErrorAction SilentlyContinue |
               Where-Object { $_.DisplayName -like "Inno Setup*" } |
               Select-Object -ExpandProperty InstallLocation -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($loc -and (Test-Path (Join-Path $loc "ISCC.exe"))) { $iscc = Join-Path $loc "ISCC.exe"; break }
    }
}
if (-not $iscc) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if ($iscc) { Say "Inno Setup: $iscc" }
if (-not $iscc) {
    # ВАЖНО: exit 1, а не 0.
    #
    # Раньше здесь стоял exit 0, и сборка без Inno Setup заканчивалась
    # «успехом»: dist\DARAVE свежий, установщик сентябрьский, а PowerShell
    # показывает обычное приглашение. Один раз этого хватило, чтобы
    # старый файл переименовали в новую версию и чуть не выложили.
    # Скрипт, который не сделал то, ради чего запускался, обязан падать.
    Say "Inno Setup НЕ НАЙДЕН — установщик НЕ собран. В папке installer лежит СТАРЫЙ файл." Red
    Say "Поставьте: winget install JRSoftware.InnoSetup — и запустите build.ps1 снова." Yellow
    Say "Папку dist\DARAVE при этом можно запускать как есть — она собрана." Gray
    exit 1
}

$iss = Join-Path $root "DARAVE.iss"
if (-not (Test-Path $iss)) {
    Fail "не нашёл $iss. ISCC на такое отвечает «не удается найти указанный файл» без имени файла, поэтому проверяем сами."
}

Say "собираю установщик"
& $iscc "/DMyAppVersion=$Version" $iss
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup завершился с ошибкой" }

$out = Get-ChildItem (Join-Path $root "installer") -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($out) {
    $mb = "{0:N0}" -f ($out.Length / 1MB)
    Say "УСТАНОВЩИК ГОТОВ: $($out.FullName) ($mb МБ)" Green
}
