"""
scripts/core/synth/fx.py — Effects: SoftClipper, Compressor, Delay.
"""

from __future__ import annotations

import numpy as np

from .base import ModuleBase


class SoftClipper(ModuleBase):
    """Tanh-based soft clipper with drive and threshold."""

    def __init__(self, name: str = "clipper"):
        super().__init__(name)
        self._params = {
            "clip_drive": 1.0,
            "clip_threshold": 0.9,
        }

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        drive = self._params.get("clip_drive", 1.0)
        threshold = self._params.get("clip_threshold", 0.9)

        # Soft clip via tanh
        driven = np.tanh(buffer * drive)
        # Scale to threshold
        out = driven * threshold
        return out.astype(np.float32)


class Compressor(ModuleBase):
    """Single-band compressor with attack/release envelope follower."""

    def __init__(self, name: str = "compressor"):
        super().__init__(name)
        self._params = {
            "comp_threshold": -20.0,  # dB
            "comp_ratio": 4.0,
            "comp_attack": 0.005,     # seconds
            "comp_release": 0.1,      # seconds
            "comp_makeup": 0.0,       # dB
        }
        self._envelope = 0.0

    def reset(self) -> None:
        self._envelope = 0.0

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        threshold_db = self._params.get("comp_threshold", -20.0)
        ratio = self._params.get("comp_ratio", 4.0)
        attack = self._params.get("comp_attack", 0.005)
        release = self._params.get("comp_release", 0.1)
        makeup_db = self._params.get("comp_makeup", 0.0)

        attack_coeff = np.exp(-1.0 / (attack * sample_rate)) if attack > 0 else 0.0
        release_coeff = np.exp(-1.0 / (release * sample_rate)) if release > 0 else 0.0

        threshold_lin = 10.0 ** (threshold_db / 20.0)
        makeup_lin = 10.0 ** (makeup_db / 20.0)

        out = np.empty(len(buffer), dtype=np.float32)
        env = self._envelope

        for i in range(len(buffer)):
            x = abs(float(buffer[i]))
            # Envelope follower
            if x > env:
                env = attack_coeff * env + (1.0 - attack_coeff) * x
            else:
                env = release_coeff * env + (1.0 - release_coeff) * x

            # Gain reduction
            if env > threshold_lin:
                gain_db = (1.0 - 1.0 / ratio) * (20.0 * np.log10(max(env, 1e-10)) - threshold_db)
                gain_lin = 10.0 ** (-gain_db / 20.0)
            else:
                gain_lin = 1.0

            out[i] = float(buffer[i]) * gain_lin * makeup_lin

        self._envelope = env
        return out.astype(np.float32)


class Delay(ModuleBase):
    """Simple delay line with feedback and wet/dry mix."""

    def __init__(self, name: str = "delay"):
        super().__init__(name)
        self._params = {
            "delay_time": 0.0,      # seconds (0 = disabled)
            "delay_feedback": 0.3,
            "delay_mix": 0.0,       # 0 = dry, 1 = wet
        }
        self._buffer: np.ndarray = np.zeros(1, dtype=np.float32)
        self._write_idx = 0
        self._max_delay_samples = 44100  # 1 second max

    def reset(self) -> None:
        self._buffer = np.zeros(self._max_delay_samples, dtype=np.float32)
        self._write_idx = 0

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        delay_s = self._params.get("delay_time", 0.0)
        feedback = self._params.get("delay_feedback", 0.3)
        mix = self._params.get("delay_mix", 0.0)

        if delay_s <= 0.0 or mix <= 0.0:
            return buffer.copy()

        delay_samples = int(delay_s * sample_rate)
        delay_samples = max(1, min(delay_samples, self._max_delay_samples - 1))

        # Ensure delay buffer is large enough
        if len(self._buffer) < self._max_delay_samples:
            self._buffer = np.zeros(self._max_delay_samples, dtype=np.float32)

        out = np.empty(len(buffer), dtype=np.float32)

        for i in range(len(buffer)):
            # Read from delay line
            read_idx = (self._write_idx - delay_samples) % self._max_delay_samples
            delayed = self._buffer[read_idx]

            # Write input + feedback to delay line
            x = float(buffer[i])
            self._buffer[self._write_idx] = x + delayed * feedback

            # Mix dry + wet
            out[i] = x * (1.0 - mix) + delayed * mix

            self._write_idx = (self._write_idx + 1) % self._max_delay_samples

        return out.astype(np.float32)
