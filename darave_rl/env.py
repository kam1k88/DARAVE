"""
darave_rl/env.py — RL environment for DJ transitions and sound design.

DaraveEnv wraps the existing DARAVE DSP pipeline into a step-based
RL interface: reset → observe → act → step → reward.

The agent controls synthesizer parameters and transition settings.
The environment renders the transition, evaluates quality, and returns
a scalar reward.

Observation space (71-dim vector):
    track_a features (35-dim) + track_b features (35-dim) + position_in_set (1-dim)

Action space (continuous, 7 + SynthParams.vector_size dims):
    crossfade_curve, eq_hp_start, eq_hp_end, bass_swap_bar,
    effect_type, effect_depth, bridge_gain, synth_params...

Usage:
    from darave_rl.env import DaraveEnv

    env = DaraveEnv(synth_engine, tracks_db)
    obs = env.reset(from_track="song_a.wav", to_track="song_b.wav")
    for _ in range(max_steps):
        action = agent.select_action(obs)
        next_obs, reward, done, info = env.step(action)
        agent.store_transition(obs, action, reward, done)
        obs = next_obs
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np

from darave_rl.reward import compute_dj_reward, compute_prod_reward
from scripts.core.synth.analytics import spectral_features, stft
from scripts.core.synth.base import SynthParams
from scripts.core.synth.engine import SynthEngine

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OBS_DIM = 71  # 35 (track_a) + 35 (track_b) + 1 (position)
SYNTH_VECTOR_SIZE = len(SynthParams().to_vector())
ACTION_DIM = 7 + SYNTH_VECTOR_SIZE  # transition params + synth params

# Effect types matching dj_effects.EFFECTS keys
EFFECT_TYPES = [
    "none",
    "filter",
    "echo",
    "loop",
    "wobble",
    "slicer",
    "flanger",
    "phaser",
    "vinyl_stop",
    "bitcrush",
    "reverb",
]

# Track feature layout (35-dim from music_index)
TRACK_FEATURE_DIM = 35


# ---------------------------------------------------------------------------
# Track feature extraction
# ---------------------------------------------------------------------------


def _extract_track_features(struct: object, audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Extract a 35-dim feature vector from a SongStructure + audio.

    Mirrors the vector layout from scripts/core/music_index.py:
      dim  0        bpm_norm
      dims 1–12     key_onehot[12]
      dim  13       mode
      dim  14       energy_mean
      dim  15       energy_std
      dim  16       drop_norm
      dim  17       danceability
      dim  18       beat_strength
      dim  19       tempo_stability
      dim  20       vocal_density
      dim  21       centroid_norm
      dim  22       rolloff_norm
      dims 23–34    chroma[12]
    """
    vec = np.zeros(TRACK_FEATURE_DIM, dtype=np.float32)

    # BPM
    bpm = getattr(struct, "bpm", 120.0)
    vec[0] = min(bpm / 200.0, 1.0)

    # Key one-hot (12 dims)
    key_name = getattr(struct, "key_name", "")
    NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    if key_name in NOTE_NAMES:
        vec[1 + NOTE_NAMES.index(key_name)] = 1.0

    # Mode
    vec[13] = 1.0 if getattr(struct, "mode", "") == "major" else 0.0

    # Energy
    vec[14] = getattr(struct, "energy_mean", 0.5)
    vec[15] = getattr(struct, "energy_std", 0.1)

    # Drop position
    drop_pos = getattr(struct, "drop_position", None)
    duration = getattr(struct, "duration", 1.0)
    vec[16] = (drop_pos / max(duration, 1.0)) if drop_pos is not None else 0.6

    # Danceability
    vec[17] = getattr(struct, "danceability", 0.5)

    # Beat strength (from sections energy)
    sections = getattr(struct, "sections", [])
    if sections:
        energies = [getattr(s, "avg_energy", 0.5) for s in sections]
        vec[18] = float(np.mean(energies)) if energies else 0.5
    else:
        vec[18] = 0.5

    # Tempo stability (inverse of beat variance)
    beats = getattr(struct, "beats", [])
    if len(beats) > 2:
        intervals = np.diff([b.time for b in beats])
        vec[19] = max(0.0, 1.0 - float(np.std(intervals) / max(np.mean(intervals), 1e-6)))
    else:
        vec[19] = 0.5

    # Vocal density
    vec[20] = getattr(struct, "vocal_density", 0.5)

    # Spectral features
    if len(audio) > 2048:
        mag, _, _ = stft(audio[:8192], sr, hop_length=1024, n_fft=2048)
        if mag.shape[0] > 0:
            feats = spectral_features(mag, sr, 2048)
            vec[21] = min(feats.get("spectral_centroid", 0.0) / 8000.0, 1.0)
            vec[22] = min(feats.get("spectral_rolloff", 0.0) / 16000.0, 1.0)

    # Chroma (12 dims) — simplified from audio
    if len(audio) > 4096:
        chroma = _simple_chroma(audio[:8192], sr)
        vec[23:35] = chroma

    return vec


