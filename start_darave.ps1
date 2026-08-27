# start_darave.ps1 — один клик (через start_darave.bat) поднимает
# loopMIDI, Mixxx, backend (uvicorn) и companion и открывает браузер.
#
# Скрипт САМ ищет loopMIDI, Mixxx и подходящий Python (через реестр и
# типичные пути) — в darave_config.ps1 пути указывать нужно только если
# автопоиск промахнулся. Всё, что делает, пишет в darave_start.log рядом.

$ErrorActionPreference = "Stop"

# Иначе кириллица из print() дочерних Python-процессов иногда ловит
# двойную порчу кодировки при перехвате через Invoke-Native/Out-String.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$logPath = Join-Path $scriptDir "darave_start.log"

# Чёрные окна PowerShell на панели задач. Прячем СВОЁ окно — оно же окно
# cmd, если запущено из start_darave.bat: они делят одну консоль, поэтому
# одного вызова хватает на оба. Backend и companion поднимаем скрытыми, но
# с перенаправлением вывода в файлы: молча терять их ошибки нельзя, раньше
# именно по этим окнам и было видно, что пошло не так.
Add-Type -Name DaraveConsole -Namespace Darave -MemberDefinition @"
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
"@ -ErrorAction SilentlyContinue

$Script:ConsoleHidden = $false

function Set-OwnConsole {
    param([bool]$Visible)
    try {
        $h = [Darave.DaraveConsole]::GetConsoleWindow()
        if ($h -eq [IntPtr]::Zero) { return }
        [void][Darave.DaraveConsole]::ShowWindow($h, $(if ($Visible) { 5 } else { 0 }))  # SW_SHOW / SW_HIDE
        $Script:ConsoleHidden = -not $Visible
    } catch { }
}

function Write-Log {
    param([string]$Message, [string]$Color = "Gray")
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line -ForegroundColor $Color
    try { Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 } catch { }
}

# Внешняя программа, чей ненулевой код возврата не должен ронять скрипт.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>&1 | Out-String
        return [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Output = $out.Trim() }
    } catch {
        return [pscustomobject]@{ Ok = $false; Output = $_.Exception.Message }
    } finally { $ErrorActionPreference = $old }
}

