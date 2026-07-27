"""
scripts/core/pattern_search.py — Intelligent track/section search for DJ techniques.

Searches the library for tracks and pairs that satisfy the constraints
of a specific DJ technique (BPM range, key compatibility, energy level, etc.).

Usage:
    from scripts.core.pattern_search import search_for_technique
    results = search_for_technique("DNB-07")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

try:
    from scripts.core.paths import LIBRARY_DIR
except Exception:
    LIBRARY_DIR = Path("library")

try:
    from scripts.core.dj_techniques import DJTechnique, get_technique
except ImportError:
    DJTechnique = None  # type: ignore
    get_technique = None  # type: ignore


# ---------------------------------------------------------------------------
# Track metadata wrapper
# ---------------------------------------------------------------------------

@dataclass
class TrackMeta:
    """Lightweight metadata for a library track."""
    name: str
    bpm: float = 0.0
    key: str = ""
    mode: str = ""
    camelot: str = ""
    energy_mean: float = 0.5
    energy_std: float = 0.2
    vocal_density: float = 0.0
    spectral_centroid_hz: float = 2000.0
    genre: str = ""
    has_stems: bool = False
    has_full_wav: bool = False

    @classmethod
    def from_meta_json(cls, name: str, song_dir: Path) -> Optional["TrackMeta"]:
        """Load metadata from a song directory's meta.json."""
        meta_path = song_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text())
        except Exception:
            return None

        has_stems = any((song_dir / f"{s}.wav").exists() for s in ("vocals", "drums", "bass", "other"))
        has_full = (song_dir / "full.wav").exists()

        return cls(
            name=name,
            bpm=float(data.get("bpm", 0)),
            key=str(data.get("key", "")),
            mode=str(data.get("mode", "")),
            camelot=str(data.get("camelot", "")),
            energy_mean=float(data.get("energy_mean", 0.5)),
            energy_std=float(data.get("energy_std", 0.2)),
            vocal_density=float(data.get("vocal_density", 0.0)),
            spectral_centroid_hz=float(data.get("spectral_centroid_hz", 2000.0)),
            genre=str(data.get("genre", "")),
            has_stems=has_stems,
            has_full_wav=has_full,
        )


# ---------------------------------------------------------------------------
# Camelot helpers
# ---------------------------------------------------------------------------

def _camelot_distance(c1: str, c2: str) -> int:
    if not c1 or not c2:
        return 6
    try:
        n1 = int(c1.rstrip("AB"))
        n2 = int(c2.rstrip("AB"))
        diff = abs(n1 - n2)
        return min(diff, 12 - diff)
    except (ValueError, AttributeError):
        return 6


def _key_compat_score(c1: str, c2: str, requirement: str) -> float:
    """Return 0.0–1.0 score for how well two keys match the requirement."""
    dist = _camelot_distance(c1, c2)
    if requirement == "any":
        return 1.0
    if requirement == "compatible":
        return 1.0 if dist <= 2 else max(0.0, 1.0 - dist / 6.0)
    if requirement == "clashing":
        return 1.0 if dist >= 4 else 0.0
    if requirement == "tritone":
        return 1.0 if dist == 6 else 0.0
    return 1.0


def _energy_matches(e: float, delta_req: str) -> float:
    """Score 0.0–1.0 for how well an energy level matches the technique's delta requirement."""
    if delta_req == "any":
        return 1.0
    if delta_req == "same":
        return 1.0 if 0.3 <= e <= 0.7 else 0.5
    if delta_req == "low_to_high":
        return 1.0 if e < 0.4 else (0.5 if e < 0.6 else 0.2)
    if delta_req == "high_to_low":
        return 1.0 if e > 0.6 else (0.5 if e > 0.4 else 0.2)
    return 1.0


# ---------------------------------------------------------------------------
# Library loader
# ---------------------------------------------------------------------------

