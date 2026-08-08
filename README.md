

# универсальный аудио-движок с гибридным AI-агентом

Это попытка объединить мощь Traktor (живое микширование), гибкость Fruity Loops (глубокий синтез и работа со слоями) и интеллект AI-чата (управление естественным языком) в одном умном сервисе. Инструмент предназначен не только для диджеев, но и для саунд-дизайнеров, продюсеров и всех, кому нужен полный контроль над звуком. В отличие от генеративных моделей вроде SUNO, которые работают с единственным готовым звуковым слоем (waveform) и просто накладывают «умные» эквалайзеры поверх уже сгенерированного трека, DARAVE построен на многоуровневой архитектуре с 71 независимым слоем управления. В основе системы — уникальный агент собственной разработки, который обеспечивает не генерацию звука нейросетью «из коробки», а настоящий физический синтез звука: реальные осцилляторы, огибающие (ADSR), фильтры и LFO, просчитываемые на GPU через PyTorch. Система анализирует треки, разделяет их на инструменты (Demucs), после чего RL-агент (PPO/SAC) и локальный LLM-ассистент (Ollama) управляют синтезом в реальном времени, строя переходы на уровне формы сигнала, а не просто смешивая готовые файлы. Это даёт полный контроль над звуком и качество живого микширования, недостижимое при классической генерации.

<img width="1920" height="1032" alt="image" src="https://github.com/user-attachments/assets/aaa77c12-9ae9-449f-a372-2ff16cd85b8b" />
<img width="1702" height="856" alt="7S6tR29lp6WY7t6_RGOaE4cWy5qaaX34RK1-3jhcn2-83uZn8NOdR0Dl2BcBU1j8wpCts81Ap0i-Xayh0vMxRGl-" src="https://github.com/user-attachments/assets/782e6bef-6e9d-4e33-ae4d-6b44b17c92c1" />
<img width="1885" height="885" alt="Y6NcmNN8aShAP6QdIKvVKfyLSq7gfTIQiYdqKm8TBXfCbnZFQD3rnkdYQwOmK8D3KVBKTvvgnt4ZXXzGLf0QzmPo" src="https://github.com/user-attachments/assets/5b1a9412-7aa6-49b7-be22-f97db70d6821" />
<img width="1569" height="900" alt="_4q94aqeZ7ciFztkycSob2gKvcmcrKP4HKezCTaXqhyb6_j41DJB0WfN2mXLQMVQ8R4PehU-7kJXd7CdaDYiylel" src="https://github.com/user-attachments/assets/ee11dc3e-f4ef-4835-a64a-3755d02c87a4" />

---

## Возможности

| Модуль | Что делает |
|--------|-----------|
| **Stem-aware DJ engine** | Beat-grid lock, phase-lock, stem-aware crossfade, bridge beats, мастеринг до −14 LUFS |
| **RL-агент** (PPO + SAC) | Самообучающийся агент: оптимизация переходов, саунд-дизайн, RLHF через лайк/дизлайк |
| **AI Chat** (Ollama) | Локальный LLM-ассистент с 10 DJ-инструментами: поиск по библиотеке, проверка совместимости, оптимизация сетлиста, запуск ремиксов |
| **GPU DSP** | PyTorch-ускорение: envelope, gate, compressor, flanger, phaser, vinyl_stop, filter_sweep, beat_stamp |
| **20 DJ-техник** | Double Drop, Bass Swap, Filter Sweep, Echo Cut, EQ Roller и др. — выбор по 8 priority rules |
| **Setlist planner** | Greedy + Markov оптимизатор, Camelot wheel, energy arcs |
| **Stem separation** | Demucs (Meta AI) — drums, bass, vocals, other |
| **Semantic search** | CLAP 512-D embeddings + 35-dim numpy vector index |
| **Crate digger** | Мульти-лейбл классификатор, DnB-поджанры |
| **Cue export** | Rekordbox XML + Serato GEOB marker export |
| **Spotify integration** | Импорт плейлистов, поиск, топ-треки |

---

## Архитектура

