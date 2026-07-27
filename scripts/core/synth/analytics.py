"""
scripts/core/synth/analytics.py — FFT/STFT, phase correlator, transient detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# STFT / Spectral features
# ---------------------------------------------------------------------------

def stft(
    audio: np.ndarray,
    sr: int,
    hop_length: int = 512,
    n_fft: int = 2048,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Short-Time Fourier Transform.

    Returns
    -------
    magnitudes : [n_frames, n_bins] float32
    phases : [n_frames, n_bins] float32
    times : [n_frames] float64 — center times in seconds
    """
    audio = audio.astype(np.float32)
    n_frames = 1 + (len(audio) - n_fft) // hop_length
    if n_frames <= 0:
        # Pad audio
        pad_len = n_fft - len(audio)
        audio = np.pad(audio, (0, pad_len))
        n_frames = 1

    window = np.hanning(n_fft).astype(np.float32)
    frames = np.stack([
        audio[i * hop_length : i * hop_length + n_fft] * window
        for i in range(n_frames)
    ])

    spectrum = np.fft.rfft(frames, axis=1)
    magnitudes = np.abs(spectrum).astype(np.float32)
    phases = np.angle(spectrum).astype(np.float32)

    times = (np.arange(n_frames) * hop_length + n_fft / 2) / sr
    return magnitudes, phases, times


def spectral_features(magnitudes: np.ndarray, sr: int, n_fft: int = 2048) -> dict:
    """Compute spectral features from STFT magnitudes.

    Returns dict with:
        spectral_centroid, spectral_rolloff, spectral_slope,
        harmonic_peaks, spectral_flatness
    """
    n_bins = magnitudes.shape[1]
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)

    # Mean spectrum
    mean_spec = magnitudes.mean(axis=0)

    # Spectral centroid
    total_energy = mean_spec.sum()
    if total_energy > 0:
        centroid = (freqs * mean_spec).sum() / total_energy
    else:
        centroid = 0.0

    # Spectral rolloff (85%)
    cumsum = np.cumsum(mean_spec)
    rolloff_idx = np.searchsorted(cumsum, 0.85 * cumsum[-1])
    rolloff = freqs[min(rolloff_idx, n_bins - 1)]

    # Spectral slope (linear regression on log magnitude)
    log_spec = np.log(mean_spec + 1e-10)
    x = freqs / (sr / 2)  # normalize to [0, 1]
    slope = np.polyfit(x, log_spec, 1)[0]

    # Spectral flatness (geometric mean / arithmetic mean)
    log_mean = log_spec.mean()
    flatness = np.exp(log_mean) / (mean_spec.mean() + 1e-10)
    flatness = min(flatness, 1.0)

    # Harmonic peaks (top 5 peaks in spectrum)
    peaks = _find_peaks(mean_spec, n=5)
    harmonic_peaks = [(float(freqs[p]), float(mean_spec[p])) for p in peaks]

    return {
        "spectral_centroid": float(centroid),
        "spectral_rolloff": float(rolloff),
        "spectral_slope": float(slope),
        "spectral_flatness": float(flatness),
        "harmonic_peaks": harmonic_peaks,
    }


def _find_peaks(spectrum: np.ndarray, n: int = 5) -> list:
    """Find top N peak indices in spectrum."""
    if len(spectrum) < 3:
        return list(range(min(n, len(spectrum))))
    # Simple local maxima
    peaks = []
    for i in range(1, len(spectrum) - 1):
        if spectrum[i] > spectrum[i - 1] and spectrum[i] > spectrum[i + 1]:
            peaks.append(i)
    # Sort by magnitude, take top N
    peaks.sort(key=lambda p: spectrum[p], reverse=True)
    return peaks[:n]


# ---------------------------------------------------------------------------
# Phase correlator
# ---------------------------------------------------------------------------

def phase_correlation(audio_a: np.ndarray, audio_b: np.ndarray, sr: int) -> dict:
    """Estimate phase correlation between two mono signals.

    Returns dict with:
        correlation : float [-1, 1]
        phase_shift_deg : estimated phase shift in degrees
    """
    n = min(len(audio_a), len(audio_b))
    if n == 0:
        return {"correlation": 0.0, "phase_shift_deg": 0.0}

    a = audio_a[:n].astype(np.float32)
    b = audio_b[:n].astype(np.float32)

    # Normalized cross-correlation
    a_norm = a - a.mean()
    b_norm = b - b.mean()
    denom = np.sqrt((a_norm ** 2).sum() * (b_norm ** 2).sum())
    if denom < 1e-10:
        return {"correlation": 0.0, "phase_shift_deg": 0.0}

    corr = float(np.dot(a_norm, b_norm) / denom)

    # Phase shift via FFT cross-spectrum
    fft_a = np.fft.rfft(a)
    fft_b = np.fft.rfft(b)
    cross = fft_a * np.conj(fft_b)
    phase_angle = np.angle(cross.mean())
    phase_deg = float(np.degrees(phase_angle))

    return {
        "correlation": corr,
        "phase_shift_deg": phase_deg,
    }


