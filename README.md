
# DARAVE — Перевернутая парадигма DAW

## 🧠 Концепция: от результата  к процессу

Классические DAW работают по линейному принципу **«Сырьё → Обработка → Результат»**. 
Продюсер берёт семплы, накладывает десятки плагинов, выстраивает сложные цепочки эффектов, тратит годы на изучение теории музыки и акустики — и только в самом конце получает готовый микс.

**DARAVE ломает этот порядок.**

Мы внедряем **реверсивную (обратную) парадигму**: 
> Точкой входа становится не семпл, а готовый, финальный продукт.

Вы даёте программе уже сведённый трек или эталонный микс. DARAVE выступает в роли аудио-детектива: она проводит **полноценный реверс-инжиниринг** аудиопотока, выявляет все использованные инструменты, цепочки эффектов, частотную динамику, ритмические паттерны и структурные приёмы, которые привели к такому звучанию.

---

## 🤖 Главная фича: Уникальный RL-агент (Reinforcement Learning Agent)

Сердце DARAVE — это **самообучающийся агент с подкреплением**. Это не статичный алгоритм и не набор заранее заданных эвристик.

Агент работает в связке с **Feature Store** (хранилищем аудиопризнаков) и выполняет три ключевые задачи:

1. **Анализ в реальном времени** (или пакетно) — выявляет DJ-техники, микшерные приёмы, спецэффекты и структурные переходы на уровне спектра и транзиентов.
2. **Персонализация через RLHF** — агент учится на вашей субъективной оценке. Вы ставите лайк или дизлайк предложенным паттернам, корректируете результаты, и агент пересчитывает политику (policy) своего поведения. С каждой итерацией он всё точнее понимает ваш вкус и продакшн-стиль.
3. **Генерация миксов/сценариев** — на основе извлечённых признаков и накопленного пользовательского опыта агент автоматически строит оптимальные цепочки действий (например, предлагает конкретные комбинации плагинов, порядок обработки и точки переходов), которые приведут к созданию вашего идеального звука.

---

## 🔄 Как это работает (User Flow)

| Шаг | Действие |
|-----|----------|
| **1. Загрузка** | Пользователь загружает любой готовый аудиофайл (референс, мастер-запись, собственный сведённый трек). |
| **2. Реверс-инжиниринг** | Система разлагает аудио на сотни признаков (BPM, ключ, частотный баланс, огибающая, динамика, стерео-панорама, эффекты). Признаки сохраняются в **Feature Store** для мгновенного доступа в будущем. |
| **3. Интерпретация агентом** | RL-агент сопоставляет извлечённую «цифровую ДНК» трека с историей ваших предыдущих оценок и предлагает интерпретацию: какие инструменты и техники были задействованы. |
| **4. Обратная связь** | Вы оцениваете выводы агента. На основе вашей оценки агент корректирует свою внутреннюю модель (обновление Q-функции или градиента политики). |
| **5. Создание микса** | Агент генерирует готовые рекомендации по построению вашего собственного микса или автоматически собирает новую аранжировку на основе найденных паттернов. |

---

## ⚡ Почему это переворот в индустрии

- **Никакого порога входа** — не нужно музыкальное образование, чтобы анализировать профессионалов.
- **Сокращение времени** — вы перестаёте гадать, какие 10 плагинов стоят в цепочке у любимого саунд-дизайнера.
- **Постоянная адаптация** — ваш личный RL-агент становится вашим цифровым ассистентом, который идеально знает ваши предпочтения быстрее, чем вы сами.
- **Автономность** — после достаточного обучения агент способен самостоятельно предлагать миксы без вашего участия (автономный режим).

---

## 🏗 Техническая архитектура (Core)

