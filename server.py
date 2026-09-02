"""
FastAPI backend — облачный сервис DARAVE.

Мультисессионность: каждый арендатор получает свой session_id ("код
комнаты"), который связывает его companion (дома, у пульта рядом с Mixxx)
с его же браузерным чатом. Комнаты полностью изолированы друг от друга —
см. session.py.

Эндпоинты:
  GET  /                            — браузерная страница чата (static/chat.html)
  WS   /ws/companion/{session_id}   — протокол companion: hello/telemetry,
                                       backend -> companion: mixplan / command
  WS   /ws/chat/{session_id}        — браузер: {"type": "user_message", "text": "..."} ->
                                       агент отвечает {"type": "agent_reply", "text": "..."}
                                       всем чат-клиентам этой комнаты и, если решил
                                       действовать, шлёт MixPlan companion'у той же
                                       комнаты; {"type": "command", "action": "..."} —
                                       разовые команды ("replay_last_mix", "recording_toggle")
  POST /api/rooms                   — сгенерировать случайный код новой комнаты
  POST /api/rooms/{session_id}/recording — companion грузит сюда файл записи
  GET  /api/rooms/{session_id}/recording — диджей скачивает запись из чата
  POST /api/rooms/{session_id}/library   — companion грузит результаты
                                            track_analysis.py --upload
  GET  /api/rooms/{session_id}/library   — браузер читает библиотеку комнаты
  POST /api/rooms/{session_id}/library/scan        — кнопка "Сканировать" в
                                            UI: запускает track_analysis.py
                                            локально на машине backend'а
  GET  /api/rooms/{session_id}/library/scan/status — статус/прогресс скана
  POST /api/rooms/{session_id}/strategy  — построить план сета (mix_strategist.py)
                                            по всей/части загруженной библиотеки
  POST /api/rooms/{session_id}/strategy/execute — исполнить ОДИН переход из
                                            последнего построенного плана прямо сейчас
  GET  /api/techniques              — библиотека техник сведения (techniques.py)

Запуск:
    export DARAVE_LLM_PROVIDER=gemini    # или "ollama" — см. llm_providers.py
    # для gemini:
    export GEMINI_API_KEY=...           # обязателен — без него DJAgent не поднимется
                                         # (получить бесплатно: https://aistudio.google.com)
    export GEMINI_MODEL=gemini-3.6-flash # опционально
    # для ollama (локальная модель, без geo-блоков, бесплатно):
    #   ollama pull llama3.1  &&  export DARAVE_LLM_PROVIDER=ollama
    uvicorn server:app --host 0.0.0.0 --port 8765

Companion подключается на ws://<host>:8765/ws/companion/{session_id}
(session_id = --companion-id, см. companion_main.py). Диджей открывает
http://<host>:8765/?room={session_id} в браузере.

ВАЖНО про Gemini из РФ: Google не пускает Gemini API/AI Studio из России
(и Беларуси, Ирана, КНР и т.д. — geo-блок на уровне аккаунта/IP, не биллинг).
Если backend крутится на машине с российским IP — вызовы будут падать вне
зависимости от денег на счету. Два выхода: (1) поднять backend на зарубежном
VPS (см. README "Деплой") — тогда обращения к Gemini идут с его IP; (2) для
локальной отладки — задать HTTPS_PROXY/HTTP_PROXY на зарубежный прокси/VPN
перед запуском, google-genai ходит через httpx и уважает эти переменные.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

try:
    import demo_render
except ImportError:
    # numpy/scipy ещё не поставлены (requirements-analysis.txt) — backend
    # должен стартовать и без них, просто кнопка "Демо" вернёт понятную
    # ошибку вместо падения ВСЕГО backend'а при импорте.
    demo_render = None
import full_catalog
import live_control
import mix_strategist
import set_export
import persistence
from track_analysis import load_library_from_db

try:
    import fix_library_bpm
except Exception:  # без numpy/librosa — просто нет автопочинки темпа
    fix_library_bpm = None
from llm_providers import make_provider
from session import SessionManager
from techniques import TECHNIQUES, build_plan

# Раньше все print() уходили только в консоль отдельного окна backend'а —
# чтобы разобрать ошибку, диджею приходилось делать скриншот этого окна
# (или терять вывод вовсе, если окно уже закрыто). Теперь дублируем в файл
# рядом со скриптом — можно просто прислать/прочитать darave_backend.log.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "darave_backend.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("darave")


# Отпечаток запущенного кода. Это не украшательство: backend не умеет
# перезагружать себя, и если старый процесс продолжает держать порт, новый
# молча не поднимается — launcher видит ответ СТАРОГО и рапортует «готово».
# Правки при этом не доезжают, а выглядит это как «я же перезапустил, а
# ничего не изменилось». Поэтому процесс сам докладывает, какой код в нём
# живёт, и launcher сверяет это с датами файлов на диске.
_STARTED_AT = time.time()
_CODE_FILES = ("server.py", "demo_render.py", "set_export.py",
               "mix_strategist.py", "techniques.py", "beatgrid.py")


def _code_mtime() -> float:
    here = Path(__file__).parent
    newest = 0.0
    for name in _CODE_FILES:
        try:
            newest = max(newest, (here / name).stat().st_mtime)
        except OSError:
            pass
    return newest


def _code_info() -> dict:
    info = {
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        "code_mtime": _code_mtime(),
    }
    try:
        info["stereo"] = bool(getattr(demo_render, "STEREO", False))
    except Exception:
        info["stereo"] = None
    try:
        info["mp3"] = set_export.mp3_encoder()
    except Exception as exc:
        info["mp3"] = {"name": None, "error": str(exc)}
    # Список маршрутов. Дата файлов не всегда ловит подмену: на Windows два
    # процесса могут держать один порт, и запросы уходят тому, кто
    # забиндился раньше, — со старым набором эндпоинтов, но, возможно, с
    # такой же датой. Поэтому спрашиваем прямо: какие адреса ты вообще
    # умеешь. Диджей это увидел как «Not Found» на новой кнопке.
    try:
        info["endpoints"] = sorted({
            str(getattr(r, "path", "")).rsplit("/", 1)[-1]
            for r in app.routes if getattr(r, "path", "")
        })
    except Exception:
        info["endpoints"] = []
    return info


def _code_fingerprint_line() -> str:
    i = _code_info()
    mp3 = i.get("mp3") or {}
    return (f"pid {i['pid']}, стерео={i.get('stereo')}, "
            f"mp3={mp3.get('name')} {mp3.get('bitrate')} кбит/с, "
            f"файлы от {time.strftime('%H:%M:%S', time.localtime(i['code_mtime']))}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # librosa — тяжёлая библиотека (numba JIT и т.п.), первый import может
    # занять ~10 секунд. Греем её в фоновом потоке сразу при старте
    # backend'а (пока диджей ждёт "Backend поднимается" в launcher'е), чтобы
    # первый клик "🔍 Сканировать"/"🎧 Демо" не казался зависшим. Не
    # критично, если её ещё нет — кнопки сами покажут ошибку с pip-командой.
    def _warm() -> None:
        try:
            import librosa  # noqa: F401
            logger.info("librosa прогрета (для сканирования библиотеки и демо техник)")
        except ImportError:
            logger.info("librosa не установлена — сканирование библиотеки и демо техник недоступны, пока не 'pip install -r requirements-analysis.txt'")
        except Exception:
            logger.exception("librosa прогрелась с ошибкой (не критично)")

    asyncio.get_event_loop().run_in_executor(None, _warm)
    logger.info("код: " + _code_fingerprint_line())

    # Уборка на СТАРТЕ, а не на выходе. Запуск backend'а — это и есть
    # начало новой сессии, а выход бывает и по kill'у, когда обработчик
    # завершения не вызывается вовсе. Правило «оставить последнее»
    # (housekeeping.SWEEP_RULES) сохраняет свежие сеты и демо: диджей
    # часто возвращается к тому, что собрал полчаса назад.
    try:
        import housekeeping

        asyncio.get_event_loop().run_in_executor(
            None, lambda: housekeeping.session_cleanup(log=logger.info))
    except Exception:
        logger.exception("уборка при старте не удалась (не критично)")

    yield


app = FastAPI(lifespan=_lifespan)

# Один провайдер на весь процесс — общий пул соединений для всех комнат, а
# не по клиенту на комнату. gemini/ollama выбирается DARAVE_LLM_PROVIDER,
# см. llm_providers.py.
_llm_provider = make_provider()
sessions = SessionManager(_llm_provider)
persistence.init_db()

CHAT_HTML_PATH = Path(__file__).parent / "static" / "chat.html"
RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

# Локальное сканирование библиотеки прямо из веб-UI (кнопка "Сканировать" во
# вкладке "Библиотека") — см. POST .../library/scan ниже. Работает, только
# когда backend и музыка на одной машине (обычный локальный запуск рядом с
# Mixxx), потому что сканирует диск ТОГО процесса, где крутится backend, а
# не диска диджея через сеть. Для облачного деплоя (backend на VPS) кнопка
# не поможет — там по-прежнему нужен CLI track_analysis.py --upload на
# машине диджея (см. README).
DEFAULT_SET_MINUTES = 90.0  # длительность сета, если диджей не задал свою
SCAN_DB_DIR = Path(__file__).parent / "scan_dbs"
SCAN_DB_DIR.mkdir(exist_ok=True)
_scan_processes: dict[str, "subprocess.Popen"] = {}  # обычный Popen, см. start_library_scan
_scan_status: dict[str, dict] = {}

# Офлайн аудио-демо техник (кнопка "🎧 Демо" во вкладке "Техника") —
# demo_render.py реально сводит два файла диджея по логике технике и пишет
# WAV сюда; отдаётся обратно через GET .../techniques/demo/{filename}.
# Требует requirements-analysis.txt в том же Python, что и backend (та же
# оговорка, что у сканирования — см. execute_technique/library/scan).
DEMOS_DIR = Path(__file__).parent / "demos"
DEMOS_DIR.mkdir(exist_ok=True)

# Выгрузка готового сета: плейлист для Mixxx, раскладка и рендер микса.
SETS_DIR = Path(__file__).parent / "sets"
SETS_DIR.mkdir(exist_ok=True)


@app.get("/")
async def chat_ui() -> FileResponse:
    # Без no-store браузер иногда отдаёт из кэша старую вкладку/HTML даже
    # после того, как мы обновили chat.html на диске — правки в UI кажутся
    # "не применились", хотя на диске уже новая версия (ровно это и
    # произошло с добавлением выбора дек в "Технике"). chat.html — сама
    # маленькая HTML-страница, а не тяжёлый ассет, кэшировать незачем.
    return FileResponse(CHAT_HTML_PATH, headers={"Cache-Control": "no-store"})


@app.post("/api/rooms")
async def create_room() -> JSONResponse:
    """Генерирует случайный длинный код комнаты для нового арендатора —
    в отличие от произвольной строки "по знанию", такой код невозможно
    подобрать/угадать. См. README "Безопасность комнат"."""
    room_id = secrets.token_urlsafe(12)  # ~16 символов, криптостойкий
    return JSONResponse({"room_id": room_id})


@app.post("/api/rooms/{session_id}/recording")
async def upload_recording(session_id: str, file: UploadFile) -> JSONResponse:
    """Companion шлёт сюда свежесозданный аудиофайл записи после остановки
    (см. recording_uploader.py) — храним последнюю запись комнаты на диске
    backend'а, чтобы диджей мог скачать её из браузерного чата."""
    suffix = Path(file.filename or "recording.wav").suffix or ".wav"
    dest = RECORDINGS_DIR / f"{session_id}{suffix}"
    with open(dest, "wb") as out:
        out.write(await file.read())

    room = sessions.get_or_create(session_id)
    room.recording_path = str(dest)
    await room.broadcast_to_chat({"type": "recording_ready", "url": f"/api/rooms/{session_id}/recording"})
    return JSONResponse({"ok": True})


