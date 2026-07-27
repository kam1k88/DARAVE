"""
darave_rl/episode.py — Data class for a single DJ transition episode.

Captures all data from one transition attempt: track metadata, audio,
agent actions, synth parameters, features, and reward breakdown.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

from scripts.core.synth.base import SynthParams


@dataclass
class DjTransitionEpisode:
    """
    One complete DJ transition episode.

    Stores everything needed for training and analysis:
      - Track identification and context
      - Audio segments (before/after transition)
      - Agent's action vector and decoded synth parameters
      - Generated mix audio
      - Feature vectors for real and generated audio
      - Full reward breakdown
    """

    # ── Identification ──────────────────────────────────────────────────
    episode_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    from_track_id: str = ""
    to_track_id: str = ""

    # ── Context ─────────────────────────────────────────────────────────
    bpm: float = 120.0
    energy_from: float = 0.5
    energy_to: float = 0.5
    position_in_set: int = 0

    # ── Audio ───────────────────────────────────────────────────────────
    real_before: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    real_after: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))

    # ── Agent action ────────────────────────────────────────────────────
    action_vec: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    synth_params: SynthParams = field(default_factory=SynthParams)
    audio_mix: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))

    # ── Features ────────────────────────────────────────────────────────
    features_real: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    features_mix: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))

    # ── Rewards ─────────────────────────────────────────────────────────
    r_total: float = 0.0
    r_energy: float = 0.0
    r_spec: float = 0.0
    r_phase: float = 0.0
    r_trans: float = 0.0
    r_user: float = 0.0

    user_score_raw: Optional[float] = None

    @property
    def reward_breakdown(self) -> Dict[str, float]:
        """Return reward components as a dict."""
        return {
            "total": self.r_total,
            "energy": self.r_energy,
            "spectral": self.r_spec,
            "phase": self.r_phase,
            "transient": self.r_trans,
            "user": self.r_user,
        }

    def set_dj_rewards(
        self,
        total: float,
        energy: float,
        spectral: float,
        phase: float,
        transient: float,
        user: float = 0.0,
    ) -> None:
        """Set all DJ reward components at once."""
        self.r_total = total
        self.r_energy = energy
        self.r_spec = spectral
        self.r_phase = phase
        self.r_trans = transient
        self.r_user = user

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict (excludes large arrays)."""
        return {
            "episode_id": self.episode_id,
            "from_track_id": self.from_track_id,
            "to_track_id": self.to_track_id,
            "bpm": self.bpm,
            "energy_from": self.energy_from,
            "energy_to": self.energy_to,
            "position_in_set": self.position_in_set,
            "action_vec": self.action_vec.tolist() if self.action_vec.size > 0 else [],
            "synth_params": self.synth_params.to_dict(),
            "features_real": self.features_real.tolist() if self.features_real.size > 0 else [],
            "features_mix": self.features_mix.tolist() if self.features_mix.size > 0 else [],
            "rewards": self.reward_breakdown,
            "user_score_raw": self.user_score_raw,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DjTransitionEpisode":
        """Deserialize from dict. Audio arrays are left empty."""
        params_dict = d.get("synth_params", {})
        synth_params = SynthParams(
            **{k: v for k, v in params_dict.items() if hasattr(SynthParams, k)}
        )

        return cls(
            episode_id=d.get("episode_id", ""),
            from_track_id=d.get("from_track_id", ""),
            to_track_id=d.get("to_track_id", ""),
            bpm=d.get("bpm", 120.0),
            energy_from=d.get("energy_from", 0.5),
            energy_to=d.get("energy_to", 0.5),
            position_in_set=d.get("position_in_set", 0),
            action_vec=np.array(d.get("action_vec", []), dtype=np.float32),
            synth_params=synth_params,
            features_real=np.array(d.get("features_real", []), dtype=np.float32),
            features_mix=np.array(d.get("features_mix", []), dtype=np.float32),
            r_total=d.get("rewards", {}).get("total", 0.0),
            r_energy=d.get("rewards", {}).get("energy", 0.0),
            r_spec=d.get("rewards", {}).get("spectral", 0.0),
            r_phase=d.get("rewards", {}).get("phase", 0.0),
            r_trans=d.get("rewards", {}).get("transient", 0.0),
            r_user=d.get("rewards", {}).get("user", 0.0),
            user_score_raw=d.get("user_score_raw"),
        )
