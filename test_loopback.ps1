# test_loopback.ps1 — prove a Windows MIDI Services loopback pair carries
# DARAVE's real traffic, end to end, through the MIDI 1.0 API.
#
# The pair is cross-wired: what goes OUT to A arrives IN on B, and back.
# So the companion holds A (out and in), and Mixxx is pointed at B. This
# script plays both halves: it writes to A and listens on B, which is
# exactly the path companion -> Mixxx.
#
# Usage:  .\test_loopback.ps1                      (defaults to the built-in pair)
#         .\test_loopback.ps1 "DARAVE A" "DARAVE B"
param(
    [string]$OutName = "Default App Loopback (A)",
    [string]$InName  = "Default App Loopback (B)"
)

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { Write-Host "python312 not found" -ForegroundColor Red; exit 1 }

$code = @"
import sys, time, statistics
sys.path.insert(0, r'$PWD')
import rtmidi
import midi_mapping as mm

OUT_NAME, IN_NAME = r'''$OutName''', r'''$InName'''

def pick(ports, name):
    for i, p in enumerate(ports):
        if name in p:
            return i, p
    return None, None

mo, mi = rtmidi.MidiOut(), rtmidi.MidiIn()
oi, op = pick(mo.get_ports(), OUT_NAME)
ii, ip = pick(mi.get_ports(), IN_NAME)
if oi is None: print('OUT port not found:', OUT_NAME); print('have:', mo.get_ports()); sys.exit(1)
if ii is None: print('IN port not found:', IN_NAME);  print('have:', mi.get_ports());  sys.exit(1)
print(f'  out -> [{oi}] {op}')
print(f'  in  <- [{ii}] {ip}')

mo.open_port(oi)
mi.open_port(ii)
mi.ignore_types(sysex=False, timing=False, active_sense=False)   # SysEx is OFF by default!
time.sleep(0.3)

# 1. correctness: a real DARAVE SysEx must arrive byte for byte
sent = mm.sysex_set('[Channel1]', 'play', 1.0)
mo.send_message(sent)
got, deadline = None, time.time() + 1.0
while time.time() < deadline:
    m = mi.get_message()
    if m: got = m[0]; break
    time.sleep(0.002)

print()
print('  sent    :', mm.decode_sysex(sent))
if got is None:
    print('  received: NOTHING — loopback does not carry SysEx to MIDI 1.0 apps')
    sys.exit(2)
print('  received:', mm.decode_sysex(got))
print('  identical:', got == sent)

# 2. a CC ramp: 200 ticks, the shape a 32-bar crossfade actually sends
N = 200
lat = []
misses = 0
while mi.get_message():
    pass
for k in range(N):
    msg = mm.resolve_ramp_tick('crossfade', 'A', k / (N - 1))
    t0 = time.perf_counter()
    mo.send_message(msg)
    hit = False
    while time.perf_counter() - t0 < 0.05:
        m = mi.get_message()
        if m:
            lat.append((time.perf_counter() - t0) * 1000); hit = True; break
    if not hit: misses += 1
    time.sleep(0.005)

print()
print(f'  ramp: {N} ticks, delivered {len(lat)}, lost {misses}')
if lat:
    print(f'  latency ms  median {statistics.median(lat):.3f}  '
          f'p95 {sorted(lat)[int(len(lat)*0.95)-1]:.3f}  max {max(lat):.3f}')
    if len(lat) > 1:
        print(f'  jitter ms   stdev {statistics.stdev(lat):.3f}')
mi.close_port(); mo.close_port()

# 3. the OTHER direction: Mixxx sends telemetry out on B, companion reads it
#    on A. Same wire, opposite way. If this half is dead, commands work and
#    the agent goes blind.
mo2, mi2 = rtmidi.MidiOut(), rtmidi.MidiIn()
o2, _ = pick(mo2.get_ports(), IN_NAME)     # send on B
i2, _ = pick(mi2.get_ports(), OUT_NAME)    # listen on A
mo2.open_port(o2); mi2.open_port(i2)
mi2.ignore_types(sysex=False, timing=False, active_sense=False)
time.sleep(0.3)
tele = mm.sysex_set('[Channel1]', 'bpm', 174.0)   # shaped like real telemetry
mo2.send_message(tele)
back, deadline = None, time.time() + 1.0
while time.time() < deadline:
    m = mi2.get_message()
    if m: back = m[0]; break
    time.sleep(0.002)
print()
print('  reverse (B -> A, telemetry path):', 'OK' if back == tele else 'FAILED')
mi2.close_port(); mo2.close_port()
print()
print('  VERDICT:', 'loopback carries DARAVE traffic' if misses == 0 else 'LOSSES — do not migrate yet')
"@

Write-Host "=== loopback round-trip ===" -ForegroundColor Cyan
& $py -c $code
