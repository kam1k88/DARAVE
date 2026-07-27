# DARAVE — Перевёрнутая парадигма DAW

## Концепция: от результата к процессу

Классические DAW работают по линейному принципу **«Сырьё → Обработка → Результат»**.
Продюсер берёт семплы, накладывает десятки плагинов, выстраивает сложные цепочки эффектов, тратит годы на изучение теории музыки и акустики — и только в самом конце получает готовый микс.

**DARAVE ломает этот порядок.**

Мы внедряем **реверсивную (обратную) парадигму**:
> Точкой входа становится не семпл, а готовый, финальный продукт.

Вы даёте программе уже сведённый трек или эталонный микс. DARAVE выступает в роли аудио-детектива: она проводит **полноценный реверс-инжиниринг** аудиопотока, выявляет все использованные инструменты, цепочки эффектов, частотную динамику, ритмические паттерны и структурные приёмы, которые привели к такому звучанию.

---

## RL-агент: самообучающийся DJ-движок

Сердце DARAVE — **`darave_rl`**: полноценный RL-модуль с агентами PPO и SAC на чистом PyTorch.

### Что делает RL-агент

| Задача | Описание |
|--------|----------|
| **Оптимизация переходов** | Агент выбирает параметры DJ-перехода (crossfade curve, EQ sweep, bass swap, эффекты) |
| **Саунд-дизайн** | Управляет параметрами мини-синтезатора (oscillator, filter, envelope, LFO, compressor, delay) |
| **RLHF** | Учится на пользовательских оценках (лайк/дизлайк) через reward shaping |
| **Автономный микс** | После обучения генерирует оптимальные переходы без участия пользователя |

### Архитектура RL-модуля

```
darave_rl/
├── env.py              # DaraveEnv — RL среда (71-dim obs, 26-dim action)
├── agent.py            # PPOAgent, SACAgent — обёртки с save/load
├── policy_ppo.py       # ActorCritic (shared backbone + Gaussian) + GAE + clipped PPO
├── policy_sac.py       # TwinCritic + SACActor + AutoAlpha (entropy tuning)
├── reward.py           # DJ-метрики + продюсерские метрики → [-1, 1]
├── episode.py          # DjTransitionEpisode dataclass
├── batch.py            # AudioBatch data container
├── logger.py           # JSONL-логирование эпизодов и обучения
└── utils.py            # Seed, device, RunningStats, soft_update
```

### Observation space (71 измерения)

| Компонент | Размер | Описание |
|-----------|--------|----------|
| Track A features | 35 | BPM, key one-hot[12], mode, energy, danceability, chroma[12]... |
| Track B features | 35 | Аналогичный вектор для второго трека |
| Position in set | 1 | Позиция в сете [0, 1] |

### Action space (26 измерений)

| Компонент | Размер | Описание |
|-----------|--------|----------|
| Crossfade curve | 1 | 0=linear, 0.33=exponential, 0.66=equal_power |
| EQ HP start/end | 2 | Частота HP-фильтра на входе (80–800 Hz) |
| Bass swap bar | 1 | Бар замены баса (0–transition_bars) |
| Effect type | 1 | none/filter/echo/loop/wobble/slicer/flanger/... |
| Effect depth | 1 | Глубина эффекта [0, 1] |
| Bridge gain | 1 | Громкость мостового бита [0, 1] |
| SynthParams | 19 | osc, filter, ADSR, LFO, compressor, delay, clipper |

### Алгоритмы

- **PPO** (Proximal Policy Optimization) — on-policy, clipped surrogate objective, GAE, entropy bonus
- **SAC** (Soft Actor-Critic) — off-policy, twin Q-critics, auto-tuning alpha (entropy coefficient)

### Метрики награды

**DJ-метрики** (веса: energy=0.25, spectral=0.20, phase=0.20, transient=0.15, user=0.20):
- `energy_continuity` — плавность RMS-энергии
- `spectral_smoothness` — спектральный flux между сегментами
- `phase_coherence` — crest factor (качество фазы)
- `transient_clarity` — attack-to-sustain ratio
- `user_score_to_reward` — маппинг пользовательской оценки

**Продюсерские метрики** (веса: harmonic=0.25, envelope=0.25, balance=0.25, compression=0.25):
- `harmonic_similarity` — расстояние по Camelot wheel
- `envelope_similarity` — корреляция RMS-огибающей
- `spectral_balance` — spectral flatness
- `compression_feel` — динамический диапазон

