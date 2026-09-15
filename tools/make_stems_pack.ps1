# make_stems_pack.ps1 — собирает "пакет слоёв": переносной Python с torch
# и demucs внутри, который DARAVE докачивает отдельно.
#
# Зачем отдельно. torch с CUDA — 2,5-3 ГБ, всё остальное вместе — 200 МБ.
# Класть их в один установщик значит требовать 3 ГБ скачивания с каждого,
# кому слои не нужны вовсе: техники на EQ и на фильтре работают без них.
#
# Почему отдельный Python, а не pip в основную сборку. Замороженный
# PyInstaller'ом exe — не интерпретатор: `DARAVE.exe -m demucs` не
# заработает никогда, а demucs запускается именно так (см. stems.py).
# Поэтому пакет — это настоящий python.exe со своими пакетами, и
# stems.py ищет его по фиксированному пути (stems.stems_pack_python).
#
# Результат: dist\DARAVE-stems-<версия>.zip
# Пользователь распаковывает его в %LOCALAPPDATA%\DARAVE\stems-python
# (или это делает сама программа).

[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [ValidateSet("cu124", "cu121", "cpu")]
    [string]$Cuda = "cu124"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Say([string]$m, [string]$c = "Gray") {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -ForegroundColor $c
}

# Встраиваемый (embeddable) дистрибутив Python: это ZIP на 15 МБ, который
# работает из любой папки и ничего не прописывает в систему.
$pyVer = "3.12.7"
$work = Join-Path $root "build\stems-python"
$zipUrl = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip"

if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force -Path $work | Out-Null

Say "качаю встраиваемый Python $pyVer"
$zip = Join-Path $env:TEMP "python-embed-darave.zip"
Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $work -Force
Remove-Item $zip

# Встраиваемая сборка по умолчанию НЕ видит site-packages: в файле
# pythonXY._pth строка import site закомментирована. Без неё pip
# поставится, но ни один пакет не найдётся — самая частая ошибка при
# такой сборке.
$pth = Get-ChildItem $work -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "не нашёл python._pth во встраиваемой сборке" }
$lines = Get-Content $pth.FullName
$lines = $lines -replace '^#\s*import site$', 'import site'
if ($lines -notcontains 'import site') { $lines += 'import site' }
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII

Say "ставлю pip"
$getpip = Join-Path $env:TEMP "get-pip-darave.py"
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip -UseBasicParsing
& (Join-Path $work "python.exe") $getpip --no-warn-script-location
Remove-Item $getpip

$index = if ($Cuda -eq "cpu") { "https://download.pytorch.org/whl/cpu" }
         else { "https://download.pytorch.org/whl/$Cuda" }

Say "ставлю torch ($Cuda) — это 2-3 ГБ и минут двадцать"
& (Join-Path $work "python.exe") -m pip install torch torchaudio --index-url $index --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "torch не поставился" }

Say "ставлю demucs и audio-separator"
& (Join-Path $work "python.exe") -m pip install demucs "audio-separator>=0.47" --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "demucs не поставился" }

# Проверка, ради которой всё и затевалось: сборка CUDA-шная или нет.
# С обычного PyPI под Windows приезжает torch БЕЗ CUDA, и никакие
# драйверы его не оживят — трек считается 20-40 минут вместо 2-4.
Say "проверяю сборку torch"
$check = & (Join-Path $work "python.exe") -c "import torch, demucs, json; print(json.dumps({'torch': torch.__version__, 'cuda_build': torch.version.cuda}))"
Say "  $check" Green
if ($Cuda -ne "cpu" -and $check -notmatch '"cuda_build": "') {
    Write-Host "ВНИМАНИЕ: torch собран БЕЗ CUDA. Слои будут считаться на процессоре — в десять раз дольше." -ForegroundColor Red
}

# Кэш pip и .pyc внутрь пакета не нужны — это сотни мегабайт.
& (Join-Path $work "python.exe") -m pip cache purge 2>$null | Out-Null
Get-ChildItem $work -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Say "пакую"
New-Item -ItemType Directory -Force -Path (Join-Path $root "dist") | Out-Null
$out = Join-Path $root "dist\DARAVE-stems-$Version-$Cuda.zip"
if (Test-Path $out) { Remove-Item $out }
Compress-Archive -Path (Join-Path $work "*") -DestinationPath $out -CompressionLevel Optimal

$mb = "{0:N0}" -f ((Get-Item $out).Length / 1MB)
Say "ПАКЕТ СЛОЁВ ГОТОВ: $out ($mb МБ)" Green
Say "Распаковывается в %LOCALAPPDATA%\DARAVE\stems-python" Gray
