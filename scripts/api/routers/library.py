"""
scripts/api/routers/library.py — Song library management endpoints.

GET  /library                              — list all songs + stats
GET  /library/names                        — lightweight song name list
GET  /library/{name}                       — single song detail
DELETE /library/{name}                     — remove song from library
GET  /library/{name}/audio                 — stream the full.wav
GET  /library/{name}/stems/{stem}          — stream a specific stem
GET  /library/{name}/export-cues           — export cue points (rekordbox|serato)
GET  /outputs/{session_id}/{filename}      — stream a rendered mix
POST /library/calibrate-lufs               — compute per-stem LUFS targets
POST /library/initialize                   — one-shot pipeline
"""

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from scripts.api import jobs as job_store
from scripts.api.routers._helpers import _require_song, _song_info, _stem_file, _check_job_cap
from scripts.api.schemas import (
    InitializeLibraryRequest,
    JobResponse,
    JobType,
    LibraryListResponse,
    LibraryStats,
    ProcessingStatusResponse,
    SongInfo,
    StorageEvictResponse,
    StoragePruneResponse,
    StorageStatusResponse,
)
from scripts.api.tasks import task_analyze_missing, task_initialize_library
from scripts.core.paths import LIBRARY_DIR, OUTPUTS_DIR

router = APIRouter()


