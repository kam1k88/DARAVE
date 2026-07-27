"""
darave_rl/policy_sac.py — Soft Actor-Critic (SAC).

Components:
  - TwinCritic: twin Q-networks (clipped double-Q)
  - SACActor: Gaussian policy with reparameterization trick
  - AutoAlpha: automatic entropy coefficient tuning
  - SACBuffer: replay buffer for off-policy learning

SAC maximizes a weighted combination of expected return and entropy,
encouraging exploration.  Twin critics reduce overestimation bias.

Usage:
    from darave_rl.policy_sac import TwinCritic, SACActor, AutoAlpha, SACBuffer

    actor = SACActor(obs_dim=71, action_dim=26)
    critic = TwinCritic(obs_dim=71, action_dim=26)
    alpha = AutoAlpha(action_dim=26)
    buffer = SACBuffer(capacity=100000)
"""

from __future__ import annotations

from collections import deque
from typing import Tuple

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.distributions import Normal

    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False


# ---------------------------------------------------------------------------
# Twin Q-Critic
# ---------------------------------------------------------------------------

if _HAS_TORCH:

    class TwinCritic(nn.Module):
        """
        Twin Q-networks for SAC.

        Uses clipped double-Q: min(Q1, Q2) to reduce overestimation.
        Each Q-network takes (state, action) → scalar Q-value.
        """

        def __init__(self, obs_dim: int, action_dim: int, hidden: int = 256):
            super().__init__()
            # Q1
            self.q1 = nn.Sequential(
                nn.Linear(obs_dim + action_dim, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, 1),
            )
            # Q2
            self.q2 = nn.Sequential(
                nn.Linear(obs_dim + action_dim, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, 1),
            )

        def forward(
            self, state: torch.Tensor, action: torch.Tensor
        ) -> Tuple[torch.Tensor, torch.Tensor]:
            """Return (Q1, Q2) values."""
            sa = torch.cat([state, action], dim=-1)
            return self.q1(sa).squeeze(-1), self.q2(sa).squeeze(-1)

        def q_min(self, state: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
            """Return min(Q1, Q2) — the clipped double-Q estimate."""
            q1, q2 = self.forward(state, action)
            return torch.min(q1, q2)


# ---------------------------------------------------------------------------
# SAC Actor
# ---------------------------------------------------------------------------

if _HAS_TORCH:

    class SACActor(nn.Module):
        """
        SAC Gaussian policy with reparameterization trick.

        Outputs squashed actions via tanh.  Log-probability includes
        the tanh squashing correction.
        """

        def __init__(self, obs_dim: int, action_dim: int, hidden: int = 256):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(obs_dim, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
            )
            self.mu = nn.Linear(hidden, action_dim)
            self.log_std = nn.Linear(hidden, action_dim)

        def forward(self, state: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
            """
            Return (sampled_action, log_prob) for training.
            """
            features = self.net(state)
            mu = self.mu(features)
            log_std = self.log_std(features).clamp(-5.0, 2.0)
            std = log_std.exp()

            dist = Normal(mu, std)
            raw_action = dist.rsample()  # reparameterization
            action = torch.tanh(raw_action)

            # Log-probability with tanh correction
            log_prob = dist.log_prob(raw_action) - torch.log(1 - action.pow(2) + 1e-6)
            log_prob = log_prob.sum(dim=-1)
            return action, log_prob

        def deterministic(self, state: torch.Tensor) -> torch.Tensor:
            """Return deterministic action (mean, no sampling)."""
            features = self.net(state)
            mu = self.mu(features)
            return torch.tanh(mu)


# ---------------------------------------------------------------------------
# Auto-Alpha (entropy coefficient)
# ---------------------------------------------------------------------------

if _HAS_TORCH:

    class AutoAlpha(nn.Module):
        """
        Automatic entropy coefficient tuning for SAC.

        Target entropy = -action_dim (common heuristic).
        alpha is learned via gradient descent on:
            loss = -alpha * (log_prob + target_entropy).mean()
        """

        def __init__(self, action_dim: int, init_alpha: float = 0.2):
            super().__init__()
            self.target_entropy = -float(action_dim)
            self.log_alpha = nn.Parameter(torch.tensor(np.log(init_alpha), dtype=torch.float32))

        @property
        def alpha(self) -> torch.Tensor:
            return self.log_alpha.exp()

        def update(self, log_probs: torch.Tensor) -> float:
            """
            Update alpha based on current policy log-probs.

            Returns the new alpha value (for logging).
            """
            loss = -(self.log_alpha * (log_probs + self.target_entropy).detach()).mean()
            return loss


# ---------------------------------------------------------------------------
# Replay Buffer
# ---------------------------------------------------------------------------


class SACBuffer:
    """
    Fixed-size circular replay buffer for SAC.

    Stores (state, action, reward, next_state, done) tuples.
    """

    def __init__(self, capacity: int = 100_000):
        self._capacity = capacity
        self._buffer: deque = deque(maxlen=capacity)
        self._size = 0

    def add(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        next_state: np.ndarray,
        done: bool,
    ) -> None:
        """Store one transition."""
        self._buffer.append((state, action, reward, next_state, done))
        self._size = min(self._size + 1, self._capacity)

    def sample(self, batch_size: int) -> Tuple[np.ndarray, ...]:
        """
        Sample a random batch of transitions.

        Returns (states, actions, rewards, next_states, dones) as numpy arrays.
        """
        indices = np.random.choice(self._size, size=min(batch_size, self._size), replace=False)
        batch = [self._buffer[i] for i in indices]

        states = np.array([t[0] for t in batch], dtype=np.float32)
        actions = np.array([t[1] for t in batch], dtype=np.float32)
        rewards = np.array([t[2] for t in batch], dtype=np.float32)
        next_states = np.array([t[3] for t in batch], dtype=np.float32)
        dones = np.array([t[4] for t in batch], dtype=np.float32)

        return states, actions, rewards, next_states, dones

    def __len__(self) -> int:
        return self._size


# ---------------------------------------------------------------------------
# SAC Update
# ---------------------------------------------------------------------------


def sac_update(
    actor: "SACActor",
    critic: "TwinCritic",
    alpha_module: "AutoAlpha",
    actor_optimizer: "torch.optim.Optimizer",
    critic_optimizer: "torch.optim.Optimizer",
    alpha_optimizer: "torch.optim.Optimizer",
    buffer: SACBuffer,
    batch_size: int = 256,
    gamma: float = 0.99,
    tau: float = 0.005,
    device: torch.device = None,
) -> dict:
    """
    Run one SAC update step.

    Updates critic, actor, and alpha in sequence.
    Returns dict of training metrics.
    """
    if device is None:
        device = next(actor.parameters()).device

    if len(buffer) < batch_size:
        return {}

    states, actions, rewards, next_states, dones = buffer.sample(batch_size)

    states_t = torch.tensor(states, dtype=torch.float32, device=device)
    actions_t = torch.tensor(actions, dtype=torch.float32, device=device)
    rewards_t = torch.tensor(rewards, dtype=torch.float32, device=device)
    next_states_t = torch.tensor(next_states, dtype=torch.float32, device=device)
    dones_t = torch.tensor(dones, dtype=torch.float32, device=device)

    # ── Critic update ──
    with torch.no_grad():
        next_actions, next_log_probs = actor(next_states_t)
        q1_next, q2_next = critic(next_states_t, next_actions)
        q_next = torch.min(q1_next, q2_next)
        q_target = rewards_t + gamma * (1.0 - dones_t) * (
            q_next - alpha_module.alpha * next_log_probs
        )

    q1, q2 = critic(states_t, actions_t)
    critic_loss = F.mse_loss(q1, q_target) + F.mse_loss(q2, q_target)

    critic_optimizer.zero_grad()
    critic_loss.backward()
    nn.utils.clip_grad_norm_(critic.parameters(), 1.0)
    critic_optimizer.step()

    # ── Actor update ──
    new_actions, log_probs = actor(states_t)
    q1_new, q2_new = critic(states_t, new_actions)
    q_new = torch.min(q1_new, q2_new)
    actor_loss = (alpha_module.alpha * log_probs - q_new).mean()

    actor_optimizer.zero_grad()
    actor_loss.backward()
    nn.utils.clip_grad_norm_(actor.parameters(), 1.0)
    actor_optimizer.step()

    # ── Alpha update ──
    alpha_loss = alpha_module.update(log_probs)
    alpha_optimizer.zero_grad()
    alpha_loss.backward()
    alpha_optimizer.step()

    # ── Soft update target (using critic as target for itself) ──
    # SAC uses online critics only — soft update is between main and
    # a separate target critic, but for simplicity we skip target networks
    # here (the twin critics already provide stable estimates).

    return {
        "critic_loss": critic_loss.item(),
        "actor_loss": actor_loss.item(),
        "alpha": alpha_module.alpha.item(),
        "alpha_loss": alpha_loss.item(),
        "log_prob": log_probs.mean().item(),
    }