- **Аудио-движок**: Работает на основе собственных алгоритмов FFT, фильтрации и детекции транзиентов (без внешних зависимостей, где это критично).
- **Feature Store**: Векторное хранилище признаков, оптимизированное для быстрого сравнения треков и поиска аналогов.
- **RL-алгоритм**: Гибридный подход (Deep Q-Learning + Policy Gradient), адаптированный под временные ряды аудиоданных.
- **Интерфейс**: Интуитивно понятный дашборд, где вы видите «вскрытую» структуру трека и управляете действиями агента через простые лайки/дизлайки.

---

**DARAVE** — это не просто очередной плагин. Это **смена парадигмы**: 
> Мы не учим вас пользоваться инструментами. Мы учим инструменты понимать ваши музыкальные цели через анализ того, что уже звучит великолепно.

---

# DARAVE

**DAW paradigm in reverse.** Traditional music production takes samples, runs them through tools that require years of study, and produces a finished track. DARAVE flips this: you give it finished music, and the system decomposes, analyzes, and recombines it — learning from human judgment (RLHF) to find the right patterns.

---

## The paradigm shift

### How it works classically

```
Samples (1, 2, or more)
    → Human operator
    → DAW tools (mixing, mastering, production)
    → Music education required
    → Finished track
```

A producer loads samples into a DAW, learns EQ, compression, sidechain, harmonic mixing, arrangement theory — then manually sculpts a finished piece. The barrier to entry is steep. The tool is passive; the intelligence is entirely human.

### How DARAVE works

```
Finished track (any genre, any complexity)
    → DARAVE analysis engine
    → Decomposes into instruments, patterns, structure
    → Recommends transitions, effects, arrangements
    → Learns from human feedback (RLHF)
    → Remix / mashup / set / mastered output
```

The user provides the **end product**. DARAVE identifies what's in it — tempo, key, energy profile, bar structure, stem separation, genre classification — and then finds the right musical patterns for transitions, sequencing, and effects. Human preference feedback (RLHF) refines the recommendations over time.

No music theory prerequisite. No DAW learning curve. The intelligence is in the system.

---

## What it does

### Stem-aware DJ engine

Give it two tracks. DARAVE analyzes both for tempo, key, energy, and bar structure. Picks exit/entry cue points at musical phrase boundaries. Time-stretches to match BPM. Phase-locks the downbeat at the sample level. Renders a stem-aware crossfade — drums, bass, and vocals fade independently, the way a real DJ would do it. Optionally synthesizes bridge beats from scratch with numpy. Masters the output to ITU-R BS.1770-4 (−14 LUFS, true-peak limited).

### Intelligent transitions

20 DJ techniques (Double Drop, Bass Swap, Filter Sweep, Echo Cut, EQ Roller, etc.) are selected by 8 priority rules based on BPM type, energy level, Camelot key compatibility, and energy delta. Each transition gets EQ strategy, crossfade shape, and effect recommendations.

### Setlist intelligence

A weighted greedy optimizer with Camelot wheel harmony, BPM continuity, energy arc shaping, and a Markov model trained on historical setlist patterns. Wordplay layer uses Genius API to find where one song's closing lyrics share phrases with the next song's opening.

### Genre classification v2

Multi-label classification: top-5 genres with confidence scores. DnB substyles: liquid, jump_up, techstep, neurofunk, darkstep, jungle, minimal, dancefloor. Russian-language descriptions with tags. Genre data stored as `{genres, tags, description}` in meta.json.

### Music library

681+ tracks in the local library. Semantic search via 35-dimensional numpy vector index. Compatibility scoring across BPM, key, energy, timbre, and vocal clash. CLAP-based 512-D semantic search. Serato/Rekordbox export.

---

## Architecture