```
darave_rl/                  # RL-модуль: PPO + SAC агенты
├── env.py                  # DaraveEnv — среда (71-dim obs, 26-dim action)
├── agent.py                # PPOAgent, SACAgent
├── policy_ppo.py           # ActorCritic + GAE + clipped PPO
├── policy_sac.py           # TwinCritic + SACActor + AutoAlpha
├── reward.py               # DJ + producer reward metrics
├── episode.py              # DjTransitionEpisode
├── batch.py                # AudioBatch
├── logger.py               # JSONL-логирование
└── utils.py                # Seed, device, normalization

scripts/
├── api/                    # FastAPI — 15 routers, async job store
│   ├── main.py             # Lifespan, CORS, request-ID middleware
│   ├── jobs.py             # SQLite job persistence
│   ├── routers/            # /library, /download, /stems, /analyze,
│   │                       #  /dj-remix, /setlist, /crates, /jobs,
│   │                       #  /chat, /events, /spotify, /mix/plan
│   └── task_modules/       # Long-running task functions
│
├── core/                   # Audio engine — 58 модулей
│   ├── ai_chat.py          # AI Chat engine (Ollama + 10 DJ tools)
│   ├── gpu.py              # GPU primitives (envelope, gate, compressor, effects, beat)
│   ├── dj_engine.py        # Transition renderer (the heart)
│   ├── dj_analysis.py      # Beat / Section / SongStructure / TransitionPlan
│   ├── transition_intel.py # 20 DJ techniques, 8 priority rules
│   ├── dj_effects.py       # 14 DJ-эффектов
│   ├── dj_techniques.py    # Полный каталог 20 DnB-техник
│   ├── stems.py            # Demucs separation + stem-aware mixer
│   ├── beat_synth.py       # Procedural drum synthesis (6 genre presets)
│   ├── mastering.py        # ITU-R BS.1770-4 LUFS + true-peak limiter
│   ├── key_detection.py    # CQT chroma + Krumhansl-Schmuckler + Camelot
│   ├── setlist_planner.py  # Greedy optimizer, Markov model, energy arcs
│   ├── music_index.py      # 35-dim numpy vector index
│   ├── energy_profiler.py  # RMS + Essentia arousal/valence
│   ├── synth/              # Mini-synthesizer (10 modules)
│   └── ...                 # genre, classify, crate_digger, cue_export, ...

frontend/                    # React + Vite + TypeScript (16 pages)
tests/                       # pytest (25 test files, 370+ tests)
```

---

## AI Chat — DJ-ассистент с инструментами

Встроенный чат-движок на базе **Ollama** (llama3.1:8b) с агентным циклом и 10 DJ-инструментами:

| Инструмент | Описание |
|-----------|----------|
| `list_library` | Список треков в библиотеке |
| `get_track_info` | Детали трека (BPM, ключ, энергия, структура) |
| `check_compatibility` | Совместимость двух треков |
| `find_similar` | Поиск похожих треков |
| `get_track_structure` | Структура трека (intro, verse, chorus...) |
| `list_effects` | Доступные DJ-эффекты |
| `list_techniques` | DJ-техники |
| `recommend_transition` | Рекомендация перехода для пары треков |
| `optimize_setlist` | Оптимизация сетлиста |
| `create_remix` | Запуск ремикса |

### Архитектура AI Chat

```
POST /chat                    ← SSE streaming (text/event-stream)
  └─ AIChatEngine.chat_stream()
       ├─ Ollama API (llama3.1:8b)
       ├─ Tool definitions (10 DJ tools)
       ├─ Agent loop (max 5 rounds tool-calling)
       └─ Tool dispatch → core modules → JSON result
```

**Быстрый старт с Chat:**

```bash
# Убедись что Ollama запущен
ollama serve &

# Отправь сообщение через API
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Какие треки у меня в библиотеке?"}]}'
```

---

## RL-агент: самообучающийся DJ-движок

### Observation space (71 измерение)

| Компонент | Размер | Описание |
|-----------|--------|----------|
| Track A features | 35 | BPM, key one-hot[12], mode, energy, danceability, chroma[12]... |
| Track B features | 35 | Аналогичный вектор для второго трека |
| Position in set | 1 | Позиция в сете [0, 1] |

### Action space (26 измерений)

| Компонент | Размер | Описание |
|-----------|--------|----------|
| Crossfade curve | 1 | 0=linear, 0.33=exponential, 0.66=equal_power |
| EQ HP start/end | 2 | Частота HP-фильтра (80–800 Hz) |
| Bass swap bar | 1 | Бар замены баса |
| Effect type | 1 | none/filter/echo/loop/wobble/slicer/flanger/... |
| Effect depth | 1 | Глубина эффекта [0, 1] |
| Bridge gain | 1 | Громкость мостового бита [0, 1] |
| SynthParams | 19 | osc, filter, ADSR, LFO, compressor, delay, clipper |

### Алгоритмы

- **PPO** (Proximal Policy Optimization) — on-policy, clipped surrogate objective, GAE, entropy bonus
- **SAC** (Soft Actor-Critic) — off-policy, twin Q-critics, auto-tuning alpha

### Метрики награды

**DJ-метрики** (energy=0.25, spectral=0.20, phase=0.20, transient=0.15, user=0.20):
- `energy_continuity` — плавность RMS-энергии
- `spectral_smoothness` — спектральный flux
- `phase_coherence` — crest factor
- `transient_clarity` — attack-to-sustain ratio
- `user_score_to_reward` — пользовательская оценка

**Продюсерские метрики** (harmonic=0.25, envelope=0.25, balance=0.25, compression=0.25):
- `harmonic_similarity` — расстояние по Camelot wheel
- `envelope_similarity` — корреляция RMS-огибающей
- `spectral_balance` — spectral flatness
- `compression_feel` — динамический диапазон

---

## GPU-accelerated DSP