@router.get("/library", response_model=LibraryListResponse, tags=["library"])
def list_library(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=5000),
    search: Optional[str] = Query(None, description="Filter by name (case-insensitive substring)"),
):
    all_dirs = sorted(
        (d for d in LIBRARY_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.name.lower(),
    )
    if search:
        pat = search.lower()
        all_dirs = [d for d in all_dirs if pat in d.name.lower()]

    total = len(all_dirs)
    page_dirs = all_dirs[(page - 1) * per_page : page * per_page]
    songs = [_song_info(d) for d in page_dirs]

    total_bytes = sum(
        f.stat().st_size
        for d in LIBRARY_DIR.iterdir() if d.is_dir()
        for f in d.rglob("*") if f.is_file()
    )
    total_gb = total_bytes / 1_073_741_824

    try:
        from scripts.core.config import cfg
        cap_gb = cfg.library.max_size_gb
    except Exception:
        cap_gb = 50.0

    songs_with_stems = sum(
        1 for d in LIBRARY_DIR.iterdir()
        if d.is_dir() and _stem_file(d, "vocals") is not None
    )

    stats = LibraryStats(
        total_songs=total,
        total_size_gb=round(total_gb, 2),
        cap_gb=cap_gb,
        within_cap=total_gb <= cap_gb,
        songs_with_stems=songs_with_stems,
    )
    return LibraryListResponse(stats=stats, songs=songs)


@router.get("/library/names", tags=["library"])
def list_library_names(
    with_wav: bool = Query(False, description="Only songs that have full.wav"),
    with_stems: bool = Query(False, description="Only songs that have Demucs stems"),
):
    """
    Return ALL song names in the library — lightweight, no pagination.
    Use this instead of /library?per_page=... when you only need the names.
    """
    all_dirs = sorted(
        (d for d in LIBRARY_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.name.lower(),
    )
    names = []
    for d in all_dirs:
        if with_wav and not (d / "full.wav").exists():
            continue
        if with_stems and not any(
            (d / f"{s}.wav").exists() or (d / f"{s}.flac").exists()
            for s in ("vocals", "drums", "bass", "other")
        ):
            continue
        names.append(d.name)
    return {"names": names, "count": len(names)}


@router.get("/library/processing-status", response_model=ProcessingStatusResponse, tags=["library"])
def processing_status():
    """
    Segregates every song in the library into one of four processing buckets,
    for the live "Processing Queue" panel on the Operations page (polled
    every ~1s). Cheap — just file-existence checks, no audio decoding.
    """
    import time
    from scripts.api.routers._helpers import _stem_file
    from scripts.core.analysis_pipeline import has_analysis as _has_analysis

    fully_processed: List[str] = []
    stems_only: List[str] = []
    analysis_only: List[str] = []
    unprocessed: List[str] = []

    for d in sorted(LIBRARY_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        has_stems = any(_stem_file(d, s) is not None for s in ("vocals", "drums", "bass", "other"))
        analyzed = _has_analysis(d)
        if has_stems and analyzed:
            fully_processed.append(d.name)
        elif has_stems:
            stems_only.append(d.name)
        elif analyzed:
            analysis_only.append(d.name)
        else:
            unprocessed.append(d.name)

    total = len(fully_processed) + len(stems_only) + len(analysis_only) + len(unprocessed)
    return ProcessingStatusResponse(
        fully_processed=fully_processed,
        stems_only=stems_only,
        analysis_only=analysis_only,
        unprocessed=unprocessed,
        total=total,
        generated_at=time.time(),
    )


@router.post("/library/analyze-missing", response_model=JobResponse, status_code=202, tags=["library", "analysis"])
def analyze_missing(
    key_profile: str = Query(
        "auto",
        description="Tonal profile for key detection: 'ks', 'edma', 'edmm', or 'auto'",
    ),
):
    """
    Batch-analyze every library song currently missing BPM/key/energy data.

    Scans the library for songs where `has_analysis()` is False, then runs
    `run_song_analysis()` on each as a single background job (poll /jobs/{id}
    or subscribe to SSE for live per-song progress). Best-effort — one
    song's failure doesn't stop the rest.
    """
    _check_job_cap()
    job_id = job_store.create_job(JobType.ANALYZE, {"type": "analyze_missing", "key_profile": key_profile})
    job_store.submit_job(job_id, task_analyze_missing, key_profile=key_profile)
    return job_store.job_to_response(job_store.get_job(job_id))


@router.get("/library/storage", response_model=StorageStatusResponse, tags=["library", "storage"])
def storage_status():
    """
    Storage overview backed by scripts/core/library.py:LibraryManager — pruning
    and LRU eviction logic that already existed but was never exposed via the API.
    """
    from scripts.core.library import get_library_manager
    from scripts.core.config import cfg

    mgr = get_library_manager()
    songs = mgr.list_songs()
    with_raw = sum(1 for s in songs if s.has_full_wav)

    return StorageStatusResponse(
        library_dir=str(LIBRARY_DIR),
        outputs_dir=str(OUTPUTS_DIR),
        total_songs=len(songs),
        total_size_gb=round(mgr.get_size_gb(), 2),
        cap_gb=mgr.max_size_gb,
        within_cap=mgr.get_size_gb() <= mgr.max_size_gb,
        songs_with_full_wav=with_raw,
        songs_stems_only=len(songs) - with_raw,
        prune_on_download=cfg.library.prune_on_download,
        keep_raw_after_separation=cfg.library.keep_raw_after_separation,
        auto_evict_on_download=cfg.library.auto_evict_on_download,
    )


# ---------------------------------------------------------------------------
# POST /library/storage/scan — scan a user-specified folder for audio files
# ---------------------------------------------------------------------------

AUDIO_IMPORT_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"}


@router.post("/library/storage/scan", tags=["library", "storage"])
def storage_scan_folder(folder: str = ""):
    """
    Scan a local folder for audio files and report what's there.
    Defaults to the Windows Music folder.
    """
    import os

    if not folder:
        folder = str(Path(os.path.expanduser("~")) / "Music")

    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise HTTPException(404, f"Folder not found: {folder}")

    files = []
    total_bytes = 0
    for f in sorted(folder_path.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_IMPORT_EXTS:
            size = f.stat().st_size
            total_bytes += size
            files.append({
                "name": f.stem,
                "filename": f.name,
                "ext": f.suffix.lower(),
                "size_mb": round(size / 1_048_576, 1),
            })

    # Check which are already in library
    for fi in files:
        fi["already_in_library"] = (LIBRARY_DIR / fi["name"]).exists()

    already = sum(1 for f in files if f["already_in_library"])
    return {
        "folder": str(folder_path),
        "total_files": len(files),
        "already_in_library": already,
        "new_files": len(files) - already,
        "total_size_mb": round(total_bytes / 1_048_576, 1),
        "files": files,
    }


# ---------------------------------------------------------------------------
# POST /library/cleanup-stale — remove dirs with no audio files
# ---------------------------------------------------------------------------

@router.post("/library/cleanup-stale", tags=["library"])
def cleanup_stale():
    """
    Scan every song directory under LIBRARY_DIR. If a directory contains
    neither full.wav / full.mp3 / full.flac nor any stem files, remove it
    and unregister from the index. Returns the list of removed songs.
    """
    import shutil
    from scripts.core.library import get_library_manager

    AUDIO_NAMES = {"full.wav", "full.mp3", "full.flac", "full.m4a", "full.ogg", "full.aac"}
    STEM_NAMES = {"vocals.wav", "vocals.flac", "drums.wav", "drums.flac",
                  "bass.wav", "bass.flac", "other.wav", "other.flac"}

    removed = []
    kept = 0
    mgr = get_library_manager()

    if not LIBRARY_DIR.exists():
        return {"removed": [], "kept": 0}

    for d in sorted(LIBRARY_DIR.iterdir()):
        if not d.is_dir():
            continue
        files_in_dir = {f.name for f in d.iterdir() if f.is_file()}
        has_audio = bool(AUDIO_NAMES & files_in_dir) or bool(STEM_NAMES & files_in_dir)

        if not has_audio:
            shutil.rmtree(d)
            mgr.unregister(d.name)
            removed.append(d.name)
        else:
            kept += 1

    return {"removed": removed, "kept": kept}


@router.post("/library/storage/prune", response_model=StoragePruneResponse, tags=["library", "storage"])
def storage_prune():
    """
    Delete full.wav for every song that already has all 4 stems — they're
    redundant once stems exist. Synchronous: it's just file deletes, fast
    even across a large library.
    """
    from scripts.core.library import get_library_manager

    mgr = get_library_manager()
    pruned: List[str] = []
    freed_bytes = 0
    for entry in mgr.list_songs():
        if not entry.has_full_wav:
            continue
        full_wav = Path(entry.path) / "full.wav"
        size = full_wav.stat().st_size if full_wav.exists() else 0
        if mgr.prune_raw(entry.name, force=True):
            pruned.append(entry.name)
            freed_bytes += size
    return StoragePruneResponse(pruned=pruned, freed_mb=round(freed_bytes / 1_048_576, 1))


@router.post("/library/storage/evict", response_model=StorageEvictResponse, tags=["library", "storage"])
def storage_evict(
    target_gb: Optional[float] = Query(None, description="Evict until under this size (default: configured cap)"),
    dry_run: bool = Query(True, description="Preview without deleting — defaults True for safety"),
):
    """
    Manually trigger LRU eviction (oldest-accessed songs lose full.wav first,
    then whole song directories if still over budget). Defaults to a dry run
    so you can see what would be deleted before committing to it.
    """
    from scripts.core.library import get_library_manager

    mgr = get_library_manager()
    size_before = mgr.get_size_gb()
    evicted = mgr.evict_lru(target_gb=target_gb, dry_run=dry_run)
    size_after = mgr.get_size_gb() if not dry_run else size_before
    return StorageEvictResponse(
        evicted=evicted, dry_run=dry_run,
        size_before_gb=round(size_before, 2), size_after_gb=round(size_after, 2),
    )


@router.get("/library/{name}", response_model=SongInfo, tags=["library"])
def get_song(name: str):
    return _song_info(_require_song(name))


@router.delete("/library/{name}", tags=["library"])
def delete_song(name: str):
    import shutil
    from scripts.core.library import get_library_manager

    d = _require_song(name)
    shutil.rmtree(d)
    # Without this, LibraryManager's separate .index.json keeps a phantom
    # entry for the deleted song forever — inflating /library/storage's
    # total_songs count and corrupting evict_lru()'s LRU ordering, since
    # both read from this same index rather than scanning LIBRARY_DIR live.
    get_library_manager().unregister(name)
    return {"deleted": name}


@router.get("/library/{name}/audio", tags=["library"])
def stream_audio(name: str):
    _require_song(name)
    from scripts.core.audio_source import resolve_source_file, load_source_audio

    # Prefer streaming an existing file (full.wav → full_enhanced.wav) directly.
    src = resolve_source_file(name)
    if src is not None:
        ext = src.suffix.lower()
        media_types = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac"}
        mt = media_types.get(ext, "application/octet-stream")
        return FileResponse(str(src), media_type=mt, filename=f"{name}{ext}")

    # No single-file source — reconstruct from Demucs stems on the fly and
    # stream the summed mix from memory (no library mutation).
    try:
        import io
        import soundfile as sf
        audio, sr = load_source_audio(name, sr=44100, mono=True)
        buf = io.BytesIO()
        sf.write(buf, audio, sr, format="WAV")
        return Response(
            content=buf.getvalue(),
            media_type="audio/wav",
            headers={"Content-Disposition": f'inline; filename="{name}.wav"'},
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No source audio for {name!r}")


@router.get("/library/{name}/structure", tags=["library"])
def get_track_structure(name: str):
    """Return sections, energy curve, and bar-level structure for a track."""
    song_dir = _require_song(name)

    # Try cached analysis.json first
    analysis_path = song_dir / "analysis.json"
    if analysis_path.exists():
        import json
        try:
            data = json.loads(analysis_path.read_text())
            sections = data.get("sections", [])
            energy_curve = data.get("energy_curve", [])
            if sections or energy_curve:
                return {
                    "name":           name,
                    "bpm":            data.get("bpm"),
                    "duration":       data.get("duration"),
                    "key":            data.get("key"),
                    "camelot":        data.get("camelot"),
                    "total_bars":     data.get("total_bars"),
                    "sections":       sections,
                    "energy_curve":   energy_curve,
                    "phrase_boundaries": data.get("phrase_boundaries", []),
                }
        except Exception:
            pass

    # No cached data — run live analysis
    from scripts.core.audio_source import load_source_audio
    from scripts.core.dj_analysis import analyze_structure

    try:
        audio, sr = load_source_audio(name, sr=22050, mono=True, duration=180.0)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No audio for {name!r}")

    struct = analyze_structure(audio, sr)

    sections_data = []
    for s in struct.sections:
        sections_data.append({
            "type":       s.type,
            "start_time": round(s.start_time, 2),
            "end_time":   round(s.end_time, 2),
            "start_bar":  s.start_bar,
            "end_bar":    s.end_bar,
            "avg_energy": round(s.avg_energy, 4),
        })

    energy_curve_data = None
    if getattr(struct, "energy_curve", None) is not None:
        import numpy as np
        ec = struct.energy_curve
        if len(ec) > 200:
            step = len(ec) // 200
            ec = ec[::step]
        energy_curve_data = [round(float(v), 4) for v in ec]

    return {
        "name":           name,
        "bpm":            round(struct.bpm, 1),
        "duration":       round(struct.duration, 1),
        "key":            struct.key_name,
        "camelot":        struct.camelot,
        "total_bars":     struct.total_bars,
        "sections":       sections_data,
        "energy_curve":   energy_curve_data,
        "phrase_boundaries": [round(s.start_time, 2) for s in struct.sections],
    }


@router.get("/library/{name}/stems/{stem}", tags=["library"])
def stream_stem(name: str, stem: str):
    if stem not in ("vocals", "drums", "bass", "other"):
        raise HTTPException(status_code=400, detail="stem must be one of: vocals drums bass other")
    d = _require_song(name)
    f = _stem_file(d, stem)
    if f is None:
        raise HTTPException(status_code=404, detail=f"{stem} stem not found for {name!r}")
    media_type = "audio/flac" if f.suffix == ".flac" else "audio/wav"
    return FileResponse(str(f), media_type=media_type, filename=f"{name}_{stem}{f.suffix}")


# ---------------------------------------------------------------------------
# Outputs (rendered mixes)
# ---------------------------------------------------------------------------

@router.get("/outputs/{session_id}/{filename}", tags=["outputs"])
def stream_output(session_id: str, filename: str):
    # Strip to final path component — rejects ../, absolute paths, and
    # embedded separators. CodeQL recognizes Path().name as a sanitizer.
    safe_session  = Path(session_id).name
    safe_filename = Path(filename).name
    if not safe_session or not safe_filename or safe_session != session_id or safe_filename != filename:
        raise HTTPException(status_code=400, detail="Invalid path component.")
    path = (OUTPUTS_DIR / safe_session / safe_filename).resolve()
    if not str(path).startswith(str(OUTPUTS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path component.")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    # Detect media type from extension
    ext = path.suffix.lower()
    media_types = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=safe_filename)


@router.get("/outputs", tags=["outputs"])
def list_outputs():
    """List all generated outputs (previews, transitions, chains, beats)."""
    if not OUTPUTS_DIR.exists():
        return []
    results = []
    for d in sorted(OUTPUTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        files = []
        for f in sorted(d.iterdir()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size_mb": round(f.stat().st_size / 1_048_576, 2),
                    "ext": f.suffix.lower(),
                    "modified": f.stat().st_mtime,
                })
        if not files:
            continue
        # Determine type from dir prefix
        dir_name = d.name
        if dir_name.startswith("dj_"):
            output_type = "transition"
        elif dir_name.startswith("chain_"):
            output_type = "chain"
        elif dir_name.startswith("beat_"):
            output_type = "beat"
        elif dir_name.startswith("lab_"):
            output_type = "lab"
        else:
            output_type = "preview"
        audio_files = [f for f in files if f["ext"] in (".wav", ".mp3", ".flac")]
        results.append({
            "session_id": dir_name,
            "type": output_type,
            "modified": d.stat().st_mtime,
            "files": files,
            "audio_file": audio_files[0]["name"] if audio_files else None,
            "total_size_mb": round(sum(f["size_mb"] for f in files), 2),
        })
    return results


# ---------------------------------------------------------------------------
# Full library initialisation (stem split → compress → index, one job)
# ---------------------------------------------------------------------------

@router.post("/library/calibrate-lufs", response_model=JobResponse, status_code=202, tags=["library"])
def calibrate_stem_lufs():
    """
    Compute per-stem-type LUFS corpus targets across the whole library.

    Iterates every song that has Demucs stems, measures integrated LUFS for
    each stem type (drums / bass / vocals / other), and writes the per-type
    means to ``data/stem_lufs_targets.json``.  Subsequent remix calls will use
    these corpus-derived targets instead of the flat -20 LUFS fallback.

    Run this once after initial library setup, or after adding many new tracks.
    Returns a job_id for SSE tracking.
    """
    _check_job_cap()

    def _task_calibrate_lufs(job_id: str) -> None:
        try:
            from scripts.core.mastering import analyze_library_stem_targets, save_stem_targets
            from scripts.core.paths import DATA_DIR

            job_store.update_job(job_id, status="running", progress=0.1,
                                 message="Scanning library stems…")
            targets = analyze_library_stem_targets()
            job_store.update_job(job_id, progress=0.9, message="Saving targets…")
            save_stem_targets(targets, cache_path=DATA_DIR / "stem_lufs_targets.json")
            job_store.update_job(job_id, status="done", progress=1.0,
                                 message="Stem LUFS calibration complete",
                                 result=targets)
        except Exception as exc:
            job_store.update_job(job_id, status="failed",
                                 message=f"Calibration failed: {exc}")

    job_id = job_store.create_job(JobType.ANALYZE, {"type": "calibrate_lufs"})
    job_store.submit_job(job_id, _task_calibrate_lufs)
    return job_store.job_to_response(job_store.get_job(job_id))


@router.get("/library/{name}/export-cues", tags=["library"])
def export_cues(
    name: str,
    fmt: str = Query("rekordbox", description="Export format: 'rekordbox' or 'serato'"),
):
    """
    Export RemixMate cue points and phrase boundaries for a song to DJ software format.

    Reads the song's analysis file (``analysis.json``) for phrase boundaries and BPM.
    Returns the exported file as a download (``Content-Disposition: attachment``).

    Formats
    -------
    rekordbox
        Standard rekordbox 6+ XML collection file with HOT CUE and MEMORY CUE markers.
        Compatible with rekordbox 6+, DJay Pro (via XML import), and VirtualDJ.
        Returns ``application/xml``.

    serato
        Serato Markers2 GEOB ID3 tags written into the song's .mp3 file and returned
        as ``audio/mpeg``.  Requires mutagen (``pip install mutagen``).
        Only works when the song was downloaded as an .mp3 (not WAV).

    The first phrase boundary is used as Cue 1, each subsequent boundary as further cues.
    If no analysis exists, only Cue 1 at 0.0 s is exported and BPM is 0.
    """
    import json, tempfile, shutil
    from fastapi.responses import FileResponse as _FileResponse

    song_dir = _require_song(name)
    fmt = fmt.lower().strip()

    if fmt not in ("rekordbox", "serato"):
        raise HTTPException(status_code=400, detail=f"Unknown format {fmt!r}. Use 'rekordbox' or 'serato'.")

    # ── Load analysis if present ──────────────────────────────────────────────
    analysis_path = song_dir / "analysis.json"
    bpm: float = 0.0
    phrase_boundaries: list[float] = []
    artist: str = ""
    duration: float = 0.0

    if analysis_path.exists():
        try:
            data = json.loads(analysis_path.read_text())
            bpm = float(data.get("bpm", 0.0))
            phrase_boundaries = [float(t) for t in data.get("phrase_boundaries", [])]
            artist = str(data.get("artist", ""))
            duration = float(data.get("duration", 0.0))
        except Exception:
            pass  # fall through to defaults

    # Cue points = phrase boundaries, or [0.0] if none
    cue_points = phrase_boundaries if phrase_boundaries else [0.0]

    try:
        from scripts.core.cue_export import export_cues as _export_cues
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"cue_export module unavailable: {e}")

    if fmt == "rekordbox":
        # Write to a temp file, stream it, then clean up
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            _export_cues(
                song_name=name,
                cue_points=cue_points,
                phrase_boundaries=phrase_boundaries,
                bpm=bpm,
                fmt="rekordbox",
                output_path=tmp_path,
                audio_path=(song_dir / "full.wav") if (song_dir / "full.wav").exists() else None,
                artist=artist,
                total_time_s=duration,
            )
            # FileResponse streams the file; FastAPI deletes it after sending via background task
            return _FileResponse(
                str(tmp_path),
                media_type="application/xml",
                filename=f"{name}_rekordbox.xml",
                background=_delete_tmp(tmp_path),
            )
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc))

    # serato — need an mp3 source file
    mp3_candidates = list(song_dir.glob("*.mp3"))
    if not mp3_candidates:
        raise HTTPException(
            status_code=422,
            detail=(
                "Serato export requires an .mp3 source file. "
                f"No .mp3 found in library/{name}/. "
                "Download the track as MP3 first."
            ),
        )
    src_mp3 = mp3_candidates[0]
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = Path(f.name)
    shutil.copy2(src_mp3, tmp_path)
    try:
        _export_cues(
            song_name=name,
            cue_points=cue_points,
            phrase_boundaries=phrase_boundaries,
            bpm=bpm,
            fmt="serato",
            output_path=tmp_path,
            audio_path=tmp_path,
        )
        return _FileResponse(
            str(tmp_path),
            media_type="audio/mpeg",
            filename=f"{name}_serato.mp3",
            background=_delete_tmp(tmp_path),
        )
    except ImportError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=501,
            detail=f"mutagen not installed — cannot write Serato tags. Install with: pip install mutagen. ({exc})",
        )
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc))


