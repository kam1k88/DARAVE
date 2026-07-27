"""
scripts/core/synth/base.py — Base classes for DARAVE mini-synthesizer.

All DSP modules inherit from ModuleBase. SynthGraph orchestrates topological
render. SynthParams is the serializable parameter vector.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OscType(str, Enum):
    SINE = "sine"
    SAW = "saw"
    SQUARE = "square"
    TRIANGLE = "triangle"
    NOISE = "noise"


class FilterType(str, Enum):
    LPF = "lpf"
    HPF = "hpf"
    BPF = "bpf"
    NOTCH = "notch"


# ---------------------------------------------------------------------------
# ModuleBase
# ---------------------------------------------------------------------------

class ModuleBase(ABC):
    """Base class for all DSP modules."""

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__
        self._params: Dict[str, float] = {}

    @abstractmethod
    def process(self, buffer: np.ndarray, sample_rate: int) -> np.ndarray:
        """Process audio buffer (float32). Returns new buffer."""
        ...

    def set_params(self, params: Dict[str, float]) -> None:
        """Update module parameters."""
        self._params.update(params)

    def get_params(self) -> Dict[str, float]:
        return dict(self._params)

    def reset(self) -> None:
        """Reset internal state."""
        pass


# ---------------------------------------------------------------------------
# SynthParams — serializable parameter vector
# ---------------------------------------------------------------------------

@dataclass
class SynthParams:
    """Flat parameter vector for the mini-synthesizer.

    Stored as a float32 numpy array for RL compatibility.
    Enum params stored as integers (indices).
    """
    # Oscillator
    osc_type: int = 0          # OscType index: 0=sine,1=saw,2=square,3=triangle,4=noise
    osc_freq: float = 440.0    # Hz
    osc_amp: float = 0.8       # 0–1

    # Filter
    filter_type: int = 0       # FilterType index: 0=lpf,1=hpf,2=bpf,3=notch
    filter_cutoff: float = 1000.0  # Hz
    filter_resonance: float = 0.7  # Q factor

    # Envelope (ADSR)
    env_a: float = 0.01        # Attack (seconds)
    env_d: float = 0.1         # Decay (seconds)
    env_s: float = 0.7         # Sustain (0–1)
    env_r: float = 0.2         # Release (seconds)

    # LFO
    lfo_rate: float = 1.0      # Hz
    lfo_depth: float = 0.0     # 0–1 modulation depth

    # Compressor
    comp_threshold: float = -20.0  # dB
    comp_ratio: float = 4.0
    comp_attack: float = 0.005     # seconds
    comp_release: float = 0.1      # seconds
    comp_makeup: float = 0.0       # dB

    # Delay
    delay_time: float = 0.0    # seconds (0 = disabled)
    delay_feedback: float = 0.3
    delay_mix: float = 0.0     # 0–1 wet/dry

    def to_vector(self) -> np.ndarray:
        """Serialize to float32 vector."""
        vals = [getattr(self, f.name) for f in fields(self)]
        return np.array(vals, dtype=np.float32)

    @classmethod
    def from_vector(cls, vec: np.ndarray) -> "SynthParams":
        """Deserialize from float32 vector."""
        field_names = [f.name for f in fields(cls)]
        kwargs = {name: float(val) for name, val in zip(field_names, vec)}
        return cls(**kwargs)

    @property
    def vector_size(self) -> int:
        return len(fields(self))

    def to_dict(self) -> Dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ---------------------------------------------------------------------------
# SynthGraph — topological render
# ---------------------------------------------------------------------------

class SynthGraph:
    """Directed graph of DSP modules with topological render order."""

    def __init__(self):
        self.modules: List[ModuleBase] = []
        self._sr: int = 44100

    def add(self, module: ModuleBase) -> "SynthGraph":
        self.modules.append(module)
        return self

    def set_sample_rate(self, sr: int) -> None:
        self._sr = sr

    def render(self, n_samples: int) -> np.ndarray:
        """Render n_samples through the graph in order. Returns float32 mono."""
        buf = np.zeros(n_samples, dtype=np.float32)
        for mod in self.modules:
            buf = mod.process(buf, self._sr)
        return buf

    def reset(self) -> None:
        for mod in self.modules:
            mod.reset()

    def get_all_params(self) -> Dict[str, Dict[str, float]]:
        return {mod.name: mod.get_params() for mod in self.modules}