| GPU Primitive | Назначение | Стратегия |
|---|---|---|
| `gpu_envelope_follower()` | One-pole envelope (limiter, gate, compressor) | CPU numba |
| `gpu_gate()` | Noise gate | Batch hop-RMS + GPU gain |
| `gpu_compressor()` | Soft-knee compressor | Batch hop-RMS + vectorized soft-knee |
| `gpu_vinyl_stop()` | Turntable power-down | Precompute positions → `gather` |
| `gpu_flanger()` | Comb filter sweep | `scatter_add` delay line |
| `gpu_phaser()` | All-pass filter sweep | Batched allpass |
| `gpu_filter_sweep()` | Time-varying LP/HP filter | STFT-domain gain |
| `gpu_beat_stamp()` | Procedural beat synthesis | `scatter_add` |

---

## Quick start

### Linux / macOS

```bash
git clone https://github.com/kam1k88/DARAVE.git
cd DARAVE
python -m venv remix-env && source remix-env/bin/activate
./start.sh          # installs deps + starts API + React UI
```

### Windows

```bash
git clone https://github.com/kam1k88/DARAVE.git
cd DARAVE
python -m venv remix-env && remix-env\Scripts\activate
start.bat           # или RemixMate.bat на рабочем столе
```

### Что запускается

| Сервис | URL |
|--------|-----|
| **React UI** | http://localhost:5173 |
| **API docs** | http://localhost:8000/docs |
| **AI Chat** | http://localhost:5173 → Right Inspector → Chat |
| **Ollama** | http://localhost:11501 |

### Другие команды

```bash
./start.sh --skip-setup    # быстрый перезапуск
./start.sh api             # только API
./start.sh stop            # остановить всё
```

### Docker

```bash
docker compose up
```

---

## Frontend — 16 страниц

| Страница | Назначение |
|----------|-----------|
| MissionControl | Главный дашборд |
| LibraryAtlas | Библиотека треков |
| MixDeck | DJ-микшер |
| SetBuilder | Конструктор сета |
| MixPlanPage | Планирование миксов |
| QuickMix | Быстрый микс |
| MixVault | Архив миксов |
| SignalSearch | Поиск по сигналу |
| AILab | AI-лаборатория |
| Strategy | Стратегия микширования |
| Solo / SoloMode | Соло-режим |
| Operations | Операции |
| Outputs | Экспорт |
| Widget | Виджет |

---

## Tech stack

| Layer | Stack |
|---|---|
| Audio analysis | librosa, numpy, scipy, numba |
| GPU DSP | PyTorch: envelope, gate, compressor, flanger, phaser, vinyl_stop, filter_sweep, beat_stamp |
| Stem separation | Demucs (Meta AI), PyTorch |
| Mastering | ITU-R BS.1770-4 LUFS, true-peak limiter |
| Mini-synth | Custom DSP: oscillator, filter, ADSR, LFO, compressor, delay |
| Genre classification | Multi-label classifier, DnB substyles |
| Semantic search | 35-dim numpy vector index, CLAP 512-D |
| Setlist planning | Greedy + Markov optimizer, Camelot wheel |
| **RL Agent** | **PPO + SAC (pure PyTorch), custom reward metrics** |
| **AI Chat** | **Ollama (llama3.1:8b), 10 DJ tools, SSE streaming, agent loop** |
| Backend | FastAPI, Uvicorn, Pydantic v2, SQLite |
| Frontend | React + Vite + TypeScript (16 pages) |
| Testing | pytest (370+ tests), vitest |
| Packaging | pyproject.toml (PEP 517), Docker |

Python 3.10+. Apple Silicon, NVIDIA, или CPU — GPU auto-detected.

---

## Tests

```bash
pytest tests/ -v                                # все тесты
pytest tests/ -x                                # stop on first failure
pytest tests/test_darave_rl.py -v               # RL-тесты
pytest tests/test_gpu_dynamics.py -v            # GPU dynamics
pytest tests/test_gpu_dj_effects.py -v          # GPU DJ effects
pytest tests/test_gpu_filter_beat.py -v         # GPU filter + beat
pytest tests/test_behavioral.py                 # behavioral correctness (36 tests)
pytest -m "not dj_analysis"                     # skip librosa-dependent tests
```

### Frontend

```bash
cd frontend
npm run typecheck    # tsc --noEmit
npm run lint         # eslint
npm run build        # production build
npm run test         # vitest
```

---

## Configuration

Приоритет (высший → низший):
1. Переменные окружения `REMIXMATE_<SECTION>_<KEY>`
2. `config.local.yaml` (gitignored, пользовательские настройки)
3. `config.yaml` (проектные дефолты)

```yaml
# config.yaml (пример)
ollama:
  host: "http://localhost:11501"
  model: "llama3.1:8b"
  timeout_sec: 120
  max_tool_rounds: 5

audio:
  sample_rate: 44100
  target_lufs: -14.0

separation:
  model: "htdemucs"
  device: "auto"
```

---

## License

The FYPM License

## Author

**Аркадий Максимов** · [github.com/kam1k88](https://github.com/kam1k88) · kam1k88@gmail.com
