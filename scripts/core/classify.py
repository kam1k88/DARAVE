"""
scripts/core/classify.py — DnB-specific track classification.

Provides:
  classify_tempo(bpm)  -> 'half-time' | 'full-time' | 'downtempo' | 'other'
  energy_to_el(e)      -> 1..5  (Energy Level)
  el_label(el)         -> human-readable label
  bpm_type_label(t)    -> short display label
  are_bpm_compatible(a, b) -> True if smooth mix possible

Pure functions with no side effects.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# BPM classification thresholds (DnB-specific)
# ---------------------------------------------------------------------------

HALF_TIME_MIN = 80
HALF_TIME_MAX = 95
FULL_TIME_MIN = 165
FULL_TIME_MAX = 185
DOWNTEMPO_MIN = 105
DOWNTEMPO_MAX = 130


def classify_tempo(bpm: float) -> str:
    """
    Classify a track's BPM into a DnB-relevant tempo category.

    Returns
    -------
    str : 'half-time' | 'full-time' | 'downtempo' | 'other'

    Examples
    --------
    >>> classify_tempo(87.6)
    'half-time'
    >>> classify_tempo(172.3)
    'full-time'
    >>> classify_tempo(117.5)
    'downtempo'
    >>> classify_tempo(140.0)
    'other'
    """
    if HALF_TIME_MIN <= bpm <= HALF_TIME_MAX:
        return "half-time"
    elif FULL_TIME_MIN <= bpm <= FULL_TIME_MAX:
        return "full-time"
    elif DOWNTEMPO_MIN <= bpm <= DOWNTEMPO_MAX:
        return "downtempo"
    else:
        return "other"


# ---------------------------------------------------------------------------
# Energy Level (EL 1-5) mapping
# ---------------------------------------------------------------------------

# EL thresholds - raw energy (0-100 scale) -> discrete level
_EL_THRESHOLDS = [
    (20,  1),   # 0-20   -> EL 1 (Intro / Chill)
    (40,  2),   # 21-40  -> EL 2 (Building)
    (60,  3),   # 41-60  -> EL 3 (Peak)
    (80,  4),   # 61-80  -> EL 4 (Heavy)
    (100, 5),   # 81-100 -> EL 5 (Climax)
]

_EL_LABELS = {
    1: "Intro",
    2: "Building",
    3: "Peak",
    4: "Heavy",
    5: "Climax",
}


def energy_to_el(energy: float) -> int:
    """
    Convert raw energy (0.0-1.0 or 0-100) to Energy Level (1-5).

    If energy is in [0.0, 1.0] range, it is automatically scaled to 0-100.
    """
    raw = energy * 100.0 if energy <= 1.0 else energy
    raw = max(0.0, min(100.0, raw))

    for threshold, level in _EL_THRESHOLDS:
        if raw <= threshold:
            return level
    return 5


def el_label(el: int) -> str:
    """Return human-readable label for an Energy Level (1-5)."""
    return _EL_LABELS.get(el, "Unknown")


# ---------------------------------------------------------------------------
# BPM type helpers
# ---------------------------------------------------------------------------

def bpm_type_label(tempo_type: str) -> str:
    """Return a short display label for a tempo type."""
    return {
        "half-time":  "Half",
        "full-time":  "Full",
        "downtempo":  "Down",
        "other":      "--",
    }.get(tempo_type, "--")


def are_bpm_compatible(type_a: str, type_b: str) -> bool:
    """
    Check if two tempo types allow smooth mixing.

    Compatible pairs:
      half-time <-> half-time (same BPM)
      half-time <-> full-time (same underlying tempo, different grid)
      full-time <-> full-time (same BPM)
    Incompatible:
      anything <-> downtempo (different BPM)
    """
    if type_a == type_b:
        return True
    pair = frozenset([type_a, type_b])
    if pair == frozenset(["half-time", "full-time"]):
        return True  # 87 * 2 = 174 -- same tempo, different grid
    return False
