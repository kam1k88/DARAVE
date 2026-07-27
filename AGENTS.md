# DARAVE — Agent Instructions

## What this is

A real-time DJ engine in Python + React. Two songs in, beat-locked stem-aware transition out, mastered to −14 LUFS. FastAPI backend with async job queue + SQLite, SSE live stream, React frontend (Vite + TypeScript, 9 pages).

## Commands

### Python

```bash
source remix-env/bin/activate          # always first
./start.sh setup                       # first-time setup (creates venv, installs deps)
./start.sh                             # full start (API on :8000 + React on :5173)
./start.sh --skip-setup                # fast restart (skip pip/npm install)
./start.sh api                         # API only (for GitHub Pages widget mode)
./start.sh stop                        # kill everything

ruff check scripts/                    # lint (faster than flake8)
ruff format scripts/                   # format
black scripts/                         # formatter (line-length 100)
mypy scripts/                          # typecheck (ignore-missing-imports on)
pre-commit run --all-files             # full pre-commit suite

pytest                                 # all tests
pytest -m "not dj_analysis"            # skip librosa-dependent tests (safe in CI)
pytest -m "not dj_analysis and not integration"  # skip both marks
pytest tests/test_behavioral.py        # behavioral correctness (36 tests)
pytest tests/test_core_modules.py      # core module tests
pytest -x                              # stop on first failure
pytest --looponfail                    # watch mode

# smoke test (API must be running)
bash tests/smoke_e2e.sh

# one-off analysis
python -c "from scripts.core.dj_analysis import analyze_structure; print(analyze_structure('song.mp3'))"
```

### Frontend (from `frontend/`)

```bash
npm install                            # install deps
npm run dev                            # Vite dev server (:5173)
npm run build                          # tsc + vite build
npm run lint                           # eslint
npm run typecheck                      # tsc --noEmit
npm run test                           # vitest
```

### Docker

```bash
docker compose up                      # API + Streamlit UI
docker compose up api                  # API only
```

## Architecture (key files)

```
scripts/
  api/
    main.py           # FastAPI app, lifespan, CORS, request-ID middleware
    jobs.py           # SQLite write-through job store (data/jobs.db)
    routes.py         # Thin aggregator — includes all routers
    routers/          # 11 domain routers
    task_modules/     # Async task functions (run in ThreadPoolExecutor)
    schemas.py        # Pydantic request/response models
  core/
    dj_engine.py      # THE transition renderer — beat-grid lock, stem crossfade, EQ fade
    dj_analysis.py    # SongStructure analysis, transition planning, bar-grid snapping
    mastering.py      # ITU-R BS.1770-4 LUFS + true-peak limiter
    beat_synth.py     # Procedural bridge beats (6 genre presets)
    stems.py          # Demucs stem separation wrapper
    beat_tracker.py   # BeatTracker protocol (librosa / BeatThis backends)
    key_detection.py  # Camelot wheel, TIV scoring, pitch shift, consonance
    music_index.py    # 35-D numpy embedding index (JSON-persisted)
    crate_digger.py   # CLAP 512-D semantic search (lazy singleton)
    energy_profiler.py # Essentia/numpy arousal/valence features
    cue_export.py     # Rekordbox XML + Serato GEOB marker export
    setlist_planner.py # Weighted greedy optimizer (Camelot, BPM, energy arc)
    config.py         # YAML config loader (config.yaml → config.local.yaml → env)
    paths.py          # Canonical path constants (outputs/, models/, data/)

frontend/src/
  pages/              # 9 pages (MissionControl, LibraryAtlas, MixDeck, SetBuilder,
                      #   SignalSearch, AILab, MixVault, Operations, Widget)
  shell/              # AppShell (3-zone grid), LeftRail, RightInspector
  stores/appStore.ts  # Zustand store
  lib/api.ts          # Thin fetch wrapper for all API namespaces
  hooks/useSSE.ts     # SSE connection + job store hydration
  types/index.ts      # TS types mirroring FastAPI schemas
  styles/tokens.css   # Design tokens (no Tailwind)

ai_remixmate_feature_lab/  # Sandboxed feature development area (see its AGENTS.md)
```

