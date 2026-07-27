"""
darave_rl/logger.py — JSONL episode logging for RL training.

Each episode is one JSON line.  Training metrics are logged separately.

Log directory layout:
    data/episode_logs/
        episodes.jsonl       — one JSON object per line (one per episode)
        training.jsonl       — one JSON object per line (one per training step)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from darave_rl.episode import DjTransitionEpisode

log = logging.getLogger(__name__)

# Default log location (relative to project root)
_DEFAULT_LOG_DIR = Path(__file__).parent.parent / "data" / "episode_logs"


class EpisodeLogger:
    """
    JSONL logger for DJ transition episodes and training metrics.

    Usage:
        logger = EpisodeLogger()
        logger.log_episode(episode)
        logger.log_training_step(step=100, metrics={"loss": 0.5, "reward": 0.8})
    """

    def __init__(self, log_dir: Optional[str] = None):
        self._log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._episode_path = self._log_dir / "episodes.jsonl"
        self._training_path = self._log_dir / "training.jsonl"

    # ------------------------------------------------------------------
    # Episode logging
    # ------------------------------------------------------------------

    def log_episode(self, episode: DjTransitionEpisode) -> None:
        """Append one episode record to episodes.jsonl."""
        record = episode.to_dict()
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        record["_type"] = "episode"

        with open(self._episode_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_episodes(self, episodes: List[DjTransitionEpisode]) -> None:
        """Append multiple episodes in a single open/write cycle."""
        with open(self._episode_path, "a", encoding="utf-8") as f:
            for ep in episodes:
                record = ep.to_dict()
                record["timestamp"] = datetime.now(timezone.utc).isoformat()
                record["_type"] = "episode"
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Training step logging
    # ------------------------------------------------------------------

    def log_training_step(self, step: int, metrics: Dict[str, Any]) -> None:
        """Append one training step record to training.jsonl."""
        record = {
            "_type": "training_step",
            "step": step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **metrics,
        }
        with open(self._training_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_episodes(self, limit: int = 1000) -> List[dict]:
        """Read the last `limit` episode records from the log."""
        if not self._episode_path.exists():
            return []

        lines = self._episode_path.read_text(encoding="utf-8").strip().splitlines()
        lines = lines[-limit:]

        episodes = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                episodes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return episodes

    def read_training_steps(self, limit: int = 1000) -> List[dict]:
        """Read the last `limit` training step records."""
        if not self._training_path.exists():
            return []

        lines = self._training_path.read_text(encoding="utf-8").strip().splitlines()
        lines = lines[-limit:]

        steps = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return steps

    def episode_count(self) -> int:
        """Count total episodes in the log."""
        if not self._episode_path.exists():
            return 0
        return sum(
            1
            for line in self._episode_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def clear(self) -> None:
        """Truncate both log files."""
        for p in (self._episode_path, self._training_path):
            if p.exists():
                p.write_text("", encoding="utf-8")
        log.info("Cleared episode logs at %s", self._log_dir)