def _delete_tmp(path: Path):
    """Background task that removes a temp file after the response is sent."""
    from starlette.background import BackgroundTask
    return BackgroundTask(lambda: path.unlink(missing_ok=True))


@router.post("/library/initialize", response_model=JobResponse, status_code=202, tags=["library"])
def initialize_library(req: InitializeLibraryRequest, background_tasks=None):
    """
    One-shot pipeline: stem-split all unsplit songs → FLAC compress → rebuild RAG index.
    Returns a single job_id so the frontend can track all three phases from one progress bar.
    """
    _check_job_cap()
    job_id = job_store.create_job(
        JobType.ANALYZE, {"type": "initialize_library"}
    )
    job_store.submit_job(
        job_id, task_initialize_library,
        enhance=req.enhance,
        model=req.model,
        delete_wav=req.delete_wav,
        run_compress=req.run_compress,
        run_index=req.run_index,
    )
    return job_store.job_to_response(job_store.get_job(job_id))


# ---------------------------------------------------------------------------
# POST /library/import-local — Import audio files from a local folder
# ---------------------------------------------------------------------------

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"}


@router.post("/library/import-local", tags=["library"])
def import_local_folder(
    folder: str = "",
    auto_analyze: bool = True,
    auto_stems: bool = False,
):
    """
    Scan a local folder for audio files, copy/convert them into the library as full.wav.
    Non-wav files (mp3, flac, etc.) are converted via librosa + soundfile.
    Defaults to the Windows Music folder.
    """
    if not folder:
        import os
        folder = str(Path(os.path.expanduser("~")) / "Music")

    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise HTTPException(404, f"Folder not found: {folder}")

    from shutil import copy2
    from scripts.core.paths import LIBRARY_DIR

    audio_files = [
        f for f in folder_path.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    ]

    if not audio_files:
        return {"imported": 0, "skipped": 0, "files": []}

    imported = []
    skipped = 0

    for f in audio_files:
        song_name = f.stem
        dest_dir = LIBRARY_DIR / song_name

        if dest_dir.exists():
            skipped += 1
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_wav = dest_dir / "full.wav"

        try:
            if f.suffix.lower() == ".wav":
                copy2(str(f), str(dest_wav))
            else:
                # Convert mp3/flac/etc → full.wav via librosa + soundfile
                import librosa
                import soundfile as sf
                audio, sr = librosa.load(str(f), sr=44100, mono=True)
                sf.write(str(dest_wav), audio, sr, format="WAV", subtype="PCM_16")
        except Exception as exc:
            # If conversion fails, copy original as-is (fallback)
            copy2(str(f), str(dest_dir / f"full{f.suffix.lower()}"))

        # Register in index
        try:
            from scripts.core.library import get_library_manager
            mgr = get_library_manager()
            mgr.register(song_name, str(dest_dir))
        except Exception:
            pass

        imported.append(song_name)

    # Optionally queue analysis jobs
    if auto_analyze and imported:
        for name in imported:
            jid = job_store.create_job(JobType.ANALYZE, {"song": name, "type": "import"})
            job_store.submit_job(jid, task_analyze_imported, song_name=name)

    return {
        "imported": len(imported),
        "skipped": skipped,
        "files": imported,
    }


def task_analyze_imported(song_name: str):
    """Background task: analyze an imported song (BPM/key/energy)."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from scripts.core.analysis_pipeline import run_song_analysis
        run_song_analysis(song_name)
        log.info("Analyzed imported song: %s", song_name)
    except Exception as exc:
        log.error("Failed to analyze %s: %s", song_name, exc)