# Ищем установленную программу: сначала явный путь, затем типичные места,
# затем InstallLocation из реестра деинсталляции (там лежит правда даже
# для нестандартных установок).
function Find-Program {
    param(
        [string]$Explicit,
        [string[]]$Candidates,
        [string]$RegistryNameLike,
        [string]$ExeName
    )
    if ($Explicit -and (Test-Path -LiteralPath $Explicit)) { return $Explicit }
    foreach ($c in $Candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    $roots = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($root in $roots) {
        try {
            $hits = Get-ItemProperty $root -ErrorAction SilentlyContinue |
                    Where-Object { $_.DisplayName -like $RegistryNameLike }
            foreach ($h in $hits) {
                foreach ($loc in @($h.InstallLocation, (Split-Path -Parent ([string]$h.DisplayIcon -replace '"','' -split ',')[0]))) {
                    if ($loc -and (Test-Path -LiteralPath $loc)) {
                        $exe = Join-Path $loc $ExeName
                        if (Test-Path -LiteralPath $exe) { return $exe }
                    }
                }
            }
        } catch { }
    }
    return $null
}

try {
    Set-Content -LiteralPath $logPath -Value "" -Encoding UTF8
    Write-Log "== DARAVE: запуск ==" "Cyan"
    Write-Log ("PowerShell {0} | {1}" -f $PSVersionTable.PSVersion, [Environment]::OSVersion.VersionString)
    Write-Log ("Папка скрипта: {0}" -f $scriptDir)

    # ---------- 0. конфиг ----------
    $configPath  = Join-Path $scriptDir "darave_config.ps1"
    $examplePath = Join-Path $scriptDir "darave_config.example.ps1"

    if (-not (Test-Path -LiteralPath $configPath)) {
        if (-not (Test-Path -LiteralPath $examplePath)) {
            throw "Не найден ни darave_config.ps1, ни darave_config.example.ps1. Распакуйте архив DARAVE целиком в одну папку."
        }
        Copy-Item -LiteralPath $examplePath -Destination $configPath
        Write-Log "Создал darave_config.ps1 из шаблона." "Green"
        Write-Log "Открываю в блокноте — заполните и сохраните, потом запустите start_darave.bat ещё раз." "Yellow"
        try { Start-Process notepad.exe -ArgumentList $configPath } catch { Write-Log "Откройте $configPath вручную." "Yellow" }
        return
    }

    # Windows метит файлы из скачанных архивов "меткой интернета", и тогда
    # dot-source падает с "is not digitally signed" даже при Bypass.
    foreach ($f in @($configPath, $examplePath, (Join-Path $scriptDir "start_darave.ps1"))) {
        if (Test-Path -LiteralPath $f) { try { Unblock-File -LiteralPath $f -ErrorAction SilentlyContinue } catch { } }
    }

    Write-Log "Читаю darave_config.ps1..."
    try { . $configPath }
    catch {
        throw ("Не удалось прочитать darave_config.ps1: {0}`nЕсли там 'is not digitally signed' — правой кнопкой по файлу -> Свойства -> 'Разблокировать' -> ОК." -f $_.Exception.Message)
    }
    $cfg = $Global:DaraveConfig
    if (-not $cfg) { throw "darave_config.ps1 не задал `$Global:DaraveConfig — возьмите свежий darave_config.example.ps1 за основу." }
    if (-not $cfg.DaraveDir)   { $cfg.DaraveDir = $scriptDir }

    # По умолчанию окна скрыты. Вернуть их можно строкой HideConsoles = $false
    # в darave_config.ps1 — иногда проще смотреть в живое окно, чем в лог.
    $hideConsoles = -not ($cfg.HideConsoles -eq $false)
    if ($hideConsoles) { Set-OwnConsole $false }
    $backendConsoleLog = Join-Path $cfg.DaraveDir "darave_backend_console.log"
    $companionConsoleLog = Join-Path $cfg.DaraveDir "darave_companion_console.log"
    if (-not $cfg.LlmProvider) { $cfg.LlmProvider = "auto" }
    if (-not $cfg.Port)        { $cfg.Port = 8765 }
    if (-not $cfg.RoomCode)    { $cfg.RoomCode = "my-room" }

    Write-Log ("Комната: {0} | Порт: {1} | LLM: {2}" -f $cfg.RoomCode, $cfg.Port, $cfg.LlmProvider) "Cyan"

    $geminiKeyOk = -not ([string]::IsNullOrWhiteSpace($cfg.GeminiApiKey) -or $cfg.GeminiApiKey -like "PASTE_*")

    if ($cfg.LlmProvider -eq "gemini" -and -not $geminiKeyOk) {
        Write-Log "LlmProvider='gemini', но ключ не заполнен — чат падал бы с 'API key not valid'. Переключаю на 'auto'." "Yellow"
        $cfg.LlmProvider = "auto"
    }

    $ollamaHost = if ($cfg.OllamaHost) { $cfg.OllamaHost } else { "http://localhost:11434" }
    if ($cfg.LlmProvider -ne "gemini" -and -not $geminiKeyOk) {
        $ollamaUp = $false
        try { Invoke-WebRequest -Uri "$ollamaHost/api/tags" -UseBasicParsing -TimeoutSec 3 | Out-Null; $ollamaUp = $true } catch { }
        if ($ollamaUp) {
            Write-Log ("Ollama отвечает на {0}." -f $ollamaHost) "Green"
            $tags = $null
            try { $tags = (Invoke-WebRequest -Uri "$ollamaHost/api/tags" -UseBasicParsing -TimeoutSec 5).Content } catch { }
            $wanted = if ($cfg.OllamaModel) { $cfg.OllamaModel } else { "llama3.1" }
            if ($tags -and ($tags -notlike "*$wanted*")) {
                Write-Log ("Но модель '{0}' в Ollama не найдена. Выполните:  ollama pull {0}" -f $wanted) "Yellow"
            } else {
                Write-Log ("Модель {0} на месте." -f $wanted) "Green"
            }
        } else {
            Write-Log ("Ollama не отвечает на {0}, ключа Gemini тоже нет — чат работать не будет." -f $ollamaHost) "Red"
            Write-Log "Запустите Ollama (она обычно висит в трее; иначе 'ollama serve') и/или 'ollama pull llama3.1'." "Yellow"
        }
    }

    # ---------- 1. Python + rtmidi ----------
    # companion'у нужен python-rtmidi, а его сборок под Windows нет новее
    # Python 3.12 — на 3.13/3.14 импорт не заведётся, это не чинится pip'ом.
    $pythonExe = if ($cfg.PythonExe) { $cfg.PythonExe } else { "python" }
    $pyCheck = Invoke-Native $pythonExe @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
    if (-not $pyCheck.Ok) {
        throw ("Не удалось запустить '{0}': {1}`nУстановите Python 3.12 или укажите путь в PythonExe в darave_config.ps1." -f $pythonExe, $pyCheck.Output)
    }
    Write-Log ("Python (backend): {0} — версия {1}" -f $pythonExe, $pyCheck.Output)

    $companionPy = $pythonExe
    $hasRtmidi = (Invoke-Native $companionPy @("-c", "import rtmidi")).Ok

    if (-not $hasRtmidi) {
        Write-Log ("В этом Python ({0}) нет rtmidi — ищу Python 3.12..." -f $pyCheck.Output) "Yellow"
        $found = $null
        $alt = Invoke-Native "py" @("-3.12", "-c", "import sys; print(sys.executable)")
        if ($alt.Ok -and $alt.Output) { $found = $alt.Output.Trim() }
        if (-not $found) {
            foreach ($c in @(
                "C:\Python312\python.exe",
                (Join-Path $Env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
                (Join-Path $Env:ProgramFiles "Python312\python.exe")
            )) { if ($c -and (Test-Path -LiteralPath $c)) { $found = $c; break } }
        }
        if ($found) {
            $companionPy = $found
            $hasRtmidi = (Invoke-Native $companionPy @("-c", "import rtmidi")).Ok
            if ($hasRtmidi) {
                Write-Log ("Companion пойдёт на {0} (там есть rtmidi)." -f $companionPy) "Green"
                Write-Log "Впишите этот путь в PythonExe в darave_config.ps1, чтобы не искать каждый раз." "Gray"
            } else {
                Write-Log ("Нашёл Python 3.12 ({0}), но rtmidi в нём не установлен. Выполните:" -f $companionPy) "Yellow"
                Write-Log ("  & '{0}' -m pip install -r requirements.txt" -f $companionPy) "Yellow"
            }
        } else {
            Write-Log "Python 3.12 не найден. ПРИЧИНА: у python-rtmidi нет сборок под Windows новее 3.12 (на PyPI только cp38-cp312)." "Red"
            Write-Log "ЧТО СДЕЛАТЬ: поставить Python 3.12 с python.org/downloads/release/python-3129/, затем: py -3.12 -m pip install -r requirements.txt" "Yellow"
        }
        if (-not $hasRtmidi) {
            Write-Log "Пока поднимаю companion в mock-режиме: чат/библиотека/стратегия работают, команды в Mixxx не уходят." "Yellow"
        }
    } else {
        Write-Log "Модуль rtmidi на месте." "Green"
    }

    # ---------- 2. loopMIDI ----------
    $loopMidi = Find-Program -Explicit $cfg.LoopMidiPath -RegistryNameLike "*loopMIDI*" -ExeName "loopMIDI.exe" -Candidates @(
        (Join-Path $Env:ProgramFiles "Tobias Erichsen\loopMIDI\loopMIDI.exe"),
        (Join-Path ${Env:ProgramFiles(x86)} "Tobias Erichsen\loopMIDI\loopMIDI.exe"),
        (Join-Path $Env:LOCALAPPDATA "Programs\Tobias Erichsen\loopMIDI\loopMIDI.exe")
    )
    if ($loopMidi) {
        if (-not (Get-Process -Name "loopMIDI" -ErrorAction SilentlyContinue)) {
            Write-Log ("Запускаю loopMIDI: {0}" -f $loopMidi) "Green"
            Start-Process -FilePath $loopMidi
            Start-Sleep -Seconds 2
        } else { Write-Log "loopMIDI уже запущен." "Green" }
    } else {
        Write-Log "loopMIDI не найден (ни по путям, ни в реестре). Если он установлен — впишите путь в LoopMidiPath; если нет — поставьте с tobias-erichsen.de/software/loopmidi.html и создайте порт '$($cfg.MidiPortName)'." "Yellow"
    }

    # ---------- 3. Mixxx ----------
    $mixxx = Find-Program -Explicit $cfg.MixxxPath -RegistryNameLike "*Mixxx*" -ExeName "mixxx.exe" -Candidates @(
        (Join-Path $Env:ProgramFiles "Mixxx\mixxx.exe"),
        (Join-Path ${Env:ProgramFiles(x86)} "Mixxx\mixxx.exe"),
        (Join-Path $Env:LOCALAPPDATA "Programs\Mixxx\mixxx.exe")
    )
    if ($mixxx) {
        if (-not (Get-Process -Name "mixxx" -ErrorAction SilentlyContinue)) {
            Write-Log ("Запускаю Mixxx: {0}" -f $mixxx) "Green"
            Start-Process -FilePath $mixxx
            Start-Sleep -Seconds 3
        } else { Write-Log "Mixxx уже запущен." "Green" }
    } else {
        Write-Log "Mixxx не найден — запустите его вручную или впишите путь в MixxxPath." "Yellow"
    }

    # Заголовок окна Mixxx -> DARAVE. Настройки заголовка в Mixxx нет:
    # название зашито в mixxx.exe строкой, которая даже не попадает в файлы
    # перевода, а править бинарник нельзя — та же строка участвует в путях
    # к настройкам. Поэтому переименовываем окно снаружи (SetWindowText)
    # отдельным фоновым скриптом. Установку Mixxx он не трогает: закрыли
    # скрипт — заголовок вернулся сам. Заменяется именно слово, поэтому
    # «Исполнитель - Трек | Mixxx» станет «... | DARAVE», а не потеряет трек.
    $titleScript = Join-Path $cfg.DaraveDir "darave_window_title.ps1"
    if (Test-Path -LiteralPath $titleScript) {
        try {
            Get-CimInstance Win32_Process -ErrorAction Stop |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'darave_window_title' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        } catch { }
        Start-Process -FilePath "powershell" -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $titleScript + '"'))
        Write-Log "Заголовок окна Mixxx переименую в DARAVE (фоновый скрипт)." "Gray"
    }

    # Иконка ярлыков Mixxx (рабочий стол, «Пуск», панель задач). Ярлык
    # хранит ссылку на иконку отдельным полем, поэтому менять exe не надо.
    # Делаем на каждом запуске: обновление Mixxx перезаписывает свои ярлыки
    # и вернуло бы родную иконку.
    $iconScript = Join-Path $cfg.DaraveDir "darave_shortcut_icons.ps1"
    if ((Test-Path -LiteralPath $iconScript) -and
        (Test-Path -LiteralPath (Join-Path $cfg.DaraveDir "darave.ico"))) {
        $ic = Invoke-Native "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass",
                                           "-File", $iconScript)
        $line = ($ic.Output -split "`n" | Where-Object { $_ -match "Обновлено ярлыков|не нашлось" } |
                 Select-Object -First 1)
        if ($line) { Write-Log ("Ярлыки: {0}" -f $line.Trim()) "Gray" }
    }

    # ---------- 4. backend ----------
    $envParts = @("`$env:DARAVE_LLM_PROVIDER = '$($cfg.LlmProvider)';")
    if ($geminiKeyOk)     { $envParts += "`$env:GEMINI_API_KEY = '$($cfg.GeminiApiKey)';" }
    if ($cfg.OllamaModel) { $envParts += "`$env:OLLAMA_MODEL = '$($cfg.OllamaModel)';" }
    $envParts += "`$env:OLLAMA_HOST = '$ollamaHost';"
    $envLines = $envParts -join " "

    # ---------- 4a. preflight: реально ли server.py импортируется этим Python ----------
    # Раньше мы просто открывали окно backend'а и ждали 16 секунд, а если он
    # падал при импорте (например, не хватает python-multipart), диджей
    # узнавал об этом только раскопав длинный traceback в отдельном окне.
    # Теперь проверяем импорт СРАЗУ, тем же Python и теми же переменными
    # окружения, что пойдут в реальный запуск — если он не пройдёт, окно
    # backend'а вообще не открываем, а сразу пишем понятную причину.
    Write-Log "Проверяю, что backend вообще импортируется (до открытия окна)..."
    $env:DARAVE_LLM_PROVIDER = $cfg.LlmProvider
    if ($geminiKeyOk) { $env:GEMINI_API_KEY = $cfg.GeminiApiKey } else { Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue }
    if ($cfg.OllamaModel) { $env:OLLAMA_MODEL = $cfg.OllamaModel }
    $env:OLLAMA_HOST = $ollamaHost

    Push-Location -LiteralPath $cfg.DaraveDir
    $preflight = Invoke-Native $pythonExe @("-c", "import server")
    Pop-Location

    if (-not $preflight.Ok) {
        $lastLine = ($preflight.Output -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        $autoFixed = $false

        # Ставим ТОЛЬКО конкретный недостающий пакет, а не весь
        # requirements.txt — там есть python-rtmidi, а её сборка из
        # исходников на "чужом" Python (не 3.8-3.12, как у backend'а)
        # требует C++-компилятор и падает, обрывая всю установку ещё до
        # python-multipart.
        $pkgToInstall = $null
        if ($preflight.Output -match "python-multipart") {
            $pkgToInstall = "python-multipart"
        } elseif ($preflight.Output -match "No module named '([^']+)'") {
            $modName = $Matches[1].Split('.')[0]
            if ($modName -ne "rtmidi") { $pkgToInstall = $modName }
        }

        if ($pkgToInstall) {
            Write-Log ("Backend не импортируется — не хватает пакета '{0}'. Ставлю сам:" -f $pkgToInstall) "Yellow"
            Write-Log ("  {0}" -f $lastLine) "Yellow"
            $install = Invoke-Native $pythonExe @("-m", "pip", "install", $pkgToInstall)
            if ($install.Ok) {
                Write-Log "pip install прошёл, перепроверяю импорт..." "Green"
                Push-Location -LiteralPath $cfg.DaraveDir
                $preflight = Invoke-Native $pythonExe @("-c", "import server")
                Pop-Location
                if ($preflight.Ok) { $autoFixed = $true }
                else { $lastLine = ($preflight.Output -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1) }
            } else {
                Write-Log "pip install не прошёл сам:" "Red"
                foreach ($ln in ($install.Output -split "`n")) { Write-Log "  $ln" "Gray" }
                Write-Log ("Выполните вручную и запустите start_darave.bat заново:  & '{0}' -m pip install {1}" -f $pythonExe, $pkgToInstall) "Yellow"
            }
        }

        if (-not $preflight.Ok) {
            Write-Log "Backend не импортируется — окно даже открывать не буду. Причина:" "Red"
            Write-Log ("  {0}" -f $lastLine) "Red"
            Write-Log "Полный вывод ошибки — ниже (он же в этом darave_start.log):" "Yellow"
            foreach ($ln in ($preflight.Output -split "`n")) { Write-Log "  $ln" "Gray" }
            Write-Log "Backend и companion НЕ запущены — сначала почините ошибку выше и запустите start_darave.bat снова." "Red"
            return
        }
    } else {
        $autoFixed = $false
    }
    if ($autoFixed) { Write-Log "Backend теперь импортируется — пакет доустановлен автоматически." "Green" }
    else { Write-Log "Backend импортируется без ошибок." "Green" }

    # Кодировщик MP3 320 кбит/с. Без него экспорт сета молча падает на
    # libsndfile, который отдаёт ~74 кбит/с моно — диджей слышит «глухой»
    # файл и не понимает почему. Пакет чистый (колёса под Windows, без
    # компилятора), поэтому ставим сам и молча; если не встал — не
    # смертельно, просто предупреждаем.
    $le = Invoke-Native $pythonExe @("-c", "import lameenc")
    if (-not $le.Ok) {
        Write-Log "Ставлю кодировщик MP3 320 кбит/с (lameenc)..." "Yellow"
        $leInstall = Invoke-Native $pythonExe @("-m", "pip", "install", "lameenc")
        if ($leInstall.Ok) { Write-Log "lameenc поставлен — экспорт сета будет 320 кбит/с стерео." "Green" }
        else { Write-Log "lameenc поставить не вышло — MP3 будет хуже по качеству (WAV не пострадает)." "Yellow" }
    }

    # --reload удобен (код подхватывается без перезапуска), но требует пакет
    # watchfiles. Без него uvicorn падает молча: импорт проходит, порт не
    # слушается — ровно тот случай, когда «не запускается» и в логе пусто.
    # Поэтому проверяем и включаем только если реально можем.
    $reloadArgs = ""
    $wf = Invoke-Native $pythonExe @("-c", "import watchfiles")
    if (-not $wf.Ok) {
        # pip install watchfiles (авто): без него uvicorn --reload падает
        # молча — импорт проходит, порт не слушается. Пакет лёгкий, поэтому
        # ставим сам, чтобы правки кода подхватывались без перезапуска.
        Write-Log "Ставлю watchfiles для автоперезагрузки кода..." "Yellow"
        $wfInstall = Invoke-Native $pythonExe @("-m", "pip", "install", "watchfiles")
        if ($wfInstall.Ok) { $wf = Invoke-Native $pythonExe @("-c", "import watchfiles") }
    }
    if ($wf.Ok) {
        $reloadArgs = " --reload --reload-dir `"$($cfg.DaraveDir)`""
        Write-Log "Автоперезагрузка кода включена (watchfiles найден)."
    } else {
        Write-Log "watchfiles не установлен — запускаю без автоперезагрузки (pip install watchfiles, если нужна)." "Yellow"
    }

    $backendCmd = "$envLines Set-Location -LiteralPath '$($cfg.DaraveDir)'; " +
                  "`$host.UI.RawUI.WindowTitle = 'DARAVE backend'; " +
                  "& '$pythonExe' -m uvicorn server:app --host 0.0.0.0 --port $($cfg.Port)$reloadArgs"
    if ($hideConsoles) {
        # uvicorn пишет свои ошибки ("address already in use" и прочее) в
        # консоль, а не в darave_backend.log — в скрытом окне они бы
        # пропали совсем. Уводим оба потока в файл.
        $backendCmd += " 2>&1 | Out-File -FilePath '$backendConsoleLog' -Append -Encoding utf8"
    }
    # Старый backend, если он ещё жив, держит порт — новый процесс тогда
    # молча не поднимается ("address already in use" в своём окне), а
    # проверка ниже видит ответ СТАРОГО и рапортует "готово". Снаружи это
    # выглядит как "перезапустил, а ничего не изменилось": правки кода не
    # доезжают часами. Поэтому сначала гасим того, кто слушает порт, — но
    # только если это действительно python, чужие процессы не трогаем.
    # Кто слушает порт. Get-NetTCPConnection есть не в каждой конфигурации
    # PowerShell 7, поэтому при его отсутствии разбираем netstat — эта
    # проверка слишком важна, чтобы отключаться молча.
    function Get-PortOwners([int]$port) {
        try {
            return @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
                     Select-Object -ExpandProperty OwningProcess -Unique)
        } catch {
            $out = & netstat -ano 2>$null
            $ids = @()
            foreach ($line in $out) {
                if ($line -match "^\s*TCP\s+\S+:$port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                    $ids += [int]$Matches[1]
                }
            }
            return @($ids | Sort-Object -Unique)
        }
    }

    # Сначала гасим сам uvicorn по командной строке. С --reload это ДВА
    # процесса: родитель-наблюдатель и рабочий, который держит порт. Убить
    # только слушателя мало — родитель поднимет его заново, и порт снова
    # занят.
    $killedAny = $false
    try {
        $allProc = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        $uvicorns = @($allProc | Where-Object { $_.CommandLine -and $_.CommandLine -match 'uvicorn\s+server:app' })
        # С --reload рабочий процесс запускается через multiprocessing, и в
        # его командной строке НЕТ "uvicorn server:app" — только у родителя.
        # Убив одного родителя, мы оставляли сироту, которая продолжала
        # держать сокет: на Windows два процесса могут забиндить один порт,
        # и запросы уходили старому. Снаружи это выглядело как «Not Found»
        # на только что добавленной кнопке. Поэтому гасим и детей.
        $ids = @($uvicorns | Select-Object -ExpandProperty ProcessId)
        $kids = @($allProc | Where-Object { $ids -contains $_.ParentProcessId -and $_.Name -match '^(python|pythonw)' })
        foreach ($u in ($kids + $uvicorns)) {
            Write-Log ("Останавливаю старый backend (PID {0})." -f $u.ProcessId) "Yellow"
            Stop-Process -Id $u.ProcessId -Force -ErrorAction SilentlyContinue
            $killedAny = $true
        }
    } catch { }

    $listeners = @(Get-PortOwners $cfg.Port)
    foreach ($procId in $listeners) {
        if (-not $procId -or $procId -le 4) { continue }
        try {
            $old = Get-Process -Id $procId -ErrorAction Stop
            if ($old.ProcessName -notmatch '^(python|pythonw|py)$') {
                Write-Log ("Порт {0} занят чужим процессом {1} (PID {2}) — не трогаю его, backend не поднимется." -f $cfg.Port, $old.ProcessName, $procId) "Red"
                continue
            }
            Write-Log ("Порт {0} занят старым backend'ом (PID {1}) — останавливаю, иначе новый код не подхватится." -f $cfg.Port, $procId) "Yellow"
            Stop-Process -Id $procId -Force -ErrorAction Stop
            $killedAny = $true
        } catch {
            Write-Log ("Не смог остановить PID {0}: {1}" -f $procId, $_.Exception.Message) "Red"
        }
    }
    if ($killedAny) {
        for ($i = 0; $i -lt 24; $i++) {
            Start-Sleep -Milliseconds 250
            if ((Get-PortOwners $cfg.Port).Count -eq 0) { break }
        }
    }

    if ($hideConsoles) {
        Write-Log "Запускаю backend скрыто (вывод в darave_backend_console.log)..." "Green"
        Start-Process -FilePath "powershell" -WindowStyle Hidden `
                      -ArgumentList @("-NoProfile", "-Command", $backendCmd)
    } else {
        Write-Log "Запускаю backend в новом окне..." "Green"
        Start-Process -FilePath "powershell" -ArgumentList @("-NoExit", "-Command", $backendCmd)
    }

    Write-Log "Жду, пока backend поднимется..."
    $backendOk = $false
    for ($i = 0; $i -lt 8; $i++) {
        Start-Sleep -Seconds 2
        try { Invoke-WebRequest -Uri "http://localhost:$($cfg.Port)/api/techniques" -UseBasicParsing -TimeoutSec 3 | Out-Null; $backendOk = $true; break } catch { }
    }
    if ($backendOk) {
        Write-Log "Backend отвечает на порту $($cfg.Port)." "Green"
        # Ответить мог и выживший старый процесс. Спрашиваем у него самого,
        # какой код в нём живёт, и сверяем с датами файлов на диске.
        try {
            $v = Invoke-RestMethod -Uri "http://localhost:$($cfg.Port)/api/version" -TimeoutSec 5
            Write-Log ("В backend'е: PID {0}, стерео={1}, mp3={2} {3} кбит/с." -f $v.pid, $v.stereo, $v.mp3.name, $v.mp3.bitrate) "Gray"
            $diskMtime = (Get-ChildItem -LiteralPath $cfg.DaraveDir -Filter *.py -File |
                          Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
            $codeTime = [DateTimeOffset]::FromUnixTimeMilliseconds([long]([double]$v.code_mtime * 1000)).UtcDateTime
            if ($diskMtime -and ($codeTime -lt $diskMtime.AddSeconds(-90))) {
                Write-Log ("ВНИМАНИЕ: backend работает на СТАРОМ коде — файлы на диске новее на {0:N0} сек. Закройте окно 'DARAVE backend' вручную и запустите заново." -f ($diskMtime - $codeTime).TotalSeconds) "Red"
            }
            # Дата файлов не ловит случай, когда на порту сидит другой
            # процесс с такой же датой, но старым набором маршрутов.
            # Спрашиваем прямо, умеет ли он то, что мы только что добавили.
            $need = @("suggest-length", "version")
            $missing = @($need | Where-Object { $v.endpoints -notcontains $_ })
            if ($missing.Count -gt 0) {
                Write-Log ("ВНИМАНИЕ: backend отвечает СТАРЫМ кодом — нет адресов: {0}. Закройте окно 'DARAVE backend' (или stop_darave.bat) и запустите заново." -f ($missing -join ", ")) "Red"
            }
            if ($v.mp3.name -ne "lameenc" -and $v.mp3.name -ne "ffmpeg") {
                Write-Log ("mp3 будет кодировать '{0}' — это НЕ 320 кбит/с. Нужен lameenc в этом Python." -f $v.mp3.name) "Yellow"
            }
            if ($v.stereo -eq $false) {
                Write-Log "ВНИМАНИЕ: рендер в моно — значит код старый." "Red"
            }
        } catch {
            Write-Log "Backend не отвечает на /api/version — скорее всего это старый процесс. Закройте окно 'DARAVE backend' и запустите заново." "Yellow"
        }
    }
    else {
        Write-Log "Backend прошёл проверку импорта, но за ~16 сек не ответил на порту $($cfg.Port) — смотрите окно 'DARAVE backend', там причина (например, порт занят другим процессом)." "Yellow"
        Write-Log "Companion всё равно запущу — если backend поднимется чуть позже, companion переподключится сам." "Gray"
    }

    # ---------- 5. companion ----------
    $midiMode = if ($hasRtmidi) { "rtmidi" } else { "mock" }

    # На Windows одного запущенного процесса loopMIDI недостаточно — ему ещё
    # нужно ОДИН РАЗ вручную создать порт с именем MidiPortName в его окне
    # (loopMIDI живёт в трее, окна на панели задач может не быть — отсюда и
    # ощущение "не запускается", хотя процесс на деле работает). Без этого
    # порта rtmidi упадёт при открытии companion — проверяем ДО запуска окна
    # и, если порта нет, честно откатываемся в mock вместо непонятного краша.
    if ($midiMode -eq "rtmidi") {
        $portCheckCode = @'
import sys, rtmidi
name = sys.argv[1]
ports = rtmidi.MidiOut().get_ports()
sys.exit(0 if any(name in p for p in ports) else 1)
'@
        $portCheck = Invoke-Native $companionPy @("-c", $portCheckCode, $cfg.MidiPortName)
        if (-not $portCheck.Ok) {
            Write-Log ("Виртуальный MIDI-порт '{0}' не создан в loopMIDI — это разовая настройка вручную (loopMIDI сидит в трее, окна на панели задач нет):" -f $cfg.MidiPortName) "Yellow"
            Write-Log "  1. Найдите значок loopMIDI в системном трее (стрелка ^ рядом с часами, если не видно сразу) и откройте его окно." "Yellow"
            Write-Log ("  2. В поле имени портов впишите ровно:  {0}" -f $cfg.MidiPortName) "Yellow"
            Write-Log "  3. Нажмите '+' — порт создан и запомнится loopMIDI на все следующие запуски." "Yellow"
            Write-Log "Пока поднимаю companion в mock-режиме (чат/библиотека/стратегия работают, команды в Mixxx не уходят) — после создания порта просто перезапустите start_darave.bat." "Yellow"
            $midiMode = "mock"
        } else {
            Write-Log ("MIDI-порт '{0}' на месте." -f $cfg.MidiPortName) "Green"
        }
    }

    $companionCmd = "Set-Location -LiteralPath '$($cfg.DaraveDir)'; " +
                    "`$host.UI.RawUI.WindowTitle = 'DARAVE companion ($midiMode)'; " +
                    "& '$companionPy' companion_main.py --midi-backend $midiMode --telemetry-backend $midiMode " +
                    "--port-name '$($cfg.MidiPortName)' --ws-url 'ws://localhost:$($cfg.Port)' " +
                    "--companion-id '$($cfg.RoomCode)' --recordings-dir '$($cfg.RecordingsDir)'"
    Write-Log ("Запускаю companion (режим MIDI: {0})..." -f $midiMode) "Green"
    if ($hideConsoles) {
        $companionCmd += " 2>&1 | Out-File -FilePath '$companionConsoleLog' -Append -Encoding utf8"
        Start-Process -FilePath "powershell" -WindowStyle Hidden `
                      -ArgumentList @("-NoProfile", "-Command", $companionCmd)
    } else {
        Start-Process -FilePath "powershell" -ArgumentList @("-NoExit", "-Command", $companionCmd)
    }
    Start-Sleep -Seconds 2

    # ---------- 6. браузер ----------
    $url = "http://localhost:$($cfg.Port)/?room=$($cfg.RoomCode)"
    Write-Log "Открываю $url" "Green"
    Start-Process $url
    if ($hideConsoles) {
        Write-Log "== Готово. Остановить — stop_darave.bat (окон нет, они скрыты). ==" "Cyan"
    } else {
        Write-Log "== Готово. Остановить — закрыть окна 'DARAVE backend' и 'DARAVE companion'. ==" "Cyan"
    }
}
catch {
    # Скрытое окно и ошибка — худшее сочетание: диджей видит, что «ничего
    # не произошло», и ему негде это прочитать. Поэтому на ошибке окно
    # возвращаем.
    Set-OwnConsole $true
    Write-Log "ОШИБКА: $($_.Exception.Message)" "Red"
    if ($_.InvocationInfo) { Write-Log "Место: $($_.InvocationInfo.PositionMessage)" "Red" }
}
finally {
    Write-Host ""
    Write-Host "Лог этого запуска: $logPath"
    # Ждать Enter в невидимом окне бессмысленно — процесс висел бы вечно.
    if (-not $Script:ConsoleHidden) {
        Read-Host "Нажмите Enter, чтобы закрыть это окно"
    }
}
