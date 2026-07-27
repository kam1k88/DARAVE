"""
darave_rl/reward.py — Reward functions for DJ transitions and sound design.

Two families of metrics:
  1. DJ metrics       — energy continuity, spectral smoothness, phase coherence,
                        transient clarity, user score mapping
  2. Producer metrics — harmonic structure similarity, envelope similarity,
                        spectral balance, compression feel

All functions are pure (no side effects), operate on numpy arrays, and return
floats in [-1, 1] where 1 = best quality.

Usage:
    from darave_rl.reward import (
        compute_dj_reward, compute_prod_reward,
        energy_continuity, spectral_smoothness, phase_coherence,
        transient_clarity, user_score_to_reward,
        harmonic_similarity, envelope_similarity, spectral_balance,
        compression_feel,
    )
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from scripts.core.synth.analytics import (
    assess_mix_quality,
    detect_transients,
    spectral_features,
    stft,
)

# ---------------------------------------------------------------------------
# DJ Metrics
# ---------------------------------------------------------------------------


def energy_continuity(mix: np.ndarray, sr: int) -> float:
    """
    Measure RMS energy smoothness across the transition.

    Computes local RMS in overlapping frames and measures the standard
    deviation of frame-to-frame energy changes.  Smooth transitions have
    low variance → high reward.

    Returns float in [-1, 1].  1 = perfectly smooth energy contour.
    """
    mix = mix.astype(np.float32)
    if len(mix) < sr:
        return 0.0

    frame_size = max(512, sr // 10)
    hop = frame_size // 2
    n_frames = max(1, (len(mix) - frame_size) // hop)

    rms_values = np.empty(n_frames, dtype=np.float64)
    for i in range(n_frames):
        start = i * hop
        frame = mix[start : start + frame_size]
        rms_values[i] = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))

    if n_frames < 2:
        return 0.0

    # Frame-to-frame energy deltas
    deltas = np.diff(rms_values)
    delta_std = float(np.std(deltas))

    # Map: low std → high reward, high std → penalty
    # Threshold: std > 0.15 is considered rough
    reward = 1.0 - min(delta_std / 0.15, 1.0)
    return float(np.clip(reward, -1.0, 1.0))


def spectral_smoothness(
    audio_a: np.ndarray,
    audio_b: np.ndarray,
    sr: int,
    n_fft: int = 2048,
) -> float:
    """
    Spectral flux between Song A exit and Song B entry segments.

    Low spectral flux → smooth frequency transition → high reward.
    Returns float in [-1, 1].
    """
    if len(audio_a) < n_fft or len(audio_b) < n_fft:
        return 0.0

    mag_a, _, _ = stft(audio_a, sr, hop_length=n_fft // 2, n_fft=n_fft)
    mag_b, _, _ = stft(audio_b, sr, hop_length=n_fft // 2, n_fft=n_fft)

    if mag_a.shape[0] == 0 or mag_b.shape[0] == 0:
        return 0.0

    # Compare the last frame of A with first frame of B
    spec_a_end = mag_a[-1].astype(np.float64)
    spec_b_start = mag_b[0].astype(np.float64)

    # Normalized spectral flux (Euclidean distance of normalised spectra)
    a_norm = spec_a_end / (spec_a_end.sum() + 1e-10)
    b_norm = spec_b_start / (spec_b_start.sum() + 1e-10)
    flux = float(np.sqrt(np.sum((a_norm - b_norm) ** 2)))

    # Map: flux in [0, ~1.4] (max for orthogonal spectra)
    # Low flux → high reward
    reward = 1.0 - min(flux / 0.8, 1.0)
    return float(np.clip(reward, -1.0, 1.0))


def phase_coherence(mix: np.ndarray, sr: int) -> float:
    """
    Phase coherence of the transition mix.

    Uses crest factor (peak / RMS ratio).  Well-phase-coherent mixes
    have moderate crest factors; extreme values indicate problems.

    Returns float in [-1, 1].
    """
    mix = mix.astype(np.float32)
    if len(mix) < sr:
        return 0.0

    peak = float(np.abs(mix).max())
    rms = float(np.sqrt(np.mean(mix.astype(np.float64) ** 2)))

    if rms < 1e-10 or peak < 1e-10:
        return 0.0

    crest_factor = peak / rms

    # Ideal crest factor for a well-mixed transition: ~3-6 dB (linear 1.4-2.0)
    # Too low (< 1.2): overcompressed → penalty
    # Too high (> 4.0): uncontrolled peaks → penalty
    if 1.2 <= crest_factor <= 4.0:
        # Map to [0, 1] centered at ideal (2.0)
        dist = abs(crest_factor - 2.0) / 2.0
        reward = 1.0 - dist
    elif crest_factor < 1.2:
        reward = -0.5  # overcompressed
    else:
        reward = -0.5  # uncontrolled peaks

    return float(np.clip(reward, -1.0, 1.0))


def transient_clarity(mix: np.ndarray, sr: int) -> float:
    """
    Transient clarity via attack-to-sustain energy ratio.

    Clear transients have strong attacks relative to sustain.
    Returns float in [-1, 1].
    """
    mix = mix.astype(np.float32)
    if len(mix) < sr:
        return 0.0

    transients = detect_transients(mix, sr, threshold=0.2)
    if len(transients) < 2:
        return 0.0

    # Compute average attack-to-sustain ratio around detected transients
    attack_window = int(0.01 * sr)  # 10 ms attack
    sustain_window = int(0.05 * sr)  # 50 ms sustain

    ratios = []
    for t in transients:
        idx = int(t * sr)
        if idx + attack_window + sustain_window > len(mix):
            continue
        attack_energy = float(np.mean(np.abs(mix[idx : idx + attack_window]) ** 2))
        sustain_energy = float(
            np.mean(np.abs(mix[idx + attack_window : idx + attack_window + sustain_window]) ** 2)
        )
        if sustain_energy > 1e-10:
            ratios.append(attack_energy / sustain_energy)

    if not ratios:
        return 0.0

    mean_ratio = float(np.mean(ratios))

    # Ideal ratio: 2-8 (attacks are 2-8x louder than sustain)
    if 2.0 <= mean_ratio <= 8.0:
        dist = abs(mean_ratio - 5.0) / 5.0
        reward = 1.0 - dist * 0.5
    elif mean_ratio < 2.0:
        reward = -0.3  # dull transients
    else:
        reward = -0.3  # harsh transients

    return float(np.clip(reward, -1.0, 1.0))


def user_score_to_reward(user_score: float) -> float:
    """
    Map user score [0, 1] to reward [-1, 1].

    0.0 → -1.0 (terrible)
    0.5 →  0.0 (neutral)
    1.0 → +1.0 (perfect)
    """
    return float(np.clip(2.0 * user_score - 1.0, -1.0, 1.0))


# ---------------------------------------------------------------------------
# DJ Reward Aggregate
# ---------------------------------------------------------------------------

# Default weights for DJ reward components
DJ_WEIGHTS = {
    "energy": 0.25,
    "spectral": 0.20,
    "phase": 0.20,
    "transient": 0.15,
    "user": 0.20,
}


def compute_dj_reward(
    mix: np.ndarray,
    audio_a: np.ndarray,
    audio_b: np.ndarray,
    sr: int,
    user_score: Optional[float] = None,
    weights: Optional[dict] = None,
) -> dict:
    """
    Compute weighted DJ reward for a transition mix.

    Returns dict with individual metrics and 'total' aggregate.
    """
    w = weights or DJ_WEIGHTS

    r_energy = energy_continuity(mix, sr)
    r_spec = spectral_smoothness(audio_a, audio_b, sr)
    r_phase = phase_coherence(mix, sr)
    r_trans = transient_clarity(mix, sr)
    r_user = user_score_to_reward(user_score) if user_score is not None else 0.0

    total = (
        w["energy"] * r_energy
        + w["spectral"] * r_spec
        + w["phase"] * r_phase
        + w["transient"] * r_trans
        + w["user"] * r_user
    )

    return {
        "total": float(np.clip(total, -1.0, 1.0)),
        "energy": r_energy,
        "spectral": r_spec,
        "phase": r_phase,
        "transient": r_trans,
        "user": r_user,
    }


# ---------------------------------------------------------------------------
# Producer Metrics
# ---------------------------------------------------------------------------


def harmonic_similarity(
    struct_a: object,
    struct_b: object,
) -> float:
    """
    Harmonic structure similarity using Camelot wheel distance.

    struct_a/struct_b should have .camelot (str) attribute
    (e.g., SongStructure from dj_analysis).

    Returns float in [-1, 1].  1 = same key / adjacent Camelot.
    """
    try:
        from scripts.core.key_detection import camelot_distance

        camelot_a = getattr(struct_a, "camelot", "")
        camelot_b = getattr(struct_b, "camelot", "")
        if not camelot_a or not camelot_b:
            return 0.0
        dist = camelot_distance(camelot_a, camelot_b)
        # dist is 0-6 (0 = same, 6 = tritone)
        # Map: 0 → 1.0, 6 → -1.0
        return float(np.clip(1.0 - dist / 3.0, -1.0, 1.0))
    except Exception:
        return 0.0


def envelope_similarity(mix: np.ndarray, sr: int) -> float:
    """
    Envelope similarity: RMS envelope symmetry across the transition midpoint.

    A good transition has roughly symmetric energy before and after the center.
    Returns float in [-1, 1].
    """
    mix = mix.astype(np.float32)
    n = len(mix)
    if n < sr:
        return 0.0

    mid = n // 2
    half = min(mid, n - mid)

    left = mix[mid - half : mid]
    right = mix[mid : mid + half]

    # RMS envelope of each half (coarse frame-level)
    frame_size = max(256, sr // 20)
    hop = frame_size // 2
    n_frames = max(
        1,
        min(
            (len(left) - frame_size) // hop,
            (len(right) - frame_size) // hop,
        ),
    )

    left_rms = np.empty(n_frames)
    right_rms = np.empty(n_frames)
    for i in range(n_frames):
        s = i * hop
        left_rms[i] = float(np.sqrt(np.mean(left[s : s + frame_size].astype(np.float64) ** 2)))
        right_rms[i] = float(np.sqrt(np.mean(right[s : s + frame_size].astype(np.float64) ** 2)))

    # Correlation of the two envelopes
    if left_rms.std() < 1e-10 or right_rms.std() < 1e-10:
        return 0.0

    corr = float(np.corrcoef(left_rms, right_rms)[0, 1])
    return float(np.clip(corr, -1.0, 1.0))


def spectral_balance(mix: np.ndarray, sr: int, n_fft: int = 2048) -> float:
    """
    Spectral flatness — how evenly energy is distributed across frequencies.

    High flatness (white noise) → 0 reward
    Moderate flatness (musical mix) → high reward
    Returns float in [-1, 1].
    """
    mix = mix.astype(np.float32)
    if len(mix) < n_fft:
        return 0.0

    features = spectral_features(
        stft(mix[:n_fft], sr, hop_length=n_fft // 2, n_fft=n_fft)[0],
        sr,
        n_fft,
    )
    flatness = features.get("spectral_flatness", 0.5)

    # Musical range: 0.01 - 0.3 is typical for well-balanced mixes
    if 0.01 <= flatness <= 0.3:
        reward = 1.0 - abs(flatness - 0.15) / 0.15
    elif flatness < 0.01:
        reward = 0.5  # too narrow
    else:
        reward = -0.5  # too noisy

    return float(np.clip(reward, -1.0, 1.0))


def compression_feel(mix: np.ndarray, sr: int) -> float:
    """
    Compression feel: dynamic range assessment.

    Well-compressed transitions have controlled dynamics without sounding
    over-squashed.  Returns float in [-1, 1].
    """
    hints = assess_mix_quality(mix, sr)

    # Dynamic range in dB (ideal: 6-14 dB for a transition)
    dr = hints.dynamic_range_db
    if 6.0 <= dr <= 14.0:
        reward = 1.0 - abs(dr - 10.0) / 10.0
    elif dr < 6.0:
        reward = -0.5  # overcompressed
    else:
        reward = 0.0  # undercompressed (not terrible)

    # Penalize clipping
    if hints.clipping:
        reward -= 0.3

    # Penalize overcompression
    if hints.overcompressed > 0.5:
        reward -= 0.2

    return float(np.clip(reward, -1.0, 1.0))


# ---------------------------------------------------------------------------
# Producer Reward Aggregate
# ---------------------------------------------------------------------------

PROD_WEIGHTS = {
    "harmonic": 0.25,
    "envelope": 0.25,
    "balance": 0.25,
    "compression": 0.25,
}


def compute_prod_reward(
    mix: np.ndarray,
    struct_a: object,
    struct_b: object,
    sr: int,
    weights: Optional[dict] = None,
) -> dict:
    """
    Compute weighted producer reward for a transition mix.

    Returns dict with individual metrics and 'total' aggregate.
    """
    w = weights or PROD_WEIGHTS

    r_harmonic = harmonic_similarity(struct_a, struct_b)
    r_envelope = envelope_similarity(mix, sr)
    r_balance = spectral_balance(mix, sr)
    r_compression = compression_feel(mix, sr)

    total = (
        w["harmonic"] * r_harmonic
        + w["envelope"] * r_envelope
        + w["balance"] * r_balance
        + w["compression"] * r_compression
    )

    return {
        "total": float(np.clip(total, -1.0, 1.0)),
        "harmonic": r_harmonic,
        "envelope": r_envelope,
        "balance": r_balance,
        "compression": r_compression,
    }
