# release.ps1 — пересборка и выкладка одной командой.
#
#   .\release.ps1 -Version 1.0.2
#   .\release.ps1 -Version 1.0.2 -RemovePrevious   ещё и удалить прошлый релиз
#   .\release.ps1 -Version 1.0.2 -DryRun           показать шаги, ничего не делая
#
# Что делает по порядку:
#   1. проверяет gh и вход в него
#   2. собирает через build.ps1 (падает, если установщик не вышел)
#   3. убеждается, что установщик СВЕЖИЙ, а не лежит с прошлого раза
#   4. считает SHA-256 и сверяет его с тем, что стоит на сайте
#   5. создаёт релиз в darave-releases и ПРОВЕРЯЕТ, что файл скачивается
#   6. правит на сайте ссылку, размер и хеш, коммитит и пушит
#   7. только теперь, и только по просьбе, удаляет прошлый релиз
#
# Порядок не случайный. Релиз создаётся ДО правки сайта, потому что сайт
# ссылается на файл: обнови мы сайт первым, между пушем и загрузкой файла
# была бы дыра, в которой кнопка «Скачать» ведёт в 404. А прошлый релиз
# удаляется последним — пока новый не проверен, он единственный рабочий.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Repo = "kam1k88/darave-releases",
    [switch]$RemovePrevious,
    [switch]$SkipBuild,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $root

function Say([string]$m, [string]$c = "Gray") {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -ForegroundColor $c
}
function Fail([string]$m) { Say $m Red; exit 1 }
function Step([string]$m) { Write-Host ""; Say $m Cyan }

if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Версия должна быть вида 1.0.2, а не «$Version»" }
if ($DryRun) {
    Write-Host ""
    Say "ХОЛОСТОЙ ПРОГОН. Ничего не будет ни собрано, ни выложено, ни изменено." Yellow
}

$tag       = "v$Version"
$setupName = "DARAVE-$Version-setup.exe"
$setup     = Join-Path $root "installer\$setupName"
$indexPath = Join-Path $root "site\index.html"

# --------------------------------------------------------------- 1. gh
Step "проверяю gh"
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Fail "gh не найден. Поставьте: winget install GitHub.cli — и откройте новый терминал."
}
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "gh не авторизован. Выполните: gh auth login" }
Say "gh на месте и авторизован"

# Релиз с такой меткой не должен существовать: иначе gh молча добавит файл
# ко ВТОРОМУ релизу или откажет на полпути, уже после сборки.
gh release view $tag --repo $Repo 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Fail "Релиз $tag в $Repo уже есть. Возьмите другую версию или удалите его: gh release delete $tag --repo $Repo --cleanup-tag"
}

# --------------------------------------------------------- 2. сборка
if ($SkipBuild) {
    Say "сборка пропущена (-SkipBuild)" Yellow
} else {
    Step "собираю $Version"
    if ($DryRun) {
        Say "(dry-run) .\build.ps1 -Version $Version"
    } else {
        & (Join-Path $root "build.ps1") -Version $Version
        if ($LASTEXITCODE -ne 0) { Fail "build.ps1 завершился с ошибкой — дальше не идём" }
    }
}

# ------------------------------------------- 3. установщик и его свежесть
Step "проверяю установщик"
if ($DryRun -and -not (Test-Path $setup)) {
    Say "(dry-run) установщика нет — дальше показываю шаги на словах" Yellow
} else {
    if (-not (Test-Path $setup)) { Fail "Нет файла $setup. Установщик не собрался." }

    # Главная проверка. Один раз старый сентябрьский файл переименовали в
    # новую версию и чуть не выложили сборку без платного замка. Если
    # установщик старше свежесобранного exe — это НЕ результат этой сборки.
    $exe = Join-Path $root "dist\DARAVE\DARAVE.exe"
    if (Test-Path $exe) {
        $tExe   = (Get-Item $exe).LastWriteTime
        $tSetup = (Get-Item $setup).LastWriteTime
        if ($tSetup -lt $tExe) {
            Fail ("Установщик СТАРШЕ программы: setup $($tSetup.ToString('dd.MM HH:mm')) против exe $($tExe.ToString('dd.MM HH:mm')). " +
                  "Это файл с прошлого раза, а не результат этой сборки.")
        }
    }
    $size   = (Get-Item $setup).Length
    $sizeMb = [math]::Round($size / 1MB)
    $hash   = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
    Say "$setupName — $sizeMb МБ"
    Say "SHA-256 $hash"

    # Совпал с тем, что уже на сайте, — значит файл не изменился.
    $index = Get-Content $indexPath -Raw
    if ($index -match [regex]::Escape($hash)) {
        Fail "Этот хеш уже стоит на сайте — файл не изменился с прошлой выкладки."
    }
}

# ------------------------------------------------------------ 4. релиз
Step "создаю релиз $tag в $Repo"
$notes = @"
DARAVE $Version для Windows.

Установщик: $setupName
SHA-256: $hash

Проверить после скачивания:
    Get-FileHash .\$setupName -Algorithm SHA256
"@

