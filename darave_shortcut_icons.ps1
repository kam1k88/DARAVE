<#
    Ставит иконку DARAVE ярлыкам Mixxx и лаунчера.

    Ярлык (.lnk) хранит ссылку на иконку отдельным полем IconLocation, и по
    умолчанию оно пустое — тогда Windows берёт иконку из самой программы,
    то есть из ресурсов mixxx.exe. Достаточно прописать в это поле свой
    .ico, и правки бинарника не нужны: сам Mixxx остаётся нетронутым.

    Обрабатываются рабочий стол (свой и общий), меню «Пуск» (своё и общее)
    и закреплённое на панели задач. Ярлыки в общих папках принадлежат
    системе, туда пишем только если Windows даст права — без прав просто
    сообщаем, а не падаем.

    Запуск:
      powershell -ExecutionPolicy Bypass -File darave_shortcut_icons.ps1
      ...                                     -Rename     # ещё и переименовать «Mixxx» -> «DARAVE»
      ...                                     -Restore    # вернуть родные иконки
#>
param(
    [string]$IconPath = "",
    [switch]$Rename,
    [switch]$Restore
)

$ErrorActionPreference = "SilentlyContinue"

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $IconPath = Join-Path $PSScriptRoot "darave.ico"
}
if (-not $Restore -and -not (Test-Path -LiteralPath $IconPath)) {
    Write-Host "Не нашёл иконку: $IconPath" -ForegroundColor Red
    return
}

$folders = @(
    [Environment]::GetFolderPath('Desktop'),
    [Environment]::GetFolderPath('CommonDesktopDirectory'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'),
    (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$shell = New-Object -ComObject WScript.Shell
$changed = 0
$failed = @()

foreach ($folder in $folders) {
    $links = Get-ChildItem -LiteralPath $folder -Filter *.lnk -Recurse -ErrorAction SilentlyContinue
    foreach ($link in $links) {
        $sc = $shell.CreateShortcut($link.FullName)
        $target = "$($sc.TargetPath)"
        # ярлык считаем нашим, только если он ведёт на mixxx.exe или на лаунчер
        $isMixxx = $target -match '(?i)mixxx\.exe$'
        $isLauncher = ($target -match '(?i)start_darave\.(bat|ps1|cmd)$') -or
                      ("$($sc.Arguments)" -match '(?i)start_darave\.(bat|ps1|cmd)')
        if (-not ($isMixxx -or $isLauncher)) { continue }

        try {
            if ($Restore) {
                # пустая ссылка = «иконку берём из самой программы»
                $sc.IconLocation = ",0"
            } else {
                $sc.IconLocation = "$IconPath,0"
            }
            $sc.Save()
            $changed++
            $where = Split-Path -Leaf $folder
            Write-Host ("  {0}  <- {1}" -f $link.FullName, $(if ($Restore) { "родная иконка" } else { "DARAVE" })) -ForegroundColor Green

            if ($Rename -and -not $Restore -and $isMixxx -and $link.BaseName -match '(?i)^mixxx$') {
                $newPath = Join-Path $link.DirectoryName "DARAVE.lnk"
                if (-not (Test-Path -LiteralPath $newPath)) {
                    Rename-Item -LiteralPath $link.FullName -NewName "DARAVE.lnk" -ErrorAction Stop
                    Write-Host "    переименован в DARAVE.lnk" -ForegroundColor Green
                }
            }
        } catch {
            $failed += $link.FullName
        }
    }
}

Write-Host ""
if ($changed -gt 0) { Write-Host "Обновлено ярлыков: $changed" -ForegroundColor Green }
else { Write-Host "Ярлыков Mixxx не нашлось — возможно, они в другом месте." -ForegroundColor Yellow }
if ($failed.Count -gt 0) {
    Write-Host "Не хватило прав на эти (они в общих папках — запустите от администратора):" -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
}
Write-Host "Проводник может показывать старую иконку из кэша. Если так — выйти и войти в систему," -ForegroundColor Gray
Write-Host "или: ie4uinit.exe -show" -ForegroundColor Gray
