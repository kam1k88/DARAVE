"""
scripts/core/synth/envelope.py — ADSR envelope generator.
"""

from __future__ import annotations

import numpy as np

from .base import ModuleBase


class ADSREnvelope(ModuleBase):
    """ADSR envelope with trigger_on/trigger_off control.

    Outputs a coefficient buffer [0..1] that can be multiplied with audio.
    """

    def __init__(self, name: str = "env"):
        super().__init__(name)
        self._params = {
            "env_a": 0.01,   # Attack (seconds)
            "env_d": 0.1,    # Decay (seconds)
            "env_s": 0.7,    # Sustain level (0–1)
            "env_r": 0.2,    # Release (seconds)
        }
        self._state = "idle"   # idle | attack | decay | sustain | release
        self._level = 0.0
        self._sample_idx = 0
        self._attack_samples = 0
        self._decay_samples = 0
        self._sustain_level = 0.7
        self._release_samples = 0

    def reset(self) -> None:
        self._state = "idle"
        self._level = 0.0
        self._sample_idx = 0

    def trigger_on(self) -> None:
        self._state = "attack"
        self._sample_idx = 0
        self._sustain_level = self._params.get("env_s", 0.7)

    def trigger_off(self) -> None:
        if self._state != "idle":
            self._state = "release"
            self._sample_idx = 0

    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        a = max(0.001, self._params.get("env_a", 0.01))
        d = max(0.001, self._params.get("env_d", 0.1))
        s = self._params.get("env_s", 0.7)
        r = max(0.001, self._params.get("env_r", 0.2))

        self._attack_samples = int(a * sample_rate)
        self._decay_samples = int(d * sample_rate)
        self._sustain_level = s
        self._release_samples = int(r * sample_rate)

        out = np.empty(len(buffer), dtype=np.float32)

        for i in range(len(buffer)):
            if self._state == "idle":
                val = 0.0
            elif self._state == "attack":
                if self._attack_samples > 0:
                    val = self._sample_idx / self._attack_samples
                else:
                    val = 1.0
                if val >= 1.0:
                    val = 1.0
                    self._state = "decay"
                    self._sample_idx = 0
            elif self._state == "decay":
                if self._decay_samples > 0:
                    val = 1.0 - (1.0 - self._sustain_level) * (
                        self._sample_idx / self._decay_samples
                    )
                else:
                    val = self._sustain_level
                if val <= self._sustain_level:
                    val = self._sustain_level
                    self._state = "sustain"
                    self._sample_idx = 0
            elif self._state == "sustain":
                val = self._sustain_level
            elif self._state == "release":
                if self._release_samples > 0:
                    val = self._sustain_level * (
                        1.0 - self._sample_idx / self._release_samples
                    )
                else:
                    val = 0.0
                if val <= 0.0:
                    val = 0.0
                    self._state = "idle"
            else:
                val = 0.0

            out[i] = max(0.0, min(1.0, val))
            self._sample_idx += 1

        return out