@app.get("/api/rooms/{session_id}/recording")
async def download_recording(session_id: str) -> FileResponse:
    room = sessions.get_or_create(session_id)
    if room.recording_path is None or not Path(room.recording_path).exists():
        raise HTTPException(status_code=404, detail="Запись ещё не готова")
    return FileResponse(room.recording_path, filename=Path(room.recording_path).name)


@app.get("/api/version")
async def code_version() -> JSONResponse:
    """Какой код реально крутится в этом процессе — чтобы «перезапустил, а
    ничего не изменилось» диагностировалось за секунду, а не на слух."""
    return JSONResponse(_code_info())


@app.get("/api/techniques")
async def list_techniques() -> JSONResponse:
    """Библиотека техник сведения — для веб-вкладки "Техника" (см.
    static/chat.html) и как справочный контекст (используется agent.py
    отдельно, тут — для UI)."""
    return JSONResponse([t.to_dict() for t in TECHNIQUES.values()])


@app.post("/api/rooms/{session_id}/techniques/{technique_id}/execute")
async def execute_technique(session_id: str, technique_id: str, body: dict | None = None) -> JSONResponse:
    """Кнопка "▶ Выполнить" во вкладке "Техника": в отличие от чата (там
    LLM сама решает source/target/technique_id) и от "Стратегии" (там план
    уже построен по всей библиотеке), здесь диджей вручную выбрал технику
    И деки — просто материализуем MixPlan под уже выбранные параметры
    (paramOverrides с левой панели) и шлём companion'у, LLM не участвует.
    body: {"source_deck": "A", "target_deck": "B", "param_overrides": {...}}"""
    room = sessions.get_or_create(session_id)
    if technique_id not in TECHNIQUES:
        raise HTTPException(status_code=404, detail=f"Неизвестная техника: {technique_id}")
    technique = TECHNIQUES[technique_id]

    body = body or {}
    source = body.get("source_deck", "A")
    target = body.get("target_deck", "B")
    third = body.get("third_deck")
    overrides = body.get("param_overrides")

    telemetry = room.latest_telemetry
    if source not in telemetry or target not in telemetry:
        raise HTTPException(
            status_code=400,
            detail=f"Дека {source} или {target} не видна в телеметрии (сейчас есть: {list(telemetry.keys())}) — companion подключён и деки загружены?",
        )

    if technique.requires_stems:
        # Mixxx 2.6+ умеет играть слои сам, если на деке лежит .stem.mp4.
        # Раньше здесь стоял безусловный отказ «живьём отдельных каналов
        # под слои нет» — он был верен для 2.4 и перестал быть верным.
        # Теперь спрашиваем деку, а не догадываемся: stem_count приходит
        # телеметрией и равен 4 у стемового файла и 0 у обычного.
        plain = [d for d in (source, target)
                 if int(telemetry.get(d, {}).get("stem_count") or 0) < 4]
        if plain:
            raise HTTPException(
                status_code=400,
                detail=f"«{technique.name}» работает по слоям, а на деке "
                       f"{' и '.join(plain)} обычный файл без слоёв. "
                       f"Соберите стемы: python stems.py --dir \"путь к музыке\" — "
                       f"рядом с треком появится .stem.mp4, загрузите в Mixxx его. "
                       f"Если стемы уже есть, обновите скрипт контроллера в Mixxx: "
                       f"без поля stem_count в телеметрии дека выглядит обычной.",
            )

    if technique.requires_decks >= 3:
        # Третью деку не угадываем: раньше она была зашита как "C", и
        # приём молча играл не на той деке, если трек лежал на D.
        if not third:
            free = [d for d in sorted(telemetry) if d not in (source, target)
                    and telemetry[d].get("track_loaded")]
            third = free[0] if free else None
        if not third:
            raise HTTPException(
                status_code=400,
                detail=f"«{technique.name}» играется на трёх деках, а кроме "
                       f"{source} и {target} ни на одной деке нет загруженного трека.")
        if third not in telemetry:
            raise HTTPException(status_code=400,
                                detail=f"Дека {third} не видна в телеметрии")

    bpm = telemetry[source].get("bpm") or 128.0
    plan_id = f"tech_{session_id}_{source}_to_{target}_{technique_id}"
    try:
        plan = build_plan(technique_id, plan_id, source, target, bpm, overrides,
                          third=third)
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sent = await room.send_plan_to_companion(plan)
    return JSONResponse({"ok": sent, "plan_id": plan_id, "third_deck": third})


@app.get("/api/rooms/{session_id}/techniques/{technique_id}/recommend")
async def recommend_technique_tracks(session_id: str, technique_id: str,
                                     offset: int = 0) -> JSONResponse:
    """Вкладка "Техника": для выбранной техники подбирает из библиотеки
    комнаты лучшие пары треков (по BPM-совместимости/тональности/энергии —
    см. mix_strategist.score_pair_for_technique) — это и есть "showcase":
    диджей видит готовые примеры, не выбирая деки руками."""
    if technique_id not in TECHNIQUES:
        raise HTTPException(status_code=404, detail=f"Неизвестная техника: {technique_id}")
    room = sessions.get_or_create(session_id)
    if not room.library_tracks:
        return JSONResponse({"pairs": [], "hint": "Библиотека комнаты пуста — сначала отсканируйте музыку во вкладке «Библиотека»."})
    technique = TECHNIQUES[technique_id]
    offset = max(0, int(offset))
    pairs = mix_strategist.recommend_tracks_for_technique(
        technique, room.library_tracks, top_n=3, offset=offset)
    if not pairs and offset:
        # пары кончились — начинаем сначала, а не показываем пустоту
        pairs = mix_strategist.recommend_tracks_for_technique(
            technique, room.library_tracks, top_n=3, offset=0)
        offset = 0
    return JSONResponse({"pairs": pairs, "offset": offset})


@app.get("/api/rooms/{session_id}/techniques/{technique_id}/cue")
async def technique_cue_for_pair(session_id: str, technique_id: str,
                                 a: str, b: str) -> JSONResponse:
    """Подсказки «откуда сводить» для КОНКРЕТНОЙ упорядоченной пары.

    Нужен для кнопки «поменять местами»: A→B и B→A — это разные переходы
    с разными точками ухода и входа, и показывать после обмена старые
    подписи было бы враньём."""
    if technique_id not in TECHNIQUES:
        raise HTTPException(status_code=404, detail=f"Неизвестная техника: {technique_id}")
    room = sessions.get_or_create(session_id)
    by_path = {t.get("path"): t for t in room.library_tracks}
    ta, tb = by_path.get(a), by_path.get(b)
    if ta is None or tb is None:
        raise HTTPException(status_code=400, detail="Трек не найден в библиотеке комнаты")
    return JSONResponse({"cue": mix_strategist.technique_cue_hints(TECHNIQUES[technique_id], ta, tb)})


@app.get("/api/rooms/{session_id}/cues")
async def track_cues(session_id: str, path: str = "", name: str = "") -> JSONResponse:
    """Именованные точки одного трека: первый бит, конец интро, билд, дроп,
    брейкдаун, аутро, места под луп. Роль впереди, время следом — диджей
    выбирает место, а не секунду."""
    import cue_points

    room = sessions.get_or_create(session_id)
    track = None
    for t in room.library_tracks:
        if (path and t.get("path") == path) or (name and t.get("name") == name):
            track = t
            break
    if track is None:
        raise HTTPException(status_code=404, detail="Трек не найден в библиотеке комнаты")
    data = cue_points.cues_for_track(track)
    data["track"] = {"name": track.get("name"), "path": track.get("path"),
                     "bpm": track.get("bpm"),
                     "duration_seconds": track.get("duration_seconds")}
    if not data["anchored"]:
        data["warning"] = ("карты барабанов у трека нет — сетка тактов условная. "
                           "Отсканируйте библиотеку заново.")
    return JSONResponse(data)


