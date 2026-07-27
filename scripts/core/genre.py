"""
scripts/core/genre.py — Genre detection and genre-aware remix presets.

Genre detection is rule-based over librosa features:
  - BPM range (primary classifier)
  - Spectral centroid (brightness/hardness)
  - Low-energy ratio (bass heaviness)
  - Dynamic range (compressed vs dynamic)
  - Zero-crossing rate (roughness / guitar vs synth)
  - Chroma variance (harmonic richness)

Supported genres:
  house | techno | hiphop | trap | pop | rnb | dnb | ambient | rock | jazz

Each genre maps to a GenrePreset with full mix parameter set.
The auto_preset() function is the main entry point — call it with raw audio
and it returns the best-matching PresetConfig for the remix engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import librosa
import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Genre Preset definition
# ---------------------------------------------------------------------------

@dataclass
class GenrePreset:
    """Full mix configuration for one genre."""
    genre: str
    display_name: str
    description: str

    # Loudness
    lufs_target: float
    true_peak_ceiling: float

    # Vocal chain
    vocal_gain_db: float
    vocal_hp_filter_hz: float       # high-pass cut on vocals
    vocal_reverb_send: float        # 0–1

    # Instrumental chain
    inst_gain_db: float
    inst_sidechain_amount: float    # 0–1 (kick→bass sidechain depth)
    inst_compression_ratio: float   # 1:1 – 8:1

    # Global EQ / dynamics
    low_shelf_hz: float             # boost/cut below this freq
    low_shelf_gain_db: float        # negative = cut
    high_shelf_hz: float
    high_shelf_gain_db: float

    # Stereo / space
    stereo_width: float             # 0–1 (0 = mono, 1 = full wide)

    # BPM tolerance for matching (± BPM)
    bpm_range: Tuple[float, float]


# ---------------------------------------------------------------------------
# Genre preset library
# ---------------------------------------------------------------------------

GENRE_PRESETS: Dict[str, GenrePreset] = {

    "house": GenrePreset(
        genre="house",
        display_name="House / Deep House",
        description="4/4 groove — punchy kick, warm bass, bright synths. Sidechain heavy.",
        lufs_target=-9.0,
        true_peak_ceiling=-0.5,
        vocal_gain_db=1.0,
        vocal_hp_filter_hz=90.0,
        vocal_reverb_send=0.15,
        inst_gain_db=0.0,
        inst_sidechain_amount=0.35,
        inst_compression_ratio=4.0,
        low_shelf_hz=80.0,
        low_shelf_gain_db=2.0,
        high_shelf_hz=12000.0,
        high_shelf_gain_db=1.5,
        stereo_width=0.85,
        bpm_range=(118.0, 135.0),
    ),

    "techno": GenrePreset(
        genre="techno",
        display_name="Techno / Industrial",
        description="Dark, driving, minimal. Hard kick, no reverb on vocals.",
        lufs_target=-8.0,
        true_peak_ceiling=-0.3,
        vocal_gain_db=0.0,
        vocal_hp_filter_hz=150.0,
        vocal_reverb_send=0.05,
        inst_gain_db=1.0,
        inst_sidechain_amount=0.40,
        inst_compression_ratio=5.0,
        low_shelf_hz=60.0,
        low_shelf_gain_db=3.0,
        high_shelf_hz=10000.0,
        high_shelf_gain_db=-1.0,
        stereo_width=0.70,
        bpm_range=(128.0, 155.0),
    ),

    "hiphop": GenrePreset(
        genre="hiphop",
        display_name="Hip-Hop / Boom Bap",
        description="Vocal-forward, 808 sub, classic SP1200 grit. Dry vocal chain.",
        lufs_target=-11.0,
        true_peak_ceiling=-1.0,
        vocal_gain_db=3.0,
        vocal_hp_filter_hz=80.0,
        vocal_reverb_send=0.06,
        inst_gain_db=-2.0,
        inst_sidechain_amount=0.10,
        inst_compression_ratio=3.0,
        low_shelf_hz=100.0,
        low_shelf_gain_db=1.5,
        high_shelf_hz=8000.0,
        high_shelf_gain_db=2.0,
        stereo_width=0.65,
        bpm_range=(80.0, 110.0),
    ),

    "trap": GenrePreset(
        genre="trap",
        display_name="Trap / Modern Hip-Hop",
        description="Sub-heavy 808s, half-time feel, hi-hat rolls. Wide, modern sound.",
        lufs_target=-10.0,
        true_peak_ceiling=-0.5,
        vocal_gain_db=2.5,
        vocal_hp_filter_hz=100.0,
        vocal_reverb_send=0.10,
        inst_gain_db=-1.0,
        inst_sidechain_amount=0.15,
        inst_compression_ratio=3.5,
        low_shelf_hz=60.0,
        low_shelf_gain_db=4.0,     # sub boost
        high_shelf_hz=10000.0,
        high_shelf_gain_db=3.0,
        stereo_width=0.90,
        bpm_range=(60.0, 80.0),    # half-time feel; full grid is 120–160
    ),

    "pop": GenrePreset(
        genre="pop",
        display_name="Pop / Mainstream",
        description="Polished, vocal-forward, bright. Radio-ready at -14 LUFS.",
        lufs_target=-14.0,
        true_peak_ceiling=-1.0,
        vocal_gain_db=2.0,
        vocal_hp_filter_hz=100.0,
        vocal_reverb_send=0.10,
        inst_gain_db=-1.5,
        inst_sidechain_amount=0.12,
        inst_compression_ratio=2.5,
        low_shelf_hz=120.0,
        low_shelf_gain_db=-1.0,    # slightly lean low end
        high_shelf_hz=12000.0,
        high_shelf_gain_db=2.0,
        stereo_width=0.75,
        bpm_range=(90.0, 130.0),
    ),

    "rnb": GenrePreset(
        genre="rnb",
        display_name="R&B / Soul",
        description="Soulful, warm midrange, smooth dynamics. Vocal sits in a pocket.",
        lufs_target=-13.0,
        true_peak_ceiling=-1.0,
        vocal_gain_db=2.0,
        vocal_hp_filter_hz=85.0,
        vocal_reverb_send=0.12,
        inst_gain_db=-2.0,
        inst_sidechain_amount=0.08,
        inst_compression_ratio=2.0,
        low_shelf_hz=100.0,
        low_shelf_gain_db=1.0,
        high_shelf_hz=8000.0,
        high_shelf_gain_db=1.5,
        stereo_width=0.70,
        bpm_range=(60.0, 100.0),
    ),

    "dnb": GenrePreset(
        genre="dnb",
        display_name="Drum & Bass / Jungle",
        description="Fast breakbeats, amen breaks, sub bass. Max energy.",
        lufs_target=-8.0,
        true_peak_ceiling=-0.3,
        vocal_gain_db=0.5,
        vocal_hp_filter_hz=120.0,
        vocal_reverb_send=0.08,
        inst_gain_db=1.0,
        inst_sidechain_amount=0.20,
        inst_compression_ratio=4.5,
        low_shelf_hz=70.0,
        low_shelf_gain_db=3.0,
        high_shelf_hz=14000.0,
        high_shelf_gain_db=2.0,
        stereo_width=0.80,
        bpm_range=(160.0, 185.0),
    ),

    "ambient": GenrePreset(
        genre="ambient",
        display_name="Ambient / Downtempo",
        description="Spacious, gentle, atmospheric. Heavy reverb, wide stereo field.",
        lufs_target=-18.0,
        true_peak_ceiling=-2.0,
        vocal_gain_db=-1.0,
        vocal_hp_filter_hz=80.0,
        vocal_reverb_send=0.35,
        inst_gain_db=-2.0,
        inst_sidechain_amount=0.03,
        inst_compression_ratio=1.5,
        low_shelf_hz=150.0,
        low_shelf_gain_db=-2.0,
        high_shelf_hz=10000.0,
        high_shelf_gain_db=3.0,
        stereo_width=1.0,
        bpm_range=(50.0, 100.0),
    ),

    "rock": GenrePreset(
        genre="rock",
        display_name="Rock / Alternative",
        description="Guitar-driven, midrange emphasis, natural dynamics.",
        lufs_target=-12.0,
        true_peak_ceiling=-1.0,
        vocal_gain_db=2.5,
        vocal_hp_filter_hz=100.0,
        vocal_reverb_send=0.08,
        inst_gain_db=0.0,
        inst_sidechain_amount=0.05,
        inst_compression_ratio=2.0,
        low_shelf_hz=80.0,
        low_shelf_gain_db=0.0,
        high_shelf_hz=6000.0,
        high_shelf_gain_db=1.5,
        stereo_width=0.80,
        bpm_range=(100.0, 160.0),
    ),

    "jazz": GenrePreset(
        genre="jazz",
        display_name="Jazz / Classical",
        description="Wide dynamic range, natural acoustics, minimal processing.",
        lufs_target=-18.0,
        true_peak_ceiling=-1.0,
        vocal_gain_db=1.0,
        vocal_hp_filter_hz=60.0,
        vocal_reverb_send=0.18,
        inst_gain_db=-1.0,
        inst_sidechain_amount=0.02,
        inst_compression_ratio=1.2,
        low_shelf_hz=80.0,
        low_shelf_gain_db=-1.0,
        high_shelf_hz=14000.0,
        high_shelf_gain_db=1.0,
        stereo_width=0.75,
        bpm_range=(60.0, 200.0),   # wide — jazz tempo is very variable
    ),
}


# ---------------------------------------------------------------------------
# Genre detection
# ---------------------------------------------------------------------------

@dataclass
class GenreResult:
    """Result of genre detection — single label (backward-compat)."""
    genre: str
    confidence: float               # 0–1
    preset: GenrePreset
    features: Dict[str, float]      # raw features for debugging
    runner_up: Optional[str] = None # second-best genre


@dataclass
class GenreClassResult:
    """Multi-label genre classification with Russian description."""
    genres: list                    # [{"name": "dnb", "confidence": 0.9}, ...]
    tags: list                      # ["агрессивный", "с глубоким сабом", "инструментал"]
    description: str                # "Мощный тяжёлый DnB, 174 BPM, минор G, 6A. ..."
    primary_genre: str              # best single genre (backward-compat)
    confidence: float
    features: Dict[str, float]
    preset: GenrePreset


def _extract_detection_features(audio: np.ndarray, sr: int = 44100) -> Dict[str, float]:
    """
    Extract the small set of features used for genre classification.
    Designed to be fast (< 2 seconds on a 30-second clip).
    """
    feats: Dict[str, float] = {}

    # --- BPM ---
    tempo, _ = librosa.beat.beat_track(y=audio, sr=sr)
    feats["bpm"] = float(np.atleast_1d(tempo)[0])

    # --- Spectral centroid (brightness, Hz) ---
    centroid = librosa.feature.spectral_centroid(y=audio, sr=sr)
    feats["spectral_centroid"] = float(np.mean(centroid))

    # --- Spectral rolloff (frequency below which 85% of energy lives) ---
    rolloff = librosa.feature.spectral_rolloff(y=audio, sr=sr, roll_percent=0.85)
    feats["spectral_rolloff"] = float(np.mean(rolloff))

    # --- Zero-crossing rate (roughness / noisiness) ---
    zcr = librosa.feature.zero_crossing_rate(y=audio)
    feats["zcr"] = float(np.mean(zcr))

    # --- Low-energy ratio: fraction of frames with RMS < mean RMS ---
    rms = librosa.feature.rms(y=audio)
    mean_rms = float(np.mean(rms))
    feats["low_energy_ratio"] = float(np.mean(rms < mean_rms * 0.5))

    # --- Sub-bass energy ratio (0–200 Hz proportion of total energy) ---
    try:
        from scripts.core.gpu import gpu_stft
        stft = np.abs(gpu_stft(audio))
    except (ImportError, Exception):
        stft = np.abs(librosa.stft(audio))
    freqs = librosa.fft_frequencies(sr=sr)
    sub_mask = freqs <= 200.0
    total_energy = float(np.sum(stft))
    sub_energy  = float(np.sum(stft[sub_mask, :]))
    feats["sub_bass_ratio"] = sub_energy / (total_energy + 1e-10)

    # --- Chroma variance (harmonic complexity) ---
    chroma = librosa.feature.chroma_cqt(y=audio, sr=sr)
    feats["chroma_variance"] = float(np.var(chroma))

    # --- Dynamic range approximation (dB difference P95 – P5 RMS) ---
    rms_flat = rms.flatten()
    p95 = float(np.percentile(rms_flat, 95))
    p5  = float(np.percentile(rms_flat, 5))
    feats["dynamic_range_db"] = 20.0 * np.log10((p95 + 1e-10) / (p5 + 1e-10))

    return feats


def _score_genre(genre: str, preset: GenrePreset,
                 feats: Dict[str, float]) -> float:
    """
    Return a confidence score [0–1] for how well the features match a genre.
    Higher = better match.
    """
    bpm = feats["bpm"]
    centroid = feats["spectral_centroid"]
    zcr = feats["zcr"]
    sub_ratio = feats["sub_bass_ratio"]
    low_energy = feats["low_energy_ratio"]
    dynamic_range = feats["dynamic_range_db"]

    score = 0.0
    weight_total = 0.0

    # --- BPM score (most important signal) ---
    bpm_lo, bpm_hi = preset.bpm_range
    bpm_mid = (bpm_lo + bpm_hi) / 2
    bpm_half_width = (bpm_hi - bpm_lo) / 2
    if bpm_half_width > 0:
        bpm_score = max(0.0, 1.0 - abs(bpm - bpm_mid) / bpm_half_width)
    else:
        bpm_score = 1.0 if abs(bpm - bpm_mid) < 5 else 0.0
    score += 3.0 * bpm_score
    weight_total += 3.0

    # --- Sub-bass ratio (bass-heavy vs light) ---
    bass_genres = {"hiphop", "trap", "dnb", "house", "techno"}
    light_genres = {"jazz", "ambient", "pop"}
    if genre in bass_genres:
        bass_score = min(1.0, sub_ratio / 0.15)
    elif genre in light_genres:
        bass_score = max(0.0, 1.0 - sub_ratio / 0.10)
    else:
        bass_score = 0.5
    score += 1.5 * bass_score
    weight_total += 1.5

    # --- Dynamic range (compressed vs dynamic) ---
    compressed_genres = {"techno", "house", "dnb", "trap"}
    dynamic_genres = {"jazz", "ambient", "rock"}
    if genre in compressed_genres:
        dr_score = max(0.0, 1.0 - dynamic_range / 30.0)   # low DR = compressed
    elif genre in dynamic_genres:
        dr_score = min(1.0, dynamic_range / 20.0)           # high DR = dynamic
    else:
        dr_score = 0.5
    score += 1.0 * dr_score
    weight_total += 1.0

    # --- Spectral brightness ---
    bright_genres = {"techno", "dnb", "pop"}
    warm_genres = {"rnb", "jazz", "hiphop", "ambient"}
    if genre in bright_genres:
        bright_score = min(1.0, centroid / 4000.0)
    elif genre in warm_genres:
        bright_score = max(0.0, 1.0 - centroid / 4000.0)
    else:
        bright_score = 0.5
    score += 0.8 * bright_score
    weight_total += 0.8

    # --- ZCR (noisiness / roughness) ---
    rough_genres = {"rock", "techno", "dnb"}
    if genre in rough_genres:
        zcr_score = min(1.0, zcr / 0.08)
    else:
        zcr_score = max(0.0, 1.0 - zcr / 0.10)
    score += 0.5 * zcr_score
    weight_total += 0.5

    return score / weight_total if weight_total > 0 else 0.0


def detect_genre(audio: np.ndarray, sr: int = 44100) -> GenreResult:
    """
    Detect the most likely genre for a piece of audio and return
    the matching GenrePreset.

    Parameters
    ----------
    audio : np.ndarray
        Mono audio signal (use first 60 seconds max for speed).
    sr : int
        Sample rate.

    Returns
    -------
    GenreResult with genre name, confidence, preset, and raw features.
    """
    # Use at most 60 seconds for analysis speed
    max_samples = 60 * sr
    clip = audio[:max_samples] if len(audio) > max_samples else audio

    log.info("Detecting genre (clip length: %.1fs)…", len(clip) / sr)

    try:
        feats = _extract_detection_features(clip, sr)
    except Exception as e:
        log.warning("Feature extraction failed (%s) — defaulting to 'pop'", e)
        return GenreResult(
            genre="pop",
            confidence=0.0,
            preset=GENRE_PRESETS["pop"],
            features={},
        )

    # Score every genre
    scores = {
        genre: _score_genre(genre, preset, feats)
        for genre, preset in GENRE_PRESETS.items()
    }

    # Rank
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_genre, best_score = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else None

    # Normalise confidence so best possible = 1.0
    max_possible = max(scores.values()) if scores else 1.0
    confidence = best_score / max_possible if max_possible > 0 else 0.0

    log.info(
        "Genre: %s (%.0f%%) | runner-up: %s (%.0f%%)",
        best_genre, confidence * 100,
        runner_up, (scores.get(runner_up, 0) / max_possible * 100) if runner_up else 0,
    )

    return GenreResult(
        genre=best_genre,
        confidence=min(1.0, confidence),
        preset=GENRE_PRESETS[best_genre],
        features=feats,
        runner_up=runner_up,
    )


def detect_genre_from_file(wav_path: Path, sr: int = 44100) -> GenreResult:
    """Convenience wrapper: load a WAV file and detect its genre."""
    audio, file_sr = librosa.load(str(wav_path), sr=sr, mono=True, duration=60.0)
    return detect_genre(audio, file_sr)


# ---------------------------------------------------------------------------
# Backward-compatibility bridge: map old preset names → genre presets
# ---------------------------------------------------------------------------

LEGACY_PRESET_MAP: Dict[str, str] = {
    "radio":   "pop",
    "club":    "house",
    "ambient": "ambient",
}


def get_preset(name: str) -> GenrePreset:
    """
    Look up a preset by genre name or legacy name.
    Falls back to 'pop' if unknown.
    """
    name = name.lower().strip()
    if name in GENRE_PRESETS:
        return GENRE_PRESETS[name]
    if name in LEGACY_PRESET_MAP:
        return GENRE_PRESETS[LEGACY_PRESET_MAP[name]]
    log.warning("Unknown preset '%s', falling back to 'pop'", name)
    return GENRE_PRESETS["pop"]


def auto_preset(audio: np.ndarray, sr: int = 44100,
                override: Optional[str] = None) -> GenrePreset:
    """
    Return the best GenrePreset for the given audio.

    If override is provided (e.g. "trap", "club", "radio"),
    it skips detection and returns that preset directly.
    This is the main entry point for the remix engine.
    """
    if override:
        return get_preset(override)
    result = detect_genre(audio, sr)
    return result.preset


# ---------------------------------------------------------------------------
# DnB sub-style classification
# ---------------------------------------------------------------------------

DNB_STYLES = [
    {"name": "liquid",     "bpm_range": (170, 176), "sub_bass": "low",    "centroid": "high",   "energy": "low",  "desc": "плавный мелодичный"},
    {"name": "jump_up",    "bpm_range": (170, 178), "sub_bass": "medium", "centroid": "medium", "energy": "high", "desc": "прыгающий энергичный"},
    {"name": "techstep",   "bpm_range": (172, 178), "sub_bass": "high",   "centroid": "low",    "energy": "high", "desc": "механический тёмный"},
    {"name": "neurofunk",  "bpm_range": (174, 180), "sub_bass": "high",   "centroid": "low",    "energy": "high", "desc": "тяжёлый нейронный"},
    {"name": "darkstep",   "bpm_range": (170, 178), "sub_bass": "medium", "centroid": "low",    "energy": "medium", "desc": "мрачный агрессивный"},
    {"name": "jungle",     "bpm_range": (160, 175), "sub_bass": "medium", "centroid": "medium", "energy": "high", "desc": "хаотичный амен-брейки"},
    {"name": "minimal",    "bpm_range": (170, 174), "sub_bass": "low",    "centroid": "medium", "energy": "low",  "desc": "чистый минималистичный"},
    {"name": "dancefloor", "bpm_range": (172, 176), "sub_bass": "medium", "centroid": "high",   "energy": "high", "desc": "яркий танцевальный"},
]


def _classify_dnb_style(feats: Dict[str, float]) -> list:
    """Classify DnB sub-styles from features. Returns sorted list of {name, score}."""
    bpm = feats.get("bpm", 174)
    sub = feats.get("sub_bass_ratio", 0.10)
    centroid = feats.get("spectral_centroid", 3000)
    energy = feats.get("low_energy_ratio", 0.5)

    # Normalise features to low/medium/high buckets
    sub_bucket = "high" if sub > 0.15 else "medium" if sub > 0.08 else "low"
    centroid_bucket = "high" if centroid > 4000 else "medium" if centroid > 2500 else "low"
    energy_bucket = "high" if energy > 0.6 else "medium" if energy > 0.4 else "low"

    results = []
    for style in DNB_STYLES:
        score = 0.0
        # BPM match (most important)
        lo, hi = style["bpm_range"]
        if lo <= bpm <= hi:
            score += 3.0
        elif lo - 5 <= bpm <= hi + 5:
            score += 1.5
        # Sub-bass match
        if sub_bucket == style["sub_bass"]:
            score += 1.0
        # Centroid match
        if centroid_bucket == style["centroid"]:
            score += 0.8
        # Energy match
        if energy_bucket == style["energy"]:
            score += 0.6
        if score > 0:
            results.append({"name": style["name"], "score": round(score, 2)})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Multi-label classification + Russian description
# ---------------------------------------------------------------------------

def classify_genre_multi(audio: np.ndarray, sr: int = 44100,
                         key_name: str = "", mode: str = "",
                         camelot: str = "", tempo_type: str = "",
                         el: int = 0) -> GenreClassResult:
    """
    Multi-label genre classification with tags and Russian description.
    Returns GenreClassResult with genres list, tags, and natural language description.
    """
    # Run base detection
    result = detect_genre(audio, sr)
    feats = result.features
    bpm = feats.get("bpm", 120)

    # Build genres list (top-3)
    max_possible = max(
        _score_genre(g, p, feats)
        for g, p in GENRE_PRESETS.items()
    ) if feats else 1.0

    genre_scores = []
    for genre, preset in GENRE_PRESETS.items():
        score = _score_genre(genre, preset, feats)
        norm = score / max_possible if max_possible > 0 else 0
        if norm > 0.15:
            genre_scores.append({"name": genre, "confidence": round(min(1.0, norm), 2)})
    genre_scores.sort(key=lambda x: x["confidence"], reverse=True)
    genres = genre_scores[:5]

    # If primary is DnB, add sub-styles
    tags = []
    if result.genre == "dnb":
        dnb_styles = _classify_dnb_style(feats)
        for s in dnb_styles[:3]:
            genres.append({"name": s["name"], "confidence": round(min(1.0, s["score"] / 5.0), 2)})
            tags.append(_dnb_style_tag(s["name"]))

    # Generate tags from features
    tags.extend(_generate_tags(feats, result.genre, el))

    # Generate Russian description
    description = _generate_description(
        genres=genres, tags=tags, bpm=bpm,
        key_name=key_name, mode=mode, camelot=camelot,
        el=el, feats=feats,
    )

    return GenreClassResult(
        genres=genres,
        tags=tags,
        description=description,
        primary_genre=result.genre,
        confidence=result.confidence,
        features=feats,
        preset=result.preset,
    )


def _dnb_style_tag(style: str) -> str:
    mapping = {
        "liquid": "liquid funk",
        "jump_up": "jump up",
        "techstep": "techstep",
        "neurofunk": "neurofunk",
        "darkstep": "darkstep",
        "jungle": "jungle",
        "minimal": "minimal dnb",
        "dancefloor": "dancefloor dnb",
    }
    return mapping.get(style, style)


def _generate_tags(feats: Dict, genre: str, el: int) -> list:
    """Generate descriptive tags from audio features."""
    tags = []
    sub = feats.get("sub_bass_ratio", 0.10)
    centroid = feats.get("spectral_centroid", 3000)
    dynamic = feats.get("dynamic_range_db", 10)
    vocal_density = feats.get("low_energy_ratio", 0.5)

    # Bass
    if sub > 0.18:
        tags.append("глубокий саб")
    elif sub > 0.12:
        tags.append("плотный бас")
    elif sub < 0.05:
        tags.append("лёгкий бас")

    # Brightness
    if centroid > 4500:
        tags.append("яркие синты")
    elif centroid < 2000:
        tags.append("тёмный саунд")

    # Dynamic
    if dynamic > 20:
        tags.append("динамичный")
    elif dynamic < 8:
        tags.append("сжатый")

    # Energy level
    el_labels = {1: "спокойный", 2: "лёгкий", 3: "умеренный", 4: "энергичный", 5: "мощный"}
    if el in el_labels:
        tags.append(el_labels[el])

    # Tempo type
    tempo_labels = {
        "half-time": "полу-темп",
        "full-time": "полный темп",
        "downtempo": "замедленный",
    }
    if genre in ("dnb", "trap", "hiphop") and feats.get("bpm", 120) < 100:
        tags.append("полу-темп")

    return tags


def _generate_description(genres: list, tags: list, bpm: float,
                          key_name: str, mode: str, camelot: str,
                          el: int, feats: Dict) -> str:
    """
    Generate a natural language description in Russian, like Yandex Music.
    Example: "Мощный тяжёлый neurofunk DnB, 174 BPM, минор G, 6A. 
    Доминируют агрессивные басы и плотные драм-паттерны. Инструментал, высокая энергия (EL 4)."
    """
    parts = []

    # Adjective + genre
    adj_map = {
        "dnb": "Мощный", "liquid": "Плавный мелодичный", "jump_up": "Прыгающий",
        "techstep": "Тяжёлый механический", "neurofunk": "Агрессивный нейронный",
        "darkstep": "Мрачный", "jungle": "Хаотичный", "minimal": "Чистый минималистичный",
        "dancefloor": "Яркий танцевальный", "house": "Танцевальный", "techno": "Драйвовый",
        "hiphop": "Ритмичный", "trap": "Тяжёлый", "pop": "Мелодичный",
        "rnb": "Душевный", "ambient": "Атмосферный", "rock": "Энергичный",
        "jazz": "Импровизационный",
    }
    primary = genres[0]["name"] if genres else "unknown"
    adj = adj_map.get(primary, "")
    genre_names = [g["name"] for g in genres[:3]]
    genre_str = " / ".join(genre_names)
    parts.append(f"{adj} {genre_str}, {bpm:.0f} BPM")

    # Key
    if key_name:
        mode_ru = "мажор" if mode == "major" else "минор" if mode == "minor" else ""
        key_str = f"{key_name} {mode_ru}".strip()
        parts.append(key_str)
    if camelot:
        parts.append(camelot)

    # Bass description
    sub = feats.get("sub_bass_ratio", 0.10)
    if sub > 0.18:
        bass_desc = "Доминируют глубокие агрессивные басы"
    elif sub > 0.12:
        bass_desc = "Плотный басовый фундамент"
    elif sub > 0.06:
        bass_desc = "Сбалансированный низ"
    else:
        bass_desc = "Лёгкий прозрачный бас"

    # Vocal/instrumental
    vocal_density = feats.get("low_energy_ratio", 0.5)
    if vocal_density > 0.65:
        vocal_desc = "с вокалом"
    elif vocal_density < 0.35:
        vocal_desc = "инструментал"
    else:
        vocal_desc = ""

    # Energy
    el_adj = {1: "спокойная", 2: "умеренная", 3: "средняя", 4: "высокая", 5: "максимальная"}
    energy_desc = f"энергия {el_adj.get(el, 'средняя')} (EL {el})"

    # Build final description
    header = ", ".join(parts) + "."
    body = f"{bass_desc}."
    if vocal_desc:
        body += f" {vocal_desc.capitalize()}."
    body += f" {energy_desc.capitalize()}."

    return f"{header} {body}"
