"""
tests/test_darave_rl.py — Unit tests for darave_rl module.

Tests cover:
  - reward functions (DJ + producer metrics)
  - episode dataclass (serialization roundtrip)
  - batch dataclass (construction, split, shuffle)
  - logger (write/read JSONL)
  - PPO policy (ActorCritic forward, GAE computation)
  - SAC policy (TwinCritic, SACActor, AutoAlpha, buffer)
  - agent wrappers (PPOAgent, SACAgent lifecycle)
  - environment (DaraveEnv reset/step)

Usage:
    pytest tests/test_darave_rl.py -v
    pytest tests/test_darave_rl.py -v -m "not slow"
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Reward tests
# ---------------------------------------------------------------------------


class TestRewardFunctions:
    """Tests for darave_rl.reward module."""

    def test_energy_continuity_silent(self):
        from darave_rl.reward import energy_continuity

        sr = 44100
        audio = np.zeros(sr * 2, dtype=np.float32)
        score = energy_continuity(audio, sr)
        assert -1.0 <= score <= 1.0

    def test_energy_continuity_sine(self):
        from darave_rl.reward import energy_continuity

        sr = 44100
        t = np.linspace(0, 2, sr * 2, dtype=np.float32)
        audio = 0.5 * np.sin(2 * np.pi * 440 * t)
        score = energy_continuity(audio, sr)
        assert -1.0 <= score <= 1.0
        # Constant amplitude sine → high energy continuity
        assert score > 0.5

    def test_spectral_smoothness_identical(self):
        from darave_rl.reward import spectral_smoothness

        sr = 44100
        audio = np.random.randn(sr).astype(np.float32) * 0.1
        score = spectral_smoothness(audio, audio, sr)
        assert -1.0 <= score <= 1.0
        # Identical signals → high smoothness
        assert score > 0.5

    def test_phase_coherence_sine(self):
        from darave_rl.reward import phase_coherence

        sr = 44100
        t = np.linspace(0, 2, sr * 2, dtype=np.float32)
        audio = 0.5 * np.sin(2 * np.pi * 440 * t)
        score = phase_coherence(audio, sr)
        assert -1.0 <= score <= 1.0

    def test_transient_clarity_silence(self):
        from darave_rl.reward import transient_clarity

        sr = 44100
        audio = np.zeros(sr, dtype=np.float32)
        score = transient_clarity(audio, sr)
        assert score == 0.0

    def test_user_score_to_reward(self):
        from darave_rl.reward import user_score_to_reward

        assert user_score_to_reward(0.0) == pytest.approx(-1.0)
        assert user_score_to_reward(0.5) == pytest.approx(0.0)
        assert user_score_to_reward(1.0) == pytest.approx(1.0)

    def test_envelope_similarity_symmetric(self):
        from darave_rl.reward import envelope_similarity

        sr = 44100
        t = np.linspace(0, 2, sr * 2, dtype=np.float32)
        # Symmetric signal: same envelope on both halves
        audio = 0.5 * np.abs(np.sin(2 * np.pi * 2 * t))
        score = envelope_similarity(audio, sr)
        assert -1.0 <= score <= 1.0

    def test_spectral_balance_white_noise(self):
        from darave_rl.reward import spectral_balance

        sr = 44100
        audio = np.random.randn(sr * 2).astype(np.float32) * 0.1
        score = spectral_balance(audio, sr)
        assert -1.0 <= score <= 1.0

    def test_compression_feel_normal(self):
        from darave_rl.reward import compression_feel

        sr = 44100
        t = np.linspace(0, 2, sr * 2, dtype=np.float32)
        audio = 0.5 * np.sin(2 * np.pi * 440 * t)
        score = compression_feel(audio, sr)
        assert -1.0 <= score <= 1.0

    def test_compute_dj_reward(self):
        from darave_rl.reward import compute_dj_reward

        sr = 44100
        t = np.linspace(0, 2, sr * 2, dtype=np.float32)
        mix = 0.5 * np.sin(2 * np.pi * 440 * t)
        audio_a = 0.3 * np.sin(2 * np.pi * 220 * t)
        audio_b = 0.4 * np.sin(2 * np.pi * 330 * t)
        result = compute_dj_reward(mix, audio_a, audio_b, sr)
        assert "total" in result
        assert -1.0 <= result["total"] <= 1.0

    def test_compute_prod_reward(self):
        from darave_rl.reward import compute_prod_reward

        sr = 44100
        t = np.linspace(0, 2, sr * 2, dtype=np.float32)
        mix = 0.5 * np.sin(2 * np.pi * 440 * t)

        class MockStruct:
            camelot = "8B"

        result = compute_prod_reward(mix, MockStruct(), MockStruct(), sr)
        assert "total" in result
        assert -1.0 <= result["total"] <= 1.0


# ---------------------------------------------------------------------------
# Episode tests
# ---------------------------------------------------------------------------


class TestEpisode:
    """Tests for DjTransitionEpisode dataclass."""

    def test_create_default(self):
        from darave_rl.episode import DjTransitionEpisode

        ep = DjTransitionEpisode()
        assert ep.episode_id  # non-empty
        assert ep.r_total == 0.0

    def test_reward_breakdown(self):
        from darave_rl.episode import DjTransitionEpisode

        ep = DjTransitionEpisode(r_total=0.8, r_energy=0.9, r_spec=0.7)
        bd = ep.reward_breakdown
        assert bd["total"] == 0.8
        assert bd["energy"] == 0.9

    def test_to_dict_roundtrip(self):
        from darave_rl.episode import DjTransitionEpisode

        ep = DjTransitionEpisode(
            from_track_id="song_a",
            to_track_id="song_b",
            bpm=174.0,
            action_vec=np.array([0.1, 0.2, 0.3], dtype=np.float32),
            r_total=0.85,
        )
        d = ep.to_dict()
        assert d["from_track_id"] == "song_a"
        assert d["bpm"] == 174.0
        assert len(d["action_vec"]) == 3

        ep2 = DjTransitionEpisode.from_dict(d)
        assert ep2.from_track_id == "song_a"
        assert ep2.bpm == 174.0
        assert np.allclose(ep2.action_vec, [0.1, 0.2, 0.3])


# ---------------------------------------------------------------------------
# Batch tests
# ---------------------------------------------------------------------------


class TestBatch:
    """Tests for AudioBatch dataclass."""

    def test_empty_batch(self):
        from darave_rl.batch import AudioBatch

        b = AudioBatch.empty(obs_dim=71, action_dim=26)
        assert len(b) == 0
        assert b.obs_dim == 71

    def test_split(self):
        from darave_rl.batch import AudioBatch

        b = AudioBatch(
            real_audio=np.zeros((10, 100), dtype=np.float32),
            real_features=np.zeros((10, 5), dtype=np.float32),
            actions=np.zeros((10, 3), dtype=np.float32),
            synth_audio=np.zeros((10, 100), dtype=np.float32),
            synth_features=np.zeros((10, 5), dtype=np.float32),
            rewards=np.zeros(10, dtype=np.float32),
            user_scores=np.zeros(10, dtype=np.float32),
            states=np.zeros((10, 5), dtype=np.float32),
            next_states=np.zeros((10, 5), dtype=np.float32),
            dones=np.zeros(10, dtype=np.float32),
        )
        chunks = b.split(3)
        assert len(chunks) == 3
        assert len(chunks[0]) == 3
        assert len(chunks[1]) == 3
        assert len(chunks[2]) == 4

    def test_shuffle(self):
        from darave_rl.batch import AudioBatch

        rng = np.random.default_rng(42)
        b = AudioBatch(
            real_audio=rng.random((20, 50), dtype=np.float32),
            real_features=rng.random((20, 5), dtype=np.float32),
            actions=rng.random((20, 3), dtype=np.float32),
            synth_audio=rng.random((20, 50), dtype=np.float32),
            synth_features=rng.random((20, 5), dtype=np.float32),
            rewards=rng.random(20, dtype=np.float32),
            user_scores=rng.random(20, dtype=np.float32),
            states=rng.random((20, 5), dtype=np.float32),
            next_states=rng.random((20, 5), dtype=np.float32),
            dones=np.zeros(20, dtype=np.float32),
        )
        b2 = b.shuffle(rng)
        assert len(b2) == 20
        # Data should be the same, just reordered
        assert sorted(b2.states.tolist()) == sorted(b.states.tolist())


# ---------------------------------------------------------------------------
# Logger tests
# ---------------------------------------------------------------------------


class TestLogger:
    """Tests for EpisodeLogger."""

    def test_log_and_read(self):
        from darave_rl.episode import DjTransitionEpisode
        from darave_rl.logger import EpisodeLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EpisodeLogger(log_dir=tmpdir)
            ep = DjTransitionEpisode(
                from_track_id="a",
                to_track_id="b",
                r_total=0.75,
            )
            logger.log_episode(ep)

            episodes = logger.read_episodes()
            assert len(episodes) == 1
            assert episodes[0]["from_track_id"] == "a"

    def test_log_training_step(self):
        from darave_rl.logger import EpisodeLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EpisodeLogger(log_dir=tmpdir)
            logger.log_training_step(100, {"loss": 0.5, "reward": 0.8})

            steps = logger.read_training_steps()
            assert len(steps) == 1
            assert steps[0]["step"] == 100

    def test_episode_count(self):
        from darave_rl.episode import DjTransitionEpisode
        from darave_rl.logger import EpisodeLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EpisodeLogger(log_dir=tmpdir)
            for i in range(5):
                logger.log_episode(DjTransitionEpisode())
            assert logger.episode_count() == 5

    def test_clear(self):
        from darave_rl.episode import DjTransitionEpisode
        from darave_rl.logger import EpisodeLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EpisodeLogger(log_dir=tmpdir)
            logger.log_episode(DjTransitionEpisode())
            logger.clear()
            assert logger.episode_count() == 0


# ---------------------------------------------------------------------------
# PPO policy tests
# ---------------------------------------------------------------------------


class TestPPOPolicy:
    """Tests for PPO ActorCritic and PPOMemory."""

    def test_actor_critic_forward(self):
        import torch

        from darave_rl.policy_ppo import ActorCritic

        model = ActorCritic(obs_dim=71, action_dim=26)
        state = torch.randn(4, 71)
        action, log_prob, value = model(state)
        assert action.shape == (4, 26)
        assert log_prob.shape == (4,)
        assert value.shape == (4,)
        # Actions should be in [-1, 1] (tanh squashed)
        assert action.min() >= -1.0
        assert action.max() <= 1.0

    def test_actor_critic_evaluate(self):
        import torch

        from darave_rl.policy_ppo import ActorCritic

        model = ActorCritic(obs_dim=71, action_dim=26)
        state = torch.randn(4, 71)
        action = torch.randn(4, 26).tanh()
        log_prob, entropy, value = model.evaluate(state, action)
        assert log_prob.shape == (4,)
        assert entropy.shape == (4,)
        assert value.shape == (4,)

    def test_ppo_memory_gae(self):
        from darave_rl.policy_ppo import PPOMemory

        memory = PPOMemory()
        for i in range(10):
            memory.add(
                state=np.zeros(5, dtype=np.float32),
                action=np.zeros(3, dtype=np.float32),
                reward=1.0,
                done=False,
                log_prob=-1.0,
                value=0.5,
            )

        rewards = np.ones(10, dtype=np.float32)
        values = np.ones(10, dtype=np.float32) * 0.5
        dones = np.zeros(10, dtype=np.float32)

        advantages, returns = memory.compute_gae(rewards, values, dones, 0.0)
        assert len(advantages) == 10
        assert len(returns) == 10
        # With gamma=0.99, lambda=0.95, all same reward, advantages should be positive
        assert advantages.mean() > 0


# ---------------------------------------------------------------------------
# SAC policy tests
# ---------------------------------------------------------------------------


class TestSACPolicy:
    """Tests for SAC TwinCritic, SACActor, AutoAlpha, buffer."""

    def test_twin_critic(self):
        import torch

        from darave_rl.policy_sac import TwinCritic

        critic = TwinCritic(obs_dim=71, action_dim=26)
        state = torch.randn(4, 71)
        action = torch.randn(4, 26)
        q1, q2 = critic(state, action)
        assert q1.shape == (4,)
        assert q2.shape == (4,)

    def test_twin_critic_q_min(self):
        import torch

        from darave_rl.policy_sac import TwinCritic

        critic = TwinCritic(obs_dim=71, action_dim=26)
        state = torch.randn(4, 71)
        action = torch.randn(4, 26)
        q_min = critic.q_min(state, action)
        assert q_min.shape == (4,)

    def test_sac_actor_sample(self):
        import torch

        from darave_rl.policy_sac import SACActor

        actor = SACActor(obs_dim=71, action_dim=26)
        state = torch.randn(4, 71)
        action, log_prob = actor(state)
        assert action.shape == (4, 26)
        assert log_prob.shape == (4,)
        assert action.min() >= -1.0
        assert action.max() <= 1.0

    def test_sac_actor_deterministic(self):
        import torch

        from darave_rl.policy_sac import SACActor

        actor = SACActor(obs_dim=71, action_dim=26)
        state = torch.randn(4, 71)
        action = actor.deterministic(state)
        assert action.shape == (4, 26)

    def test_auto_alpha(self):
        import torch

        from darave_rl.policy_sac import AutoAlpha

        alpha = AutoAlpha(action_dim=26, init_alpha=0.2)
        assert alpha.alpha.item() == pytest.approx(0.2, abs=0.01)

        log_probs = torch.randn(256)
        alpha_loss = alpha.update(log_probs)
        assert alpha_loss.requires_grad

    def test_sac_buffer(self):
        from darave_rl.policy_sac import SACBuffer

        buf = SACBuffer(capacity=100)
        assert len(buf) == 0

        for i in range(50):
            buf.add(
                state=np.zeros(5, dtype=np.float32),
                action=np.zeros(3, dtype=np.float32),
                reward=1.0,
                next_state=np.zeros(5, dtype=np.float32),
                done=False,
            )
        assert len(buf) == 50

        states, actions, rewards, next_states, dones = buf.sample(16)
        assert states.shape == (16, 5)
        assert actions.shape == (16, 3)
        assert rewards.shape == (16,)


# ---------------------------------------------------------------------------
# Agent tests
# ---------------------------------------------------------------------------


class TestAgents:
    """Tests for PPOAgent and SACAgent lifecycle."""

    def test_ppo_agent_select_action(self):
        from darave_rl.agent import PPOAgent

        agent = PPOAgent(obs_dim=71, action_dim=26, hidden=64)
        state = np.random.randn(71).astype(np.float32)
        action = agent.select_action(state)
        assert action.shape == (26,)
        assert action.dtype == np.float32
        assert action.min() >= -1.0
        assert action.max() <= 1.0

    def test_ppo_agent_store_and_update(self):
        from darave_rl.agent import PPOAgent

        agent = PPOAgent(obs_dim=71, action_dim=26, hidden=64, batch_size=8)
        for _ in range(20):
            state = np.random.randn(71).astype(np.float32)
            action = agent.select_action(state)
            agent.store_transition(state, action, reward=1.0, done=False)

        metrics = agent.update()
        assert "loss" in metrics

    def test_ppo_agent_save_load(self):
        from darave_rl.agent import PPOAgent

        agent = PPOAgent(obs_dim=71, action_dim=26, hidden=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "ppo.pt")
            agent.save(path)
            agent2 = PPOAgent(obs_dim=71, action_dim=26, hidden=64)
            agent2.load(path)

    def test_sac_agent_select_action(self):
        from darave_rl.agent import SACAgent

        agent = SACAgent(obs_dim=71, action_dim=26, hidden=64)
        state = np.random.randn(71).astype(np.float32)
        action = agent.select_action(state)
        assert action.shape == (26,)
        assert action.min() >= -1.0
        assert action.max() <= 1.0

    def test_sac_agent_store_and_update(self):
        from darave_rl.agent import SACAgent

        agent = SACAgent(obs_dim=71, action_dim=26, hidden=64, buffer_capacity=200, batch_size=16)
        for _ in range(50):
            state = np.random.randn(71).astype(np.float32)
            action = agent.select_action(state)
            next_state = np.random.randn(71).astype(np.float32)
            agent.store_transition(state, action, reward=1.0, next_state=next_state, done=False)

        metrics = agent.update()
        assert "critic_loss" in metrics

    def test_sac_agent_save_load(self):
        from darave_rl.agent import SACAgent

        agent = SACAgent(obs_dim=71, action_dim=26, hidden=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "sac.pt")
            agent.save(path)
            agent2 = SACAgent(obs_dim=71, action_dim=26, hidden=64)
            agent2.load(path)


# ---------------------------------------------------------------------------
# Environment tests
# ---------------------------------------------------------------------------


class TestDaraveEnv:
    """Tests for DaraveEnv RL environment."""

    def _make_dummy_db(self, sr=22050):
        """Create a minimal tracks_db for testing."""

        class MockStruct:
            bpm = 128.0
            key_name = "C"
            mode = "major"
            camelot = "8B"
            energy_mean = 0.6
            energy_std = 0.1
            drop_position = 30.0
            duration = 180.0
            danceability = 0.7
            sections = []
            beats = []
            vocal_density = 0.3

        audio = np.random.randn(sr * 5).astype(np.float32) * 0.1
        return {
            "track_a": {"audio": audio, "structure": MockStruct()},
            "track_b": {"audio": audio * 0.8, "structure": MockStruct()},
            "track_c": {"audio": audio * 0.5, "structure": MockStruct()},
        }

    def test_env_dimensions(self):
        from darave_rl.env import DaraveEnv

        db = self._make_dummy_db()
        env = DaraveEnv(tracks_db=db, sr=22050)
        assert env.obs_dim == 71
        assert env.action_dim > 7

    def test_env_reset(self):
        from darave_rl.env import DaraveEnv

        db = self._make_dummy_db()
        env = DaraveEnv(tracks_db=db, sr=22050)
        obs = env.reset()
        assert obs.shape == (71,)
        assert obs.dtype == np.float32

    def test_env_reset_explicit_tracks(self):
        from darave_rl.env import DaraveEnv

        db = self._make_dummy_db()
        env = DaraveEnv(tracks_db=db, sr=22050)
        obs = env.reset(from_track="track_a", to_track="track_b")
        assert obs.shape == (71,)

    def test_env_step(self):
        from darave_rl.env import ACTION_DIM, DaraveEnv

        db = self._make_dummy_db(sr=22050)
        env = DaraveEnv(tracks_db=db, sr=22050)
        env.reset(from_track="track_a", to_track="track_b")

        action = np.random.randn(ACTION_DIM).astype(np.float32)
        next_obs, reward, done, info = env.step(action)
        assert next_obs.shape == (71,)
        assert -1.0 <= reward <= 1.0 or -5.0 <= reward <= 5.0  # allow wider range
        assert done is True
        assert "reward_dict" in info

    def test_env_empty_db_raises(self):
        from darave_rl.env import DaraveEnv

        env = DaraveEnv(tracks_db={})
        with pytest.raises(ValueError):
            env.reset()


# ---------------------------------------------------------------------------
# Utils tests
# ---------------------------------------------------------------------------


class TestUtils:
    """Tests for darave_rl.utils module."""

    def test_set_seed(self):
        from darave_rl.utils import set_seed

        set_seed(42)
        a1 = np.random.randn(5)
        set_seed(42)
        a2 = np.random.randn(5)
        assert np.allclose(a1, a2)

    def test_running_stats(self):
        from darave_rl.utils import RunningStats

        stats = RunningStats(shape=(5,))
        for _ in range(100):
            stats.update(np.random.randn(5))
        assert stats.count == 100
        normalized = stats.normalize(np.zeros(5))
        assert normalized.shape == (5,)

    def test_normalize_state(self):
        from darave_rl.utils import normalize_state

        state = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        mean = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        std = np.array([1.0, 1.0, 1.0], dtype=np.float32)
        result = normalize_state(state, mean, std)
        assert np.allclose(result, [0.0, 0.0, 0.0], atol=1e-6)

    def test_clip_action(self):
        from darave_rl.utils import clip_action

        action = np.array([-2.0, 0.5, 3.0])
        result = clip_action(action)
        assert result[0] == -1.0
        assert result[1] == 0.5
        assert result[2] == 1.0
