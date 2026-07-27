"""
scripts/core/synth/lfo.py — Low-Frequency Oscillator for modulation.
"""

from __future__ import annotations

import numpy as np

from .base import ModuleBase


class LFO(ModuleBase):
    """LFO producing sine or triangle modulation signal."""

    def __init__(self, name: str = "lfo"):
        super().__init__(name)
        self._params = {
            "lfo_rate": 1.0,    # Hz
            "lfo_depth": 0.0,   # 0–1 modulation depth
        }
        self._phase = 0.0

    def reset(self) -> None:
        self._phase = 0.0

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        rate = self._params.get("lfo_rate", 1.0)
        depth = self._params.get("lfo_depth", 0.0)
        n = len(buffer)

        phase_inc = 2.0 * np.pi * rate / sample_rate
        t = np.arange(n, dtype=np.float64)
        phases = self._phase + t * phase_inc

        # Sine LFO
        lfo = np.sin(phases).astype(np.float32)

        # Normalize to [0, 1] range for modulation
        lfo = (lfo + 1.0) * 0.5

        self._phase = phases[-1] % (2.0 * np.pi) if n > 0 else self._phase

        return (lfo * depth).astype(np.float32)
