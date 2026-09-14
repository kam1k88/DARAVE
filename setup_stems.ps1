# setup_stems.ps1 — ставит разделение на стемы в ПОДХОДЯЩИЙ Python.
#
# Зачем отдельный скрипт. pip отменяет ВЕСЬ файл требований, если хоть
# одна строка не поставилась, — поэтому вместе с необязательным
# audio-separator переставал ставиться и обязательный demucs. Здесь они
# ставятся по очереди и независимо.
#
# Про Python 3.14: torch 2.10 и demucs 4.1 под него уже есть и работают
# (проверено на этой машине). Единственный пакет без колёс — diffq-fixed,
# зависимость audio-separator, и она обходится через --no-deps, см. п.5.
# Поэтому 3.14 больше не исключается, а просто стоит последним в списке
# предпочтений: на 3.12 колёс заведомо больше.
#
# Скрипт делает три вещи:
#   1. находит Python 3.10-3.12 (у demucs/torch/rtmidi там есть колёса);
#   2. ставит torch — с CUDA, если найдена видеокарта NVIDIA;
#   3. ставит demucs, и ОТДЕЛЬНО, не роняя всё остальное, audio-separator.
#
# stems.py потом сам найдёт этот Python: ему не нужно, чтобы всё стояло в
# одном интерпретаторе с бэкендом (см. stems.demucs_python).

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

Say "DARAVE — установка разделения на стемы" "Cyan"
Say ""

# ---------- 1. ищем подходящий Python ----------
$candidates = @()
try {
    # py.exe знает про все установленные версии
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($v in @("3.12", "3.11", "3.10", "3.13", "3.14")) {
            $probe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $probe) { $candidates += $probe.Trim() }
        }
    }
} catch { }
foreach ($v in @("312", "311", "310", "313", "314")) {
    $candidates += "C:\Python$v\python.exe"
    $candidates += (Join-Path $Env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")
    $candidates += (Join-Path $Env:ProgramFiles "Python$v\python.exe")
}

$target = $null
foreach ($c in $candidates) {
    if (-not $c) { continue }
    if (-not (Test-Path $c)) { continue }
    $ver = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    $parts = $ver.Trim().Split(".")
    $minor = [int]$parts[1]
    if ($parts[0] -eq "3" -and $minor -ge 10 -and $minor -le 14) {
        $target = $c
        Say ("Нашёл Python {0}: {1}" -f $ver.Trim(), $c) "Green"
        break
    }
}

if (-not $target) {
    Say "Не нашёл Python 3.10-3.14." "Red"
    Say "ЧТО СДЕЛАТЬ: поставьте Python 3.12 с python.org/downloads/release/python-31210/" "Yellow"
    Say "(галочка «Add python.exe to PATH» при установке) и запустите этот скрипт снова." "Yellow"
    exit 1
}

# ---------- 2. видеокарта ----------
$hasNvidia = $false
try {
    $null = & nvidia-smi 2>$null
    if ($LASTEXITCODE -eq 0) { $hasNvidia = $true }
} catch { }
if (-not $hasNvidia) {
    try {
        $gpu = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match "NVIDIA" }
        if ($gpu) { $hasNvidia = $true }
    } catch { }
}
Say ("Видеокарта NVIDIA: {0}" -f $(if ($hasNvidia) { "есть" } else { "не найдена" })) `
    $(if ($hasNvidia) { "Green" } else { "Yellow" })

$cudaOk = & $target -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
$cudaOk = ($LASTEXITCODE -eq 0)

# ---------- 3. torch ----------
if ($hasNvidia -and -not $cudaOk) {
    # Индексы перебираем от новых к старым: у каждой версии torch колёса
    # лежат не на всех cu*, и жёстко зашитый cu124 (так было раньше) для
    # torch 2.10 просто пуст — pip молча уходил на обычный PyPI и ставил
    # СБОРКУ БЕЗ CUDA. Снаружи это выглядело как «поставил, а считает
    # процессор»: 20-40 минут на трек вместо 2-4.
    $ok = $false
    foreach ($cu in @("cu130", "cu129", "cu128", "cu126", "cu124")) {
        Say ("Ставлю torch с поддержкой CUDA ({0}, ~2.5 ГБ)..." -f $cu) "Cyan"
        & $target -m pip install --upgrade --index-url "https://download.pytorch.org/whl/$cu" torch torchaudio
        if ($LASTEXITCODE -eq 0) {
            $probe = & $target -c "import torch; print(torch.version.cuda or '')" 2>$null
            if ($probe -and $probe.Trim()) { $ok = $true; break }
        }
        Say ("  под {0} колеса нет, пробую следующий индекс" -f $cu) "Yellow"
    }
    if (-not $ok) {
        Say "CUDA-сборка не встала — ставлю обычную (будет считать на CPU)." "Yellow"
        & $target -m pip install --upgrade torch torchaudio
    }
} else {
    & $target -m pip install --upgrade torch torchaudio
}

# Проверяем СРАЗУ и вслух: сборка без CUDA — самая дорогая тихая ошибка
# во всей установке.
$build = & $target -c "import torch; print(torch.version.cuda or 'cpu')" 2>$null
$seen  = & $target -c "import torch; print(torch.cuda.is_available())" 2>$null
Say ("torch: сборка {0}, видеокарта видна: {1}" -f $build.Trim(), $seen.Trim()) `
    $(if ($seen.Trim() -eq "True") { "Green" } else { "Yellow" })

