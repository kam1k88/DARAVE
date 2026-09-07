"""
OSC output backend — sends Mixxx control changes over UDP instead of MIDI.

Why this exists. On Windows `RtMidiBackend` cannot create a virtual MIDI
port (RtMidi has no such API there), so it requires loopMIDI — the only
DARAVE dependency that cannot be installed from pip. OSC needs no driver:
a UDP socket is enough. This backend is the escape hatch, and a second
transport for TouchOSC / OSCleton style receivers.

It requires a Mixxx built from the OSC branch (PR #14388,
Eve00000/mixxx:OSCLiblo). Official 2.7 builds have no OSC at all.

Design: variant A. This class sits at the SAME seam as MidiBackend and
takes ready MIDI bytes, decoding them back into (group, key, value). No
change is needed anywhere upstream (midi_mapping, scheduler, techniques).
The reverse tables are derived from mixxx_controls — the same single
source of truth the XML mapping is generated from — so they cannot drift
apart from the forward direction.

=== THE IMPORTANT PART: setParameter vs setValue ===

Mixxx's OSC receiver calls `proxy->setParameter(value)` in `doSet()`.
DARAVE's SysEx branch ("S|") calls `engine.setValue()` directly, with RAW
values. These are NOT the same thing and mixing them up is loud and
dangerous: `S|[Channel1]|pregain|1.0` means raw gain 1.0 out of 0..4
(i.e. unity), but sent verbatim over OSC it becomes setParameter(1.0),
which is the MAXIMUM — gain 4.0, +12 dB into the mix.

So every raw value must be converted to a 0..1 parameter before it goes
out over OSC. The conversion depends on the control's range, which is why
RAW_TO_PARAM below is keyed on (group family, key) and not on key alone:
`parameter1` on an EqualizerRack is 0..4, while `parameter1` on an FX slot
has whatever range that particular effect declares.

Anything whose range we do not know is REFUSED and counted, never guessed.
A silently wrong gain is worse than a skipped message — see the roformer
fallback story in claude/stems.md.

=== WHAT CANNOT GO OVER OSC AT ALL ===

* "E|" engine calls (brake / spinback / softStart). These are not
  ControlObjects, they are script-engine functions. Mixxx's OSC receiver
  only resolves ConfigKeys, so there is no path for them. Turntable tricks
  stay on MIDI.
* "T|" toggle. OSC has no toggle verb, and flipping a value requires
  knowing the current one. Mixxx can report it via /Get/cov, but that is
  an async round trip and would break scheduler timing.
* beatjump — the value is a number of beats, not a 0..1 parameter.

For those, run the MIDI backend alongside (see make_backend("osc") notes).
"""
from __future__ import annotations

import re
import socket
import struct
from collections import Counter

from midi_bridge import MidiBackend
from mixxx_controls import (
    BY_ID,
    CC_NUMBER,
    DECK_CHANNEL,
    MASTER_CHANNEL,
    NOTE_NUMBER,
    resolve_group_key,
    variant_note,
)
import midi_mapping as mm

# ----------------------------------------------------------------- OSC wire

def encode_message(address: str, value: float) -> bytes:
    """Minimal OSC 1.0 encoder: one float argument.

    Written by hand on purpose — pulling in python-osc for 20 lines would
    add a dependency to the very component whose point is having none.
    Mixxx reads argv[0]->f, so a single float is all it ever looks at.
    """
    def pad(raw: bytes) -> bytes:
        return raw + b"\0" * (4 - len(raw) % 4)

    return pad(address.encode("ascii") + b"\0") + pad(b",f\0") + struct.pack(">f", value)


# ------------------------------------------------- exact port of translatePath
#
# A line-by-line port of OscFunctions::translatePath() from the PR, INCLUDING
# its bugs — the point is to know what Mixxx will actually do with a path, not
# what it ought to do. Order matters: the general patterns are tested before
# the specific ones, which is why the hotcue/loop branches at the bottom are
# unreachable. Qt's QRegularExpression::match() is not anchored, so re.search
# is used rather than re.match.

_STEM_SPECIFIC = re.compile(r"/([^/]+)/Channel(\d+)Stem(\d+)/(.*)")
_UNDERSCORE_STEM = re.compile(r"/([^/]+)/Channel(\d+)_Stem(\d+)/(.*)")
_COMPLEX_RACK = re.compile(r"/([^/]+)/([^/]+)/([^/]+)/([^/]+)")
_NESTED_RACK = re.compile(r"/([^/]+)/([^/]+)/([^/]+)")
_CHANNEL_SPECIFIC = re.compile(r"/Channel(\d+)/(.*)")
_GENERAL = re.compile(r"/([^/]+)/([^/]+)")


