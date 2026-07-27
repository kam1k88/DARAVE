"""
scripts/api/routers/mix_plan.py — Visual mix plan endpoint.

POST /mix/plan — Build a transition plan from metadata (no audio files needed).
POST /mix/plan/all — Auto-build plan for ALL library tracks.
"""

import json
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException

from scripts.api.routers._helpers import _require_song
from scripts.api.schemas import DJChainRequest
from scripts.core.classify import classify_tempo, energy_to_el
from scripts.core.dj_analysis import SongStructure, Beat, Section
from scripts.core.paths import LIBRARY_DIR
from scripts.core.transition_intel import analyze_transition_pair, get_alternative_techniques

import numpy as np

router = APIRouter(prefix="/mix", tags=["mix"])


def _sec_to_mmss(sec: float) -> str:
    m = int(sec) // 60
    s = int(sec) % 60
    return f"{m:02d}:{s:02d}"


def _load_structure_from_meta(song_dir: Path) -> SongStructure:
    """Build a SongStructure from meta.json + analysis.json (no audio needed)."""
    meta_path = song_dir / "meta.json"
    analysis_path = song_dir / "analysis.json"

    meta = {}
    analysis = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    if analysis_path.exists():
        analysis = json.loads(analysis_path.read_text(encoding="utf-8", errors="replace"))

    bpm = float(meta.get("bpm", 120))
    energy_mean = float(meta.get("energy_mean", 0.5))
    energy_std = float(meta.get("energy_std", 0.1))
    duration = float(analysis.get("duration", 180.0))
    total_bars = int(analysis.get("total_bars", int(duration / (60.0 / bpm * 4))))

    sections = []
    for sec_data in analysis.get("sections", []):
        sections.append(Section(
            type=sec_data.get("type", "verse"),
            start_bar=sec_data.get("start_bar", 0),
            end_bar=sec_data.get("end_bar", 0),
            start_time=sec_data.get("start_time", sec_data.get("start", 0)),
            end_time=sec_data.get("end_time", sec_data.get("end", 0)),
            avg_energy=sec_data.get("avg_energy", 0.5),
            avg_spectral=sec_data.get("avg_spectral", 0.5),
        ))

    s = SongStructure(
        bpm=bpm,
        duration=duration,
        beats=[],
        bars=[(i * 60.0 / bpm * 4, (i + 1) * 60.0 / bpm * 4) for i in range(total_bars)],
        sections=sections,
        key_name=meta.get("key", ""),
        mode=meta.get("mode", ""),
        camelot=meta.get("camelot", ""),
        key_confidence=float(meta.get("key_confidence", 0.8)),
        energy_mean=energy_mean,
        energy_std=energy_std,
        danceability=float(meta.get("danceability", 0.5)),
        vocal_density=float(meta.get("vocal_density", 0.5)),
        spectral_centroid_hz=float(meta.get("spectral_centroid_hz", 3000)),
        tempo_type=meta.get("tempo_type", "") or classify_tempo(bpm),
        el=meta.get("el", 0) or energy_to_el(energy_mean),
    )
    return s


