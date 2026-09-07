# check_midi.ps1 — is Windows MIDI Services available, and can it replace loopMIDI?
#
# Answers three questions in one run:
#   1. Windows build — MIDI Services needs Win11 24H2 / 25H2 / 26H1.
#   2. Is the service actually installed on this machine?
#   3. Which MIDI ports does a MIDI 1.0 app (python-rtmidi, same API Mixxx
#      uses) actually see right now? That is the only thing that matters:
#      Microsoft says WinMM was replumbed onto the new service, but the
#      loopback docs never confirm loopback endpoints reach MIDI 1.0 apps.
#
# Run: right-click -> Run with PowerShell, or  pwsh -File check_midi.ps1
$ErrorActionPreference = 'SilentlyContinue'

function Head($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }

Head "Windows"
$os = Get-CimInstance Win32_OperatingSystem
$build = [System.Environment]::OSVersion.Version.Build
Write-Host "$($os.Caption)  build $build"
if ($build -ge 26100) { Write-Host "OK: supported build range for Windows MIDI Services" -ForegroundColor Green }
elseif ($build -ge 22000) { Write-Host "Windows 11, but older than 24H2 - update first" -ForegroundColor Yellow }
else { Write-Host "Windows 10 - Windows MIDI Services is NOT available. loopMIDI stays." -ForegroundColor Red }

Head "Windows MIDI Services present?"
$svc = Get-Service | Where-Object { $_.Name -match 'midi' -or $_.DisplayName -match 'MIDI' }
if ($svc) { $svc | Format-Table Name, Status, DisplayName -AutoSize | Out-String | Write-Host }
else { Write-Host "no MIDI-related service found" -ForegroundColor Yellow }

$pkg = Get-AppxPackage | Where-Object { $_.Name -match 'Midi' }
if ($pkg) { $pkg | Select-Object Name, Version | Format-Table -AutoSize | Out-String | Write-Host }
else { Write-Host "no MIDI Settings app package found" -ForegroundColor Yellow }

$tool = Get-Command midi.exe
if ($tool) { Write-Host "midi console tool: $($tool.Source)" -ForegroundColor Green }
else { Write-Host "midi console tool not on PATH (SDK/tools not installed)" -ForegroundColor Yellow }

foreach ($p in @("$env:ProgramFiles\Windows MIDI Services", "${env:ProgramFiles(x86)}\Windows MIDI Services")) {
    if (Test-Path $p) { Write-Host "found: $p" -ForegroundColor Green }
}

Head "MIDI ports as a MIDI 1.0 app sees them (python-rtmidi = what Mixxx uses)"
# Reuse the same interpreter the companion picks: 3.14 has no rtmidi wheels.
$py = @(
  "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
  "python"
) | Where-Object { (Get-Command $_ -ErrorAction SilentlyContinue) -or (Test-Path $_) } | Select-Object -First 1

if (-not $py) { Write-Host "no python with rtmidi found" -ForegroundColor Red; exit }
Write-Host "using $py"
& $py -c @"
import rtmidi
for label, cls in (('OUT', rtmidi.MidiOut), ('IN', rtmidi.MidiIn)):
    ports = cls().get_ports()
    print(f'  {label}: {len(ports)} port(s)')
    for i, p in enumerate(ports):
        print(f'      [{i}] {p}')
"@

Head "What to do with this"
Write-Host @"
If a loopback endpoint created in the MIDI Settings app shows up in the list
above, WinMM sees it and loopMIDI can go: point DARAVE at that port name and
nothing else changes.

If MIDI Settings created a loopback but it does NOT appear above, then
loopback endpoints are not reaching MIDI 1.0 apps on this build - keep
loopMIDI, and the OSC route stays on the table.

Note: Microsoft lists a known issue where third-party dynamic ports (loopMIDI,
loopBE, teVirtualMIDI, rtpMIDI) are 'not always visible' under the new stack.
If loopMIDI is running but missing above, that is this bug, not your setup.
"@