def translate_path(path: str) -> tuple[str, str] | None:
    """OSC path -> (group, key) exactly as Mixxx's OSC branch would resolve it."""
    m = _STEM_SPECIFIC.search(path)
    if m:
        return f"[{m.group(1)}_[Channel{m.group(2)}Stem{m.group(3)}]]", m.group(4)
    m = _UNDERSCORE_STEM.search(path)
    if m:
        return f"[{m.group(1)}_[Channel{m.group(2)}_Stem{m.group(3)}]]", m.group(4)
    m = _COMPLEX_RACK.search(path)
    if m:
        unit = m.group(2)
        if unit.startswith("Channel"):
            unit = f"[{unit}]"
        return f"[{m.group(1)}_{unit}_{m.group(3)}]", m.group(4)
    m = _NESTED_RACK.search(path)
    if m:
        return f"[{m.group(1)}_{m.group(2)}]", m.group(3)
    m = _CHANNEL_SPECIFIC.search(path)
    if m:
        return f"[Channel{m.group(1)}]", m.group(2)
    m = _GENERAL.search(path)
    if m:
        return f"[{m.group(1)}]", m.group(2)
    return None


def control_path(group: str, key: str) -> str | None:
    """Mixxx group/key -> OSC path, or None if this control is UNREACHABLE.

    Instead of trusting a string transformation, every candidate spelling is
    round-tripped through the port of translatePath above and accepted only
    if it maps back to exactly the group and key we started from. That turns
    "should work" into "verified", and it is what caught the QuickEffect hole
    below.

    Known casualty: [QuickEffectRack1_[Channel1]] — the deck filter. Its only
    possible spelling is the 3-segment /QuickEffectRack1/Channel1/super1, and
    nestedRackRegex resolves that to [QuickEffectRack1_Channel1] without the
    inner brackets, which is not a real ControlObject. Mixxx then drops the
    message silently (the miss is only logged under sDebugOSC). The same
    control on a STEM works, because underscoreStemRegex does add the
    brackets. So the filter knob — one of the most used controls there is —
    cannot be driven over OSC in the PR as it stands.
    """
    inner = group[1:-1] if group.startswith("[") and group.endswith("]") else group
    # [A_[B]_C] -> /A/B/C ;  [A_[B]] -> /A/B ;  [A_B] -> /A/B ;  [A] -> /A
    candidate = "/" + inner.replace("_[", "/").replace("]_", "/").rstrip("]") + "/" + key
    candidate = candidate.replace("]/", "/")
    return candidate if translate_path(candidate) == (group, key) else None


# ------------------------------------------------- raw value -> 0..1 parameter

_BOOL_KEYS = frozenset({
    "play", "mute", "quantize", "slip_enabled", "reverseroll", "eject",
    "enabled", "sync_enabled", "keylock",
})
_UNIT_KEYS = frozenset({"volume", "super1"})          # already 0..1
_ZERO_TO_FOUR = frozenset({"pregain"})                 # 0..4, unity at 1.0
_EQ_PARAMS = frozenset({"parameter1", "parameter2", "parameter3"})
_REFUSE_KEYS = frozenset({"beatjump"})


def raw_to_parameter(group: str, key: str, raw: float) -> float | None:
    """Convert a raw setValue into the 0..1 parameter OSC expects.

    Returns None when the range is unknown — the caller must then skip the
    message and count it, not send a guess.
    """
    if key in _REFUSE_KEYS:
        return None
    if key in _BOOL_KEYS or key.startswith("button_parameter") \
            or key.startswith("group_") or key.startswith("beatlooproll_") \
            or key.startswith("beatloop_") or key.startswith("hotcue_"):
        return 1.0 if float(raw) >= 0.5 else 0.0
    if key in _UNIT_KEYS:
        return max(0.0, min(1.0, float(raw)))
    if key in _ZERO_TO_FOUR:
        return max(0.0, min(1.0, float(raw) / 4.0))
    if key == "rate":                                  # -1..1
        return max(0.0, min(1.0, (float(raw) + 1.0) / 2.0))
    if key in _EQ_PARAMS:
        if group.startswith("[EqualizerRack"):         # 0..4, unity at 1.0
            return max(0.0, min(1.0, float(raw) / 4.0))
        return None                                    # FX slot: range unknown
    return None


# ------------------------------------------------------------ reverse tables

def _build_reverse() -> tuple[dict, dict]:
    """(channel, note) -> (control, deck, variant) and (channel, cc) -> (control, deck).

    Built by asking the FORWARD functions what they emit, rather than by
    re-deriving the allocation. If the allocation in mixxx_controls ever
    changes, this follows automatically.
    """
    notes: dict[tuple[int, int], tuple] = {}
    ccs: dict[tuple[int, int], tuple] = {}
    for deck, channel in DECK_CHANNEL.items():
        for ctrl in BY_ID.values():
            chan = MASTER_CHANNEL if ctrl.scope == "master" else channel
            if ctrl.variants:
                for v in ctrl.variants:
                    try:
                        notes[(chan, variant_note(ctrl.id, v))] = (ctrl, deck, v)
                    except Exception:
                        pass
            if ctrl.id in NOTE_NUMBER:
                notes.setdefault((chan, NOTE_NUMBER[ctrl.id]), (ctrl, deck, None))
            if ctrl.id in CC_NUMBER:
                ccs.setdefault((chan, CC_NUMBER[ctrl.id]), (ctrl, deck))
    return notes, ccs


