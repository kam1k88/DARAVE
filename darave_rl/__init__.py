"""
darave_rl — Reinforcement Learning module for DARAVE DJ transitions.

Adds a full RL agent for learning optimal DJ transition parameters
and sound design, built on top of the existing DARAVE DSP pipeline.

Modules
-------
    env         — DaraveEnv: RL environment wrapping the DARAVE DSP chain
    agent       — PPOAgent, SACAgent: high-level agent wrappers
    policy_ppo  — ActorCritic, PPOMemory, GAE, PPO update
    policy_sac  — TwinCritic, SACActor, AutoAlpha, SAC update
    reward      — DJ + producer reward metrics
    episode     — DjTransitionEpisode dataclass
    batch       — AudioBatch data container
    logger      — JSONL episode/training logger
    utils       — seed, device, normalization, network helpers

Quick start
-----------
    from darave_rl import DaraveEnv, PPOAgent, SACAgent

    # Create environment
    env = DaraveEnv(tracks_db=my_tracks, reward_mode="dj")

    # Create agent
    agent = PPOAgent(obs_dim=env.obs_dim, action_dim=env.action_dim)

    # Training loop
    obs = env.reset()
    for _ in range(max_steps):
        action = agent.select_action(obs)
        next_obs, reward, done, info = env.step(action)
        agent.store_transition(obs, action, reward, done)
        obs = next_obs

    metrics = agent.update()
"""

from darave_rl.agent import PPOAgent, SACAgent
from darave_rl.batch import AudioBatch
from darave_rl.env import ACTION_DIM, OBS_DIM, DaraveEnv
from darave_rl.episode import DjTransitionEpisode
from darave_rl.logger import EpisodeLogger
from darave_rl.reward import (
    compression_feel,
    compute_dj_reward,
    compute_prod_reward,
    energy_continuity,
    envelope_similarity,
    harmonic_similarity,
    phase_coherence,
    spectral_balance,
    spectral_smoothness,
    transient_clarity,
    user_score_to_reward,
)
from darave_rl.utils import RunningStats, get_device, set_seed

__all__ = [
    # Environment
    "DaraveEnv",
    "OBS_DIM",
    "ACTION_DIM",
    # Agents
    "PPOAgent",
    "SACAgent",
    # Data
    "DjTransitionEpisode",
    "AudioBatch",
    # Logging
    "EpisodeLogger",
    # Rewards
    "compute_dj_reward",
    "compute_prod_reward",
    "energy_continuity",
    "spectral_smoothness",
    "phase_coherence",
    "transient_clarity",
    "user_score_to_reward",
    "harmonic_similarity",
    "envelope_similarity",
    "spectral_balance",
    "compression_feel",
    # Utils
    "set_seed",
    "get_device",
    "RunningStats",
]