@router.post("/plan")
def plan_mix(req: DJChainRequest):
    """Build a transition plan from metadata only — no audio files required."""
    # Validate all songs exist
    song_dirs = {}
    for name in req.songs:
        try:
            song_dirs[name] = _require_song(name)
        except Exception:
            raise HTTPException(404, f"Song not found in library: {name}")

    # Build structures from meta.json
    structures = []
    for name in req.songs:
        structures.append(_load_structure_from_meta(song_dirs[name]))

    # Smart intel
    intel = []
    for i in range(len(req.songs) - 1):
        rec = analyze_transition_pair(structures[i], structures[i + 1])
        intel.append(rec)

    # Energy arc
    energy_arc = []
    cumulative_sec = 0.0
    for i, s in enumerate(structures):
        duration = s.duration
        energy_arc.append({
            "song_index": i,
            "song_name": req.songs[i],
            "energy_mean": round(float(s.energy_mean), 3),
            "energy_std": round(float(s.energy_std), 3),
            "start_sec": round(cumulative_sec, 1),
            "end_sec": round(cumulative_sec + duration, 1),
            "start_label": _sec_to_mmss(cumulative_sec),
            "end_label": _sec_to_mmss(cumulative_sec + duration),
        })
        cumulative_sec += duration

    # Build response
    transitions_data = []
    for i, rec in enumerate(intel):
        transitions_data.append({
            "pair_index": i,
            "from_song": req.songs[i],
            "to_song": req.songs[i + 1],
            "technique": rec.technique,
            "technique_id": rec.technique_id,
            "effect": rec.effect,
            "transition_bars": rec.transition_bars,
            "crossfade_type": rec.crossfade_type,
            "confidence": round(rec.confidence, 2),
            "reason": rec.reason,
            "energy_from": round(float(structures[i].energy_mean), 3),
            "energy_to": round(float(structures[i + 1].energy_mean), 3),
            "el_from": rec.el_a,
            "el_to": rec.el_b,
            "bpm_from": round(structures[i].bpm, 1),
            "bpm_to": round(structures[i + 1].bpm, 1),
            "tempo_type_from": rec.tempo_type_a,
            "tempo_type_to": rec.tempo_type_b,
            "camelot_from": structures[i].camelot or "??",
            "camelot_to": structures[i + 1].camelot or "??",
            "bridge_beat": rec.bridge_beat,
            "priority_rule": rec.priority_rule,
            "tempo_ratio": round(structures[i + 1].bpm / max(structures[i].bpm, 1), 3),
        })

    structures_data = []
    for i, s in enumerate(structures):
        structures_data.append({
            "song_name": req.songs[i],
            "bpm": round(float(s.bpm), 1),
            "camelot": s.camelot or "??",
            "key": f"{s.key_name} {s.mode}".strip() if s.key_name else "??",
            "energy_mean": round(float(s.energy_mean), 3),
            "energy_std": round(float(s.energy_std), 3),
            "total_bars": s.total_bars,
            "tempo_type": s.tempo_type,
            "el": s.el,
        })

    avg_confidence = float(np.mean([r.confidence for r in intel])) if intel else 0.0

    return {
        "songs": req.songs,
        "track_order": list(range(len(req.songs))),
        "structures": structures_data,
        "transitions": transitions_data,
        "energy_arc": energy_arc,
        "total_duration_sec": round(cumulative_sec, 1),
        "avg_confidence": round(avg_confidence, 2),
    }


