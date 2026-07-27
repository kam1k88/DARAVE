"""
scripts/core/transition_intel.py — Intelligent transition technique selection (DARAVE v2.0).

Analyses a pair of tracks (SongStructure objects) and recommends the optimal
DJ mixing technique using 8 priority rules based on:
  1. BPM type (classify_tempo): half-time / full-time / downtempo / other
  2. Energy Level (EL 1-5): 0-20→EL1, 21-40→EL2, 41-60→EL3, 61-80→EL4, 81-100→EL5
  3. Camelot key: compatible (±1), same, tritone (±6)
  4. Energy delta: +20 or EL+2 → Quick Cut / Stutter / Reverse Drop

Priority rules (from spec):
  1. Tritone (Key ±6) → Echo Cut / Quick Cut
  2. BPM match + Key ±1 + smooth Energy → EQ Roller / Bass Swap / Phrase Matching
  3. Different BPM (Half↔Down, Full↔Down) → Quick Cut / Filter Sweep
  4. Half-time → Full-time → Double Drop / Key Jump
  5. Full-time → Half-time → Delay Out / Echo Cut
  6. Energy jump +20 or EL+2 → Quick Cut / Stutter / Reverse Drop
  7. Energy decrease → Texture Layering / Filter Sweep / Delay Out
  8. Experimental → Mashup / Time Stretch / Tone Play

Usage:
    from scripts.core.transition_intel import analyze_transition_pair
    rec = analyze_transition_pair(struct_a, struct_b)
    print(rec.technique_id, rec.reason)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from scripts.core.classify import classify_tempo, energy_to_el, are_bpm_compatible

log = logging.getLogger(__name__)


@dataclass
class TransitionRecommendation:
    """Recommendation for a single A->B transition."""

    technique: str          # Legacy type: "cut" | "cosine" | "extended_blend" | "sharp_handoff"
    effect: str             # "echo" | "filter" | "reverb" | "loop" | "wobble" | "slicer" | "flanger" | "phaser" | "vinyl_stop" | "bitcrush" | "none"
    transition_bars: int    # 2-16
    crossfade_type: str     # "extended" | "standard" | "sharp"
    bridge_beat: bool
    eq_strategy: str        # "default" | "bass_heavy" | "vocal_priority" | "aggressive"

    confidence: float       # 0.0-1.0
    reason: str

    bpm_a: float = 0.0
    bpm_b: float = 0.0
    camelot_a: str = ""
    camelot_b: str = ""
    harmonic_score: float = 0.0
    energy_a: float = 0.0
    energy_b: float = 0.0
    section_exit_a: str = ""
    section_entry_b: str = ""

    # DARAVE v2.0 fields
    technique_id: str = ""       # "DNB-01" ... "DNB-20"
    tempo_type_a: str = ""       # "half-time" | "full-time" | "downtempo" | "other"
    tempo_type_b: str = ""
    el_a: int = 0                # Energy Level 1-5
    el_b: int = 0
    priority_rule: int = 0       # 1-8 which rule fired


def _camelot_distance(c1: str, c2: str) -> int:
    """Camelot wheel distance (0-6)."""
    if not c1 or not c2:
        return 6
    try:
        n1 = int(c1.rstrip("AB"))
        n2 = int(c2.rstrip("AB"))
        diff = abs(n1 - n2)
        return min(diff, 12 - diff)
    except (ValueError, AttributeError):
        return 6


def _camelot_is_tritone(c1: str, c2: str) -> bool:
    """Check if two keys are exactly tritone (±6 semitones)."""
    return _camelot_distance(c1, c2) == 6


def _get_exit_section(struct) -> Optional[str]:
    for sec in reversed(struct.sections):
        if sec.is_mixable_exit:
            return sec.type
    return None


def _get_entry_section(struct) -> Optional[str]:
    for sec in struct.sections:
        if sec.is_mixable_entry:
            return sec.type
    return None


def _has_section_type(struct, section_type: str) -> bool:
    return any(s.type == section_type for s in struct.sections)


def analyze_transition_pair(
    struct_a,
    struct_b,
    stems_available: bool = False,
) -> TransitionRecommendation:
    """
    Analyse a pair of tracks and recommend the optimal transition technique
    using 8 priority rules from DARAVE v2.0 spec.
    """
    bpm_a = struct_a.bpm
    bpm_b = struct_b.bpm
    bpm_diff = abs(bpm_a - bpm_b)

    camelot_a = struct_a.camelot
    camelot_b = struct_b.camelot
    cam_dist = _camelot_distance(camelot_a, camelot_b)
    cam_compatible = cam_dist <= 2

    e_a = struct_a.energy_mean
    e_b = struct_b.energy_mean
    e_delta = e_b - e_a  # signed: positive = energy up, negative = energy down

    # Classify BPM types
    tempo_a = getattr(struct_a, "tempo_type", None) or classify_tempo(bpm_a)
    tempo_b = getattr(struct_b, "tempo_type", None) or classify_tempo(bpm_b)

    # Compute Energy Levels
    el_a = getattr(struct_a, "el", None) or energy_to_el(e_a)
    el_b = getattr(struct_b, "el", None) or energy_to_el(e_b)
    el_delta = el_b - el_a  # signed

    exit_section = _get_exit_section(struct_a) or "verse"
    entry_section = _get_entry_section(struct_b) or "verse"

    has_build_a = _has_section_type(struct_a, "build")
    has_drop_b = _has_section_type(struct_b, "drop")
    has_drop_a = _has_section_type(struct_a, "drop")
    has_break_b = _has_section_type(struct_b, "break")
    has_intro_b = _has_section_type(struct_b, "intro")

    harm_score = 1.0 - cam_dist / 6.0

    # ── PRIORITY RULE 1: Tritone (Key ±6) → Echo Cut / Quick Cut ──
    if _camelot_is_tritone(camelot_a, camelot_b):
        effect = "echo"
        technique_id = "DNB-07"  # Echo Cut
        if bpm_diff > 15 or not cam_compatible:
            technique_id = "DNB-04"  # Quick Cut
        return _rec(
            "sharp_handoff", effect, 8, "sharp", False, "default", 0.88,
            f"Priority 1: Tritone (Camelot {camelot_a}→{camelot_b}, dist=6). "
            f"{'Quick cut' if technique_id == 'DNB-04' else 'Echo cut'} masks the key clash.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 1,
        )

    # ── PRIORITY RULE 2: BPM match + Key ±1 + smooth Energy → EQ Roller / Bass Swap / Phrase Matching ──
    if cam_compatible and abs(e_delta) < 0.15 and bpm_diff < 5:
        if has_build_a and has_drop_b:
            technique_id = "DNB-06"  # EQ Roller
            effect = "none"
        else:
            technique_id = "DNB-02"  # Bass Swap
            effect = "none"
        return _rec(
            "cosine", effect, 8, "standard", False, "default", 0.95,
            f"Priority 2: BPM match ({bpm_a:.0f}→{bpm_b:.0f}), "
            f"Key ±1 ({camelot_a}→{camelot_b}), smooth energy (Δ{e_delta:+.2f}). "
            f"{'EQ Roller' if technique_id == 'DNB-06' else 'Bass Swap'}.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 2,
        )

    # ── PRIORITY RULE 3: Different BPM (Half↔Down, Full↔Down) → Quick Cut / Filter Sweep ──
    if not are_bpm_compatible(tempo_a, tempo_b):
        if bpm_diff > 20:
            technique_id = "DNB-04"  # Quick Cut
            effect = "none"
        else:
            technique_id = "DNB-03"  # Filter Sweep
            effect = "filter"
        return _rec(
            "sharp_handoff", effect, 8, "sharp", True, "default", 0.82,
            f"Priority 3: Incompatible BPM types ({tempo_a}→{tempo_b}, "
            f"{bpm_a:.0f}→{bpm_b:.0f}). "
            f"{'Quick cut' if technique_id == 'DNB-04' else 'Filter sweep'} bridges the gap.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 3,
        )

    # ── PRIORITY RULE 4: Half-time → Full-time → Double Drop / Key Jump ──
    if tempo_a == "half-time" and tempo_b == "full-time":
        technique_id = "DNB-01"  # Double Drop
        if cam_dist >= 3:
            technique_id = "DNB-12"  # Key Jump
        return _rec(
            "cut", "none", 4, "sharp", False, "default", 0.90,
            f"Priority 4: Half-time→Full-time ({bpm_a:.0f}→{bpm_b:.0f}). "
            f"{'Key jump' if technique_id == 'DNB-12' else 'Double drop'} for energy lift.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            "drop" if has_drop_a else exit_section, "drop" if has_drop_b else entry_section,
            technique_id, tempo_a, tempo_b, el_a, el_b, 4,
        )

    # ── PRIORITY RULE 5: Full-time → Half-time → Delay Out / Echo Cut ──
    if tempo_a == "full-time" and tempo_b == "half-time":
        technique_id = "DNB-05"  # Delay Out
        if cam_dist >= 3:
            technique_id = "DNB-07"  # Echo Cut
        return _rec(
            "extended_blend", "echo", 12, "extended", False, "bass_heavy", 0.88,
            f"Priority 5: Full-time→Half-time ({bpm_a:.0f}→{bpm_b:.0f}). "
            f"{'Echo cut' if technique_id == 'DNB-07' else 'Delay out'} smooths the energy drop.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 5,
        )

    # ── PRIORITY RULE 6: Energy jump +20 or EL+2 → Quick Cut / Stutter / Reverse Drop ──
    energy_jump_20 = abs(e_b - e_a) * 100 > 20
    el_jump_2 = abs(el_delta) >= 2
    if energy_jump_20 or el_jump_2:
        if e_b > e_a:
            # Energy going UP
            technique_id = "DNB-04"  # Quick Cut
            if el_delta >= 3:
                technique_id = "DNB-13"  # Reverse Drop
            elif el_delta == 2:
                technique_id = "DNB-17"  # Stutter
        else:
            # Energy going DOWN
            technique_id = "DNB-17"  # Stutter
        return _rec(
            "sharp_handoff" if e_b > e_a else "extended_blend",
            "loop" if technique_id == "DNB-17" else "none",
            4 if e_b > e_a else 12,
            "sharp" if e_b > e_a else "extended",
            False, "default", 0.85,
            f"Priority 6: Energy jump ({e_a:.2f}→{e_b:.2f}, EL {el_a}→{el_b}). "
            f"{'Quick cut' if technique_id == 'DNB-04' else 'Stutter' if technique_id == 'DNB-17' else 'Reverse drop'} maintains energy flow.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 6,
        )

    # ── PRIORITY RULE 7: Energy decrease → Texture Layering / Filter Sweep / Delay Out ──
    if e_delta < -0.15:
        technique_id = "DNB-18"  # Texture Layering
        if cam_dist >= 3:
            technique_id = "DNB-03"  # Filter Sweep
        if el_delta <= -3:
            technique_id = "DNB-05"  # Delay Out
        return _rec(
            "extended_blend", "filter" if technique_id == "DNB-03" else "none",
            12, "extended", False, "bass_heavy", 0.83,
            f"Priority 7: Energy decrease ({e_a:.2f}→{e_b:.2f}, EL {el_a}→{el_b}). "
            f"{'Texture layering' if technique_id == 'DNB-18' else 'Filter sweep' if technique_id == 'DNB-03' else 'Delay out'} smooths the descent.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 7,
        )

    # ── PRIORITY RULE 8: Experimental → Mashup / Time Stretch / Tone Play ──
    # Large BPM gap + different tempo types = experimental territory
    if bpm_diff > 15 and not are_bpm_compatible(tempo_a, tempo_b):
        technique_id = "DNB-15"  # Mashup
        if cam_dist >= 4:
            technique_id = "DNB-16"  # Time Stretch Glitch
        if el_delta >= 2:
            technique_id = "DNB-20"  # Tone Play
        return _rec(
            "sharp_handoff", "slicer", 16, "sharp", True, "aggressive", 0.75,
            f"Priority 8: Experimental ({tempo_a}→{tempo_b}, Δ{bpm_diff:.0f} BPM). "
            f"{'Tone play' if technique_id == 'DNB-20' else 'Time stretch' if technique_id == 'DNB-16' else 'Mashup'} for creative bridge.",
            bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
            exit_section, entry_section, technique_id,
            tempo_a, tempo_b, el_a, el_b, 8,
        )

    # ── Fallback: Standard Bass Swap ──
    bars = 8 if bpm_diff < 10 else 12
    return _rec(
        "cosine", "none", bars, "standard", False, "default", 0.70,
        f"Fallback: BPM {bpm_a:.0f}→{bpm_b:.0f}, Camelot {camelot_a}→{camelot_b} "
        f"(dist={cam_dist}), EL {el_a}→{el_b}. Standard bass swap.",
        bpm_a, bpm_b, camelot_a, camelot_b, harm_score, e_a, e_b,
        exit_section, entry_section, "DNB-02",
        tempo_a, tempo_b, el_a, el_b, 0,
    )


def _rec(
    technique, effect, bars, crossfade, bridge, eq, confidence, reason,
    bpm_a, bpm_b, camelot_a, camelot_b, harmonic_score, energy_a, energy_b,
    section_exit_a, section_entry_b, technique_id="",
    tempo_type_a="", tempo_type_b="", el_a=0, el_b=0, priority_rule=0,
) -> TransitionRecommendation:
    return TransitionRecommendation(
        technique=technique,
        effect=effect,
        transition_bars=bars,
        crossfade_type=crossfade,
        bridge_beat=bridge,
        eq_strategy=eq,
        confidence=round(confidence, 2),
        reason=reason,
        bpm_a=round(bpm_a, 1),
        bpm_b=round(bpm_b, 1),
        camelot_a=camelot_a,
        camelot_b=camelot_b,
        harmonic_score=round(harmonic_score, 3),
        energy_a=round(energy_a, 3),
        energy_b=round(energy_b, 3),
        section_exit_a=section_exit_a,
        section_entry_b=section_entry_b,
        technique_id=technique_id,
        tempo_type_a=tempo_type_a,
        tempo_type_b=tempo_type_b,
        el_a=el_a,
        el_b=el_b,
        priority_rule=priority_rule,
    )


# ---------------------------------------------------------------------------
# Alternative techniques for a pair of tracks
# ---------------------------------------------------------------------------

_TECHNIQUE_ALTERNATIVES = {
    "DNB-01": {"name": "Double Drop", "alt": ["DNB-12", "DNB-04"], "rule": "half→full: energy lift"},
    "DNB-02": {"name": "Bass Swap", "alt": ["DNB-06", "DNB-03"], "rule": "smooth blend: same energy"},
    "DNB-03": {"name": "Filter Sweep", "alt": ["DNB-18", "DNB-05"], "rule": "filter: bridge BPM gap"},
    "DNB-04": {"name": "Quick Cut", "alt": ["DNB-07", "DNB-17"], "rule": "sharp cut: mask clash"},
    "DNB-05": {"name": "Delay Out", "alt": ["DNB-03", "DNB-07"], "rule": "delay: smooth energy drop"},
    "DNB-06": {"name": "EQ Roller", "alt": ["DNB-02", "DNB-18"], "rule": "EQ: build→drop transition"},
    "DNB-07": {"name": "Echo Cut", "alt": ["DNB-04", "DNB-05"], "rule": "echo: mask tritone/key clash"},
    "DNB-08": {"name": "Phrase Match", "alt": ["DNB-02", "DNB-06"], "rule": "phrase: bar-grid alignment"},
    "DNB-09": {"name": "A Cappella Overlay", "alt": ["DNB-15", "DNB-02"], "rule": "vocal: overlay over instrumental"},
    "DNB-10": {"name": "Triple Drop", "alt": ["DNB-01", "DNB-04"], "rule": "triple: multi-drop impact"},
    "DNB-11": {"name": "Loop & Roll", "alt": ["DNB-17", "DNB-04"], "rule": "loop: build tension"},
    "DNB-12": {"name": "Key Jump", "alt": ["DNB-01", "DNB-04"], "rule": "key: harmonic shift"},
    "DNB-13": {"name": "Reverse Drop", "alt": ["DNB-04", "DNB-17"], "rule": "reverse: energy reversal"},
    "DNB-14": {"name": "Fader FX Series", "alt": ["DNB-04", "DNB-07"], "rule": "fader: manual FX chain"},
    "DNB-15": {"name": "Mashup Transition", "alt": ["DNB-09", "DNB-16"], "rule": "mashup: vocal+instrumental blend"},
    "DNB-16": {"name": "Time Stretch Glitch", "alt": ["DNB-15", "DNB-04"], "rule": "stretch: tempo manipulation"},
    "DNB-17": {"name": "Stutter Effect", "alt": ["DNB-11", "DNB-04"], "rule": "stutter: rhythmic glitch"},
    "DNB-18": {"name": "Texture Layering", "alt": ["DNB-06", "DNB-03"], "rule": "texture: layer atmospheric elements"},
    "DNB-19": {"name": "Scratch In", "alt": ["DNB-04", "DNB-14"], "rule": "scratch: vinyl-style entry"},
    "DNB-20": {"name": "Tone Play", "alt": ["DNB-16", "DNB-15"], "rule": "tone: melodic bridge"},
}


def get_alternative_techniques(struct_a, struct_b) -> list:
    """
    Return a list of alternative techniques for a track pair, sorted by
    confidence. Each entry: {technique_id, name, confidence, rule, selected}.
    The first item is the recommended one (selected=True).
    """
    rec = analyze_transition_pair(struct_a, struct_b)
    primary_id = rec.technique_id

    cam_dist = _camelot_distance(rec.camelot_a, rec.camelot_b)
    bpm_diff = abs(rec.bpm_a - rec.bpm_b)
    e_delta = rec.energy_b - rec.energy_a
    el_delta = rec.el_b - rec.el_a

    results = []

    # Always include the primary recommendation first
    results.append({
        "technique_id": primary_id,
        "name": _TECHNIQUE_ALTERNATIVES.get(primary_id, {}).get("name", primary_id),
        "confidence": rec.confidence,
        "rule": f"#{rec.priority_rule}: {rec.reason.split('. ')[0] if rec.reason else 'auto'}",
        "selected": True,
    })

    # Add alternatives
    alt_info = _TECHNIQUE_ALTERNATIVES.get(primary_id, {})
    for alt_id in alt_info.get("alt", []):
        alt_info2 = _TECHNIQUE_ALTERNATIVES.get(alt_id, {})
        # Score based on compatibility with the pair's characteristics
        score = rec.confidence * 0.75  # base: slightly less than primary
        # Bonus if BPM compatible
        if bpm_diff < 10:
            score += 0.05
        # Bonus if key compatible
        if cam_dist <= 2:
            score += 0.05
        # Bonus if energy smooth
        if abs(e_delta) < 0.15:
            score += 0.05

        results.append({
            "technique_id": alt_id,
            "name": alt_info2.get("name", alt_id),
            "confidence": round(min(0.95, score), 2),
            "rule": alt_info2.get("rule", "alternative"),
            "selected": False,
        })

    return results
