"""
scripts/core/synth/oscillator.py — Oscillator modules: sine, saw, square, triangle, noise.
"""

from __future__ import annotations

import numpy as np

from .base import ModuleBase


class Oscillator(ModuleBase):
    """Polyblep-anti-aliased oscillator with waveform selection."""

    def __init__(self, name: str = "osc"):
        super().__init__(name)
        self._phase = 0.0
        self._params = {
            "osc_type": 0.0,    # 0=sine,1=saw,2=square,3=triangle,4=noise
            "osc_freq": 440.0,
            "osc_amp": 0.8,
        }

    def reset(self) -> None:
        self._phase = 0.0

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        n = len(buffer)
        freq = self._params.get("osc_freq", 440.0)
        amp = self._params.get("osc_amp", 0.8)
        osc_type = int(self._params.get("osc_type", 0))

        t = np.arange(n, dtype=np.float64)
        phase_inc = 2.0 * np.pi * freq / sample_rate
        phases = self._phase + t * phase_inc
        self._phase = phases[-1] % (2.0 * np.pi) if n > 0 else self._phase

        if osc_type == 0:  # sine
            out = np.sin(phases).astype(np.float32)
        elif osc_type == 1:  # saw (polyblep)
            norm_phase = (phases / (2.0 * np.pi)) % 1.0
            out = (2.0 * norm_phase - 1.0).astype(np.float32)
            out = _polyblep_saw(norm_phase, out, sample_rate)
        elif osc_type == 2:  # square (polyblep)
            norm_phase = (phases / (2.0 * np.pi)) % 1.0
            out = np.where(norm_phase < 0.5, 1.0, -1.0).astype(np.float32)
            out = _polyblep_square(norm_phase, out, sample_rate)
        elif osc_type == 3:  # triangle (integrated square)
            norm_phase = (phases / (2.0 * np.pi)) % 1.0
            square = np.where(norm_phase < 0.5, 1.0, -1.0).astype(np.float64)
            out = np.cumsum(square) / sample_rate * freq * 4.0
            out = out.astype(np.float32)
            peak = np.abs(out).max()
            if peak > 0:
                out = out / peak
        else:  # noise
            out = np.random.randn(n).astype(np.float32)

        return (buffer + out * amp).astype(np.float32)


def _polyblep_saw(phase: np.ndarray, out: np.ndarray, sr: int) -> np.ndarray:
    """Polyblep anti-aliasing for saw wave."""
    dt = 1.0 / sr
    result = out.copy().astype(np.float64)
    # Discontinuity at phase = 0
    mask = phase < dt
    t = phase[mask] / dt
    result[mask] -= t - t * t * 0.5
    # Discontinuity at phase = 1
    mask2 = phase > (1.0 - dt)
    t2 = (phase[mask2] - 1.0) / dt
    result[mask2] += 0.5 * t2 * t2
    return result.astype(np.float32)


def _polyblep_square(phase: np.ndarray, out: np.ndarray, sr: int) -> np.ndarray:
    """Polyblep anti-aliasing for square wave."""
    dt = 1.0 / sr
    result = out.copy().astype(np.float64)
    # Discontinuity at phase = 0
    mask = phase < dt
    t = phase[mask] / dt
    result[mask] -= t - t * t * 0.5
    # Discontinuity at phase = 0.5
    mask2 = (phase >= 0.5) & (phase < 0.5 + dt)
    t2 = (phase[mask2] - 0.5) / dt
    result[mask2] -= t2 - t2 * t2 * 0.5
    return result.astype(np.float32)
