"""
scripts/core/synth/ — DARAVE mini-synthesizer.

Physical reference model for RL-based pattern learning.

Modules:
    base        — ModuleBase, SynthGraph, SynthParams
    oscillator  — sine, saw, square, triangle, noise
    filters     — biquad LPF/HPF/BPF/Notch
    envelope    — ADSR
    lfo         — sine/triangle modulation
    fx          — soft clipper, compressor, delay
    analytics   — STFT, spectral features, transients, mix quality
    engine      — SynthEngine (high-level API)
"""

from .base import ModuleBase, SynthGraph, SynthParams, OscType, FilterType
from .oscillator import Oscillator
from .filters import BiquadFilter
from .envelope import ADSREnvelope
from .lfo import LFO
from .fx import SoftClipper, Compressor, Delay
from .analytics import stft, spectral_features, phase_correlation, detect_transients, assess_mix_quality
from .engine import SynthEngine

__all__ = [
    "ModuleBase", "SynthGraph", "SynthParams", "OscType", "FilterType",
    "Oscillator", "BiquadFilter", "ADSREnvelope", "LFO",
    "SoftClipper", "Compressor", "Delay",
    "stft", "spectral_features", "phase_correlation", "detect_transients", "assess_mix_quality",
    "SynthEngine",
]