# ---------- 4. demucs ----------
Say "Ставлю demucs..." "Cyan"
& $target -m pip install "demucs>=4.0.1"
$demucsOk = ($LASTEXITCODE -eq 0)

# ---------- 5. audio-separator (необязательно) ----------
# Ставим БЕЗ зависимостей и доставляем их сами. Обычная команда
# `pip install audio-separator` тянет diffq-fixed, а она собирается из
# исходников и на Python 3.13+ падает («'bitpack.pyx' doesn't match any
# files»); колёс под новые Python у неё нет. Роформеру diffq не нужен —
# в audio-separator он импортируется только внутри вложенной копии
# demucs, а роформер идёт другой веткой (MDXC) и её не загружает.
Say "Ставлю audio-separator (роформеры на вокал; необязательно)..." "Cyan"
& $target -m pip install audio-separator --no-deps
$sepOk = ($LASTEXITCODE -eq 0)
if ($sepOk) {
    Say "Доставляю зависимости роформера (кроме diffq-fixed — она ему не нужна)..." "Cyan"
    & $target -m pip install "beartype==0.18.5" einops julius librosa ml_collections `
        numpy onnx onnx2torch-py313 pydub pyyaml requests resampy samplerate scipy `
        six soundfile tqdm rotary-embedding-torch audioop-lts
    $sepOk = ($LASTEXITCODE -eq 0)
    if ($sepOk) {
        Say "Ставлю onnxruntime-gpu (роформер на видеокарте)..." "Cyan"
        & $target -m pip install onnxruntime-gpu
        if ($LASTEXITCODE -ne 0) {
            Say "onnxruntime-gpu не встал — роформер посчитает процессор." "Yellow"
        }
    }
}
if (-not $sepOk) {
    Say "audio-separator не встал — не страшно: DARAVE посчитает стемы через htdemucs_ft." "Yellow"
    Say "Вокал будет чуть хуже, чем у роформера, но заметно лучше базовой htdemucs." "Yellow"
}

# ---------- 6. что получилось ----------
Say ""
Say "Проверяю..." "Cyan"
& $target -c @"
import json, torch, importlib.util
print(json.dumps({
    'python': __import__('sys').version.split()[0],
    'torch': torch.__version__,
    'cuda': bool(torch.cuda.is_available()),
    'gpu': (torch.cuda.get_device_name(0) if torch.cuda.is_available() else None),
    'demucs': importlib.util.find_spec('demucs') is not None,
    'audio_separator': importlib.util.find_spec('audio_separator') is not None,
}, ensure_ascii=False, indent=2))
"@

Say ""
if ($demucsOk) {
    Say "Готово. Считать стемы всей библиотеки:" "Green"
    Say ('  python stems.py --dir "C:\Users\{0}\music"' -f $Env:USERNAME) "White"
    Say "Способ выберется сам (--backend auto): роформер, если встал audio-separator," "Gray"
    Say "иначе htdemucs_ft. Кэш живёт в DARAVE\stems и считается один раз." "Gray"
} else {
    Say "demucs поставить не удалось — смотрите вывод выше." "Red"
}