def _load_all_tracks() -> List[TrackMeta]:
    """Load metadata for every song in the library."""
    tracks: List[TrackMeta] = []
    if not LIBRARY_DIR.exists():
        return tracks
    for song_dir in LIBRARY_DIR.iterdir():
        if not song_dir.is_dir() or song_dir.name.startswith("."):
            continue
        meta = TrackMeta.from_meta_json(song_dir.name, song_dir)
        if meta and meta.bpm > 0:
            tracks.append(meta)
    return tracks


# ---------------------------------------------------------------------------
# Search result types
# ---------------------------------------------------------------------------

@dataclass
class TechniqueMatch:
    """A single track that matches a technique's requirements."""
    track: TrackMeta
    score: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class TechniquePair:
    """A pair of tracks that work together for a technique."""
    track_a: TrackMeta
    track_b: TrackMeta
    score: float
    reasons: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core search functions
# ---------------------------------------------------------------------------

def search_for_technique(
    technique_id: str,
    max_results: int = 20,
) -> List[TechniqueMatch]:
    """
    Find tracks suitable for a specific DJ technique.

    Returns a scored list of tracks sorted by match quality.
    """
    if get_technique is None:
        return []
    tech = get_technique(technique_id)
    if tech is None:
        return []

    tracks = _load_all_tracks()
    if not tracks:
        return []

    matches: List[TechniqueMatch] = []

    for t in tracks:
        score = 0.0
        reasons: List[str] = []

        # BPM in range
        if tech.bpm_range[0] <= t.bpm <= tech.bpm_range[1]:
            score += 0.40
            reasons.append(f"BPM {t.bpm:.0f} in range {tech.bpm_range}")
        else:
            dist = min(abs(t.bpm - tech.bpm_range[0]), abs(t.bpm - tech.bpm_range[1]))
            bpm_score = max(0.0, 0.40 - dist * 0.02)
            score += bpm_score
            if bpm_score > 0:
                reasons.append(f"BPM {t.bpm:.0f} near range (penalty)")

        # Key compatibility (self-score = always compatible with itself)
        score += 0.20
        reasons.append("Key: self-compatible")

        # Energy level
        e_score = _energy_matches(t.energy_mean, tech.energy_delta)
        score += e_score * 0.20
        if e_score > 0.5:
            reasons.append(f"Energy {t.energy_mean:.2f} matches {tech.energy_delta}")

        # Stems availability (bonus for stem techniques)
        if "stems" in tech.effects_used:
            if t.has_stems:
                score += 0.10
                reasons.append("Stems available")
            else:
                score -= 0.10
                reasons.append("Stems missing (required)")

        # Spectral focus
        if tech.frequency_focus == "high" and t.spectral_centroid_hz > 3000:
            score += 0.05
            reasons.append("Bright spectral profile")
        elif tech.frequency_focus == "low" and t.spectral_centroid_hz < 2000:
            score += 0.05
            reasons.append("Dark spectral profile")

        # Vocal density check
        if tech.category == "stem" and t.vocal_density > 0.3:
            score += 0.05
            reasons.append("Vocal presence (good for stem techniques)")

        if score > 0.2:
            matches.append(TechniqueMatch(track=t, score=min(1.0, score), reasons=reasons))

    matches.sort(key=lambda m: m.score, reverse=True)
    return matches[:max_results]