@router.post("/plan/all")
def plan_all_library():
    """Auto-build a transition plan for ALL analyzed library tracks, sorted by BPM."""
    songs = []
    for d in sorted(LIBRARY_DIR.iterdir()):
        if d.is_dir() and (d / "meta.json").exists():
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if meta.get("bpm"):
                songs.append((d.name, float(meta["bpm"])))

    songs.sort(key=lambda x: x[1])
    names = [s[0] for s in songs]

    if len(names) < 2:
        raise HTTPException(400, "Need at least 2 analyzed tracks for a plan")

    # Build structures directly — bypass DJChainRequest's 8-song limit
    song_dirs = {}
    for name in names:
        try:
            song_dirs[name] = _require_song(name)
        except Exception:
            continue

    valid_names = [n for n in names if n in song_dirs]
    structures = [_load_structure_from_meta(song_dirs[n]) for n in valid_names]

    intel = []
    for i in range(len(valid_names) - 1):
        rec = analyze_transition_pair(structures[i], structures[i + 1])
        intel.append(rec)

    energy_arc = []
    cumulative_sec = 0.0
    for i, s in enumerate(structures):
        energy_arc.append({
            "song_index": i,
            "song_name": valid_names[i],
            "energy_mean": round(float(s.energy_mean), 3),
            "energy_std": round(float(s.energy_std), 3),
            "start_sec": round(cumulative_sec, 1),
            "end_sec": round(cumulative_sec + s.duration, 1),
            "start_label": _sec_to_mmss(cumulative_sec),
            "end_label": _sec_to_mmss(cumulative_sec + s.duration),
        })
        cumulative_sec += s.duration

    transitions_data = []
    for i, rec in enumerate(intel):
        transitions_data.append({
            "pair_index": i,
            "from_song": valid_names[i],
            "to_song": valid_names[i + 1],
            "technique": rec.technique,
            "technique_id": rec.technique_id,
            "effect": rec.effect,
            "transition_bars": rec.transition_bars,
            "crossfade_type": rec.crossfade_type,
            "confidence": round(rec.confidence, 2),
            "reason": rec.reason,
            "energy_from": round(float(structures[i].energy_mean), 3),
            "energy_to": round(float(structures[i + 1].energy_mean), 3),
            "el_from": rec.el_a,
            "el_to": rec.el_b,
            "bpm_from": round(structures[i].bpm, 1),
            "bpm_to": round(structures[i + 1].bpm, 1),
            "tempo_type_from": rec.tempo_type_a,
            "tempo_type_to": rec.tempo_type_b,
            "camelot_from": structures[i].camelot or "??",
            "camelot_to": structures[i + 1].camelot or "??",
            "bridge_beat": rec.bridge_beat,
            "priority_rule": rec.priority_rule,
            "tempo_ratio": round(structures[i + 1].bpm / max(structures[i].bpm, 1), 3),
        })

    structures_data = []
    for i, s in enumerate(structures):
        structures_data.append({
            "song_name": valid_names[i],
            "bpm": round(float(s.bpm), 1),
            "camelot": s.camelot or "??",
            "key": f"{s.key_name} {s.mode}".strip() if s.key_name else "??",
            "energy_mean": round(float(s.energy_mean), 3),
            "energy_std": round(float(s.energy_std), 3),
            "total_bars": s.total_bars,
            "tempo_type": s.tempo_type,
            "el": s.el,
        })

    avg_confidence = float(np.mean([r.confidence for r in intel])) if intel else 0.0

    return {
        "songs": valid_names,
        "track_order": list(range(len(valid_names))),
        "structures": structures_data,
        "transitions": transitions_data,
        "energy_arc": energy_arc,
        "total_duration_sec": round(cumulative_sec, 1),
        "avg_confidence": round(avg_confidence, 2),
    }


# ---------------------------------------------------------------------------
# Smart set ordering algorithm
# ---------------------------------------------------------------------------

def _camelot_num(c: str) -> int:
    try:
        return int(c.rstrip("AB"))
    except (ValueError, AttributeError):
        return 0


def _camelot_distance(c1: str, c2: str) -> int:
    if not c1 or not c2:
        return 6
    n1, n2 = _camelot_num(c1), _camelot_num(c2)
    d = abs(n1 - n2)
    return min(d, 12 - d)


