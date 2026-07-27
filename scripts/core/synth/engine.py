"""
scripts/core/synth/engine.py — High-level SynthEngine: patch loading, render, analysis.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .base import SynthGraph, SynthParams
from .oscillator import Oscillator
from .filters import BiquadFilter
from .envelope import ADSREnvelope
from .lfo import LFO
from .fx import SoftClipper, Compressor, Delay
from .analytics import (
    stft,
    spectral_features,
    phase_correlation,
    detect_transients,
    assess_mix_quality,
    MixQualityHints,
)


class SynthEngine:
    """High-level synthesizer engine for DARAVE.

    Usage:
        engine = SynthEngine()
        audio = engine.render(params, n_samples=44100, sample_rate=44100)
        features = engine.analyze(audio)
    """

    # Default patch: Osc → Filter → Envelope → Delay → Compressor → Clipper → Out
    DEFAULT_PATCH = ["osc", "filter", "env", "delay", "compressor", "clipper"]

    def __init__(self):
        self._graph = SynthGraph()
        self._modules: Dict[str, Any] = {}
        self._build_default_patch()

    def _build_default_patch(self) -> None:
        """Build the default synthesis graph."""
        self._graph = SynthGraph()
        self._modules = {}

        osc = Oscillator("osc")
        filt = BiquadFilter("filter")
        env = ADSREnvelope("env")
        delay = Delay("delay")
        comp = Compressor("compressor")
        clipper = SoftClipper("clipper")

        for mod in [osc, filt, env, delay, comp, clipper]:
            self._graph.add(mod)
            self._modules[mod.name] = mod

    def load_patch(self, patch_config: Dict[str, Any]) -> None:
        """Load a patch configuration.

        patch_config can contain:
            modules: list of module names to enable
            params: dict of parameter overrides
        """
        self._build_default_patch()

        if "modules" in patch_config:
            enabled = set(patch_config["modules"])
            self._graph.modules = [
                m for m in self._graph.modules if m.name in enabled
            ]

        if "params" in patch_config:
            self._apply_params(patch_config["params"])

    def _apply_params(self, params: Dict[str, float]) -> None:
        """Apply parameters to the appropriate modules."""
        MODULE_PARAM_MAP = {
            "osc_type": "osc",
            "osc_freq": "osc",
            "osc_amp": "osc",
            "filter_type": "filter",
            "filter_cutoff": "filter",
            "filter_resonance": "filter",
            "env_a": "env",
            "env_d": "env",
            "env_s": "env",
            "env_r": "env",
            "lfo_rate": "lfo",
            "lfo_depth": "lfo",
            "delay_time": "delay",
            "delay_feedback": "delay",
            "delay_mix": "delay",
            "comp_threshold": "compressor",
            "comp_ratio": "compressor",
            "comp_attack": "compressor",
            "comp_release": "compressor",
            "comp_makeup": "compressor",
            "clip_drive": "clipper",
            "clip_threshold": "clipper",
        }
        for key, val in params.items():
            mod_name = MODULE_PARAM_MAP.get(key)
            if mod_name and mod_name in self._modules:
                self._modules[mod_name].set_params({key: val})

    def render(
        self,
        params: Optional[SynthParams] = None,
        n_samples: int = 44100,
        sample_rate: int = 44100,
    ) -> np.ndarray:
        """Render audio with given parameters.

        Returns float32 mono audio buffer.
        """
        self._graph.set_sample_rate(sample_rate)
        self._graph.reset()

        if params is not None:
            self._apply_params(params.to_dict())

        # Trigger envelope
        if "env" in self._modules:
            self._modules["env"].trigger_on()

        buf = self._graph.render(n_samples)

        # Release envelope at end
        if "env" in self._modules:
            self._modules["env"].trigger_off()

        return buf

    def analyze(self, audio: np.ndarray, sr: int = 44100) -> Dict[str, Any]:
        """Analyze audio buffer and return features.

        Returns dict with:
            spectral_features, transients, mix_quality
        """
        magnitudes, phases, times = stft(audio, sr)
        spec_feats = spectral_features(magnitudes, sr)
        transients = detect_transients(audio, sr)
        quality = assess_mix_quality(audio, sr)

        return {
            "spectral_features": spec_feats,
            "transients": transients,
            "mix_quality": {
                "bass_mud": quality.bass_mud,
                "treble_harsh": quality.treble_harsh,
                "overcompressed": quality.overcompressed,
                "clipping": quality.clipping,
                "clipping_count": quality.clipping_count,
                "dynamic_range_db": quality.dynamic_range_db,
            },
        }

    def compute_reward(
        self,
        features_real: Dict[str, Any],
        features_synth: Dict[str, Any],
        user_reward: float = 0.0,
    ) -> float:
        """Compute RL reward as distance between real and synth features.

        reward = -distance + user_reward + heuristic_bonus
        """
        # Spectral feature distance
        real_spec = features_real.get("spectral_features", {})
        synth_spec = features_synth.get("spectral_features", {})

        distance = 0.0
        n_features = 0
        for key in ["spectral_centroid", "spectral_rolloff", "spectral_slope", "spectral_flatness"]:
            r = real_spec.get(key, 0)
            s = synth_spec.get(key, 0)
            if r != 0:
                distance += abs(r - s) / abs(r)
            else:
                distance += abs(s)
            n_features += 1
        if n_features > 0:
            distance /= n_features

        # Mix quality penalties
        real_quality = features_real.get("mix_quality", {})
        synth_quality = features_synth.get("mix_quality", {})

        penalty = 0.0
        if synth_quality.get("clipping", False):
            penalty += 0.3
        penalty += synth_quality.get("bass_mud", 0) * 0.2
        penalty += synth_quality.get("treble_harsh", 0) * 0.1
        penalty += synth_quality.get("overcompressed", 0) * 0.2

        reward = -distance + user_reward - penalty
        return float(reward)

    @staticmethod
    def compute_reward_batch(
        features_real: List[Dict[str, Any]],
        features_synth: List[Dict[str, Any]],
        user_rewards: Optional[List[float]] = None,
    ) -> np.ndarray:
        """Compute rewards for a batch of (real, synth) pairs."""
        engine = SynthEngine()
        n = min(len(features_real), len(features_synth))
        if user_rewards is None:
            user_rewards = [0.0] * n

        rewards = np.zeros(n, dtype=np.float32)
        for i in range(n):
            rewards[i] = engine.compute_reward(
                features_real[i], features_synth[i], user_rewards[i]
            )
        return rewards