# ---------------------------------------------------------------------------
# Transient detector
# ---------------------------------------------------------------------------

def detect_transients(
    audio: np.ndarray,
    sr: int,
    threshold: float = 0.3,
    hop_length: int = 512,
) -> list:
    """Detect transient onsets in audio.

    Uses spectral flux + amplitude change.
    Returns list of transient times in seconds.
    """
    audio = audio.astype(np.float32)
    n_fft = 2048

    if len(audio) < n_fft:
        return []

    # Spectral flux
    magnitudes, _, times = stft(audio, sr, hop_length=hop_length, n_fft=n_fft)
    if magnitudes.shape[0] < 2:
        return []

    # Positive spectral flux
    diff = np.diff(magnitudes, axis=0)
    flux = np.maximum(diff, 0).sum(axis=1)

    # Amplitude envelope
    n_frames = len(flux)
    frame_size = hop_length
    amplitudes = np.array([
        np.abs(audio[i * frame_size : (i + 1) * frame_size]).mean()
        for i in range(n_frames)
    ])

    # Combine spectral flux + amplitude change
    if flux.max() > 0:
        flux_norm = flux / flux.max()
    else:
        flux_norm = flux

    if amplitudes.max() > 0:
        amp_change = np.diff(amplitudes, prepend=amplitudes[0])
        amp_norm = amp_change / (amplitudes.max() + 1e-10)
    else:
        amp_norm = np.zeros_like(flux_norm)

    combined = flux_norm + np.maximum(amp_norm, 0)

    # Threshold + local maxima
    transients = []
    for i in range(1, len(combined) - 1):
        if combined[i] > threshold and combined[i] > combined[i - 1]:
            t = times[i + 1] if i + 1 < len(times) else times[i]
            transients.append(float(t))

    return transients


# ---------------------------------------------------------------------------
# Mix quality hints
# ---------------------------------------------------------------------------

@dataclass
class MixQualityHints:
    """Numeric hints about mix quality."""
    bass_mud: float = 0.0          # 0–1, how muddy the bass is
    treble_harsh: float = 0.0      # 0–1, how harsh the treble is
    overcompressed: float = 0.0    # 0–1, dynamic range compression
    clipping: bool = False
    clipping_count: int = 0
    dynamic_range_db: float = 0.0


def assess_mix_quality(audio: np.ndarray, sr: int) -> MixQualityHints:
    """Analyze mix quality and return numeric hints."""
    audio = audio.astype(np.float32)
    hints = MixQualityHints()

    if len(audio) == 0:
        return hints

    # Clipping
    clip_threshold = 0.99
    clipped = np.abs(audio) >= clip_threshold
    hints.clipping = bool(clipped.any())
    hints.clipping_count = int(clipped.sum())

    # Dynamic range
    peak = float(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms > 0 and peak > 0:
        hints.dynamic_range_db = float(20 * np.log10(peak / rms))

    # Bass mud: energy in 20–200 Hz vs total
    n_fft = 2048
    if len(audio) >= n_fft:
        spectrum = np.abs(np.fft.rfft(audio[:n_fft]))
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
        total = spectrum.sum()
        if total > 0:
            bass_mask = freqs < 200
            bass_energy = spectrum[bass_mask].sum() / total
            hints.bass_mud = float(min(bass_energy * 2, 1.0))

    # Treble harshness: energy in 4–8 kHz
    if len(audio) >= n_fft:
        if total > 0:
            harsh_mask = (freqs >= 4000) & (freqs <= 8000)
            harsh_energy = spectrum[harsh_mask].sum() / total
            hints.treble_harsh = float(min(harsh_energy * 5, 1.0))

    # Overcompression: ratio of RMS to peak
    if peak > 0:
        compression_ratio = rms / peak
        if compression_ratio > 0.5:
            hints.overcompressed = float((compression_ratio - 0.5) * 2)

    return hints