```
scripts/
├── api/                    # FastAPI — 12 routers, 7 task modules, async job store
│   ├── main.py             # Lifespan, CORS, request-ID middleware
│   ├── jobs.py             # SQLite job persistence, ETA, cancel/retry
│   ├── routers/            # /library, /download, /stems, /analyze,
│   │                       #  /dj-remix, /setlist, /crates, /jobs
│   └── task_modules/       # Long-running task functions
│
├── core/                   # Audio engine — 41 modules
│   ├── dj_engine.py        # Transition renderer (the heart)
│   ├── dj_analysis.py      # Beat / Section / SongStructure / TransitionPlan
│   ├── transition_intel.py # 20 DJ techniques, 8 priority rules
│   ├── stems.py            # Demucs separation + stem-aware mixer
│   ├── beat_synth.py       # Procedural drum synthesis + Strudel export
│   ├── mastering.py        # ITU-R BS.1770-4 LUFS + true-peak limiter
│   ├── key_detection.py    # Krumhansl-Schmuckler + camelot_modulation()
│   ├── genre.py            # Multi-label genre classifier v2
│   ├── setlist_planner.py  # Greedy optimizer, Markov model, Exportify CSV
│   ├── music_index.py      # 35-dim numpy vector index for semantic search
│   ├── crate_digger.py     # CLAP 512-D semantic search
│   └── ...                 # energy_profiler, cue_export, audio_enhance, ...
│
└── ui/
    └── app.py              # Streamlit dashboard

frontend/                    # React + Vite + TypeScript (9 pages)
tests/                       # pytest + librosa probe guard + e2e suite
docs/                        # Architecture notes, DJ theory, feature gaps
```

Runtime layout (gitignored): `library/` for songs, `outputs/` for mixes, `data/` for SQLite/embeddings, `models/` for Demucs weights.

---

## Quick start

```bash
git clone https://github.com/Chunduri-Aditya/ai-remixmate.git
cd ai-remixmate
python -m venv remix-env && source remix-env/bin/activate
./start.sh          # installs deps + starts API + React UI
```

Then open:
- **React UI** → http://localhost:5173
- **API docs** → http://localhost:8000/docs
- **Streamlit UI** → `./start.sh ui` → http://localhost:8501

Docker: `docker compose up`

---

## Tech stack

| Layer | Stack |
|---|---|
| Audio analysis | librosa, numpy, scipy |
| Stem separation | Demucs (Meta AI), PyTorch (MPS / CUDA / CPU) |
| Mastering | ITU-R BS.1770-4 LUFS, true-peak limiter |
| Genre classification | Multi-label classifier, DnB substyles, RU descriptions |
| Semantic search | 35-dim numpy vector index, CLAP 512-D, weighted cosine |
| Setlist planning | Greedy + Markov optimizer, Camelot wheel, spectral flux |
| Backend | FastAPI, Uvicorn, Pydantic v2, SQLite |
| Frontend | React + Vite + TypeScript (9 pages) |
| Testing | pytest (226+ tests), vitest |
| Packaging | pyproject.toml (PEP 517), Docker |

Python 3.10+. Apple Silicon, NVIDIA, or CPU — GPU auto-detected.

---

## Try it

```bash
# Download + stem-split + analyze
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"query": "Anyma Voices In My Head", "separate": true}'

# Render a transition
curl -X POST http://localhost:8000/dj-remix \
  -H "Content-Type: application/json" \
  -d '{
    "song_a": "Anyma - Voices In My Head",
    "song_b": "Dom Dolla - Define",
    "transition_bars": 16
  }'

# Check job status
curl http://localhost:8000/jobs/{job_id}
```

Interactive Swagger UI at http://localhost:8000/docs for everything else.

---

## Feature gap documentation

See [docs/FEATURE_GAP.md](docs/FEATURE_GAP.md) for what the backend computes vs. what the frontend renders.

---

## Tests

```bash
pytest tests/ -v
pytest tests/ -x                    # stop on first failure
pytest -m "not dj_analysis"         # skip librosa-dependent tests
```

---

## Configuration

`config.yaml` → `config.local.yaml` (gitignored) → env vars `REMIXMATE_<SECTION>_<KEY>`.

---

## License

MIT — see [LICENSE](LICENSE).

## Author

**Aditya Chunduri** · [github.com/Chunduri-Aditya](https://github.com/Chunduri-Aditya) · chunduri@usc.edu
