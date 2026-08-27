<#
    Переименовывает окно Mixxx в DARAVE и ставит ему иконку DARAVE.

    Почему снаружи, а не внутри Mixxx: настройки заголовка там нет,
    название — строка, зашитая в mixxx.exe, и она даже не попадает в файлы
    перевода. Править бинарник нельзя: та же строка участвует в путях к
    настройкам, и Mixxx перестанет находить свою конфигурацию. Иконка окна
    точно так же лежит в ресурсах exe.

    Поэтому меняем уже созданное окно через обычный Windows API:
    SetWindowText для заголовка и WM_SETICON для значка. Установку Mixxx
    скрипт не трогает вообще — закрыли его, и всё вернулось как было.

    Заголовок не прибивается намертво: Mixxx сам переписывает его при
    загрузке трека («Исполнитель - Трек | Mixxx»), поэтому мы ЗАМЕНЯЕМ в
    нём слово, а имя трека остаётся на месте.
#>
param(
    [string]$From = "Mixxx",
    [string]$To = "DARAVE",
    [string]$IconPath = "",     # пусто — взять darave.ico рядом со скриптом
    [int]$IntervalMs = 400,
    [int]$IdleExitMinutes = 60  # выйти, если Mixxx не запущен столько минут (0 — жить всегда)
)

$ErrorActionPreference = "SilentlyContinue"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class DaraveWin {
    public const uint WM_SETICON = 0x0080;
    public const int ICON_SMALL = 0;
    public const int ICON_BIG = 1;
    public const uint IMAGE_ICON = 1;
    public const uint LR_LOADFROMFILE = 0x00000010;
    public const int SM_CXICON = 11;
    public const int SM_CXSMICON = 49;

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetWindowTextW(IntPtr hWnd, string lpString);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadImageW(IntPtr hinst, string lpszName, uint uType,
                                           int cx, int cy, uint fuLoad);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessageW(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern int GetSystemMetrics(int nIndex);
}
"@

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $IconPath = Join-Path $PSScriptRoot "darave.ico"
}

# Иконки грузим ОДИН раз: каждый LoadImage создаёт новый HICON, и вызов их
# в цикле — это утечка дескрипторов на всё время работы скрипта.
$hIconSmall = [IntPtr]::Zero
$hIconBig = [IntPtr]::Zero
if (Test-Path -LiteralPath $IconPath) {
    $sm = [DaraveWin]::GetSystemMetrics([DaraveWin]::SM_CXSMICON)
    $bg = [DaraveWin]::GetSystemMetrics([DaraveWin]::SM_CXICON)
    if ($sm -le 0) { $sm = 16 }
    if ($bg -le 0) { $bg = 32 }
    $hIconSmall = [DaraveWin]::LoadImageW([IntPtr]::Zero, $IconPath, [DaraveWin]::IMAGE_ICON,
                                          $sm, $sm, [DaraveWin]::LR_LOADFROMFILE)
    $hIconBig = [DaraveWin]::LoadImageW([IntPtr]::Zero, $IconPath, [DaraveWin]::IMAGE_ICON,
                                        $bg, $bg, [DaraveWin]::LR_LOADFROMFILE)
}

$pattern = [regex]::Escape($From)
$iconDone = @{}          # какие окна уже получили иконку — чтобы не слать WM_SETICON каждые полсекунды
$idleTicks = 0
$ticksPerMinute = [int](60000 / [Math]::Max(50, $IntervalMs))

while ($true) {
    $procs = @(Get-Process -Name "mixxx" -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) {
        $iconDone.Clear()          # Mixxx закрылся — старые дескрипторы окон больше не наши
        if ($IdleExitMinutes -gt 0) {
            $idleTicks++
            if ($idleTicks -ge $IdleExitMinutes * $ticksPerMinute) { break }
        }
        Start-Sleep -Milliseconds 1000
        continue
    }
    $idleTicks = 0

    foreach ($p in $procs) {
        $h = $p.MainWindowHandle
        if ($h -eq [IntPtr]::Zero) { continue }

        $sb = New-Object System.Text.StringBuilder 512
        [void][DaraveWin]::GetWindowTextW($h, $sb, $sb.Capacity)
        $title = $sb.ToString()
        if (-not [string]::IsNullOrEmpty($title) -and $title -match $pattern) {
            $new = [regex]::Replace($title, $pattern, $To)
            if ($new -ne $title) { [void][DaraveWin]::SetWindowTextW($h, $new) }
        }

        $key = $h.ToString()
        if (-not $iconDone.ContainsKey($key) -and $hIconBig -ne [IntPtr]::Zero) {
            [void][DaraveWin]::SendMessageW($h, [DaraveWin]::WM_SETICON,
                [IntPtr][DaraveWin]::ICON_SMALL, $hIconSmall)
            [void][DaraveWin]::SendMessageW($h, [DaraveWin]::WM_SETICON,
                [IntPtr][DaraveWin]::ICON_BIG, $hIconBig)
            $iconDone[$key] = $true
        }
    }
    Start-Sleep -Milliseconds $IntervalMs
}
