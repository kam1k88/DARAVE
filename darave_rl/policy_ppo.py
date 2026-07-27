"""
darave_rl/policy_ppo.py — Proximal Policy Optimization (PPO).

Components:
  - ActorCritic: shared backbone + Gaussian actor + value critic
  - PPOMemory: rollout buffer with GAE computation
  - GAE: Generalized Advantage Estimation

PPO uses a clipped surrogate objective to prevent destructive policy updates.
The actor outputs a Gaussian distribution over continuous actions.

Usage:
    from darave_rl.policy_ppo import ActorCritic, PPOMemory

    model = ActorCritic(obs_dim=71, action_dim=26)
    memory = PPOMemory()
    # ... collect rollouts ...
    memory.compute_gae(rewards, values, dones, gamma=0.99, lam=0.95)
    for batch in memory.get_batches(batch_size=64):
        loss = ppo_update(model, batch)
"""

from __future__ import annotations

from typing import Iterator, Tuple

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.distributions import Normal

    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

from darave_rl.batch import AudioBatch

# ---------------------------------------------------------------------------
# Actor-Critic Network
# ---------------------------------------------------------------------------

if _HAS_TORCH:

    class ActorCritic(nn.Module):
        """
        Shared-backbone Actor-Critic for PPO.

        Architecture:
            shared: obs_dim → hidden → hidden
            actor:  hidden → action_dim (mean) + action_dim (log_std)
            critic: hidden → 1 (value)
        """

        def __init__(self, obs_dim: int, action_dim: int, hidden: int = 256):
            super().__init__()
            self.shared = nn.Sequential(
                nn.Linear(obs_dim, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.LayerNorm(hidden),
                nn.ReLU(),
            )
            # Actor: Gaussian policy
            self.actor_mean = nn.Linear(hidden, action_dim)
            self.actor_log_std = nn.Parameter(torch.zeros(action_dim, dtype=torch.float32))
            # Critic: value head
            self.critic = nn.Linear(hidden, 1)

        def forward(self, state: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
            """
            Forward pass.

            Returns (action, log_prob, value).
            Action is sampled from the Gaussian distribution.
            """
            features = self.shared(state)
            mean = self.actor_mean(features)
            log_std = self.actor_log_std.clamp(-5.0, 2.0)
            std = log_std.exp()

            dist = Normal(mean, std)
            raw_action = dist.rsample()  # reparameterization trick
            action = torch.tanh(raw_action)  # squash to [-1, 1]

            # Log-probability with tanh squashing correction
            log_prob = dist.log_prob(raw_action) - torch.log(1 - action.pow(2) + 1e-6)
            log_prob = log_prob.sum(dim=-1)

            value = self.critic(features).squeeze(-1)
            return action, log_prob, value

        def evaluate(
            self, state: torch.Tensor, action: torch.Tensor
        ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
            """
            Evaluate a given action (for PPO update).

            Returns (new_log_prob, entropy, value).
            """
            features = self.shared(state)
            mean = self.actor_mean(features)
            log_std = self.actor_log_std.clamp(-5.0, 2.0)
            std = log_std.exp()

            dist = Normal(mean, std)

            # Inverse tanh to get raw action
            raw_action = torch.atanh(action.clamp(-0.999, 0.999))
            log_prob = dist.log_prob(raw_action) - torch.log(1 - action.pow(2) + 1e-6)
            log_prob = log_prob.sum(dim=-1)

            entropy = dist.entropy().sum(dim=-1)
            value = self.critic(features).squeeze(-1)
            return log_prob, entropy, value

        def get_value(self, state: torch.Tensor) -> torch.Tensor:
            """Get value estimate only (for bootstrap)."""
            features = self.shared(state)
            return self.critic(features).squeeze(-1)


# ---------------------------------------------------------------------------
# PPO Memory (Rollout Buffer)
# ---------------------------------------------------------------------------


class PPOMemory:
    """
    Rollout buffer for PPO with GAE computation.

    Stores (state, action, reward, done, log_prob, value) per timestep.
    After a rollout, computes GAE advantages and discounted returns.
    """

    def __init__(self):
        self.states: list = []
        self.actions: list = []
        self.rewards: list = []
        self.dones: list = []
        self.log_probs: list = []
        self.values: list = []

    def clear(self) -> None:
        """Clear all stored data."""
        self.states.clear()
        self.actions.clear()
        self.rewards.clear()
        self.dones.clear()
        self.log_probs.clear()
        self.values.clear()

    def add(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        done: bool,
        log_prob: float,
        value: float,
    ) -> None:
        """Store one timestep."""
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.dones.append(done)
        self.log_probs.append(log_prob)
        self.values.append(value)

    def __len__(self) -> int:
        return len(self.states)

    def compute_gae(
        self,
        rewards: np.ndarray,
        values: np.ndarray,
        dones: np.ndarray,
        next_value: float,
        gamma: float = 0.99,
        lam: float = 0.95,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute Generalized Advantage Estimation.

        Parameters
        ----------
        rewards : array [T]
        values : array [T+1] (includes bootstrap value at end)
        dones : array [T]
        next_value : float — bootstrap value for last state
        gamma : float — discount factor
        lam : float — GAE lambda

        Returns
        -------
        advantages : array [T]
        returns : array [T]
        """
        T = len(rewards)
        advantages = np.zeros(T, dtype=np.float32)
        returns = np.zeros(T, dtype=np.float32)

        gae = 0.0
        for t in reversed(range(T)):
            if t == T - 1:
                next_val = next_value
            else:
                next_val = values[t + 1]

            mask = 1.0 - dones[t]
            delta = rewards[t] + gamma * next_val * mask - values[t]
            gae = delta + gamma * lam * mask * gae
            advantages[t] = gae
            returns[t] = gae + values[t]

        return advantages, returns

    def get_batches(self, batch_size: int) -> Iterator[AudioBatch]:
        """
        Yield shuffled mini-batches of size batch_size.

        Returns AudioBatch objects with all necessary fields for PPO update.
        """
        n = len(self)
        if n == 0:
            return

        states = np.array(self.states, dtype=np.float32)
        actions = np.array(self.actions, dtype=np.float32)

        # Ensure advantages/returns are computed
        if not hasattr(self, "_advantages") or self._advantages is None:
            raise RuntimeError("Call compute_gae() before get_batches()")

        advantages = self._advantages
        returns = self._returns

        indices = np.random.permutation(n)
        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            idx = indices[start:end]

            yield AudioBatch(
                real_audio=np.zeros((len(idx), 0), dtype=np.float32),
                real_features=states[idx],
                actions=actions[idx],
                synth_audio=np.zeros((len(idx), 0), dtype=np.float32),
                synth_features=np.zeros((len(idx), 0), dtype=np.float32),
                rewards=returns[idx],
                user_scores=advantages[idx],
                states=states[idx],
                next_states=states[idx],  # placeholder
                dones=np.zeros(len(idx), dtype=np.float32),
            )

    def get_tensors(self, device: torch.device) -> dict:
        """Convert all stored data to torch tensors on the given device."""
        return {
            "states": torch.tensor(np.array(self.states), dtype=torch.float32, device=device),
            "actions": torch.tensor(np.array(self.actions), dtype=torch.float32, device=device),
            "old_log_probs": torch.tensor(self.log_probs, dtype=torch.float32, device=device),
            "advantages": torch.tensor(self._advantages, dtype=torch.float32, device=device),
            "returns": torch.tensor(self._returns, dtype=torch.float32, device=device),
        }


# ---------------------------------------------------------------------------
# PPO Update
# ---------------------------------------------------------------------------


def ppo_update(
    model: "ActorCritic",
    memory: PPOMemory,
    optimizer: "torch.optim.Optimizer",
    clip_epsilon: float = 0.2,
    entropy_coef: float = 0.01,
    value_coef: float = 0.5,
    max_grad_norm: float = 0.5,
    n_epochs: int = 4,
    batch_size: int = 64,
    device: torch.device = None,
) -> dict:
    """
    Run PPO update on collected rollout data.

    Returns dict of training metrics: loss, policy_loss, value_loss, entropy, approx_kl.
    """
    if device is None:
        device = next(model.parameters()).device

    model.train()
    all_states, all_actions, all_old_log_probs, all_advantages, all_returns = memory.get_tensors(
        device
    ).values()

    # Normalize advantages
    all_advantages = (all_advantages - all_advantages.mean()) / (all_advantages.std() + 1e-8)

    total_loss_val = 0.0
    total_pg_loss = 0.0
    total_v_loss = 0.0
    total_entropy = 0.0
    total_kl = 0.0
    n_updates = 0

    T = len(all_states)
    for _ in range(n_epochs):
        indices = torch.randperm(T, device=device)
        for start in range(0, T, batch_size):
            end = min(start + batch_size, T)
            idx = indices[start:end]

            states = all_states[idx]
            actions = all_actions[idx]
            old_log_probs = all_old_log_probs[idx]
            advantages = all_advantages[idx]
            returns = all_returns[idx]

            # Evaluate actions
            new_log_probs, entropy, values = model.evaluate(states, actions)

            # Policy loss (clipped)
            ratio = torch.exp(new_log_probs - old_log_probs)
            surr1 = ratio * advantages
            surr2 = torch.clamp(ratio, 1.0 - clip_epsilon, 1.0 + clip_epsilon) * advantages
            policy_loss = -torch.min(surr1, surr2).mean()

            # Value loss (clipped)
            value_loss = F.mse_loss(values, returns)

            # Entropy bonus
            entropy_loss = -entropy.mean()

            # Total loss
            loss = policy_loss + value_coef * value_loss + entropy_coef * entropy_loss

            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
            optimizer.step()

            # Approximate KL divergence for monitoring
            with torch.no_grad():
                approx_kl = (old_log_probs - new_log_probs).mean().item()

            total_loss_val += loss.item()
            total_pg_loss += policy_loss.item()
            total_v_loss += value_loss.item()
            total_entropy += -entropy_loss.item()
            total_kl += approx_kl
            n_updates += 1

    return {
        "loss": total_loss_val / max(n_updates, 1),
        "policy_loss": total_pg_loss / max(n_updates, 1),
        "value_loss": total_v_loss / max(n_updates, 1),
        "entropy": total_entropy / max(n_updates, 1),
        "approx_kl": total_kl / max(n_updates, 1),
    }
