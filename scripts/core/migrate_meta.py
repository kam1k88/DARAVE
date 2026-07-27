"""
scripts/core/migrate_meta.py — One-shot migration: add tempo_type + el to existing meta.json files.

Reads BPM and energy_mean from each song's meta.json, computes classify_tempo()
and energy_to_el(), then writes the two new fields back. No re-analysis needed.

Usage:
    python -m scripts.core.migrate_meta
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from scripts.core.classify import classify_tempo, energy_to_el
from scripts.core.paths import LIBRARY_DIR

log = logging.getLogger(__name__)


def migrate_meta_files(dry_run: bool = False) -> dict:
    """
    Scan all songs in the library. For each meta.json that has bpm and
    energy_mean but is missing tempo_type / el, compute and insert them.

    Returns summary dict: { total, updated, skipped, errors }.
    """
    total = 0
    updated = 0
    skipped = 0
    errors = 0

    for song_dir in sorted(LIBRARY_DIR.iterdir()):
        if not song_dir.is_dir():
            continue
        total += 1
        meta_path = song_dir / "meta.json"
        if not meta_path.exists():
            skipped += 1
            continue

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to read %s: %s", meta_path, exc)
            errors += 1
            continue

        bpm = meta.get("bpm")
        energy_mean = meta.get("energy_mean")

        if bpm is None or energy_mean is None:
            skipped += 1
            continue

        # Already migrated?
        if meta.get("tempo_type") and meta.get("el") is not None:
            skipped += 1
            continue

        tempo_type = classify_tempo(float(bpm))
        el = energy_to_el(float(energy_mean))

        meta["tempo_type"] = tempo_type
        meta["el"] = el

        if dry_run:
            log.info("[DRY RUN] Would update %s: tempo_type=%s, el=%d", song_dir.name, tempo_type, el)
        else:
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("Updated %s: tempo_type=%s, el=%d", song_dir.name, tempo_type, el)

        updated += 1

    summary = {"total": total, "updated": updated, "skipped": skipped, "errors": errors}
    log.info("Migration complete: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = migrate_meta_files(dry_run=False)
    print(f"\nMigration done: {result['updated']} updated, {result['skipped']} skipped, {result['errors']} errors (of {result['total']} total)")
