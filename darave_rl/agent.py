"""
darave_rl/agent.py — High-level agent wrappers for PPO and SAC.

Provides a unified interface for both algorithms, hiding the internals
of PyTorch optimizers, memory buffers, and network management.

Usage:
    from darave_rl.agent import PPOAgent, SACAgent

    agent = PPOAgent(obs_dim=71, action_dim=26)
    action = agent.select_action(state)
    # ... environment step ...
    metrics = agent.update(batch)
    agent.save("checkpoints/ppo_step1000.pt")
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from darave_rl.batch import AudioBatch
from darave_rl.utils import clip_action, get_device

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base Agent
# ---------------------------------------------------------------------------


class BaseAgent(ABC):
    """Abstract base class for RL agents."""

    @abstractmethod
    def select_action(self, state: np.ndarray, deterministic: bool = False) -> np.ndarray:
        """Select an action given the current observation."""
        ...

    @abstractmethod
    def update(self, batch: AudioBatch) -> dict:
        """Update the policy from a batch of data. Returns training metrics."""
        ...

    @abstractmethod
    def save(self, path: str) -> None:
        """Save model checkpoint."""
        ...

    @abstractmethod
    def load(self, path: str) -> None:
        """Load model checkpoint."""
        ...

    @property
    @abstractmethod
    def action_dim(self) -> int: ...

    @property
    @abstractmethod
    def obs_dim(self) -> int: ...


# ---------------------------------------------------------------------------
# PPO Agent
# ---------------------------------------------------------------------------


class PPOAgent(BaseAgent):
    """
    PPO agent with on-policy rollout collection.

    PPO is simpler to tune and more stable than SAC.  Best for:
    - Environments with moderate dimensionality
    - When you want stable, monotonic improvement
    - Single-step episodes (like DJ transitions)
    """

    def __init__(
        self,
        obs_dim: int,
        action_dim: int,
        hidden: int = 256,
        lr: float = 3e-4,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        clip_epsilon: float = 0.2,
        entropy_coef: float = 0.01,
        n_epochs: int = 4,
        batch_size: int = 64,
    ):
        from darave_rl.policy_ppo import ActorCritic, PPOMemory

        self._obs_dim = obs_dim
        self._action_dim = action_dim
        self._gamma = gamma
        self._gae_lambda = gae_lambda
        self._clip_epsilon = clip_epsilon
        self._entropy_coef = entropy_coef
        self._n_epochs = n_epochs
        self._batch_size = batch_size

        self._device = get_device()
        self._model = ActorCritic(obs_dim, action_dim, hidden).to(self._device)
        self._optimizer = __import__("torch").optim.Adam(self._model.parameters(), lr=lr, eps=1e-5)
        self._memory = PPOMemory()
        self._step_count = 0

    @property
    def obs_dim(self) -> int:
        return self._obs_dim

    @property
    def action_dim(self) -> int:
        return self._action_dim

    @property
    def device(self):
        return self._device

    def select_action(self, state: np.ndarray, deterministic: bool = False) -> np.ndarray:
        """Select action via PPO policy.  Stores transition in memory."""
        import torch

        state_t = torch.tensor(state, dtype=torch.float32, device=self._device).unsqueeze(0)

        self._model.eval()
        with torch.no_grad():
            if deterministic:
                action = self._model.get_value(state_t)  # not used for action
                # Use deterministic mean
                features = self._model.shared(state_t)
                mean = self._model.actor_mean(features)
                action = torch.tanh(mean)
            else:
                action, log_prob, value = self._model(state_t)

        action_np = action.squeeze(0).cpu().numpy().astype(np.float32)
        return clip_action(action_np)

    def store_transition(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        done: bool,
    ) -> None:
        """Store a transition in the PPO memory buffer."""
        import torch

        state_t = torch.tensor(state, dtype=torch.float32, device=self._device).unsqueeze(0)

        self._model.eval()
        with torch.no_grad():
            _, log_prob, value = self._model(state_t)

        self._memory.add(
            state=state,
            action=action,
            reward=reward,
            done=done,
            log_prob=log_prob.item(),
            value=value.item(),
        )

    def update(self, batch: AudioBatch = None) -> dict:
        """
        Run PPO update on collected rollout data.

        If batch is provided, it's used directly.  Otherwise, uses
        the internal memory buffer (from store_transition calls).
        """
        import torch

        from darave_rl.policy_ppo import ppo_update

        if len(self._memory) == 0:
            return {"loss": 0.0, "message": "empty memory"}

        # Compute GAE
        states_t = torch.tensor(
            np.array(self._memory.states), dtype=torch.float32, device=self._device
        )
        with torch.no_grad():
            values = self._model.get_value(states_t).cpu().numpy()
            # Bootstrap value: 0 for terminal states
            next_value = 0.0

        rewards = np.array(self._memory.rewards, dtype=np.float32)
        dones = np.array(self._memory.dones, dtype=np.float32)

        advantages, returns = self._memory.compute_gae(
            rewards,
            values,
            dones,
            next_value,
            gamma=self._gamma,
            lam=self._gae_lambda,
        )
        self._memory._advantages = advantages
        self._memory._returns = returns

        # PPO update
        metrics = ppo_update(
            model=self._model,
            memory=self._memory,
            optimizer=self._optimizer,
            clip_epsilon=self._clip_epsilon,
            entropy_coef=self._entropy_coef,
            n_epochs=self._n_epochs,
            batch_size=self._batch_size,
            device=self._device,
        )

        self._memory.clear()
        self._step_count += 1
        return metrics

    def save(self, path: str) -> None:
        """Save PPO model and optimizer state."""
        import torch

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "model": self._model.state_dict(),
                "optimizer": self._optimizer.state_dict(),
                "step": self._step_count,
                "obs_dim": self._obs_dim,
                "action_dim": self._action_dim,
            },
            path,
        )
        log.info("PPO agent saved to %s", path)

    def load(self, path: str) -> None:
        """Load PPO model and optimizer state."""
        import torch

        checkpoint = torch.load(path, map_location=self._device, weights_only=True)
        self._model.load_state_dict(checkpoint["model"])
        self._optimizer.load_state_dict(checkpoint["optimizer"])
        self._step_count = checkpoint.get("step", 0)
        log.info("PPO agent loaded from %s (step %d)", path, self._step_count)


# ---------------------------------------------------------------------------
# SAC Agent
# ---------------------------------------------------------------------------


class SACAgent(BaseAgent):
    """
    SAC agent with off-policy replay buffer.

    SAC is more sample-efficient than PPO but harder to tune.  Best for:
    - Environments with high-dimensional continuous action spaces
    - When sample efficiency matters (expensive environment steps)
    - When exploration is critical (entropy bonus)
    """

    def __init__(
        self,
        obs_dim: int,
        action_dim: int,
        hidden: int = 256,
        lr: float = 3e-4,
        gamma: float = 0.99,
        tau: float = 0.005,
        alpha_init: float = 0.2,
        auto_alpha: bool = True,
        buffer_capacity: int = 100_000,
        batch_size: int = 256,
    ):
        import torch

        from darave_rl.policy_sac import AutoAlpha, SACActor, SACBuffer, TwinCritic

        self._obs_dim = obs_dim
        self._action_dim = action_dim
        self._gamma = gamma
        self._tau = tau
        self._batch_size = batch_size

        self._device = get_device()
        self._actor = SACActor(obs_dim, action_dim, hidden).to(self._device)
        self._critic = TwinCritic(obs_dim, action_dim, hidden).to(self._device)
        self._alpha_module = AutoAlpha(action_dim, alpha_init).to(self._device)

        self._actor_optimizer = torch.optim.Adam(self._actor.parameters(), lr=lr, eps=1e-5)
        self._critic_optimizer = torch.optim.Adam(self._critic.parameters(), lr=lr, eps=1e-5)
        self._alpha_optimizer = torch.optim.Adam([self._alpha_module.log_alpha], lr=lr, eps=1e-5)

        self._buffer = SACBuffer(buffer_capacity)
        self._step_count = 0
        self._auto_alpha = auto_alpha

    @property
    def obs_dim(self) -> int:
        return self._obs_dim

    @property
    def action_dim(self) -> int:
        return self._action_dim

    @property
    def alpha(self) -> float:
        """Current entropy coefficient."""
        return self._alpha_module.alpha.item()

    @property
    def device(self):
        return self._device

    def select_action(self, state: np.ndarray, deterministic: bool = False) -> np.ndarray:
        """Select action via SAC policy."""
        import torch

        state_t = torch.tensor(state, dtype=torch.float32, device=self._device).unsqueeze(0)

        self._actor.eval()
        with torch.no_grad():
            if deterministic:
                action = self._actor.deterministic(state_t)
            else:
                action, _ = self._actor(state_t)

        return clip_action(action.squeeze(0).cpu().numpy().astype(np.float32))

    def store_transition(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        next_state: np.ndarray,
        done: bool,
    ) -> None:
        """Store a transition in the SAC replay buffer."""
        self._buffer.add(state, action, reward, next_state, done)

    def update(self, batch: AudioBatch = None) -> dict:
        """
        Run one SAC update step.

        Uses the internal replay buffer.  Returns empty dict if buffer
        is too small.
        """
        from darave_rl.policy_sac import sac_update

        if len(self._buffer) < self._batch_size:
            return {}

        metrics = sac_update(
            actor=self._actor,
            critic=self._critic,
            alpha_module=self._alpha_module,
            actor_optimizer=self._actor_optimizer,
            critic_optimizer=self._critic_optimizer,
            alpha_optimizer=self._alpha_optimizer,
            buffer=self._buffer,
            batch_size=self._batch_size,
            gamma=self._gamma,
            tau=self._tau,
            device=self._device,
        )

        self._step_count += 1
        return metrics

    def save(self, path: str) -> None:
        """Save SAC actor, critic, alpha, and optimizer states."""
        import torch

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "actor": self._actor.state_dict(),
                "critic": self._critic.state_dict(),
                "alpha_module": self._alpha_module.state_dict(),
                "actor_optimizer": self._actor_optimizer.state_dict(),
                "critic_optimizer": self._critic_optimizer.state_dict(),
                "alpha_optimizer": self._alpha_optimizer.state_dict(),
                "step": self._step_count,
                "obs_dim": self._obs_dim,
                "action_dim": self._action_dim,
            },
            path,
        )
        log.info("SAC agent saved to %s", path)

    def load(self, path: str) -> None:
        """Load SAC actor, critic, alpha, and optimizer states."""
        import torch

        checkpoint = torch.load(path, map_location=self._device, weights_only=True)
        self._actor.load_state_dict(checkpoint["actor"])
        self._critic.load_state_dict(checkpoint["critic"])
        self._alpha_module.load_state_dict(checkpoint["alpha_module"])
        self._actor_optimizer.load_state_dict(checkpoint["actor_optimizer"])
        self._critic_optimizer.load_state_dict(checkpoint["critic_optimizer"])
        self._alpha_optimizer.load_state_dict(checkpoint["alpha_optimizer"])
        self._step_count = checkpoint.get("step", 0)
        log.info("SAC agent loaded from %s (step %d)", path, self._step_count)
