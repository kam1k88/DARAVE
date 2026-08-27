# DARAVE

**DARAVE** — это ИИ-ассистент диджея, который сводит треки в [Mixxx](https://www.mixxx.org/) по вашим командам из чата. Вы пишете «сделай эхо-переход в конце этого трека» — ИИ строит план сведения (MixPlan), а локальный «companion» превращает его в реальные MIDI-команды и исполняет прямо в Mixxx: крутит фильтры, кроссфейдер, эквалайзер, эффекты, делает бэкспин и т.д., точно по битам.

Работает как облачный сервис с изолированными «комнатами» (каждому диджею/арендатору — свой код комнаты), так и полностью локально у вас на компьютере.

---

## Как это устроено

```
┌─────────────────────────┐         WebSocket          ┌──────────────────────────────┐
│  Компьютер диджея        │  ───────────────────────▶  │  Backend (FastAPI)            │
│                         │  ◀───────────────────────  │                              │
│  Mixxx                  │     MixPlan / control      │  • чат (static/chat.html)     │
│    ▲  MIDI (loopMIDI)   │                            │  • DJAgent (LLM: Gemini/      │
│    │                    │                            │    Ollama)                    │
│  companion_main.py       │                            │  • библиотека / анализ треков │
│    (rtmidi + ws_client)  │                            │  • техники сведения           │
│    телеметрия ◀─────────┼── SysEx/Note в Mixxx      │  • построение и экспорт сетов │
└─────────────────────────┘                            └──────────────────────────────┘
        │                                                        │
        │ запись (WAV) ── upload ───────────────────────────────┘
        └─ телеметрия дек (BPM, позиция, что играет) ── broadcast в чат
```

**Три части:**

1. **Backend** (`server.py`) — FastAPI-сервис. Держит чат, изолирует комнаты (`session.py`), запускает LLM-агента (`agent.py` + `llm_providers.py`), хранит и анализирует библиотеку, строит планы сетов (`mix_strategist.py`), рендерит офлайн-демо техник (`demo_render.py`) и отдаёт готовый сет (`set_export.py`).
2. **Companion** (`companion_main.py`) — процесс на машине диджея рядом с Mixxx. Через виртуальный MIDI-порт (loopMIDI + `python-rtmidi`) шлёт команды в Mixxx, слушает его телеметрию и держит WebSocket с backend'ом. Именно он физически «крутит ручки».
3. **Mixxx** — собственно проигрыватель. DARAVE управляет им через MIDI-мэппинг `DARAVE-Virtual-Controller.midi.xml` (сгенерирован из `mixxx_controls.py`).

> Точный тайминг перехода считает детерминированный код (`techniques.py::build_plan`), а не LLM: модель решает *когда* и *какой техникой*, а механику MIDI — код. Это защищает от «фантазий» модели с таймингами.

---

## Возможности

- **Чат-ассистент диджея** — общается на языке диджея, видит телеметрию дек (какой трек играет, BPM, позиция) и по просьбе исполняет переход.
- **Библиотека техник сведения** — 20+ техник (эхо-срез, бэкспин, фильтр-свип, EQ-переходы и т.д.) с описанием, сложностью и настраиваемыми параметрами.
- **Офлайн-демо техники** — кнопка «🎧 Демо» сводит два ваших трека по логике техники и отдаёт WAV, чтобы услышать переход до живого исполнения.
- **Анализ библиотеки** — `track_analysis.py` меряет BPM/тональность/структуру (дропы, брейкдауны) через librosa; backend сам чинит «заглушки» BPM, из-за которых разъезжаются такты.
- **Построение сета** — `mix_strategist.py` собирает плейлист переходов по всей библиотеке; экспорт в M3U (нативно в Mixxx) и в единый аудиофайл (WAV/MP3).
- **Запись сета** — companion грузит свежую запись Mixxx в комнату, диджей скачивает её из чата.
- **Изолированные комнаты** — каждый арендатор получает криптостойкий код; чаты и библиотеки не пересекаются.

---

## Быстрый старт (Windows, один клик)

1. Положите папку DARAVE куда-нибудь на диск (путь без кириллицы желательно).
2. Скопируйте шаблон и заполните конфиг:
   ```powershell
   Copy-Item darave_config.example.ps1 darave_config.ps1
   # откройте darave_config.ps1 и укажите GeminiApiKey (https://aistudio.google.com)
   # ИЛИ поставьте Ollama и модель:  ollama pull llama3.1
   ```
3. Запустите:
   ```
   start_darave.bat
   ```
   Скрипт сам найдёт loopMIDI, Mixxx и подходящий Python, поднимет backend, companion и откроет чат в браузере.

> **Важно про Python и MIDI на Windows.** Companion'у нужен `python-rtmidi`, а его сборок под Windows нет новее **Python 3.12**. Если ваш основной Python новее — скрипт сам найдёт `py -3.12` (или укажите `PythonExe` в конфиге). Без rtmidi companion поднимается в `mock`-режиме: чат/библиотека/стратегия работают, но команды в Mixxx не уходят.

> **Важно про Gemini из РФ.** Google блокирует Gemini API из России/Беларуси/Ирана/КНР на уровне geo-блока (не биллинга). Варианты: (а) поднять backend на зарубежном VPS; (б) для локальной отладки задать `HTTP_PROXY`/`HTTPS_PROXY` на зарубежный прокси; (в) использовать локальную Ollama — бесплатно и без гео-ограничений.

---

## Ручная установка

### Backend

```bash
pip install -r requirements.txt          # минимум для чата/планов
# опционально, для сканирования библиотеки и демо техник:
pip install -r requirements-analysis.txt

export DARAVE_LLM_PROVIDER=gemini        # или ollama / auto
export GEMINI_API_KEY=...                # для gemini (обязателен)
# export GEMINI_MODEL=gemini-3.6-flash  # опционально
uvicorn server:app --host 0.0.0.0 --port 8765
```

Чат откроется на `http://<host>:8765/?room=<код-комнаты>`.

### Companion (на машине с Mixxx)

```bash
pip install -r requirements.txt          # нужен python-rtmidi (Python ≤ 3.12 на Windows)

python companion_main.py \
    --midi-backend rtmidi --telemetry-backend rtmidi \
    --port-name "DARAVE Virtual Controller" \
    --ws-url ws://localhost:8765 \
    --companion-id my-room \
    --recordings-dir "$Env:USERPROFILE\Documents\Mixxx\Recordings"
```

`--companion-id` — это тот же «код комнаты», что диджей вводит в браузере, чтобы его companion и чат оказались в одной изолированной комнате.

### Mixxx: MIDI-мэппинг и виртуальный порт

1. **loopMIDI**: создайте порт с именем `DARAVE Virtual Controller` (разово, запоминается программой).
2. **Мэппинг**: скопируйте `DARAVE-Virtual-Controller.midi.xml` в папку контроллеров Mixxx
   (`%USERPROFILE%\AppData\Local\Mixxx\controllers\`), затем выберите «DARAVE Virtual Controller» в
   *Настройки → Контроллеры*. Мэппинг сгенерирован из `mixxx_controls.py`; если меняете контролы — пересоберите:
   ```bash
   python make_mixxx_mapping.py --out DARAVE-Virtual-Controller.midi.xml
   ```
3. В Mixxx выберите виртуальный порт loopMIDI как входящий MIDI.

---

## LLM-провайдеры

DARAVE абстрагиет LLM через `llm_providers.py`; выбор — переменная `DARAVE_LLM_PROVIDER`:

| Провайдер | Когда | Требования |
|-----------|-------|------------|
| `gemini`  | облако, бесплатный тир | `GEMINI_API_KEY` (aistudio.google.com); **недоступно из РФ** geo-блоком |
| `ollama`  | локально, бесплатно всегда, без интернета | запущенная Ollama + `ollama pull llama3.1` |
| `auto`    | рекомендуется | есть ключ → Gemini; нет ключа, но Ollama поднята → Ollama |

---

## Облачный деплой (VPS + Docker)

`docker-compose.yml` поднимает backend и Caddy (HTTPS/WSS из коробки через Let's Encrypt). Companion в контейнер **не** кладётся — он всегда живёт у диджея рядом с Mixxx.

```bash
# .env: GEMINI_API_KEY=...   (опционально GEMINI_MODEL)
docker compose up -d --build
```

В `Caddyfile` замените `your-domain.example` на свой домен. Companion подключается к `wss://<ваш-домен>` (или `ws://<ip>:8765` без TLS).

> Для облачного backend'а кнопка «🔍 Сканировать» в веб-UI не поможет (сканирует диск сервера, а не диджея) — библиотеку грузят CLI `track_analysis.py --upload` на машине диджея, либо companion'ом.

---

## Структура репозитория

| Файл/папка | Назначение |
|------------|-----------|
| `server.py` | FastAPI backend: чат, комнаты, библиотека, стратегия, демо, экспорт |
| `companion_main.py` | Процесс у диджея: MIDI ↔ WebSocket ↔ Mixxx |
| `agent.py` | DJAgent — LLM с tool-use (строит MixPlan по просьбе) |
| `llm_providers.py` | Gemini / Ollama — взаимозаменяемые провайдеры |
| `session.py` | Изоляция комнат (история чата, библиотека, план) |
| `techniques.py` | Библиотека техник сведения + `build_plan()` (детерминированный MIDI) |
| `mixplan.py` / `scheduler.py` / `midi_bridge.py` | План перехода → тайминги → MIDI-события |
| `midi_mapping.py` / `mixxx_controls.py` | Контракт контролов и номера Note/CC |
| `make_mixxx_mapping.py` | Генератор `DARAVE-Virtual-Controller.midi.xml` из `mixxx_controls.py` |
| `track_analysis.py` | Офлайн-анализ BPM/тональности/структуры (librosa) |
| `fix_library_bpm.py` | Авто-уточнение «заглушек» BPM и карт барабанов |
| `demo_render.py` / `set_export.py` | Офлайн-сведение в WAV/MP3, экспорт M3U/раскладки сета |
| `live_control.py` / `full_catalog.py` | Каталог всех контролов Mixxx (для чата и веб-пульта) |
| `static/chat.html` | Браузерный интерфейс чата/библиотеки/техник |
| `start_darave.bat` / `start_darave.ps1` / `darave_config.example.ps1` | Однокликовый запуск на Windows |
| `docker-compose.yml` / `Dockerfile` / `Caddyfile` | Облачный деплой backend'а |
| `mixxx/` | Кастомные файлы скинов Mixxx (логотипы LateNight) |
| `requirements*.txt` | Зависимости: `requirements.txt` (база), `-server` (Docker), `-analysis` (librosa/демо), `-stems` (разделение на стемы) |

---

## Безопасность комнат

Код комнаты генерируется `secrets.token_urlsafe(12)` — его нельзя подобрать. Companion и чат сверяются по одному коду, поэтому чужой companion не управляет чужой комнатой. Не публикуйте свой код комнаты.

---

## Лицензия

Уточните у автора. (В репозитории пока нет файла LICENSE.)
