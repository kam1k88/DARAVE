"""
scripts/core/synth/filters.py — Biquad filters: LPF, HPF, BPF, Notch.
"""

from __future__ import annotations

import math

import numpy as np

from .base import ModuleBase


class BiquadFilter(ModuleBase):
    """Single biquad filter with variable type (LPF/HPF/BPF/Notch)."""

    def __init__(self, name: str = "filter"):
        super().__init__(name)
        self._params = {
            "filter_type": 0.0,     # 0=lpf,1=hpf,2=bpf,3=notch
            "filter_cutoff": 1000.0,
            "filter_resonance": 0.7,
        }
        self._x1 = 0.0
        self._x2 = 0.0
        self._y1 = 0.0
        self._y2 = 0.0

    def reset(self) -> None:
        self._x1 = self._x2 = self._y1 = self._y2 = 0.0

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        cutoff = self._params.get("filter_cutoff", 1000.0)
        q = self._params.get("filter_resonance", 0.7)
        filter_type = int(self._params.get("filter_type", 0))

        # Clamp cutoff
        cutoff = max(20.0, min(cutoff, sample_rate * 0.499))

        # Compute biquad coefficients
        w0 = 2.0 * math.pi * cutoff / sample_rate
        alpha = math.sin(w0) / (2.0 * max(q, 0.01))
        cos_w0 = math.cos(w0)

        if filter_type == 0:  # LPF
            b0 = (1.0 - cos_w0) / 2.0
            b1 = 1.0 - cos_w0
            b2 = (1.0 - cos_w0) / 2.0
            a0 = 1.0 + alpha
            a1 = -2.0 * cos_w0
            a2 = 1.0 - alpha
        elif filter_type == 1:  # HPF
            b0 = (1.0 + cos_w0) / 2.0
            b1 = -(1.0 + cos_w0)
            b2 = (1.0 + cos_w0) / 2.0
            a0 = 1.0 + alpha
            a1 = -2.0 * cos_w0
            a2 = 1.0 - alpha
        elif filter_type == 2:  # BPF
            b0 = alpha
            b1 = 0.0
            b2 = -alpha
            a0 = 1.0 + alpha
            a1 = -2.0 * cos_w0
            a2 = 1.0 - alpha
        else:  # Notch
            b0 = 1.0
            b1 = -2.0 * cos_w0
            b2 = 1.0
            a0 = 1.0 + alpha
            a1 = -2.0 * cos_w0
            a2 = 1.0 - alpha

        # Normalize
        b0 /= a0; b1 /= a0; b2 /= a0
        a1 /= a0; a2 /= a0

        # Process sample-by-sample (biquad feedback)
        out = np.empty(len(buffer), dtype=np.float32)
        x1, x2, y1, y2 = self._x1, self._x2, self._y1, self._y2

        for i in range(len(buffer)):
            x = float(buffer[i])
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, x
            y2, y1 = y1, y
            out[i] = y

        self._x1, self._x2, self._y1, self._y2 = x1, x2, y1, y2
        return out