def search_compatible_pairs(
    technique_id: str,
    max_results: int = 10,
) -> List[TechniquePair]:
    """
    Find pairs of tracks that work well together for a specific technique.

    For techniques like Double Drop (DNB-01), we need two tracks with
    compatible keys and similar BPM.
    """
    if get_technique is None:
        return []
    tech = get_technique(technique_id)
    if tech is None:
        return []

    tracks = _load_all_tracks()
    if len(tracks) < 2:
        return []

    pairs: List[TechniquePair] = []

    for i, ta in enumerate(tracks):
        for tb in tracks[i + 1:]:
            score = 0.0
            reasons: List[str] = []

            # Both tracks in BPM range
            bpm_a_ok = tech.bpm_range[0] <= ta.bpm <= tech.bpm_range[1]
            bpm_b_ok = tech.bpm_range[0] <= tb.bpm <= tech.bpm_range[1]
            if bpm_a_ok and bpm_b_ok:
                score += 0.30
                reasons.append(f"Both BPM in range ({ta.bpm:.0f}, {tb.bpm:.0f})")
            elif bpm_a_ok or bpm_b_ok:
                score += 0.10
                reasons.append("One BPM in range")

            # BPM proximity between tracks
            bpm_diff = abs(ta.bpm - tb.bpm)
            if bpm_diff < 3:
                score += 0.15
                reasons.append(f"BPM delta {bpm_diff:.0f} (close)")
            elif bpm_diff < 8:
                score += 0.05
                reasons.append(f"BPM delta {bpm_diff:.0f} (moderate)")

            # Key compatibility
            if ta.camelot and tb.camelot:
                kc = _key_compat_score(ta.camelot, tb.camelot, tech.key_compatibility)
                score += kc * 0.25
                if kc > 0.7:
                    reasons.append(f"Keys compatible ({ta.camelot}->{tb.camelot})")
                elif kc < 0.3:
                    reasons.append(f"Keys clash ({ta.camelot}->{tb.camelot})")

            # Energy compatibility
            e_delta = abs(ta.energy_mean - tb.energy_mean)
            if tech.energy_delta == "same" and e_delta < 0.2:
                score += 0.15
                reasons.append(f"Energy delta {e_delta:.2f} (similar)")
            elif tech.energy_delta == "any":
                score += 0.15
                reasons.append("Any energy delta OK")

            # Stems bonus
            if "stems" in tech.effects_used:
                if ta.has_stems and tb.has_stems:
                    score += 0.10
                    reasons.append("Both have stems")
                elif ta.has_stems or tb.has_stems:
                    score += 0.03
                    reasons.append("One has stems")

            if score > 0.3:
                pairs.append(TechniquePair(
                    track_a=ta, track_b=tb,
                    score=min(1.0, score),
                    reasons=reasons,
                ))

    pairs.sort(key=lambda p: p.score, reverse=True)
    return pairs[:max_results]


def suggest_technique_for_pair(
    track_a: TrackMeta,
    track_b: TrackMeta,
) -> List[Dict]:
    """
    Given two tracks, suggest which techniques would work best.

    Returns a list of {technique_id, score, reasons} dicts.
    """
    from scripts.core.dj_techniques import TECHNIQUES

    suggestions: List[Dict] = []

    for tech in TECHNIQUES:
        score = 0.0
        reasons: List[str] = []

        # BPM in range for both
        bpm_a_ok = tech.bpm_range[0] <= track_a.bpm <= tech.bpm_range[1]
        bpm_b_ok = tech.bpm_range[0] <= track_b.bpm <= tech.bpm_range[1]
        if bpm_a_ok and bpm_b_ok:
            score += 0.30
        elif bpm_a_ok or bpm_b_ok:
            score += 0.10

        # BPM proximity
        bpm_diff = abs(track_a.bpm - track_b.bpm)
        if bpm_diff < 3:
            score += 0.15
        elif bpm_diff < 8:
            score += 0.05

        # Key compatibility
        if track_a.camelot and track_b.camelot:
            kc = _key_compat_score(track_a.camelot, track_b.camelot, tech.key_compatibility)
            score += kc * 0.25

        # Energy
        e_delta = abs(track_a.energy_mean - track_b.energy_mean)
        if tech.energy_delta == "same" and e_delta < 0.2:
            score += 0.15
        elif tech.energy_delta == "any":
            score += 0.15

        # Stems
        if "stems" in tech.effects_used:
            if track_a.has_stems and track_b.has_stems:
                score += 0.10

        if score > 0.3:
            suggestions.append({
                "technique_id": tech.id,
                "name": tech.name,
                "difficulty": tech.difficulty,
                "level": tech.level,
                "score": round(min(1.0, score), 3),
                "reasons": reasons,
            })

    suggestions.sort(key=lambda s: s["score"], reverse=True)
    return suggestions
