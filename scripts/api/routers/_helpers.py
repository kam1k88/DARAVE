"""
scripts/api/routers/_helpers.py — Shared helper functions for all domain routers.

These utilities are used by multiple routers for common operations like
song validation, stem file lookup, and rate limiting.
"""

from pathlib import Path
from typing import Dict, Optional

from fastapi import HTTPException

# Song names on disk are produced by scripts.core.paths.sanitize_song_name()
# — the single shared source of truth for "what characters are allowed in a
# song name", also used by scripts/download.py:_sanitize(). Both used to be
# independent definitions (a loose blocklist on the download side, a strict
# allowlist here) that disagreed on common title punctuation — a perfectly
# normal download could land on disk fine and then 400 forever on every
# subsequent request. Re-exported here (rather than imported directly) to
# keep this file's existing CodeQL-recognized-sanitizer shape intact.
# This pattern is used as an explicit CodeQL-recognized sanitizer in _require_song.
from scripts.core.paths import SONG_NAME_RE as _SAFE_SONG_NAME_RE

from scripts.api import jobs as job_store
from scripts.api.schemas import SongInfo
from scripts.core.paths import LIBRARY_DIR


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _stem_file(song_dir: Path, stem: str) -> Path | None:
    """Return the stem file path (prefers .flac, falls back to .wav), or None."""
    flac = song_dir / f"{stem}.flac"
    if flac.exists():
        return flac
    wav = song_dir / f"{stem}.wav"
    if wav.exists():
        return wav
    return None


def _song_info(song_dir: Path) -> SongInfo:
    size = sum(f.stat().st_size for f in song_dir.rglob("*") if f.is_file())
    stems = [
        s for s in ("vocals", "drums", "bass", "other")
        if _stem_file(song_dir, s) is not None
    ]
    license_type = None
    source = None
    try:
        import json
        lic_path = song_dir / "license.json"
        if lic_path.exists():
            lic = json.loads(lic_path.read_text())
            license_type = lic.get("license_type")
            source = lic.get("source")
    except Exception:
        pass

    from scripts.core.analysis_pipeline import has_analysis as _has_analysis

    analyzed = _has_analysis(song_dir)
    bpm = key = mode = camelot = genre = duration = energy = None
    tempo_type = el = drops = breakdowns = None
    if analyzed:
        try:
            import json
            meta = json.loads((song_dir / "meta.json").read_text())
            bpm      = meta.get("bpm")
            key      = meta.get("key")
            mode     = meta.get("mode")
            camelot  = meta.get("camelot")
            genre    = meta.get("genre")
            energy   = meta.get("energy_mean")  # 0-1 already, matches frontend's expected scale
            tempo_type = meta.get("tempo_type")  # "half-time" | "full-time" | "downtempo" | "other"
            el       = meta.get("el")            # Energy Level 1-5
        except Exception:
            pass
        # duration isn't persisted to meta.json (only bpm/key/genre/etc are) —
        # it lives in analysis.json instead, written alongside it.
        try:
            import json
            analysis_doc = json.loads((song_dir / "analysis.json").read_text())
            duration = analysis_doc.get("duration")
            # Count drops and breakdowns from sections
            sections = analysis_doc.get("sections", [])
            drops = sum(1 for s in sections if s.get("type") == "drop")
            breakdowns = sum(1 for s in sections if s.get("type") in ("break", "build"))
        except Exception:
            pass

    return SongInfo(
        name=song_dir.name,
        path=str(song_dir),
        size_mb=round(size / 1_048_576, 2),
        has_full_wav=(song_dir / "full.wav").exists(),
        has_stems=len(stems) > 0,
        stems=stems,
        has_analysis=analyzed,
        bpm=bpm,
        key=key,
        mode=mode,
        camelot=camelot,
        energy=energy,
        genre=genre,
        duration=duration,
        license_type=license_type,
        source=source,
        tempo_type=tempo_type,
        el=el,
        drops=drops,
        breakdowns=breakdowns,
    )


def _require_song(name: str) -> Path:
    """Return the song dir or raise 404.  Prevents path traversal.

    Three-layer sanitization (each layer alone would suffice for CodeQL):
    1. Regex allowlist — rejects path separators, null bytes, shell chars.
    2. Path().name — strips any leading directory components.
    3. resolve() + prefix check — symlink-safe confinement to LIBRARY_DIR.
    """
    # Layer 1: allowlist regex — CodeQL's primary recognized sanitizer
    if not name or not _SAFE_SONG_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid song name")
    # Layer 2: extract final component only (strips any ../ that slipped through)
    safe = Path(name).name
    if not safe or safe != name:
        raise HTTPException(status_code=400, detail="Invalid song name")
    # Layer 3: resolve and confirm confinement
    d = (LIBRARY_DIR / safe).resolve()
    if not str(d).startswith(str(LIBRARY_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid song name")
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"Song not found in library: {name!r}")
    return d


# ---------------------------------------------------------------------------
# Rate limiting / active job caps
# ---------------------------------------------------------------------------

_MAX_ACTIVE_JOBS = 4  # Hard cap on concurrent background jobs


def _check_job_cap() -> None:
    """Raise 429 if the user already has too many active jobs running."""
    active = [j for j in job_store.list_jobs() if j["status"] == "running"]
    if len(active) >= _MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many active jobs ({len(active)}/{_MAX_ACTIVE_JOBS}). "
                f"Wait for one to finish before submitting another."
            ),
        )


def _check_duplicate_job(job_type: str, meta: Optional[Dict] = None) -> None:
    """Raise 409 if an active job with the same type and matching meta already exists."""
    if meta is None:
        return
    active = [j for j in job_store.list_jobs() if j["status"] == "running"]
    for j in active:
        if j.get("job_type") != job_type:
            continue
        jmeta = j.get("meta") or {}
        # For stem splits, match on song name
        if job_type == "analyze" and jmeta.get("type") == meta.get("type"):
            if meta.get("song") and jmeta.get("song") == meta.get("song"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Active {meta.get('type')} job already running for '{meta.get('song')}'.",
                )
            if meta.get("type") == "batch_stem_split" and jmeta.get("type") == "batch_stem_split":
                raise HTTPException(
                    status_code=409,
                    detail="A batch stem split job is already running.",
                )
        # For DJ remix, match on song pair
        if job_type == "dj_remix":
            if jmeta.get("song_a") == meta.get("song_a") and jmeta.get("song_b") == meta.get("song_b"):
                raise HTTPException(
                    status_code=409,
                    detail=f"A remix job for '{meta.get('song_a')}' → '{meta.get('song_b')}' is already running.",
                )
