"""
darave_rl/utils.py — Seed, device, normalization, and network helpers.

All pure utility functions with no state. Used across PPO, SAC, and env.
"""

from __future__ import annotations

import random
from typing import Tuple

import numpy as np

try:
    import torch
    import torch.nn as nn

    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


def set_seed(seed: int) -> None:
    """Set deterministic seed across random, numpy, and torch."""
    random.seed(seed)
    np.random.seed(seed)
    if _HAS_TORCH:
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------


def get_device() -> "torch.device":
    """Auto-detect best available device: cuda > mps > cpu."""
    if not _HAS_TORCH:
        raise ImportError("PyTorch is required for darave_rl")
    if torch.cuda.is_available():
        return torch.device("cuda")
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


# ---------------------------------------------------------------------------
# State normalization
# ---------------------------------------------------------------------------


class RunningStats:
    """Welford online algorithm for running mean/std (no stored history)."""

    def __init__(self, shape: Tuple[int, ...] = ()):
        self.mean = np.zeros(shape, dtype=np.float64)
        self.var = np.zeros(shape, dtype=np.float64)
        self.count: float = 0.0

    def update(self, batch: np.ndarray) -> None:
        """Update with a new batch of observations."""
        batch = batch.astype(np.float64)
        if batch.ndim == 1:
            batch = batch.reshape(1, -1)
        batch_mean = batch.mean(axis=0)
        batch_var = batch.var(axis=0)
        batch_count = batch.shape[0]
        self._update_from_moments(batch_mean, batch_var, batch_count)

    def _update_from_moments(
        self, batch_mean: np.ndarray, batch_var: np.ndarray, batch_count: float
    ) -> None:
        delta = batch_mean - self.mean
        total = self.count + batch_count
        new_mean = self.mean + delta * batch_count / total
        m_a = self.var * self.count
        m_b = batch_var * batch_count
        m2 = m_a + m_b + np.square(delta) * self.count * batch_count / total
        self.mean = new_mean
        self.var = m2 / total
        self.count = total

    def normalize(self, x: np.ndarray, eps: float = 1e-8) -> np.ndarray:
        """Z-score normalize using accumulated statistics."""
        std = np.sqrt(self.var + eps)
        return ((x - self.mean) / std).astype(np.float32)


def normalize_state(
    state: np.ndarray,
    mean: np.ndarray,
    std: np.ndarray,
    eps: float = 1e-8,
) -> np.ndarray:
    """Z-score normalization with given statistics."""
    return ((state - mean) / (std + eps)).astype(np.float32)


def clip_action(action: np.ndarray, low: float = -1.0, high: float = 1.0) -> np.ndarray:
    """Clip action vector to [low, high]."""
    return np.clip(action, low, high)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def soft_update(target: "nn.Module", source: "nn.Module", tau: float = 0.005) -> None:
    """Polyak averaging: target = (1 - tau) * target + tau * source."""
    if not _HAS_TORCH:
        return
    for tp, sp in zip(target.parameters(), source.parameters()):
        tp.data.copy_(tp.data * (1.0 - tau) + sp.data * tau)


def hard_update(target: "nn.Module", source: "nn.Module") -> None:
    """Full parameter copy: target <- source."""
    if not _HAS_TORCH:
        return
    target.load_state_dict(source.state_dict())


def weights_init(m: "nn.Module") -> None:
    """Xavier uniform init for linear layers."""
    if not _HAS_TORCH:
        return
    if isinstance(m, nn.Linear):
        nn.init.xavier_uniform_(m.weight)
        if m.bias is not None:
            nn.init.zeros_(m.bias)
