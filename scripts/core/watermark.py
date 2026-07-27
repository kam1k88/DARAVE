"""
scripts/core/watermark.py — Audio watermarking for demo exports.

Adds a "DARAVE preview" text-to-speech watermark or a subtle
inaudible watermark for demo/trial mixes.

Usage:
    from scripts.core.watermark import add_watermark
    watermarked = add_watermark(audio, sr, mode="tts")
"""

from __future__ import annotations

import logging
import numpy as np
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Watermark text
WATERMARK_TEXT = "DARAVE preview"


def _generate_tts_watermark(
    sr: int,
    duration_sec: float = 2.0,
    volume: float = 0.15,
) -> Optional[np.ndarray]:
    """
    Generate a simple sine-wave beep as a placeholder TTS watermark.
    In production, this would use a real TTS engine.
    Returns mono audio array or None if generation fails.
    """
    try:
        t = np.linspace(0, duration_sec, int(sr * duration_sec), dtype=np.float32)
        # Two-tone beep pattern (880Hz + 1320Hz)
        tone1 = 0.5 * np.sin(2 * np.pi * 880 * t)
        tone2 = 0.3 * np.sin(2 * np.pi * 1320 * t)
        beep = (tone1 + tone2) * volume

        # Apply envelope (fade in/out)
        fade_samples = int(0.05 * sr)
        envelope = np.ones_like(beep)
        envelope[:fade_samples] = np.linspace(0, 1, fade_samples)
        envelope[-fade_samples:] = np.linspace(1, 0, fade_samples)
        beep *= envelope

        return beep.astype(np.float32)
    except Exception as e:
        log.warning("Failed to generate TTS watermark: %s", e)
        return None


def _generate_spectral_watermark(
    sr: int,
    duration_samples: int,
    volume: float = 0.02,
) -> np.ndarray:
    """
    Generate an inaudible spectral watermark (spread-spectrum).
    Embeds energy in frequency bands that are perceptually masked
    by typical music content.
    """
    n_fft = 2048
    watermark = np.zeros(duration_samples, dtype=np.float32)

    # Embed in 8-12 kHz band (inaudible in most DnB)
    freq_low = 8000
    freq_high = 12000

    for start in range(0, duration_samples - n_fft, n_fft):
        frame = np.zeros(n_fft, dtype=np.float32)
        # Generate spread-spectrum noise in target band
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
        mask = (freqs >= freq_low) & (freqs <= freq_high)
        spectrum = np.zeros(len(freqs), dtype=np.complex64)
        spectrum[mask] = np.random.randn(mask.sum()) + 1j * np.random.randn(mask.sum())
        frame = np.fft.irfft(spectrum, n_fft).astype(np.float32)
        frame *= volume / (np.max(np.abs(frame)) + 1e-8)
        watermark[start:start + n_fft] += frame

    return watermark


def add_watermark(
    audio: np.ndarray,
    sr: int,
    mode: str = "tts",
    volume: float = 0.15,
    repeat_interval_sec: float = 30.0,
) -> np.ndarray:
    """
    Add watermark to audio.

    Parameters
    ----------
    audio : np.ndarray
        Input audio (mono or stereo).
    sr : int
        Sample rate.
    mode : str
        "tts" — periodic beep watermark (demo mode).
        "spectral" — inaudible spread-spectrum watermark.
        "both" — TTS + spectral combined.
    volume : float
        Watermark volume (0.0-1.0).
    repeat_interval_sec : float
        How often to repeat the TTS watermark (seconds).

    Returns
    -------
    np.ndarray
        Watermarked audio (same shape as input).
    """
    if mode == "none":
        return audio

    result = audio.copy().astype(np.float32)
    total_samples = len(result)

    # Ensure mono for processing
    if result.ndim > 1:
        mono = result.mean(axis=1)
    else:
        mono = result

    if mode in ("tts", "both"):
        tts_wm = _generate_tts_watermark(sr, duration_sec=2.0, volume=volume)
        if tts_wm is not None:
            repeat_samples = int(repeat_interval_sec * sr)
            for start in range(0, total_samples - len(tts_wm), repeat_samples):
                end = min(start + len(tts_wm), total_samples)
                mono[start:end] += tts_wm[: end - start]

    if mode in ("spectral", "both"):
        spectral_wm = _generate_spectral_watermark(
            sr, total_samples, volume=volume * 0.1,
        )
        mono += spectral_wm[:total_samples]

    # Clip to prevent overflow
    mono = np.clip(mono, -1.0, 1.0)

    if result.ndim > 1:
        result = np.stack([mono] * result.shape[1], axis=1)
    else:
        result = mono

    return result.astype(np.float32)


def add_watermark_to_file(
    input_path: str | Path,
    output_path: str | Path,
    mode: str = "tts",
    volume: float = 0.15,
) -> bool:
    """
    Read an audio file, add watermark, write to output.
    Returns True on success.
    """
    try:
        import soundfile as sf
        audio, sr = sf.read(str(input_path))
        watermarked = add_watermark(audio, sr, mode=mode, volume=volume)
        sf.write(str(output_path), watermarked, sr)
        log.info("Watermarked: %s -> %s (mode=%s)", input_path, output_path, mode)
        return True
    except Exception as e:
        log.error("Watermark failed for %s: %s", input_path, e)
        return False