### Быстрый старт

```python
from darave_rl import DaraveEnv, PPOAgent, SACAgent

# Создать среду
env = DaraveEnv(tracks_db=my_tracks, reward_mode="dj")

# Создать агента
agent = PPOAgent(obs_dim=env.obs_dim, action_dim=env.action_dim)

# Цикл обучения
obs = env.reset()
for _ in range(1000):
    action = agent.select_action(obs)
    next_obs, reward, done, info = env.step(action)
    agent.store_transition(obs, action, reward, done)
    obs = next_obs

metrics = agent.update()
agent.save("checkpoints/ppo_step1000.pt")
```

---

## Как это работает (User Flow)

| Шаг | Действие |
|-----|----------|
| **1. Загрузка** | Пользователь загружает аудиофайлы (референс, мастер-запись, собственный микс) |
| **2. Анализ** | Система извлекает признаки: BPM, ключ, энергия, спектр, транзиенты. Сохраняет в Feature Store |
| **3. RL-агент** | Агент анализирует пару треков и предлагает оптимальные параметры перехода |
| **4. Рендер** | DARAVE рендерит переход: time-stretch, phase-lock, stem-aware crossfade, bridge beats |
| **5. Обратная связь** | Пользователь оценивает результат → обновление политики агента |

---

## Stem-aware DJ engine

Два трека → анализ BPM, ключа, энергии, бар-структуры → выбор точек выхода/входа на границах фраз → time-stretch до совпадения BPM → phase-lock даунбита на уровне сэмплов → stem-aware crossfade (drums, bass, vocals.fade independently) → синтез bridge beats → мастеринг до -14 LUFS.

---

## GPU-accelerated DSP

Все CPU-bound DSP-примитивы ускорены через PyTorch GPU (auto-detect CUDA/MPS/CPU):

| GPU Primitive | Назначение | Стратегия |
|---|---|---|
| `gpu_envelope_follower()` | One-pole envelope (limiter, gate, compressor) | CPU numba (sequential) |
| `gpu_gate()` | Noise gate | Batch hop-RMS via `unfold` + GPU gain |
| `gpu_compressor()` | Soft-knee compressor | Batch hop-RMS + vectorized soft-knee |
| `gpu_vinyl_stop()` | Turntable power-down | Precompute read positions → `gather` |
| `gpu_flanger()` | Comb filter sweep | `scatter_add` delay line + chunked feedback |
| `gpu_phaser()` | All-pass filter sweep | Batched allpass (poles independent) |
| `gpu_filter_sweep()` | Time-varying LP/HP filter | STFT-domain gain curve |
| `gpu_beat_stamp()` | Procedural beat synthesis | `scatter_add` for batch writes |

Интегрировано в: `mastering.py`, `audio_enhance.py`, `dj_effects.py`, `dj_engine.py`, `beat_synth.py`.

---

## Intelligent transitions

20 DJ-техник (Double Drop, Bass Swap, Filter Sweep, Echo Cut, EQ Roller и др.) выбираются по 8 priority rules на основе BPM type, energy level, Camelot key compatibility и energy delta.

---

## Architecture