Runtime layout (gitignored): `library/` (songs), `outputs/` (renders), `data/` (DB, embeddings), `models/` (Demucs weights).

## Key gotchas

- **uvicorn hot-reload**: `start.sh` restricts `--reload-dir` to `scripts/` only. If you widen it to the project root, downloads/remixes write to `library/`, `outputs/`, `data/` and uvicorn restarts mid-job, killing the ThreadPoolExecutor worker. This causes "stuck at 90%" jobs.

- **Thread pool I/O**: `job_store.submit_job()` runs tasks in a `ThreadPoolExecutor`. Don't do async I/O inside task functions — use `asyncio.run()` if you need it.

- **Job progress normalization**: SSE frames use 0–100. REST `/jobs` returns 0–1. `normalizeJob()` in `api.ts` handles the conversion.

- **Logger API**: `StructuredLogger.warning(msg, extra_dict=None)`, NOT `warning(msg, *args)`. Passing an exception as a positional arg crashes at `for key in extra:`.

- **SSE from worker threads**: Capture the event loop in `lifespan` with `asyncio.get_running_loop()`. Never call `asyncio.get_event_loop()` from a worker — Python 3.12 raises `RuntimeError`.

- **Camelot semitone table**: Canonical source is `CAMELOT` + `NOTE_NAMES` in `key_detection.py`. Don't maintain a separate inline dict — it will drift and produce wrong pitch shifts.

- **JobResponse.status**: REST and SSE emit uppercase (`PENDING | RUNNING | COMPLETED | FAILED | CANCELLED`). Internal `JobStatus` enum uses lowercase. `_norm_status()` bridges them.

- **Config priority**: `config.yaml` → `config.local.yaml` (gitignored) → env vars `REMIXMATE_<SECTION>_<KEY>`.

- **favoritesApi shape**: `GET /favorites` returns `{songs: string[], count: number}`, not a plain array. Frontend must unwrap `.songs`.

- **CLAP auto-download**: `_load_clap_model()` auto-downloads ~300 MB to `~/.cache/` on first call (30–60 s). Set `models.clap_model` in config to a local path to avoid re-downloading.

- **Serato GEOB**: Requires `.mp3` source. Raises `ValueError` for WAV/FLAC. Use rekordbox XML for non-MP3 tracks.

- **Behavioral tests**: `tests/test_behavioral.py` (36 tests) assert *what you hear*, not just dtype/shape. They are the regression guard for correctness fixes. Don't remove them.

## Testing marks

| Mark | Meaning |
|---|---|
| `dj_analysis` | Requires librosa/numba (auto-skipped when probe fails) |
| `integration` | Spins up full FastAPI TestClient |

## API pattern

All mutating endpoints return `{ job_id: string }` (202 Accepted). Poll `/jobs/{id}` or subscribe to SSE for updates. Job store is SQLite write-through with in-memory dict for O(1) reads. Jobs recorded as RUNNING at process start are rolled back to FAILED on restart. Cancel via `DELETE /jobs/{id}`.

## Frontend pattern

New pages:
1. Create `src/pages/MyPage.tsx` + `src/pages/MyPage.css`
2. Add `NavItem` to `shell/LeftRail.tsx`
3. Add `NavDestination` union type to `src/types/index.ts`
4. Add `<Route>` in `shell/AppShell.tsx`

Follow `PageBase.css` layout (`page-base` → `page-base__header` → `page-base__body`). Each page gets its own CSS file.

## Feature lab

`ai_remixmate_feature_lab/` has its own `AGENTS.md` with strict scope rules. Work inside it must not touch root repo files. Do not run package managers from the repository root.

## GitHub Pages

Frontend deploys to GitHub Pages and talks to your locally running backend. Enable: repo Settings → Pages → Source: "GitHub Actions". The `pages.yml` workflow builds and deploys on push to `main`. Set `VITE_API_BASE=http://localhost:8000` in production.