NOTE_REVERSE, CC_REVERSE = _build_reverse()


# ------------------------------------------------------------------- backend

class OscBackend(MidiBackend):
    def __init__(self, host: str = "127.0.0.1", port: int = 9000,
                 strict: bool = False) -> None:
        self.host, self.port = host, port
        self.strict = strict
        # One socket for the whole session. Mixxx's own sender creates and
        # frees an lo_address per message; on a ramp of hundreds of events
        # that is pure overhead, and there is no reason to copy it here.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0
        self.skipped: Counter = Counter()
        self.log: list[tuple[str, float]] = []

    # -- semantic entry point (this is the seed of "variant B") ------------
    def set_control(self, group: str, key: str, parameter: float) -> None:
        """Send an already-normalised 0..1 parameter. Full float precision.

        Upstream code can migrate to this one action at a time; until then
        send() below feeds it from decoded MIDI bytes.
        """
        path = control_path(group, key)
        if path is None:
            self._skip("unreachable-path-in-osc-pr", f"{group},{key}")
            return
        self._sock.sendto(encode_message(path, float(parameter)), (self.host, self.port))
        self.sent += 1
        self.log.append((path, float(parameter)))

    # -- byte-compatible entry point ---------------------------------------
    def send(self, message: list[int]) -> None:
        if not message:
            return
        if message[0] == mm.SYSEX_START:
            self._send_sysex(message)
            return
        status, chan = message[0] & 0xF0, message[0] & 0x0F
        if status == 0x90:
            self._send_note(chan, message[1], message[2])
        elif status == 0xB0:
            self._send_cc(chan, message[1], message[2])
        else:
            self._skip("unknown-status", f"0x{status:02X}")

    # -- internals ---------------------------------------------------------
    def _skip(self, reason: str, what: str) -> None:
        self.skipped[f"{reason}:{what}"] += 1
        if self.strict:
            raise ValueError(f"OSC backend cannot send {reason} ({what})")

    def _send_sysex(self, message: list[int]) -> None:
        parts = mm.decode_sysex(message).split(mm.SYSEX_SEP)
        op = parts[0]
        if op == "S" and len(parts) == 4:
            group, key, raw = parts[1], parts[2], float(parts[3])
            param = raw_to_parameter(group, key, raw)
            if param is None:
                self._skip("unknown-range", f"{group},{key}")
                return
            self.set_control(group, key, param)
        elif op == "P" and len(parts) == 3:
            # Press = 1 then 0. Mixxx has no momentary verb over OSC either,
            # so we spell it out; both datagrams go out back to back.
            self.set_control(parts[1], parts[2], 1.0)
            self.set_control(parts[1], parts[2], 0.0)
        elif op == "T":
            self._skip("toggle-needs-current-value", f"{parts[1]},{parts[2]}")
        elif op == "E":
            self._skip("engine-function-not-a-control", parts[1])
        else:
            self._skip("bad-sysex", op)

    def _send_note(self, chan: int, note: int, velocity: int) -> None:
        hit = NOTE_REVERSE.get((chan, note))
        if hit is None:
            self._skip("unmapped-note", f"ch{chan}/{note}")
            return
        ctrl, deck, variant = hit
        group, key = resolve_group_key(ctrl, deck, variant)
        self.set_control(group, key, 1.0 if velocity > 0 else 0.0)

    def _send_cc(self, chan: int, controller: int, value: int) -> None:
        hit = CC_REVERSE.get((chan, controller))
        if hit is None:
            self._skip("unmapped-cc", f"ch{chan}/{controller}")
            return
        ctrl, deck = hit
        group, key = resolve_group_key(ctrl, deck)
        # CC already went through <normal/> semantics on the XML side, i.e.
        # it IS a 0..1 parameter. Nothing to convert — but note the damage
        # is already done: 7 bits, 128 steps. Full precision only arrives
        # when upstream calls set_control() directly.
        self.set_control(group, key, value / 127.0)

    def report(self) -> str:
        lines = [f"OSC -> {self.host}:{self.port}   sent: {self.sent}"]
        for reason, n in self.skipped.most_common():
            lines.append(f"  skipped {n:4d}  {reason}")
        return "\n".join(lines)

    def close(self) -> None:
        self._sock.close()