```
darave_rl/                  # ⭐ RL-модуль: PPO + SAC агенты
├── env.py                  # DaraveEnv — среда для DJ-переходов
├── agent.py                # PPOAgent, SACAgent
├── policy_ppo.py           # ActorCritic + GAE + PPO update
├── policy_sac.py           # TwinCritic + SACActor + AutoAlpha
├── reward.py               # DJ + producer reward metrics
├── episode.py              # DjTransitionEpisode dataclass
├── batch.py                # AudioBatch container
├── logger.py               # JSONL-логирование
└── utils.py                # Seed, device, normalization

scripts/
├── api/                    # FastAPI — 12 routers, async job store
│   ├── main.py             # Lifespan, CORS, request-ID middleware
│   ├── jobs.py             # SQLite job persistence
│   ├── routers/            # /library, /download, /stems, /analyze,
│   │                       #  /dj-remix, /setlist, /crates, /jobs
│   └── task_modules/       # Long-running task functions
│
├── core/                   # Audio engine — 50 модулей
│   ├── gpu.py              # ⭐ GPU primitives (envelope, gate, compressor, effects, beat)
│   ├── dj_engine.py        # Transition renderer (the heart)
│   ├── dj_analysis.py      # Beat / Section / SongStructure / TransitionPlan
│   ├── transition_intel.py # 20 DJ techniques, 8 priority rules
│   ├── dj_effects.py       # 14 DJ-эффектов (loop, echo, wobble, slicer, ...)
│   ├── dj_techniques.py    # Полный каталог 20 DnB-техник
│   ├── stems.py            # Demucs separation + stem-aware mixer
│   ├── beat_synth.py       # Procedural drum synthesis (6 genre presets)
│   ├── mastering.py        # ITU-R BS.1770-4 LUFS + true-peak limiter
│   ├── key_detection.py    # CQT chroma + Krumhansl-Schmuckler + Camelot
│   ├── setlist_planner.py  # Greedy optimizer, Markov model, energy arcs
│   ├── music_index.py      # 35-dim numpy vector index
│   ├── energy_profiler.py  # RMS + Essentia arousal/valence
│   ├── synth/              # Mini-synthesizer (Oscillator, Filter, ADSR, ...)
│   └── ...                 # genre, classify, crate_digger, cue_export, ...

frontend/                    # React + Vite + TypeScript (9 pages)
tests/                       # pytest + behavioral tests + RL tests
```

Runtime layout (gitignored): `library/` (songs), `outputs/` (mixes), `data/` (SQLite/embeddings), `models/` (Demucs weights).

---

## Quick start

```bash
git clone https://github.com/kam1k88/DARAVE.git
cd DARAVE
python -m venv remix-env && source remix-env/bin/activate
./start.sh          # installs deps + starts API + React UI
```

Then:
- **React UI** → http://localhost:5173
- **API docs** → http://localhost:8000/docs
- **Streamlit UI** → `./start.sh ui` → http://localhost:8501

### Windows Desktop Shortcuts

| Shortcut | Description |
|----------|-------------|
| `DARAVE_Start.bat` | Quick start: API + React UI |
| `DARAVE_Debug.bat` | Debug mode: debugpy on :5678 + auto-run tests |

### VS Code Debugging

1. Run `DARAVE_Debug.bat` (starts API with debugpy on :5678)
2. In VS Code: Run → "Attach to DARAVE API (debugpy)"
3. Set breakpoints, step through code

Docker: `docker compose up`

---

## Tech stack

| Layer | Stack |
|---|---|
| Audio analysis | librosa, numpy, scipy, numba |
| **GPU DSP** | **PyTorch: envelope, gate, compressor, flanger, phaser, vinyl_stop, filter_sweep, beat_stamp** |
| Stem separation | Demucs (Meta AI), PyTorch |
| Mastering | ITU-R BS.1770-4 LUFS, true-peak limiter |
| Mini-synth | Custom DSP: oscillator, filter, ADSR, LFO, compressor, delay |
| Genre classification | Multi-label classifier, DnB substyles |
| Semantic search | 35-dim numpy vector index, CLAP 512-D |
| Setlist planning | Greedy + Markov optimizer, Camelot wheel |
| **RL Agent** | **PPO + SAC (pure PyTorch), custom reward metrics** |
| Backend | FastAPI, Uvicorn, Pydantic v2, SQLite |
| Frontend | React + Vite + TypeScript (9 pages) |
| Testing | pytest (370+ tests), vitest |
| Packaging | pyproject.toml (PEP 517), Docker |

Python 3.10+. Apple Silicon, NVIDIA, or CPU — GPU auto-detected.

---

## Tests

```bash
pytest tests/ -v
pytest tests/ -x                              # stop on first failure
pytest tests/test_darave_rl.py -v             # RL-тесты (45 tests)
pytest tests/test_gpu_dynamics.py -v          # GPU dynamics (23 tests)
pytest tests/test_gpu_dj_effects.py -v        # GPU DJ effects (27 tests)
pytest tests/test_gpu_filter_beat.py -v       # GPU filter + beat (20 tests)
pytest -m "not dj_analysis"                   # skip librosa-dependent tests
```

---

## Configuration

`config.yaml` → `config.local.yaml` (gitignored) → env vars `REMIXMATE_<SECTION>_<KEY>`.

---

## License

MIT — see [LICENSE](LICENSE).

## Author

**Аркадий Максимов** · [github.com/kam1k88](https://github.com/kam1k88) · kam1k88@gmail.com