@app.get("/api/rooms/{session_id}/sections")
async def track_sections(session_id: str, path: str = "", name: str = "") -> JSONResponse:
    """Непрерывное разбиение трека на секции: интро, билд, дроп,
    брейкдаун, грув, аутро.

    Отличие от /cues принципиальное. Точка отвечает «где это место»;
    секция отвечает «что звучит сейчас и сколько это продлится». Для
    сведения нужно второе: завести трек в брейкдаун на 8 тактов и в
    брейкдаун на 32 такта — разные приёмы, и точка об этом молчит."""
    import cue_points

    room = sessions.get_or_create(session_id)
    track = None
    for t in room.library_tracks:
        if (path and t.get("path") == path) or (name and t.get("name") == name):
            track = t
            break
    if track is None:
        raise HTTPException(status_code=404, detail="Трек не найден в библиотеке комнаты")
    data = cue_points.sections_for_track(track)
    data["track"] = {"name": track.get("name"), "path": track.get("path"),
                     "bpm": track.get("bpm"),
                     "duration_seconds": track.get("duration_seconds"),
                     "genre": track.get("genre"), "subgenre": track.get("subgenre")}
    if not data.get("anchored"):
        data["warning"] = ("карты барабанов у трека нет — границы секций условны. "
                           "Отсканируйте библиотеку заново.")
    return JSONResponse(data)


@app.post("/api/rooms/{session_id}/cues/export")
async def export_cues_to_mixxx(session_id: str, body: dict | None = None) -> JSONResponse:
    """Выгружает точки всей библиотеки в горячие метки Mixxx.

    body: {"dry_run": true} — показать, что будет записано;
          {"slots": 8}      — сколько кнопок занимать.
    ВАЖНО: Mixxx должен быть закрыт — он держит библиотеку в памяти и
    перезапишет файл при выходе."""
    import mixxx_cues

    body = body or {}
    room = sessions.get_or_create(session_id)
    if not room.library_tracks:
        raise HTTPException(status_code=400,
                            detail="Библиотека комнаты пуста — сначала отсканируйте музыку")
    r = await asyncio.to_thread(
        mixxx_cues.export_library, room.library_tracks, None,
        bool(body.get("dry_run")), logger.info, int(body.get("slots") or 8))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "не вышло")
    return JSONResponse(r)