if ($DryRun) {
    Say "(dry-run) gh release create $tag `"$setup`" --repo $Repo --title `"DARAVE $Version`""
} else {
    gh release create $tag $setup --repo $Repo --title "DARAVE $Version" --notes $notes
    if ($LASTEXITCODE -ne 0) { Fail "gh release create не отработал" }

    # «Создался» и «скачивается» — разные вещи: загрузка файла может
    # оборваться, а релиз при этом останется. Сайт направим на него только
    # после того, как файл действительно отдаётся.
    Step "проверяю, что файл скачивается"
    $url = "https://github.com/$Repo/releases/download/$tag/$setupName"
    try {
        $head = Invoke-WebRequest -Uri $url -Method Head -MaximumRedirection 5 -ErrorAction Stop
        # В PowerShell 7 значения заголовков — массивы строк, в 5.1 —
        # просто строка, и [0] там вернул бы ПЕРВЫЙ СИМВОЛ. Разбираем оба.
        $cl = $head.Headers['Content-Length']
        if ($cl -is [array]) { $cl = $cl[0] }
        $remote = [int64]$cl
        if ($remote -ne $size) {
            Fail "На GitHub лежит $remote байт вместо $size — загрузка оборвалась. Сайт НЕ трогаю."
        }
        Say "файл отдаётся, $remote байт — сходится" Green
    } catch {
        Fail "Файл по адресу $url не скачивается: $($_.Exception.Message). Сайт НЕ трогаю."
    }
}

# ------------------------------------------------------------- 5. сайт
Step "правлю сайт"

# Замены по образцу, а не по точной строке: иначе скрипт сработает ровно
# один раз, а в следующий выпуск тихо ничего не заменит. И каждая замена
# проверяется — молчаливый промах здесь дороже падения.
function Replace-Once([string]$text, [string]$pattern, [string]$replacement, [string]$what) {
    $n = ([regex]::Matches($text, $pattern)).Count
    if ($n -eq 0) { Fail "На сайте не нашёл: $what. Разметка изменилась — правьте руками." }
    Say "  $what — совпадений: $n"
    return [regex]::Replace($text, $pattern, $replacement)
}

if (-not $DryRun) {
    $index = Get-Content $indexPath -Raw
    $index = Replace-Once $index `
        'releases/download/v[\d.]+/DARAVE-[\d.]+-setup\.exe' `
        "releases/download/$tag/$setupName" "ссылка на скачивание"
    $index = Replace-Once $index `
        'Скачать DARAVE [\d.]+ для Windows · \d+ МБ' `
        "Скачать DARAVE $Version для Windows · $sizeMb МБ" "подпись кнопки"
    $index = Replace-Once $index `
        '<b>[0-9a-f]{64}</b>' `
        "<b>$hash</b>" "контрольная сумма"
    $index = Replace-Once $index `
        'Get-FileHash \.\\DARAVE-[\d.]+-setup\.exe' `
        "Get-FileHash .\$setupName" "команда проверки"

    # UTF-8 без BOM: с BOM браузер покажет мусор в первой строке.
    [System.IO.File]::WriteAllText($indexPath, $index, (New-Object System.Text.UTF8Encoding $false))
    Say "site\index.html обновлён" Green

    Step "коммичу и пушу сайт"
    # server.py тоже: build.ps1 приводит в нём APP_VERSION к собираемой
    # версии, и без этой строки правка осталась бы висеть незакоммиченной
    # — а в следующий раз уехала бы в чужой коммит.
    git add site/index.html server.py
    # ${Version}, а не $Version: двоеточие сразу после имени PowerShell
    # читает как префикс области видимости ($env:, $script:) и падает ещё
    # на разборе. Тот же капкан, что и ${env:ProgramFiles(x86)} выше.
    git commit -m "Релиз ${Version}: ссылка и контрольная сумма"
    if ($LASTEXITCODE -ne 0) { Fail "git commit не прошёл" }
    git push
    if ($LASTEXITCODE -ne 0) { Fail "git push не прошёл" }
    Say "выкладка сайта пойдёт сама: GitHub Actions ловит изменения в site/**" Green
} else {
    Say "(dry-run) в site\index.html поменялись бы ссылка, подпись кнопки, хеш и команда проверки"
}

# ------------------------------------------- 5b. локальная копия рядом
#
# Папка darave-releases — это клон репозитория релизов, и держать в ней
# свежий установщик под рукой удобно: не надо качать свой же файл с
# GitHub, чтобы кому-то его передать. В сам репозиторий он не попадёт —
# там .gitignore на *.exe, потому что GitHub отклоняет файлы тяжелее
# 100 МБ, а установщик весит под 180.
$localCopyDir = Join-Path (Split-Path -Parent $root) "darave-releases"
if (Test-Path $localCopyDir) {
    Step "кладу копию в darave-releases"
    if ($DryRun) {
        Say "(dry-run) копия -> $localCopyDir\$setupName"
    } else {
        Copy-Item $setup (Join-Path $localCopyDir $setupName) -Force
        Say "копия: $localCopyDir\$setupName" Green
    }
} else {
    Say "папки darave-releases рядом нет — локальную копию пропускаю" Gray
}

# ------------------------------------------------- 6. прошлый релиз
if ($RemovePrevious) {
    Step "удаляю прошлый релиз"
    $prev = gh release list --repo $Repo --limit 20 --json tagName --jq ".[].tagName" |
            Where-Object { $_ -ne $tag } | Select-Object -First 1
    if (-not $prev) {
        Say "прошлых релизов нет" Yellow
    } elseif ($DryRun) {
        Say "(dry-run) gh release delete $prev --repo $Repo --cleanup-tag --yes"
    } else {
        gh release delete $prev --repo $Repo --cleanup-tag --yes
        if ($LASTEXITCODE -ne 0) { Say "не смог удалить $prev — удалите вручную" Yellow }
        else { Say "$prev удалён" Green }
    }
} else {
    Say "прошлый релиз оставлен. Удалить: .\release.ps1 ... -RemovePrevious, или вручную" Gray
}

Write-Host ""
if ($DryRun) {
    Say "ХОЛОСТОЙ ПРОГОН ЗАВЕРШЁН. Ничего не выложено, сайт не тронут." Yellow
    Say "Боевой запуск: .\release.ps1 -Version $Version" Gray
} else {
    Say "ГОТОВО: $tag выложен, сайт обновлён" Green
    Say "https://github.com/$Repo/releases/tag/$tag"
}
