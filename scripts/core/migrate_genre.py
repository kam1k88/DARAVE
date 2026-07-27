"""
One-shot migration: convert old genre strings in meta.json to new multi-label format.
Run once after deploying genre classifier v2.

Usage: python -m scripts.core.migrate_genre
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.core.paths import LIBRARY_DIR
from scripts.core.genre import classify_genre_multi
from scripts.core.analysis_pipeline import _load_song_audio
from scripts.core.classify import classify_tempo, energy_to_el
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("migrate_genre")


def migrate():
    count = 0
    skipped = 0

    for d in sorted(LIBRARY_DIR.iterdir()):
        if not d.is_dir():
            continue

        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        genre = meta.get("genre")

        # Skip if already in new format
        if isinstance(genre, dict) and "genres" in genre:
            skipped += 1
            continue

        # Need audio to classify
        try:
            audio, sr = _load_song_audio(d.name, sr=44100, duration=120.0)
        except FileNotFoundError:
            log.warning("No audio for %s — skipping", d.name)
            skipped += 1
            continue

        # Classify
        try:
            bpm = meta.get("bpm", 120)
            result = classify_genre_multi(
                audio, sr,
                key_name=meta.get("key", ""),
                mode=meta.get("mode", ""),
                camelot=meta.get("camelot", ""),
                tempo_type=classify_tempo(bpm),
                el=energy_to_el(meta.get("energy_mean", 0.5)),
            )
        except Exception as e:
            log.error("Failed to classify %s: %s", d.name, e)
            skipped += 1
            continue

        # Update meta.json
        meta["genre"] = {
            "genres": result.genres,
            "tags": result.tags,
            "description": result.description,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        count += 1
        log.info("Migrated %s → %s", d.name, result.primary_genre)

    log.info("Done: %d migrated, %d skipped", count, skipped)


if __name__ == "__main__":
    migrate()