def _simple_chroma(audio: np.ndarray, sr: int, n_fft: int = 4096) -> np.ndarray:
    """Compute a simple 12-dim chroma vector from audio."""
    if len(audio) < n_fft:
        return np.zeros(12, dtype=np.float32)

    window = np.hanning(n_fft).astype(np.float32)
    frame = audio[:n_fft] * window
    spectrum = np.abs(np.fft.rfft(frame))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)

    chroma = np.zeros(12, dtype=np.float64)
    for i, f in enumerate(freqs):
        if f < 20 or f > sr / 2:
            continue
        # Map frequency to pitch class (A4=440Hz)
        pitch = 12 * np.log2(f / 440.0) + 69
        chroma_idx = int(round(pitch)) % 12
        chroma[chroma_idx] += spectrum[i] ** 2

    total = chroma.sum()
    if total > 0:
        chroma = chroma / np.sqrt(total)  # L2 normalize
    return chroma.astype(np.float32)


# ---------------------------------------------------------------------------
# DaraveEnv
# ---------------------------------------------------------------------------


class DaraveEnv:
    """
    RL environment for DJ transitions.

    Single-step episode: reset → step → done.

    The agent outputs a continuous action vector controlling:
      - Transition parameters (crossfade, EQ, effects)
      - Synthesizer parameters (oscillator, filter, envelope, etc.)

    The environment renders the transition using the existing DARAVE
    DSP pipeline, evaluates quality, and returns a scalar reward.
    """

    def __init__(
        self,
        synth_engine: Optional[SynthEngine] = None,
        tracks_db: Optional[Dict[str, dict]] = None,
        sr: int = 44100,
        transition_bars: int = 16,
        reward_mode: str = "dj",  # "dj" | "prod" | "combined"
    ):
        """
        Parameters
        ----------
        synth_engine : SynthEngine, optional
            The DARAVE synth engine for bridge beat synthesis.
            If None, a default one is created.
        tracks_db : dict, optional
            Map of track_id → {"audio": np.ndarray, "structure": SongStructure}.
            Used for track pair sampling.
        sr : int
            Sample rate.
        transition_bars : int
            Number of bars for the transition overlap.
        reward_mode : str
            "dj" for DJ metrics, "prod" for producer metrics,
            "combined" for weighted average of both.
        """
        self._synth = synth_engine or SynthEngine()
        self._tracks_db = tracks_db or {}
        self._sr = sr
        self._transition_bars = transition_bars
        self._reward_mode = reward_mode

        self._current_from: Optional[str] = None
        self._current_to: Optional[str] = None
        self._position_in_set: int = 0
        self._step_count: int = 0
        self._max_steps: int = 1  # single-step episode

    @property
    def obs_dim(self) -> int:
        return OBS_DIM

    @property
    def action_dim(self) -> int:
        return ACTION_DIM

    @property
    def sr(self) -> int:
        return self._sr

    def reset(
        self,
        from_track: Optional[str] = None,
        to_track: Optional[str] = None,
        position_in_set: int = 0,
    ) -> np.ndarray:
        """
        Reset environment for a new transition.

        If from_track/to_track are None, randomly samples from the database.
        Returns the initial observation.
        """
        self._step_count = 0
        self._position_in_set = position_in_set

        track_ids = list(self._tracks_db.keys())
        if len(track_ids) < 2:
            raise ValueError("tracks_db must contain at least 2 tracks")

        if from_track is None or to_track is None:
            rng = np.random.default_rng()
            pair = rng.choice(track_ids, size=2, replace=False)
            self._current_from = pair[0]
            self._current_to = pair[1]
        else:
            self._current_from = from_track
            self._current_to = to_track

        return self._get_obs()

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, float, bool, dict]:
        """
        Execute one transition with the given action.

        Returns (next_obs, reward, done, info).
        """
        self._step_count += 1

        # Decode action vector
        params = self._decode_action(action)

        # Get track data
        from_data = self._tracks_db.get(self._current_from, {})
        to_data = self._tracks_db.get(self._current_to, {})
        audio_a = from_data.get("audio", np.zeros(0, dtype=np.float32))
        audio_b = to_data.get("audio", np.zeros(0, dtype=np.float32))
        struct_a = from_data.get("structure", None)
        struct_b = to_data.get("structure", None)

        # Render transition
        mix = self._render_transition(audio_a, audio_b, params)

        # Compute features for the mix
        mix_features = self._extract_mix_features(mix)

        # Compute reward
        reward_dict = self._compute_reward(mix, audio_a, audio_b, struct_a, struct_b)
        reward = reward_dict["total"]

        # Build info
        info = {
            "params": params,
            "reward_dict": reward_dict,
            "from_track": self._current_from,
            "to_track": self._current_to,
            "mix_length": len(mix),
            "position_in_set": self._position_in_set,
        }

        done = True  # single-step episode
        next_obs = self._get_obs_with_mix(mix_features)
        return next_obs, reward, done, info

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _get_obs(self) -> np.ndarray:
        """Build the observation vector from current track pair."""
        from_data = self._tracks_db.get(self._current_from, {})
        to_data = self._tracks_db.get(self._current_to, {})
        audio_a = from_data.get("audio", np.zeros(0, dtype=np.float32))
        audio_b = to_data.get("audio", np.zeros(0, dtype=np.float32))
        struct_a = from_data.get("structure", None)
        struct_b = to_data.get("structure", None)

        feat_a = (
            _extract_track_features(struct_a, audio_a, self._sr)
            if struct_a is not None
            else np.zeros(TRACK_FEATURE_DIM, dtype=np.float32)
        )
        feat_b = (
            _extract_track_features(struct_b, audio_b, self._sr)
            if struct_b is not None
            else np.zeros(TRACK_FEATURE_DIM, dtype=np.float32)
        )

        position = np.array([self._position_in_set / 100.0], dtype=np.float32)

        obs = np.concatenate([feat_a, feat_b, position]).astype(np.float32)
        return obs

    def _get_obs_with_mix(self, mix_features: np.ndarray) -> np.ndarray:
        """Build observation using mix features as the 'next' state."""
        from_data = self._tracks_db.get(self._current_from, {})
        to_data = self._tracks_db.get(self._current_to, {})
        audio_a = from_data.get("audio", np.zeros(0, dtype=np.float32))
        audio_b = to_data.get("audio", np.zeros(0, dtype=np.float32))
        struct_a = from_data.get("structure", None)
        struct_b = to_data.get("structure", None)

        feat_a = (
            _extract_track_features(struct_a, audio_a, self._sr)
            if struct_a is not None
            else np.zeros(TRACK_FEATURE_DIM, dtype=np.float32)
        )

        position = np.array([self._position_in_set / 100.0], dtype=np.float32)
        obs = np.concatenate([feat_a, mix_features[:TRACK_FEATURE_DIM], position]).astype(
            np.float32
        )
        return obs

    def _decode_action(self, action: np.ndarray) -> dict:
        """
        Decode a continuous action vector into transition + synth parameters.

        Action layout (first 7 are transition params, rest are synth):
          [0] crossfade_curve:  0=linear, 0.33=exponential, 0.66=equal_power
          [1] eq_hp_start:     normalized [0,1] → 80..800 Hz
          [2] eq_hp_end:       normalized [0,1] → 20..400 Hz
          [3] bass_swap_bar:   normalized [0,1] → bar 0..transition_bars
          [4] effect_type:     normalized [0,1] → index in EFFECT_TYPES
          [5] effect_depth:    [0,1]
          [6] bridge_gain:     [0,1]
          [7:] synth_params:   SynthParams vector
        """
        a = np.clip(action, -1.0, 1.0)

        # Rescale from [-1, 1] to [0, 1]
        t = (a[:7] + 1.0) / 2.0

        # Transition params
        effect_idx = int(t[4] * (len(EFFECT_TYPES) - 1) + 0.5)
        effect_idx = max(0, min(effect_idx, len(EFFECT_TYPES) - 1))

        params = {
            "crossfade_curve": float(t[0]),
            "eq_hp_start_hz": 80.0 + t[1] * 720.0,
            "eq_hp_end_hz": 20.0 + t[2] * 380.0,
            "bass_swap_bar": int(t[3] * self._transition_bars),
            "effect_type": EFFECT_TYPES[effect_idx],
            "effect_depth": float(t[5]),
            "bridge_gain": float(t[6]),
        }

        # Synth params
        synth_vec = np.clip(a[7:], -1.0, 1.0)
        # Rescale synth vector: [-1,1] → SynthParams ranges
        synth_vec[0] = int(abs(synth_vec[0]) * 4)  # osc_type: 0-4
        synth_vec[1] = (synth_vec[1] + 1) / 2 * 2000  # osc_freq: 0-2000 Hz
        synth_vec[2] = (synth_vec[2] + 1) / 2  # osc_amp: 0-1
        synth_vec[3] = int(abs(synth_vec[3]) * 3)  # filter_type: 0-3
        synth_vec[4] = 20 + (synth_vec[4] + 1) / 2 * 19980  # filter_cutoff: 20-20000
        synth_vec[5] = 0.1 + (synth_vec[5] + 1) / 2 * 9.9  # filter_resonance: 0.1-10

        try:
            synth_params = SynthParams.from_vector(synth_vec.astype(np.float32))
        except Exception:
            synth_params = SynthParams()

        params["synth_params"] = synth_params
        return params

    def _render_transition(
        self,
        audio_a: np.ndarray,
        audio_b: np.ndarray,
        params: dict,
    ) -> np.ndarray:
        """
        Render the transition using DARAVE DSP pipeline.

        This is a simplified version of DJEngine.render_stem_blend()
        focused on the RL-specific parameters.
        """
        if len(audio_a) == 0 and len(audio_b) == 0:
            return np.zeros(self._sr * 4, dtype=np.float32)  # 4 seconds of silence

        sr = self._sr
        trans_samples = int(self._transition_bars * 4 * 60.0 / 120.0 * sr)  # rough estimate

        # Ensure minimum length
        trans_samples = max(trans_samples, sr)

        # Build crossfade envelope based on curve type
        curve = params.get("crossfade_curve", 0.5)
        t = np.linspace(0, 1, trans_samples, dtype=np.float32)

        if curve < 0.33:
            # Linear
            fade_out = 1.0 - t
            fade_in = t
        elif curve < 0.66:
            # Exponential
            fade_out = np.exp(-3.0 * t)
            fade_in = 1.0 - np.exp(-3.0 * t)
        else:
            # Equal power
            fade_out = np.cos(t * np.pi / 2)
            fade_in = np.sin(t * np.pi / 2)

        # Apply HP sweep to incoming track
        hp_start = params.get("eq_hp_start_hz", 400.0)
        hp_end = params.get("eq_hp_end_hz", 80.0)

        # Simple HP approximation: attenuate low frequencies progressively
        hp_factor = np.linspace(hp_start / 1000.0, hp_end / 1000.0, trans_samples, dtype=np.float32)
        hp_factor = np.clip(hp_factor, 0.0, 1.0)
        fade_in_hp = fade_in * hp_factor

        # Extract transition segments
        a_seg = _pad_or_trim(
            audio_a[-trans_samples:] if len(audio_a) > trans_samples else audio_a, trans_samples
        )
        b_seg = _pad_or_trim(
            audio_b[:trans_samples] if len(audio_b) > trans_samples else audio_b, trans_samples
        )

        # Mix
        mix = a_seg * fade_out + b_seg * fade_in_hp

        # Bridge beat
        bridge_gain = params.get("bridge_gain", 0.0)
        if bridge_gain > 0.01:
            try:
                from scripts.core.beat_synth import render_beat

                bridge_beat = render_beat(bpm=120.0, genre="techno", bars=4, sr=sr)
                reps = int(np.ceil(trans_samples / max(len(bridge_beat), 1)))
                tiled = np.tile(bridge_beat, reps)[:trans_samples]
                bell = np.sin(np.pi * np.linspace(0, 1, trans_samples, dtype=np.float32))
                mix += tiled * bell * bridge_gain
            except Exception:
                pass

        # Normalize
        peak = float(np.abs(mix).max())
        if peak > 1.0:
            mix = mix / peak

        return mix.astype(np.float32)

    def _extract_mix_features(self, mix: np.ndarray) -> np.ndarray:
        """Extract a 35-dim feature vector from the generated mix."""
        if len(mix) < 2048:
            return np.zeros(TRACK_FEATURE_DIM, dtype=np.float32)

        # Use the same extraction as track features, but with a dummy struct
        class _DummyStruct:
            bpm = 120.0
            key_name = ""
            mode = ""
            energy_mean = 0.5
            energy_std = 0.1
            drop_position = None
            duration = max(len(mix) / self._sr, 1.0)
            danceability = 0.5
            sections = []
            beats = []
            vocal_density = 0.5

        return _extract_track_features(_DummyStruct(), mix, self._sr)

    def _compute_reward(
        self,
        mix: np.ndarray,
        audio_a: np.ndarray,
        audio_b: np.ndarray,
        struct_a: object,
        struct_b: object,
    ) -> dict:
        """Compute reward based on the configured mode."""
        if self._reward_mode == "dj":
            return compute_dj_reward(mix, audio_a, audio_b, self._sr)
        elif self._reward_mode == "prod":
            return compute_prod_reward(mix, struct_a, struct_b, self._sr)
        else:
            # Combined: average of DJ and producer rewards
            dj = compute_dj_reward(mix, audio_a, audio_b, self._sr)
            prod = compute_prod_reward(mix, struct_a, struct_b, self._sr)
            return {
                "total": (dj["total"] + prod["total"]) / 2.0,
                "dj": dj,
                "prod": prod,
                "energy": dj.get("energy", 0.0),
                "spectral": dj.get("spectral", 0.0),
                "phase": dj.get("phase", 0.0),
                "transient": dj.get("transient", 0.0),
                "harmonic": prod.get("harmonic", 0.0),
                "envelope": prod.get("envelope", 0.0),
                "balance": prod.get("balance", 0.0),
                "compression": prod.get("compression", 0.0),
            }


def _pad_or_trim(audio: np.ndarray, target: int) -> np.ndarray:
    """Pad or trim audio to target length."""
    if len(audio) == target:
        return audio
    if len(audio) > target:
        return audio[:target]
    return np.concatenate([audio, np.zeros(target - len(audio), dtype=audio.dtype)])