def smart_order(names: list, structures: dict, arc_mode: str = "dynamic") -> list:
    """
    Order tracks for an optimal DJ set.
    arc_mode:
      - "dynamic"  — EL arc: 1→5→1→2 (climax in middle, cooldown, final rise)
      - "fade_out"  — starts high energy, gradually decreases (5→4→3→2→1)
      - "fade_in"   — starts low energy, gradually increases (1→2→3→4→5)
    """
    if len(names) <= 2:
        return names

    # Build lookup
    tracks = []
    for n in names:
        s = structures.get(n, {})
        tracks.append({
            "name": n,
            "bpm": s.get("bpm", 120),
            "el": s.get("el", 3),
            "camelot": s.get("camelot", "??"),
            "outro_bars": s.get("outro_bars", 8),
            "intro_bars": s.get("intro_bars", 8),
        })

    # Separate by EL for arc building
    el_buckets = {1: [], 2: [], 3: [], 4: [], 5: []}
    for t in tracks:
        el = t["el"]
        if el in el_buckets:
            el_buckets[el].append(t)
        else:
            el_buckets[3].append(t)

    ordered = []
    used = set()

    def pick_best(candidates, target_bpm, target_camelot, used):
        best, best_score = None, -999
        for t in candidates:
            if t["name"] in used:
                continue
            bpm_score = -abs(t["bpm"] - target_bpm) * 0.1
            cam_score = -_camelot_distance(t["camelot"], target_camelot) * 0.5
            score = bpm_score + cam_score
            if score > best_score:
                best, best_score = t, score
        return best

    def fill_bucket(el, count):
        """Pick 'count' tracks from the given EL bucket, best matching target."""
        nonlocal target_bpm, target_camelot
        bucket = el_buckets.get(el, [])
        for _ in range(count):
            t = pick_best(bucket, target_bpm, target_camelot, used)
            if t:
                ordered.append(t)
                used.add(t["name"])
                target_bpm = t["bpm"]
                target_camelot = t["camelot"]

    def fill_remaining():
        for t in tracks:
            if t["name"] not in used:
                ordered.append(t)
                used.add(t["name"])

    target_bpm = tracks[0]["bpm"]
    target_camelot = tracks[0]["camelot"]

    if arc_mode == "fade_out":
        # High energy start → gradual decrease
        for el in [5, 5, 4, 4, 3, 3, 2, 2, 1]:
            fill_bucket(el, 2 if el > 1 else len(tracks))
        fill_remaining()

    elif arc_mode == "fade_in":
        # Low energy start → gradual increase
        for el in [1, 1, 2, 2, 3, 3, 4, 4, 5]:
            fill_bucket(el, 2 if el < 5 else len(tracks))
        fill_remaining()

    else:
        # "dynamic" — DnB-style arc: start low, ramp up, climax, cooldown, final rise
        starters = el_buckets[1] + el_buckets[2]
        if not starters:
            starters = tracks[:1]
        start = min(starters, key=lambda t: abs(t["bpm"] - 170))
        ordered.append(start)
        used.add(start["name"])
        target_bpm = start["bpm"]
        target_camelot = start["camelot"]

        # Ramp up: EL 2→3→4
        for target_el in [2, 3, 3, 4, 4]:
            fill_bucket(target_el, 1)

        # Climax: EL=4-5 at ~3/4 of set
        climax_count = min(3, len(tracks) // 3)
        climax_pool = el_buckets[5] + el_buckets[4]
        for _ in range(climax_count):
            t = pick_best(climax_pool, target_bpm, target_camelot, used)
            if t:
                ordered.append(t)
                used.add(t["name"])
                target_bpm = t["bpm"]
                target_camelot = t["camelot"]

        # Cooldown: EL 4→3→2→1
        for target_el in [4, 3, 2, 1]:
            fill_bucket(target_el, 1)

        fill_remaining()

    return [t["name"] for t in ordered]


@router.post("/plan/smart")
def plan_smart(arc_mode: str = "dynamic"):
    """Auto-build plan with smart EL-arc ordering, not just BPM sort."""
    songs = []
    for d in sorted(LIBRARY_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        analysis_path = d / "analysis.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            analysis = {}
            if analysis_path.exists():
                analysis = json.loads(analysis_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if meta.get("bpm"):
            songs.append(d.name)

    if len(songs) < 2:
        raise HTTPException(400, "Need at least 2 analyzed tracks")

    # Build structures
    structs = {}
    for name in songs:
        try:
            sd = _require_song(name)
            structs[name] = _load_structure_from_meta(sd)
        except Exception:
            continue

    # Smart ordering
    ordered = smart_order(songs, {
        n: {
            "bpm": float(structs[n].bpm),
            "el": structs[n].el,
            "camelot": structs[n].camelot,
            "outro_bars": 8,
            "intro_bars": 8,
        }
        for n in songs if n in structs
    }, arc_mode=arc_mode)

    # Build plan from ordered list
    song_dirs = {}
    valid_names = []
    for name in ordered:
        try:
            sd = _require_song(name)
            song_dirs[name] = sd
            valid_names.append(name)
        except Exception:
            continue

    structures = [_load_structure_from_meta(song_dirs[n]) for n in valid_names]

    intel = []
    for i in range(len(valid_names) - 1):
        rec = analyze_transition_pair(structures[i], structures[i + 1])
        intel.append(rec)

    energy_arc = []
    cumulative_sec = 0.0
    for i, s in enumerate(structures):
        energy_arc.append({
            "song_index": i,
            "song_name": valid_names[i],
            "energy_mean": round(float(s.energy_mean), 3),
            "energy_std": round(float(s.energy_std), 3),
            "start_sec": round(cumulative_sec, 1),
            "end_sec": round(cumulative_sec + s.duration, 1),
            "start_label": _sec_to_mmss(cumulative_sec),
            "end_label": _sec_to_mmss(cumulative_sec + s.duration),
        })
        cumulative_sec += s.duration

    transitions_data = []
    for i, rec in enumerate(intel):
        transitions_data.append({
            "pair_index": i,
            "from_song": valid_names[i],
            "to_song": valid_names[i + 1],
            "technique": rec.technique,
            "technique_id": rec.technique_id,
            "effect": rec.effect,
            "transition_bars": rec.transition_bars,
            "crossfade_type": rec.crossfade_type,
            "confidence": round(rec.confidence, 2),
            "reason": rec.reason,
            "energy_from": round(float(structures[i].energy_mean), 3),
            "energy_to": round(float(structures[i + 1].energy_mean), 3),
            "el_from": rec.el_a,
            "el_to": rec.el_b,
            "bpm_from": round(structures[i].bpm, 1),
            "bpm_to": round(structures[i + 1].bpm, 1),
            "tempo_type_from": rec.tempo_type_a,
            "tempo_type_to": rec.tempo_type_b,
            "camelot_from": structures[i].camelot or "??",
            "camelot_to": structures[i + 1].camelot or "??",
            "bridge_beat": rec.bridge_beat,
            "priority_rule": rec.priority_rule,
            "tempo_ratio": round(structures[i + 1].bpm / max(structures[i].bpm, 1), 3),
        })

    structures_data = []
    for i, s in enumerate(structures):
        structures_data.append({
            "song_name": valid_names[i],
            "bpm": round(float(s.bpm), 1),
            "camelot": s.camelot or "??",
            "key": f"{s.key_name} {s.mode}".strip() if s.key_name else "??",
            "energy_mean": round(float(s.energy_mean), 3),
            "energy_std": round(float(s.energy_std), 3),
            "total_bars": s.total_bars,
            "tempo_type": s.tempo_type,
            "el": s.el,
        })

    avg_confidence = float(np.mean([r.confidence for r in intel])) if intel else 0.0

    return {
        "songs": valid_names,
        "track_order": list(range(len(valid_names))),
        "structures": structures_data,
        "transitions": transitions_data,
        "energy_arc": energy_arc,
        "total_duration_sec": round(cumulative_sec, 1),
        "avg_confidence": round(avg_confidence, 2),
        "ordering": f"smart_{arc_mode}",
    }


# ---------------------------------------------------------------------------
# Alternatives for a specific transition pair
# ---------------------------------------------------------------------------

@router.post("/plan/alternatives")
def get_alternatives(song_a: str, song_b: str):
    """Get alternative technique recommendations for a specific track pair."""
    from scripts.core.transition_intel import get_alternative_techniques as _get_alts
    try:
        sd_a = _require_song(song_a)
        sd_b = _require_song(song_b)
    except Exception:
        raise HTTPException(404, "Song not found")

    struct_a = _load_structure_from_meta(sd_a)
    struct_b = _load_structure_from_meta(sd_b)
    alternatives = _get_alts(struct_a, struct_b)
    return {"alternatives": alternatives}
