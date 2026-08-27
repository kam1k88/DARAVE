<#
    Останавливает DARAVE: backend, companion и скрипт переименования окна.

    Понадобился, когда окна консолей стали скрытыми: раньше «остановить»
    означало закрыть два чёрных окна, а теперь закрывать нечего. Ищем
    процессы по КОМАНДНОЙ СТРОКЕ, а не по имени: имя у всех троих просто
    "python" или "powershell", и по нему легко прибить чужое.

    Mixxx и loopMIDI не трогаем — вы могли остаться в них работать.
    Нужно закрыть и Mixxx: stop_darave.bat -All
#>
param([switch]$All)

$ErrorActionPreference = "SilentlyContinue"

$targets = @(
    @{ Name = "backend";          Pattern = 'uvicorn\s+server:app' },
    @{ Name = "companion";        Pattern = 'companion_main\.py' },
    @{ Name = "заголовок окна";   Pattern = 'darave_window_title' }
)

$stopped = 0
$me = $PID

try {
    $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop)
} catch {
    Write-Host "Не смог получить список процессов: $($_.Exception.Message)" -ForegroundColor Red
    return
}

foreach ($t in $targets) {
    $hits = @($procs | Where-Object {
        $_.ProcessId -ne $me -and $_.CommandLine -and $_.CommandLine -match $t.Pattern
    })
    # uvicorn с --reload держит РАБОЧИЙ процесс отдельно, и в его командной
    # строке нет "uvicorn server:app" — он запущен через multiprocessing.
    # Без этого сирота продолжала держать порт 8765, и новый backend
    # поднимался рядом, но запросы уходили старому.
    $parentIds = @($hits | Select-Object -ExpandProperty ProcessId)
    if ($parentIds.Count -gt 0) {
        $hits += @($procs | Where-Object {
            $_.ProcessId -ne $me -and $parentIds -contains $_.ParentProcessId -and
            $_.Name -match '^(python|pythonw)'
        })
    }
    foreach ($h in $hits) {
        try {
            Stop-Process -Id $h.ProcessId -Force -ErrorAction Stop
            Write-Host ("остановлен {0} (PID {1})" -f $t.Name, $h.ProcessId) -ForegroundColor Green
            $stopped++
        } catch {
            Write-Host ("не смог остановить {0} (PID {1}): {2}" -f $t.Name, $h.ProcessId, $_.Exception.Message) -ForegroundColor Red
        }
    }
    if ($hits.Count -eq 0) { Write-Host ("{0}: не запущен" -f $t.Name) -ForegroundColor Gray }
}

if ($All) {
    foreach ($n in @("mixxx", "loopMIDI")) {
        $p = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
        foreach ($x in $p) {
            Stop-Process -Id $x.Id -Force -ErrorAction SilentlyContinue
            Write-Host ("закрыт {0} (PID {1})" -f $n, $x.Id) -ForegroundColor Yellow
            $stopped++
        }
    }
}

Write-Host ""
Write-Host ("Остановлено процессов: {0}" -f $stopped) -ForegroundColor Cyan
Start-Sleep -Seconds 2