@app.post("/api/rooms/{session_id}/techniques/{technique_id}/demo")
async def render_technique_demo(session_id: str, technique_id: str, body: dict) -> JSONResponse:
    """Кнопка "🎧 Демо": офлайн рендерит короткий (до ~25с) WAV,
    показывающий, как реально звучит эта техника на выбранной паре треков
    из библиотеки (demo_render.py — переиспользует тот же build_plan(),
    что и живое исполнение). Рендер — CPU-bound (librosa/scipy), поэтому
    гоним его в отдельном потоке через asyncio.to_thread, чтобы не
    блокировать event loop (и WebSocket телеметрию/чат) на несколько секунд.
    body: {"track_a_path": str, "track_b_path": str, "param_overrides": {...}}"""
    if demo_render is None:
        raise HTTPException(
            status_code=503,
            detail="Рендер демо недоступен: не установлены numpy/scipy. Выполните pip install -r requirements-analysis.txt и перезапустите backend.",
        )
    if technique_id not in TECHNIQUES:
        raise HTTPException(status_code=404, detail=f"Неизвестная техника: {technique_id}")
    technique = TECHNIQUES[technique_id]
    # Проверку «есть ли стемы» делает сам рендер: он один знает, для каких
    # ДВУХ файлов они нужны, и может назвать тот, из-за которого не вышло.
    # Здесь оставалась заглушка «офлайн-демо пока не поддержано», и она
    # отсекала техники по слоям ещё до того, как кто-то посмотрел в кэш.

    body = body or {}
    track_a_path = body.get("track_a_path")
    track_b_path = body.get("track_b_path")
    overrides = body.get("param_overrides")
    if not track_a_path or not track_b_path:
        raise HTTPException(status_code=400, detail="Нужны track_a_path и track_b_path (из /recommend)")

    room = sessions.get_or_create(session_id)
    by_path = {t.get("path"): t for t in room.library_tracks}
    track_a = by_path.get(track_a_path)
    track_b = by_path.get(track_b_path)
    if track_a is None or track_b is None:
        raise HTTPException(status_code=400, detail="Один из треков не найден в текущей библиотеке комнаты (пересканировали после подбора пар?)")

    bpm_a = track_a.get("bpm") or 128.0
    bpm_b = track_b.get("bpm") or 128.0

    # Точки сведения. Карточка техники ПОКАЗЫВАЕТ «выход из A на 3:12,
    # запуск B с 0:48» — а рендер их не получал вовсе и брал хвост A и
    # голову B. То есть подпись и звук говорили про разные места трека, и
    # приём срабатывал там, где ему нечего делать. Считаем те же самые
    # подсказки тем же кодом, что рисует карточку, — иначе они снова
    # разъедутся при первой же правке.
    source_at = body.get("source_at")
    target_at = body.get("target_at")
    if source_at is None or target_at is None:
        try:
            cue = mix_strategist.technique_cue_hints(technique, track_a, track_b)
            if source_at is None:
                source_at = (cue.get("from_track") or {}).get("time_seconds")
            if target_at is None:
                target_at = (cue.get("into_track") or {}).get("time_seconds")
        except Exception:
            source_at = target_at = None

    # mp3, а не wav: демо слушают десятками за вечер, и 25 секунд стерео
    # это 600 КБ против 4-5 МБ. На слух для «послушать и решить» разницы
    # нет, на диске — восьмикратная.
    out_name = f"{session_id}_{technique_id}_{secrets.token_hex(6)}.mp3"
    out_path = DEMOS_DIR / out_name

    try:
        meta = await asyncio.to_thread(
            demo_render.render_demo,
            technique_id, track_a_path, track_b_path, bpm_a, bpm_b, str(out_path),
            param_overrides=overrides,
            source_at=(float(source_at) if source_at else None),
            target_at=(float(target_at) if target_at else None),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"Файл трека не найден на диске backend'а: {exc}") from exc
    except (ValueError, NotImplementedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({
        "ok": True,
        "url": f"/api/rooms/{session_id}/techniques/demo/{out_name}",
        **meta,
    })


@app.get("/api/rooms/{session_id}/techniques/demo/{filename}")
async def get_technique_demo(session_id: str, filename: str) -> FileResponse:
    """Отдаёт отрендеренный demo WAV обратно в <audio> плеер фронтенда."""
    # session_id участвует только в маршруте (симметрично остальным /rooms/{id}/...
    # эндпоинтам) — сами demo-файлы содержат session_id в имени, отдельная
    # директория на комнату не нужна (secrets.token_hex делает имя уникальным).
    path = DEMOS_DIR / filename
    if ".." in filename or "/" in filename or not path.is_file():
        raise HTTPException(status_code=404, detail="Демо-файл не найден")
    # Демо теперь mp3; отдаём тип по расширению, чтобы старые wav из
    # прошлых прогонов продолжали открываться.
    return FileResponse(path, media_type="audio/mpeg" if path.suffix.lower() == ".mp3"
                        else "audio/wav")


@app.get("/api/controls")
async def list_controls() -> JSONResponse:
    """Каталог всех контролов Mixxx, которыми умеет управлять DARAVE."""
    return JSONResponse({"controls": live_control.catalog_for_ui()})


@app.post("/api/rooms/{session_id}/control")
async def set_control(session_id: str, body: dict) -> JSONResponse:
    """Дёрнуть один контрол Mixxx прямо сейчас (веб-пульт, отладка, и тот же
    путь, которым пользуется ИИ-чат через set_mixxx_control).
    body: {"control": "eq_low", "deck": "A", "value": 0 | "neutral"}"""
    room = sessions.get_or_create(session_id)
    body = body or {}
    try:
        cmd = live_control.validate(
            body.get("deck") or "A", body.get("control") or "", body.get("value"),
        )
    except live_control.ControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sent = await room.send_control_to_companion(cmd)
    if not sent:
        raise HTTPException(status_code=409, detail="Companion не подключён — команду некуда слать.")
    logger.info(f"контрол: {live_control.describe(cmd)} (комната '{session_id}')")
    return JSONResponse({"ok": True, "applied": live_control.describe(cmd), "command": cmd})


@app.get("/api/controls/search")
async def search_controls(q: str = "", limit: int = 25) -> JSONResponse:
    """Поиск по ПОЛНОМУ каталогу Mixxx (то же, чем пользуется ИИ-чат)."""
    return JSONResponse({"stats": full_catalog.stats(), "hits": full_catalog.search(q, limit)})


@app.post("/api/rooms/{session_id}/control/raw")
async def set_control_raw(session_id: str, body: dict) -> JSONResponse:
    """Дёрнуть ЛЮБОЙ контрол Mixxx по ключу — идёт через SysEx, номера
    маплить не нужно. body: {"key": "parameter3", "unit": 1, "slot": 2,
    "deck": "A", "index": 3, "value": 0.7}"""
    room = sessions.get_or_create(session_id)
    body = body or {}
    try:
        cmd = live_control.validate_raw(
            key=body.get("key") or "", deck=body.get("deck"), unit=body.get("unit"),
            slot=body.get("slot"), index=body.get("index"), value=body.get("value"),
            family=body.get("family"),
        )
    except live_control.ControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sent = await room.send_control_to_companion(cmd)
    if not sent:
        raise HTTPException(status_code=409, detail="Companion не подключён — команду некуда слать.")
    logger.info(f"контрол(raw): {live_control.describe(cmd)} (комната '{session_id}')")
    return JSONResponse({"ok": True, "applied": live_control.describe(cmd), "command": cmd})


@app.post("/api/rooms/{session_id}/strategy/export")
async def export_set(session_id: str, body: dict | None = None) -> JSONResponse:
    """Отдаёт сет наружу: M3U-плейлист для Mixxx + текстовая раскладка.

    Плейлист — это и есть «нативно в Mixxx»: треки играет сам Mixxx, а не
    браузер. Загрузить его в Mixxx: Медиатека -> Импорт плейлиста."""
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план (кнопка «Построить план»).")

    title = (body or {}).get("title") or f"DARAVE {session_id}"
    stem = f"{session_id}_{secrets.token_hex(4)}"
    m3u = SETS_DIR / f"{stem}.m3u8"
    cue = SETS_DIR / f"{stem}.txt"

    result = set_export.export_m3u(room.last_strategy, str(m3u), title=title)
    cue.write_text(set_export.cue_sheet(room.last_strategy), encoding="utf-8")

    logger.info(f"сет выгружен: {result['tracks']} треков -> {m3u.name}")
    return JSONResponse({
        "ok": True,
        "playlist_url": f"/api/rooms/{session_id}/sets/{m3u.name}",
        "cue_url": f"/api/rooms/{session_id}/sets/{cue.name}",
        "playlist_path": str(m3u),
        "tracks": result["tracks"],
        "skipped": result["skipped"],
        "how_to": "В Mixxx: Медиатека -> Импорт плейлиста -> выбрать этот .m3u8",
    })


@app.post("/api/rooms/{session_id}/strategy/render")
async def render_set(session_id: str, body: dict | None = None) -> JSONResponse:
    """Рендерит весь сет одним аудиофайлом (превью: все переходы подряд).

    Считается в отдельном потоке — это CPU-bound работа на десятки секунд,
    в event loop её пускать нельзя, иначе встанут телеметрия и чат."""
    if demo_render is None:
        raise HTTPException(status_code=503,
                            detail="Нужны numpy/scipy/librosa: pip install -r requirements-analysis.txt")
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план.")

    body = body or {}
    out = SETS_DIR / f"{session_id}_{secrets.token_hex(4)}.wav"
    try:
        meta = await asyncio.to_thread(
            set_export.render_set, room.last_strategy, str(out),
            44100, float(body.get("seconds_between") or 50.0),
            float(body.get("max_minutes") or 25.0),
            float(body.get("join_bars") or 1.0),
            None,
            int(body.get("sample") or 8),
            str(body.get("format") or "mp3"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    used = meta.get("techniques_used") or {}
    out = Path(meta["path"])          # render_set сам меняет расширение под формат
    logger.info(f"сет отрендерен: {meta['transitions_rendered']}/{meta['transitions_total']} переходов, "
                f"{meta['duration_seconds']}с, техник использовано: {len(used)} "
                f"({', '.join(f'{k}x{v}' for k, v in sorted(used.items()))})")
    return JSONResponse({"ok": True, "url": f"/api/rooms/{session_id}/sets/{out.name}", **meta})


@app.post("/api/rooms/{session_id}/strategy/transitions/{index}/demo")
async def demo_transition(session_id: str, index: int, body: dict | None = None) -> JSONResponse:
    """Слушаем ОДИН переход плана — ровно так, как он собран сейчас: та же
    техника, те же точки ухода и входа, тот же темп сета.

    Нужно, чтобы правку перехода можно было проверить на слух сразу, а не
    пересобирать весь сет ради одного стыка."""
    if demo_render is None:
        raise HTTPException(status_code=503,
                            detail="Нужны numpy/scipy/librosa: pip install -r requirements-analysis.txt")
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план.")
    transitions = room.last_strategy.get("transitions") or []
    if not (0 <= index < len(transitions)):
        raise HTTPException(status_code=400, detail=f"Нет перехода №{index}")

    tr = transitions[index]
    by_name = {t["name"]: t for t in (room.last_strategy.get("tracks") or [])}
    a, b = by_name.get(tr["from"]), by_name.get(tr["to"])
    if not a or not b or not a.get("path") or not b.get("path"):
        raise HTTPException(status_code=400, detail="Не найдены файлы треков этого перехода")

    body = body or {}
    out_name = f"{session_id}_tr{index}_{secrets.token_hex(5)}.mp3"
    out_path = DEMOS_DIR / out_name

    def _pt(p):
        try:
            v = float((p or {}).get("time_seconds"))
        except (TypeError, ValueError):
            return None
        return v if v > 0.5 else None

    def _num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0.5 else None

    # Можно послушать ЛЮБОЙ вариант, не применяя его к плану: диджей
    # перебирает техники и точки на слух, а план меняет только когда
    # услышал то, что хочет.
    technique_id = body.get("technique_id") or tr["technique_id"]
    if technique_id not in TECHNIQUES:
        raise HTTPException(status_code=400, detail=f"Нет такой техники: {technique_id}")
    from_at = _num(body.get("from_seconds")) if body.get("from_seconds") is not None else _pt(tr.get("from_point"))
    to_at = _num(body.get("to_seconds")) if body.get("to_seconds") is not None else _pt(tr.get("to_point"))

    # Темп ведёт уходящий трек: его не трогаем вообще, подстраивается
    # только входящий. Общий мастер сдвигал по высоте оба и расстраивал их
    # друг относительно друга.
    master = None
    try:
        meta = await asyncio.to_thread(
            demo_render.render_demo,
            technique_id, a["path"], b["path"], a["bpm"], b["bpm"], str(out_path),
            # Длину сведения задаёт план (4-12 тактов по контексту) — либо
            # её присылают из UI, когда диджей перебирает варианты на слух.
            set_export._blend_override({"blend_bars": body.get("blend_bars") or tr.get("blend_bars"),
                                        "bars": tr.get("bars"),
                                        "mid_duck": body.get("mid_duck", tr.get("mid_duck"))}),
            float(body.get("max_seconds") or 45.0), 44100,
            from_at, to_at, master,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"Файл трека не найден: {exc}") from exc
    except (ValueError, NotImplementedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({
        "ok": True, "index": index,
        "url": f"/api/rooms/{session_id}/techniques/demo/{out_name}",
        "technique_id": technique_id, "technique_name": TECHNIQUES[technique_id].name,
        "from": tr["from"], "to": tr["to"],
        "from_point": (tr.get("from_point") or {}).get("label"),
        "to_point": (tr.get("to_point") or {}).get("label"),
        "from_seconds": from_at, "to_seconds": to_at,
        **meta,
    })


_full_render_tasks: dict[str, asyncio.Task] = {}
_full_render_status: dict[str, dict] = {}


@app.post("/api/rooms/{session_id}/strategy/render-full")
async def render_full(session_id: str, body: dict | None = None) -> JSONResponse:
    """Собирает ВЕСЬ сет одним непрерывным треком — не превью переходов, а
    столько же минут, сколько в плане.

    Запускается фоном: 90 минут аудио считаются пару минут, и держать всё
    это время открытый HTTP-запрос — верный способ получить таймаут в
    браузере. Прогресс забирается через GET того же адреса."""
    if demo_render is None:
        raise HTTPException(status_code=503,
                            detail="Нужны numpy/scipy/librosa: pip install -r requirements-analysis.txt")
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план.")
    task = _full_render_tasks.get(session_id)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Сборка уже идёт — дождитесь окончания")

    body = body or {}
    strategy = room.last_strategy
    out = SETS_DIR / f"{session_id}_full_{secrets.token_hex(4)}.wav"
    total = len(strategy.get("transitions") or [])
    _full_render_status[session_id] = {"running": True, "done": 0, "total": total, "minutes": 0.0}

    def _progress(done: int, all_n: int, seconds: float,
                  stage: str = "mix", frac: float = 0.0) -> None:
        # stage="encode" — сведения уже посчитаны, идёт перекодирование в
        # mp3. Раньше этой стадии не было в статусе, и последние минуты
        # сборки выглядели как зависший индикатор на "43 из 43".
        _full_render_status[session_id] = {"running": True, "done": done, "total": all_n,
                                           "minutes": round(seconds / 60.0, 1),
                                           "stage": stage, "encoded": round(float(frac), 3)}

    async def _run() -> None:
        try:
            meta = await asyncio.to_thread(
                set_export.render_full_set, strategy, str(out), 44100,
                float(body.get("max_minutes") or 150.0),
                body.get("master_bpm"),
                _progress,
                str(body.get("format") or "mp3"),
            )
        except Exception as exc:
            logger.exception("не смог собрать полный сет")
            _full_render_status[session_id] = {"running": False, "error": str(exc)}
            return
        used = meta.get("techniques_used") or {}
        logger.info(f"полный сет собран: {meta['transitions_rendered']}/{meta['transitions_total']} переходов, "
                    f"{meta['duration_seconds'] / 60:.1f} мин, темп {meta['master_bpm']}, "
                    f"техник {len(used)}")
        _full_render_status[session_id] = {
            "running": False, "done": meta["transitions_rendered"], "total": meta["transitions_total"],
            "minutes": round(meta["duration_seconds"] / 60.0, 1),
            # render_full_set сам меняет расширение под формат — берём
            # имя из результата, а не из того, что просили
            "url": f"/api/rooms/{session_id}/sets/{Path(meta['path']).name}", **meta,
        }

    _full_render_tasks[session_id] = asyncio.create_task(_run())
    return JSONResponse({"ok": True, "started": True, "transitions": total})


@app.get("/api/rooms/{session_id}/strategy/render-full")
async def render_full_status(session_id: str) -> JSONResponse:
    return JSONResponse(_full_render_status.get(session_id, {"running": False}))


@app.get("/api/rooms/{session_id}/sets/{filename}")
async def get_set_file(session_id: str, filename: str) -> FileResponse:
    path = SETS_DIR / filename
    if ".." in filename or "/" in filename or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл сета не найден")
    media = {"m3u8": "audio/x-mpegurl", "wav": "audio/wav", "mp3": "audio/mpeg",
             "txt": "text/plain; charset=utf-8"}
    return FileResponse(path, media_type=media.get(filename.rsplit(".", 1)[-1], "application/octet-stream"))


# --- автоматическое уточнение BPM библиотеки ---------------------------------
# Диджей не должен запускать никаких отдельных утилит: BPM, записанный
# сканером, — это значение с сетки-приора librosa, а не измерение (у трети
# библиотеки один и тот же темп до сотых). Пока он не исправлен, такты в
# сведении разъезжаются, а треки с ошибкой доли (117.5 вместо ~176) вообще
# не попадают в план. Поэтому чиним сами, в фоне, сразу как увидели
# библиотеку — и рассказываем об этом в чат, чтобы это не выглядело
# «программа что-то делает молча».

_bpm_refine_tasks: dict[str, asyncio.Task] = {}
_bpm_refine_status: dict[str, dict] = {}
_bpm_refine_done: set[str] = set()  # чинить один раз за жизнь процесса, а не на каждый план


def _room_db_path(session_id: str) -> str:
    return str(SCAN_DB_DIR / f"{session_id}.db")


async def _refine_room_bpm(session_id: str, force: bool = False) -> None:
    """Перемеряет темп треков комнаты и обновляет БД + библиотеку в памяти."""
    if fix_library_bpm is None:
        return
    room = sessions.get_or_create(session_id)
    tracks = room.library_tracks or []
    if not tracks:
        return
    db_path = _room_db_path(session_id)
    if not Path(db_path).exists():
        return

    report = fix_library_bpm.stale_bpm_report([t.get("bpm") for t in tracks])
    # Карта барабанов нужна не меньше BPM: без неё точка входа выбирается
    # по ненадёжным дропам, и трек заводится в собственное интро без бита.
    missing_maps = sum(1 for t in tracks if not ((t.get("structure") or {}).get("drum_map")))
    # Карта энергии — то же самое для брейкдаунов, ям и дропов. Дропы
    # structure-анализа находились с уверенностью 0.14-0.26 (почти
    # наугад), и точка сведения по ним попадала мимо музыки.
    missing_energy = sum(1 for t in tracks if not ((t.get("structure") or {}).get("energy_map")))
    need_maps = missing_maps > len(tracks) * 0.2 or missing_energy > len(tracks) * 0.2
    _bpm_refine_done.add(session_id)
    if not force and not report["stale"] and not need_maps:
        _bpm_refine_status[session_id] = {"running": False, "skipped": "BPM и карта барабанов уже посчитаны"}
        return

    _bpm_refine_status[session_id] = {"running": True, "done": 0, "total": len(tracks)}
    why = []
    if report["stale"]:
        why.append(f"у {report['count']} из {report['total']} треков стоит одинаковый темп "
                   f"{report['value']} — это заглушка детектора, а не измерение, и по такому "
                   f"BPM такты в сведении разъезжаются")
    if missing_maps > len(tracks) * 0.2:
        why.append(f"у {missing_maps} треков не посчитано, где играют барабаны — без этого "
                   f"новый трек может завестись в своё интро без бита")
    if missing_energy > len(tracks) * 0.2:
        why.append(f"у {missing_energy} треков не посчитаны брейкдауны и дропы — без них "
                   f"сведение ставится не туда, где музыка к нему готова")
    await room.broadcast_to_chat({
        "type": "system",
        "text": "Проверяю библиотеку: " + "; ".join(why) +
                ". Пара минут, планом пока можно пользоваться.",
    })

    loop = asyncio.get_running_loop()

    def _progress(done: int, total: int) -> None:
        _bpm_refine_status[session_id] = {"running": True, "done": done, "total": total}

    def _work() -> dict:
        return fix_library_bpm.fix_db(db_path, dry_run=False, allow_octave=True,
                                      progress=_progress, log=lambda m: logger.info(f"[bpm] {m}"))

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        logger.exception(f"не смог уточнить BPM комнаты '{session_id}'")
        _bpm_refine_status[session_id] = {"running": False, "error": True}
        return

    try:
        fresh = await asyncio.to_thread(load_library_from_db, db_path, None)
        if fresh:
            room.library_tracks = fresh
            persistence.save_library(session_id, fresh)
    except Exception:
        logger.exception("не смог перечитать библиотеку после уточнения BPM")

    _bpm_refine_status[session_id] = {"running": False, "done": result.get("tracks", 0),
                                      "changed": result.get("changed", 0),
                                      "changes": result.get("changes", [])[:40]}
    big = [c for c in result.get("changes", []) if abs(c["new"] - c["old"]) > 3]
    logger.info(f"BPM комнаты '{session_id}' уточнён: изменено {result.get('changed')} из "
                f"{result.get('tracks')}, из них с исправленной долей {len(big)}")
    await room.broadcast_to_chat({
        "type": "system",
        "text": (f"Библиотека проверена: BPM поправлен у {result.get('changed')} треков из {result.get('tracks')}, "
                 f"карта барабанов посчитана у всех"
                 + (f", из них у {len(big)} была неверно посчитана доля "
                    f"(например {big[0]['name'][:40]}: {big[0]['old']:g} -> {big[0]['new']:g})" if big else "")
                 + ". Постройте план заново — теперь такты будут сходиться."),
    })


def _scan_is_running(session_id: str) -> bool:
    proc = _scan_processes.get(session_id)
    return proc is not None and proc.poll() is None


def _kick_bpm_refine(session_id: str, force: bool = False) -> None:
    if not force and session_id in _bpm_refine_done:
        return
    if _scan_is_running(session_id):
        # Сканер прямо сейчас пишет в ТОТ ЖЕ файл БД. Перемер держит базу
        # занятой минутами, сканер упирался в блокировку и падал с кодом 1 —
        # диджей видел «при сканировании вылазит ошибка», а причина была в
        # том, что он в этот момент открыл вкладку «Стратегия». Ждать не
        # надо: по окончании скана уточнение кикается само (см. _watch_scan).
        logger.info(f"уточнение BPM '{session_id}' отложено: идёт сканирование библиотеки")
        return
    task = _bpm_refine_tasks.get(session_id)
    if task is not None and not task.done():
        return
    _bpm_refine_tasks[session_id] = asyncio.create_task(_refine_room_bpm(session_id, force=force))


@app.post("/api/cleanup")
async def cleanup_now(body: dict | None = None) -> JSONResponse:
    """Убрать демо, сеты, записи и оборванные папки стемов.

    body: {"dry_run": true} — только показать, что было бы удалено;
          {"all": true}     — убрать всё, не оставляя последних файлов."""
    import housekeeping

    body = body or {}
    rules = dict(housekeeping.SWEEP_RULES)
    if body.get("all"):
        for name in housekeeping.SWEEP_RULES:
            housekeeping.SWEEP_RULES[name] = (0, 0.0)
    try:
        report = housekeeping.session_cleanup(
            dry_run=bool(body.get("dry_run")), log=logger.info)
    finally:
        housekeeping.SWEEP_RULES.update(rules)
    return JSONResponse(report)


@app.post("/api/rooms/{session_id}/library/refine-bpm")
async def refine_library_bpm(session_id: str, body: dict | None = None) -> JSONResponse:
    """Ручной запуск того же уточнения (обычно оно идёт само)."""
    if fix_library_bpm is None:
        raise HTTPException(status_code=503, detail="Нужны numpy/scipy/librosa для перемера темпа.")
    _kick_bpm_refine(session_id, force=bool((body or {}).get("force")))
    return JSONResponse({"ok": True, "status": _bpm_refine_status.get(session_id, {"running": True})})


@app.get("/api/rooms/{session_id}/library/refine-bpm")
async def refine_library_bpm_status(session_id: str) -> JSONResponse:
    return JSONResponse(_bpm_refine_status.get(session_id, {"running": False}))


@app.post("/api/rooms/{session_id}/library")
async def upload_library(session_id: str, body: dict) -> JSONResponse:
    """Companion шлёт сюда результаты track_analysis.py --upload (список
    аналитических записей треков — BPM/тональность/структура). Полностью
    заменяет предыдущую библиотеку комнаты (пересканирование = новый снимок)."""
    tracks = body.get("tracks", [])
    room = sessions.get_or_create(session_id)
    room.library_tracks = tracks
    persistence.save_library(session_id, tracks)
    await room.broadcast_to_chat({"type": "library_updated", "count": len(tracks)})
    return JSONResponse({"ok": True, "count": len(tracks)})


@app.get("/api/rooms/{session_id}/library")
async def get_library(session_id: str) -> JSONResponse:
    """Библиотека комнаты + два поля, которых в базе сканера нет.

    `stems` — готовы ли слои у трека. Без этого признака диджей не мог
    понять, почему один и тот же приём на одном треке звучит как задумано,
    а на другом — как обычный кроссфейд: половина техник опирается на
    стемы и молча падает на запасной путь, когда их нет.

    `genre`/`subgenre` — то же самое, что лежит в tags["genre"], но
    разложенное по полям: во вложенном виде их не видел ни интерфейс, ни
    подбор пары."""
    import style as _style

    room = sessions.get_or_create(session_id)
    out = []
    for t in room.library_tracks:
        item = dict(t)
        g, sub = _style.track_genre(t)
        item["genre"] = item.get("genre") or g or None
        item["subgenre"] = item.get("subgenre") or sub or None
        try:
            item["stems"] = bool(_style.has_stems(t))
        except Exception:
            item["stems"] = False
        out.append(item)
    return JSONResponse({"tracks": out})


@app.post("/api/rooms/{session_id}/library/scan")
async def start_library_scan(session_id: str, body: dict) -> JSONResponse:
    """Кнопка "🔍 Сканировать" во вкладке "Библиотека": запускает
    track_analysis.py --scan <path> --db ... подпроцессом backend'а, вместо
    того чтобы диджей сам открывал терминал; результат backend забирает
    прямо из SQLite-файла сканера (см. _watch_scan), без HTTP на себя. Требует requirements-analysis.txt в ТОМ ЖЕ Python-
    окружении, что и сам backend (librosa/soundfile/numpy) — если их нет,
    подпроцесс упадёт с ImportError, это будет видно в /scan/status.tail."""
    if _scan_is_running(session_id):
        raise HTTPException(status_code=409, detail="Сканирование уже идёт — дождитесь завершения")

    refine = _bpm_refine_tasks.get(session_id)
    if refine is not None and not refine.done():
        # Обратная сторона той же блокировки: перемер BPM пишет в файл базы
        # сканера. Пустить их одновременно — гарантированно уронить один из них.
        raise HTTPException(
            status_code=409,
            detail="Идёт проверка библиотеки (перемер BPM) — она пишет в тот же файл базы. "
                   "Подождите пару минут, пока закончится, и запустите сканирование снова.")

    path = (body or {}).get("path", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="Укажите путь к папке с музыкой")
    if not Path(path).exists():
        raise HTTPException(status_code=400, detail=f"Папка не найдена на диске backend'а: {path}")

    script = str(Path(__file__).parent / "track_analysis.py")
    db_path = str(SCAN_DB_DIR / f"{session_id}.db")

    # НЕ передаём --upload: сканер и backend на одной машине и делят один
    # файл БД, поэтому результат забираем прямо из SQLite по завершении
    # (см. _watch_scan ниже). Раньше здесь был HTTP-хоп на собственный
    # localhost, и у пользователя с системным прокси/VPN он превращался в
    # пустой 503 — библиотека молча оставалась пустой, а следом "не
    # работали" и Стратегия, и рекомендации во вкладке "Техника".
    scan_started_at = time.time()
    # ОБЫЧНЫЙ subprocess, а не asyncio.create_subprocess_exec.
    # На Windows тот работает только на ProactorEventLoop, а uvicorn
    # поднимает селекторный цикл — и запуск падал с голым
    # NotImplementedError. Наружу это выглядело как «Ошибка сети:
    # Unexpected token 'I', "Internal S"... is not valid JSON»: браузер
    # получал страницу 500 и пытался разобрать её как JSON. Чтение вывода
    # уводим в поток, событийный цикл не блокируется.
    import subprocess as _sp

    try:
        proc = _sp.Popen(
            [sys.executable, script, "--scan", path, "--db", db_path],
            stdout=_sp.PIPE, stderr=_sp.STDOUT,
            cwd=str(Path(__file__).parent),
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        logger.exception("не смог запустить сканер")
        raise HTTPException(status_code=500,
                            detail=f"Не смог запустить сканер: {exc}") from exc
    logger.info(f"сканирование запущено: комната '{session_id}', папка {path}, БД {db_path}")
    _scan_processes[session_id] = proc
    _scan_status[session_id] = {"running": True, "tail": [], "ok": None, "returncode": None}

    def _pump() -> None:
        """Читает вывод сканера в отдельном потоке и ждёт его конца."""
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    status = _scan_status[session_id]
                    status["tail"] = (status["tail"] + [line])[-30:]
        proc.wait()

    async def _watch_scan() -> None:
        await asyncio.to_thread(_pump)
        status = _scan_status[session_id]
        status["running"] = False
        status["returncode"] = proc.returncode
        status["ok"] = proc.returncode == 0
        room = sessions.get_or_create(session_id)

        # Забираем результат из БД сканера напрямую — без HTTP на самого себя.
        loaded = 0
        if status["ok"]:
            try:
                tracks = await asyncio.to_thread(load_library_from_db, db_path, scan_started_at)
                if tracks:
                    room.library_tracks = tracks
                    persistence.save_library(session_id, tracks)
                    loaded = len(tracks)
                    logger.info(f"библиотека комнаты '{session_id}' обновлена из БД: {loaded} треков")
                    _bpm_refine_done.discard(session_id)  # новый скан — новая проверка
                    _kick_bpm_refine(session_id)
                else:
                    # Пустой результат при успешном коде возврата — не затираем
                    # то, что уже было загружено раньше.
                    logger.info(f"сканирование комнаты '{session_id}' не дало треков — библиотеку не трогаю")
                    status["tail"] = (status["tail"] + ["Сканирование не нашло треков — библиотека оставлена без изменений."])[-30:]
            except Exception:
                logger.exception(f"не смог прочитать результаты сканирования из {db_path}")
                status["ok"] = False
                status["tail"] = (status["tail"] + [f"Не смог прочитать БД сканера: {db_path}"])[-30:]
        else:
            logger.info(f"сканирование комнаты '{session_id}' завершилось с кодом {proc.returncode}")
            # Вывод сканера раньше жил только в status["tail"] — то есть в
            # памяти процесса и в одной вкладке UI. В логе оставалось голое
            # «завершилось с кодом 1» без единого намёка на причину.
            for line in status["tail"][-15:]:
                logger.info(f"[скан] {line}")

        status["loaded"] = loaded
        await room.broadcast_to_chat({
            "type": "scan_finished",
            "ok": status["ok"],
            "count": loaded,
            "tail": status["tail"][-5:],
        })
        if loaded:
            await room.broadcast_to_chat({"type": "library_updated", "count": loaded})

    asyncio.create_task(_watch_scan())
    return JSONResponse({"ok": True, "started": True})


@app.get("/api/rooms/{session_id}/library/scan/status")
async def library_scan_status(session_id: str) -> JSONResponse:
    status = _scan_status.get(session_id, {"running": False, "tail": [], "ok": None, "returncode": None})
    return JSONResponse(status)


# Разделение на слои идёт подпроцессом и часами: держим его состояние
# рядом со сканом и по тем же правилам (один проход на комнату).
_stem_processes: dict[str, object] = {}
_stem_status: dict[str, dict] = {}


@app.post("/api/rooms/{session_id}/stems/build")
async def start_stems_build(session_id: str, body: dict | None = None) -> JSONResponse:
    """Кнопка «🎚 Стемы»: разделить треки папки на четыре слоя и собрать
    рядом с каждым .stem.mp4 — файл, который Mixxx играет по слоям.

    body: {"dir": путь, "backend": "auto|roformer|demucs|fast|hpss",
           "only_live": true}  — only_live не считает модель, а только
    упаковывает уже посчитанные слои в .stem.mp4 (секунды на трек)."""
    import stems as _stems

    body = body or {}
    proc = _stem_processes.get(session_id)
    if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
        raise HTTPException(status_code=409, detail="Разделение уже идёт — дождитесь конца")

    path = (body.get("dir") or "").strip()
    if not path:
        # Кнопка в скине Mixxx папку передать не может — у неё нет полей
        # ввода. Берём общего родителя путей уже отсканированной
        # библиотеки: это ровно та папка, с которой диджей и работает.
        room_ = sessions.get_or_create(session_id)
        known = [t.get("path") for t in room_.library_tracks if t.get("path")]
        if known:
            try:
                path = str(Path(os.path.commonpath(known)))
            except ValueError:
                path = ""
    if not path:
        raise HTTPException(
            status_code=400,
            detail="Не знаю, какую папку считать: библиотека ещё не сканирована. "
                   "Отсканируйте её во вкладке «Библиотека» — дальше кнопка "
                   "в Mixxx будет брать эту же папку сама.")
    if not Path(path).exists():
        raise HTTPException(status_code=400, detail=f"Папка не найдена: {path}")

    backend = body.get("backend") or "auto"
    # Отказ ДО запуска, а не через час перебора: чего не хватает, видно
    # сразу, и видно человеческим текстом с командой установки.
    problem = _stems.backend_problem(_stems.resolve_backend(backend))
    if problem and not body.get("only_live"):
        raise HTTPException(status_code=400, detail=problem)
    if _stems.stem_mp4 is None or not _stems.stem_mp4.ffmpeg_exe():
        raise HTTPException(
            status_code=400,
            detail="Для .stem.mp4 нужен ffmpeg, а его нет в PATH. "
                   "Поставьте: winget install Gyan.FFmpeg — и перезапустите DARAVE.")

    # По умолчанию — полный конвейер одной кнопкой: сперва доложить
    # роформерный вокал к трекам, у которых слои уже посчитаны (обычный
    # проход считает их готовыми и молча пропустил бы), потом досчитать
    # остальные; .stem.mp4 собирается сам в обоих этапах.
    cmd = [sys.executable, str(Path(__file__).parent / "stems.py"),
           "--dir", path, "--backend", backend]
    if body.get("only_live"):
        cmd.append("--only-live")
    elif body.get("mode", "all") == "all":
        cmd.append("--all")

    import subprocess as _sp

    try:
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                         cwd=str(Path(__file__).parent),
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не смог запустить stems.py: {exc}") from exc

    info = _stems.device_info()
    logger.info(f"стемы: запуск для '{path}', способ {backend}, "
                f"устройство {'CUDA ' + str(info.get('name')) if info.get('cuda') else 'CPU'}")
    _stem_processes[session_id] = proc
    _stem_status[session_id] = {"running": True, "tail": [], "ok": None,
                                "device": info, "backend": backend}

    def _pump() -> None:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    st = _stem_status[session_id]
                    st["tail"] = (st["tail"] + [line])[-40:]
        proc.wait()

    async def _watch() -> None:
        await asyncio.to_thread(_pump)
        st = _stem_status[session_id]
        st["running"] = False
        st["ok"] = proc.returncode == 0
        for line in st["tail"][-15:]:
            logger.info(f"[стемы] {line}")
        room = sessions.get_or_create(session_id)
        await room.broadcast_to_chat({"type": "stems_finished", "ok": st["ok"],
                                      "tail": st["tail"][-5:]})

    asyncio.create_task(_watch())
    hint = ("собираю .stem.mp4 из готовых слоёв" if body.get("only_live")
            else ("сперва вокал роформером к готовым слоям, потом остальные треки — "
                  + ("видеокарта " + str(info.get("name")) if info.get("cuda")
                     else "ПРОЦЕССОР, это долго: поставьте torch с CUDA")))
    return JSONResponse({"ok": True, "started": True, "detail": "Разделение запущено: " + hint})


@app.get("/api/rooms/{session_id}/stems/status")
async def stems_status(session_id: str) -> JSONResponse:
    import stems as _stems

    st = dict(_stem_status.get(session_id, {"running": False, "tail": [], "ok": None}))
    room = sessions.get_or_create(session_id)
    paths = [t.get("path") for t in room.library_tracks if t.get("path")]
    if paths:
        st["coverage"] = _stems.library_coverage(paths)
    return JSONResponse(st)


# --- судейский цикл: предложили шов -> послушали -> вердикт ------------
#
# Алгоритм умеет оценить темп, тональность, энергию и стыковку секций.
# Он не умеет оценить, ЛЯЖЕТ ли этот переход в этом сете — это вкус.
# Поэтому его роль здесь не решать, а сужать перебор: предложить шов,
# который стоит послушать, а решение оставить диджею.

def _room_tracks(session_id: str) -> list[dict]:
    room = sessions.get_or_create(session_id)
    if not room.library_tracks:
        raise HTTPException(status_code=400,
                            detail="Библиотека пуста — сначала отсканируйте папку с музыкой")
    return room.library_tracks


@app.get("/api/rooms/{session_id}/audition/next")
async def audition_next(session_id: str, a_path: str = "", b_path: str = "",
                        avoid_a_genre: str = "", avoid_b_genre: str = "",
                        limit: int = 1) -> JSONResponse:
    """Следующий шов на прослушивание.

    Без параметров продолжает уже одобренную цепочку с её конца — диджей
    слушает переходы подряд. a_path/b_path сужают перебор: так работают
    причины отказа («не та техника» оставляет обоих, «не тот второй
    трек» — только первого)."""
    import audition

    tracks = _room_tracks(session_id)
    items = audition.candidates(session_id, tracks, a_path=a_path or None,
                                b_path=b_path or None,
                                avoid_a_genre=avoid_a_genre or None,
                                avoid_b_genre=avoid_b_genre or None,
                                limit=max(1, min(limit, 12)))
    return JSONResponse({"candidates": items,
                         "progress": audition.progress(session_id, tracks)})


@app.post("/api/rooms/{session_id}/audition/verdict")
async def audition_verdict(session_id: str, body: dict) -> JSONResponse:
    """Приговор шву: {seam, verdict: approved|rejected, reason, note}.

    reason обязателен при отказе и определяет, что предложат дальше:
    technique | junction | second | pair."""
    import audition

    seam = body.get("seam") or {}
    if not seam.get("a_path") or not seam.get("b_path") or not seam.get("technique_id"):
        raise HTTPException(status_code=400, detail="в шве нужны a_path, b_path и technique_id")
    try:
        audition.record(session_id, seam, body.get("verdict", "approved"),
                        reason=body.get("reason"), note=body.get("note"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Отклонённое демо на диске не остаётся. Отбор — это перебор десятков
    # вариантов, и каждый оставлял бы после себя wav на несколько
    # мегабайт; за вечер это гигабайты мусора, которого диджей не просил.
    # Одобренное демо сохраняем: его ещё будут переслушивать.
    demo_file = str(body.get("demo_file") or "").strip()
    if demo_file and body.get("verdict") == "rejected":
        try:
            f = DEMOS_DIR / Path(demo_file).name
            if f.exists() and f.is_file():
                f.unlink()
        except OSError as exc:
            logger.info(f"[отбор] не смог убрать демо {demo_file}: {exc}")
    tracks = _room_tracks(session_id)
    # Сразу отдаём следующий шов: иначе интерфейсу пришлось бы делать
    # второй запрос ради каждого нажатия.
    nxt = None
    reason = body.get("reason")
    kw = {}
    if body.get("verdict") == "approved":
        # Продолжаем ровно ту последовательность, которую диджей только
        # что одобрил: уходящим в следующем шве становится трек, который
        # был входящим в этом. Опираться на «самую длинную цепочку» тут
        # нельзя — если одобренных ветвей несколько, предложение уехало бы
        # не туда, куда он смотрит.
        kw = {"a_path": seam["b_path"]}
    elif reason in ("technique", "junction"):
        kw = {"a_path": seam["a_path"], "b_path": seam["b_path"]}
    elif reason == "second":
        kw = {"a_path": seam["a_path"]}
    elif reason == "first":
        kw = {"b_path": seam["b_path"]}
    elif reason in ("genre_a", "genre_b"):
        # «Другой жанр» — это не «другой трек»: второй трек диджей
        # оставляет, а первому меняем не сам трек, а его поджанр. Без
        # этого кнопка вела себя как «другая пара» и меняла оба.
        import style as _style

        by_path = {t.get("path"): t for t in tracks}
        which = "a" if reason == "genre_a" else "b"
        held = seam["b_path"] if which == "a" else seam["a_path"]
        subject = by_path.get(seam["a_path" if which == "a" else "b_path"]) or {}
        g, sub = _style.track_genre(subject)
        kw = {("b_path" if which == "a" else "a_path"): held,
              ("avoid_a_genre" if which == "a" else "avoid_b_genre"): (sub or g or None)}
    items = audition.candidates(session_id, tracks, limit=1, **kw)
    if items:
        nxt = items[0]
    return JSONResponse({"ok": True, "next": nxt,
                         "progress": audition.progress(session_id, tracks)})


@app.get("/api/rooms/{session_id}/audition/pool")
async def audition_pool(session_id: str) -> JSONResponse:
    import audition

    tracks = _room_tracks(session_id)
    return JSONResponse({"pool": audition.pool(session_id),
                         "rejected": audition.rejections(session_id),
                         "taste": audition.taste(session_id),
                         "progress": audition.progress(session_id, tracks)})


@app.post("/api/rooms/{session_id}/audition/build")
async def audition_build(session_id: str) -> JSONResponse:
    """Сет из одобренных швов — самая длинная цепочка, какая складывается."""
    import audition

    tracks = _room_tracks(session_id)
    out = audition.build_set(session_id, tracks)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "не собрать"))
    out["progress"] = audition.progress(session_id, tracks)
    return JSONResponse(out)


@app.post("/api/rooms/{session_id}/strategy")
async def compute_strategy(session_id: str, body: dict | None = None) -> JSONResponse:
    """Строит план сета (см. mix_strategist.py) по загруженной библиотеке
    комнаты. body: {"arc_shape": "rising"|"wave"|"peak_middle", "track_names": [...]}
    (track_names — необязательный отбор подмножества библиотеки; по
    умолчанию используется вся)."""
    room = sessions.get_or_create(session_id)
    if not room.library_tracks:
        raise HTTPException(status_code=400, detail="Библиотека комнаты пуста — сначала запустите track_analysis.py --upload")

    # Библиотека могла приехать из старого скана, где BPM — заглушка
    # детектора. Чиним в фоне сами, не заставляя диджея ничего запускать;
    # текущий план при этом строится сразу, а не ждёт перемера.
    _kick_bpm_refine(session_id)

    body = body or {}
    arc_shape = body.get("arc_shape", "rising")
    track_names = body.get("track_names")
    tracks = room.library_tracks
    manual_order = body.get("track_order")
    if track_names:
        wanted = set(track_names)
        tracks = [t for t in tracks if t.get("name") in wanted]
        if not tracks:
            raise HTTPException(status_code=400, detail="Ни один из указанных треков не найден в библиотеке")

    strategy = mix_strategist.plan_strategy(
        tracks,
        arc_shape=arc_shape,
        track_order=manual_order,
        only_mixable=bool(body.get("only_mixable", True)),
        exclude=body.get("exclude"),
        # По умолчанию — сет, а не вся библиотека: пустое поле раньше
        # означало «взять все 48 треков», то есть 4.5 часа, чего никто
        # никогда не имеет в виду под «собрать микс».
        # fit_mode "natural" приходит из вкладки «Отбор»: там сет уже
        # собран из одобренных швов, и подгонять его под хронометраж
        # нельзя — сдвинутся точки, которые диджей слушал и утверждал.
        fit_mode=body.get("fit_mode") or "compress",
        target_minutes=body.get("target_minutes") or DEFAULT_SET_MINUTES,
        variant=int(body.get("variant") or 0),
        overrides=body.get("overrides"),
        include_genres=body.get("include_genres"),
        include_subgenres=body.get("include_subgenres"),
    )
    strategy["genre_breakdown"] = mix_strategist.genre_breakdown(room.library_tracks)
    room.last_strategy = strategy

    # Пишем в лог, ЧТО реально получилось с энергией: без этого «EL всё ещё
    # 3-4» невозможно отличить от «план на экране старый» — снаружи и то, и
    # другое выглядит одинаково.
    import collections as _c
    els = _c.Counter(t["el"] for t in strategy["tracks"])
    energies = [t["energy"] for t in strategy["tracks"] if t.get("energy") is not None]
    logger.info(
        "стратегия '%s': %d треков, energy %.3f..%.3f, EL %s",
        session_id, len(strategy["tracks"]),
        min(energies) if energies else 0.0, max(energies) if energies else 0.0,
        dict(sorted(els.items())),
    )
    return JSONResponse(strategy)


@app.post("/api/rooms/{session_id}/strategy/suggest-length")
async def suggest_length(session_id: str, body: dict | None = None) -> JSONResponse:
    """Подбирает длительность сета вместо диджея.

    Длительность — не косметика: она задаёт, к какой секунде трек должен
    уйти, а рядом с этой секундой может не оказаться ни брейкдауна, ни
    ямы. Диджей это услышал сам («на 120 сводит не там, на 180 почти в
    конце трека, хорошо где-то посередине»), и замер это подтвердил: доля
    точек, попавших на музыкальное событие, падает с 93% на 90-105
    минутах до 74% на 180.

    Поэтому перебираем длительности, считаем качество каждого плана
    целиком и отдаём три лучших — выбрать из трёх понятнее, чем угадывать
    минуты. Планы строятся по метаданным, без чтения аудио, поэтому весь
    перебор занимает доли секунды."""
    room = sessions.get_or_create(session_id)
    tracks = room.library_tracks or []
    if not tracks:
        raise HTTPException(status_code=400, detail="Библиотека комнаты пуста")

    # Лог на ВХОДЕ, а не только на выходе: если перебор падает, в логе
    # раньше не было ни строчки, и снаружи это выглядело как «кнопка
    # ничего не делает».
    logger.info("подбор длительности '%s': начал, треков %d", session_id, len(tracks))
    body = body or {}
    exclude = body.get("exclude")
    kwargs = dict(
        arc_shape=body.get("arc_shape", "rising"),
        track_order=body.get("track_order"),
        only_mixable=bool(body.get("only_mixable", True)),
        variant=int(body.get("variant") or 0),
        overrides=body.get("overrides"),
        exclude=exclude,
    )

    playable = [t for t in tracks
                if not exclude or t.get("name") not in set(exclude)]
    total_minutes = sum(float(t.get("duration_seconds") or 0) for t in playable) / 60.0
    lo = max(20.0, len(playable) * 0.6)          # хотя бы ~36 секунд на трек
    hi = max(lo + 20.0, min(240.0, total_minutes * 0.92))
    step = 5.0

    scored = []
    target = lo
    while target <= hi + 1e-6:
        try:
            st = mix_strategist.plan_strategy(tracks, target_minutes=target, **kwargs)
        except ValueError:
            target += step
            continue
        except Exception as exc:
            logger.exception("подбор длительности сорвался на %.0f мин", target)
            raise HTTPException(status_code=500,
                                detail=f"Не смог построить план на {target:.0f} мин: {exc}") from exc
        q = mix_strategist.plan_quality(st)
        scored.append({"minutes": round(target),
                       "actual_minutes": st.get("total_duration_minutes"),
                       "tracks": len(st.get("tracks") or []),
                       **q})
        target += step

    if not scored:
        raise HTTPException(status_code=400, detail="Не удалось построить ни одного плана")

    scored.sort(key=lambda x: -x["score"])
    # Три варианта должны отличаться заметно, иначе диджей выбирает между
    # 90, 95 и 100 минутами — это не выбор, а шум.
    picks: list[dict] = []
    for cand in scored:
        if all(abs(cand["minutes"] - p["minutes"]) >= 15 for p in picks):
            picks.append(cand)
        if len(picks) == 3:
            break

    logger.info("подбор длительности '%s': перебрано %d, лучшие %s",
                session_id, len(scored), [p["minutes"] for p in picks])
    return JSONResponse({"ok": True, "best": picks, "scanned": len(scored),
                         "curve": [{"minutes": x["minutes"], "score": x["score"]}
                                   for x in sorted(scored, key=lambda y: y["minutes"])]})


@app.post("/api/rooms/{session_id}/strategy/execute")
async def execute_strategy_transition(session_id: str, body: dict) -> JSONResponse:
    """Материализует и сразу отправляет companion'у ОДИН переход из
    последнего построенного плана — кнопка "▶" у перехода в "Стратегии".
    body: {"transition_index": int, "source_deck": "A", "target_deck": "B"}"""
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план (POST .../strategy)")

    index = body.get("transition_index")
    source = body.get("source_deck", "A")
    target = body.get("target_deck", "B")
    overrides = body.get("param_overrides")
    if index is None or not (0 <= index < len(room.last_strategy["transitions"])):
        raise HTTPException(status_code=400, detail="Некорректный transition_index")

    plan = mix_strategist.build_transition_plan(
        room.last_strategy, index, source, target, f"strategy_{session_id}_{index}", overrides,
    )
    sent = await room.send_plan_to_companion(plan)
    return JSONResponse({"ok": sent, "plan_id": plan["plan_id"]})


@app.websocket("/ws/companion/{session_id}")
async def companion_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    room = sessions.get_or_create(session_id)
    room.companion_ws = websocket
    logger.info(f"companion подключился, комната '{session_id}'")

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "hello":
                logger.info(f"hello от companion_id={msg.get('companion_id')} (комната '{session_id}')")

            elif msg_type == "telemetry":
                room.latest_telemetry = msg["decks"]
                # Транслируем сырую телеметрию в чат тоже — фронтенду пригодится
                # для отрисовки BPM/waveform-позиции, не только агенту.
                payload = {"type": "telemetry", "decks": msg["decks"]}
                if "recording_status" in msg:
                    payload["recording_status"] = msg["recording_status"]
                await room.broadcast_to_chat(payload)

            elif msg_type in ("plan_started", "plan_finished", "plan_rejected", "control_done"):
                # Companion теперь исполняет MixPlan отдельной задачей и
                # рассказывает, что с ним происходит. Транслируем в чат: без
                # этого нажатие "Выполнить" во время уже идущего перехода
                # выглядело как "кнопка не сработала".
                logger.info(f"{msg_type}: {msg.get('plan_id')} (комната '{session_id}')")
                await room.broadcast_to_chat(msg)

    except WebSocketDisconnect:
        logger.info(f"companion отключился, комната '{session_id}'")
        if room.companion_ws is websocket:
            room.companion_ws = None
        await room.broadcast_to_chat({"type": "companion_status", "connected": False})
        sessions.drop_if_empty(session_id)


@app.websocket("/ws/chat/{session_id}")
async def chat_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    room = sessions.get_or_create(session_id)
    room.chat_websockets.add(websocket)
    logger.info(f"чат-клиент подключился, комната '{session_id}'")

    # Новому клиенту (например, открывшему второй таб) сразу отдаём то, что
    # уже знаем о деках — не заставляем ждать следующего тика телеметрии.
    if room.latest_telemetry:
        await websocket.send_json({"type": "telemetry", "decks": room.latest_telemetry})
    await websocket.send_json({"type": "companion_status", "connected": room.companion_ws is not None})

    try:
        while True:
            msg = await websocket.receive_json()

            if msg.get("type") == "user_message":
                text = msg.get("text", "")
                if not text.strip():
                    continue
                try:
                    reply_text, plan, controls = await room.agent.handle_message(text, room.latest_telemetry)
                except Exception as exc:  # ошибка LLM не должна ронять WS-соединение
                    logger.exception(f"ошибка DJAgent в комнате '{session_id}'")
                    await room.broadcast_to_chat({
                        "type": "agent_reply",
                        "text": "Ошибка на стороне ассистента, попробуйте ещё раз.",
                    })
                    continue

                persistence.append_history(session_id, "user", text)
                persistence.append_history(session_id, "model", reply_text)

                await room.broadcast_to_chat({"type": "agent_reply", "text": reply_text})

                # Прямые движения ручек (set_mixxx_control) — до плана:
                # если агент и покрутил EQ, и построил переход, ручки должны
                # встать ДО того, как поедет кроссфейдер.
                for cmd in controls or []:
                    sent = await room.send_control_to_companion(cmd)
                    await room.broadcast_to_chat({
                        "type": "agent_reply",
                        "text": (f"⇒ {live_control.describe(cmd)}" if sent
                                 else f"⇒ {live_control.describe(cmd)} — companion не подключён"),
                    })

                if plan is not None:
                    sent = await room.send_plan_to_companion(plan)
                    if not sent:
                        await room.broadcast_to_chat({
                            "type": "agent_reply",
                            "text": "(companion не подключён — план не отправлен)",
                        })

            elif msg.get("type") == "command":
                action = msg.get("action", "")
                if action == "replay_last_mix":
                    if room.last_plan is None:
                        await room.broadcast_to_chat({
                            "type": "agent_reply",
                            "text": "Пока нечего повторять — ни один микс в этой комнате ещё не игрался.",
                        })
                    else:
                        sent = await room.send_plan_to_companion(room.last_plan)
                        await room.broadcast_to_chat({
                            "type": "agent_reply",
                            "text": "Повторяю последний микс." if sent else "(companion не подключён)",
                        })
                elif action == "recording_toggle":
                    sent = await room.send_command_to_companion("recording_toggle")
                    if not sent:
                        await room.broadcast_to_chat({
                            "type": "agent_reply",
                            "text": "(companion не подключён — команда записи не отправлена)",
                        })

    except WebSocketDisconnect:
        logger.info(f"чат-клиент отключился, комната '{session_id}'")
        room.chat_websockets.discard(websocket)
        sessions.drop_if_empty(session_id)
