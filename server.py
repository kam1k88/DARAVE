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
  POST /api/rooms/{session_id}/autopilot/start  — играть СЕТ в самом Mixxx:
                                            DARAVE сам грузит треки на деки,
                                            ставит иглу в нужную секунду, ждёт
                                            момент по телеметрии и сводит
                                            (mode="clip" — один стык с разбегом)
  POST /api/rooms/{session_id}/autopilot/stop   — остановить автопилот
  GET  /api/rooms/{session_id}/autopilot        — что играет и сколько до перехода
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
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

try:
    import demo_render
except ImportError:
    # numpy/scipy ещё не поставлены (requirements-analysis.txt) — backend
    # должен стартовать и без них, просто кнопка "Демо" вернёт понятную
    # ошибку вместо падения ВСЕГО backend'а при импорте.
    demo_render = None
import full_catalog
from child import child_cmd
import library_analysis as _la
import live_control
import mix_strategist
import set_export
import persistence
from track_analysis import load_library_from_db

try:
    import fix_library_bpm
except Exception:  # без numpy/librosa — просто нет автопочинки темпа
    fix_library_bpm = None
import llm_providers
from llm_providers import make_provider
from session import SessionManager
from techniques import TECHNIQUES, build_plan

# Раньше все print() уходили только в консоль отдельного окна backend'а —
# чтобы разобрать ошибку, диджею приходилось делать скриншот этого окна
# (или терять вывод вовсе, если окно уже закрыто). Теперь дублируем в файл
# рядом со скриптом — можно просто прислать/прочитать darave_backend.log.
import paths as _paths

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_paths.data_dir() / "darave_backend.log", encoding="utf-8"),
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

# ---------------------------------------------------------------- доступ
#
# Интерфейс живёт на darave.ru, а работа идёт здесь, на 127.0.0.1.
# Значит браузер шлёт сюда кросс-доменные запросы, и без этих двух
# прослоек получается либо «ничего не работает», либо «работает у кого
# угодно».
#
# CORS: список origin'ов закрытый. Со звёздочкой браузер не отдаёт
# учётные данные, и вход просто не заработает, — а заодно любая открытая
# в соседней вкладке страница смогла бы читать чужую фонотеку.
#
# Токен: без него сосед по кафейному Wi-Fi, знающий про порт 8765,
# управлял бы вашим DARAVE. Раньше это было неважно (сервер слушал
# 127.0.0.1 и UI лежал рядом), теперь — важно.
import cloud_auth

# Версия программы. Одна строка на всё: её печатает /health, её видит
# сайт при спаривании, и по ней же будет решаться, предлагать ли
# обновление.
APP_VERSION = "1.0.6"

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "DARAVE_ALLOWED_ORIGINS",
    "https://darave.ru,https://www.darave.ru").split(",") if o.strip()]

# Что открыто без входа. Ровно три вещи: поздороваться (страница должна
# уметь понять, запущена ли программа), войти и выйти.
OPEN_PATHS = {"/health", "/pair", "/unpair", "/account"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def require_account(request: "Request", call_next):
    """Пускать дальше только со свежим токеном из darave.ru."""
    path = request.url.path
    if (request.method == "OPTIONS" or path in OPEN_PATHS
            or path == "/" or path.startswith("/static/")):
        return await call_next(request)

    if os.environ.get("DARAVE_NO_AUTH") == "1":
        # Режим разработки: запускаем без сайта. Намеренно НЕ включается
        # сам по себе — только явной переменной, иначе однажды уедет в
        # сборку и откроет всё всем.
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # Тег <audio src="..."> и ссылка «скачать» заголовков НЕ ставят: их
    # грузит сам браузер, а не наш fetch. Поэтому у файлов токен едет
    # параметром — иначе кнопка «Послушать» молча отдаёт 401, и человек
    # видит подпись «играет» при полной тишине.
    #
    # Только GET: параметр в адресе оседает в логах и в Referer, и
    # позволять им ЧТО-ТО МЕНЯТЬ не стоит. Читать — можно: программа
    # слушает 127.0.0.1, и у того, кто способен подставить этот адрес,
    # токен и так уже есть.
    if not token and request.method == "GET":
        token = str(request.query_params.get("token") or "").strip()
    if not cloud_auth.token_ok(token):
        st = cloud_auth.status()
        if st.get("signed_in") is False and st.get("email"):
            detail = ("Проверка входа устарела — откройте darave.ru и войдите снова. "
                      "Это бывает раз в месяц.")
        else:
            detail = "Войдите на darave.ru — оттуда и открывается DARAVE."
        return JSONResponse({"detail": detail, "need_login": True}, status_code=401)
    return await call_next(request)


# ----------------------------------------------------------- платная часть
#
# Граница платного — решение продуктовое, и оно ровно одно, поэтому
# живёт здесь одной функцией, а не рассыпано по обработчикам.
#
# БЕСПЛАТНО — всё, что можно посмотреть и послушать:
#   разбор библиотеки любого размера, слои, подбор пар, прослушивание и
#   отбор швов, план сета (порядок, дуга, длительность) и превью
#   сведений — выборка переходов по всему сету.
#
# ПЛАТНО — унести результат:
#   свести сет целиком в файл, собрать сет из пула одобренных швов,
#   выгрузить порядок в Mixxx и Auto DJ, автопилот, проиграть переход
#   из плана на деках.
#
# Одной фразой: смотреть и слушать — сколько угодно, унести — за деньги.
#
# Почему план отдан бесплатно. Соседи продают ровно его — порядок
# треков, точки, подсказки, — а сводить всё равно приходится руками.
# Свести кодом не умеет почти никто. Отдавать бесплатно то, на чём
# зарабатывают другие, и брать за то, чего у них нет, — единственный
# способ объяснить, чем DARAVE отличается, не уговаривая.
#
# И это честная демка: человек слышит, КАК программа сводит его
# собственные треки, и упирается ровно в ту кнопку, ради которой всё
# написано. Работает даже с торрент-копии.
#
# Ограничений по размеру библиотеки и длине сета НЕТ намеренно: счётчик
# «до 50 треков» наказывает того, кто принёс свою музыку разбираться,
# то есть самого заинтересованного человека.
#
# Здесь НЕ замок, а объяснение. Настоящая проверка — только эта функция
# на сервере: страница открыта в браузере на машине пользователя, и
# любая пометка в интерфейсе снимается за секунду.
PAID_HINT = ("Сборка сета — в платной версии: 690 ₽ один раз на darave.ru. "
             "Разбор, отбор швов, план и превью сведений работают без оплаты.")


def require_paid(what: str) -> None:
    """Отказать бесплатному режиму в сборке сета — внятно.

    402 Payment Required, а не 403: по нему страница показывает не
    «доступ запрещён», а что именно платное и сколько стоит. Отказ,
    который не объясняет себя, читается как поломка — и человек уходит
    не купив, а решив, что программа сломана.
    """
    if os.environ.get("DARAVE_NO_AUTH") == "1":
        return
    st = cloud_auth.status()
    if str(st.get("tier") or "free") != "free" and not st.get("expired_tier"):
        return
    if st.get("expired_tier"):
        why = "Срок оплаты истёк, работает бесплатный режим."
    elif not st.get("signed_in"):
        why = "Вы не вошли — работает бесплатный режим."
    else:
        why = "Сейчас работает бесплатный режим."
    raise HTTPException(status_code=402, detail=f"«{what}» — платная часть. {why} {PAID_HINT}")


@app.get("/health")
async def health() -> JSONResponse:
    """Коротко: «я здесь». Открыт без входа — страница на darave.ru должна уметь
    отличить «программа не запущена» от «программа отказала»."""
    return JSONResponse({
        "app": "DARAVE",
        "version": APP_VERSION,
        "account": cloud_auth.status(),
    })


@app.get("/account")
async def account_status() -> JSONResponse:
    return JSONResponse(cloud_auth.status())


@app.post("/pair")
async def pair_with_cloud(body: dict) -> JSONResponse:
    """Принять код спаривания от страницы и проверить его на darave.ru."""
    token = str((body or {}).get("pair_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Нет кода спаривания")
    try:
        res = await asyncio.to_thread(cloud_auth.pair, token, APP_VERSION)
    except cloud_auth.PairError as exc:
        # Текст пришёл с сервера и объясняет, что делать. Подменять его
        # общим «ошибка входа» — значит отправить человека гадать.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    logger.info(f"вход: {res.get('email')}, тариф {res.get('tier')}")
    return JSONResponse(res)


@app.post("/unpair")
async def unpair() -> JSONResponse:
    cloud_auth.clear_state()
    return JSONResponse({"ok": True})


# Один провайдер на весь процесс — общий пул соединений для всех комнат, а
# не по клиенту на комнату. gemini/ollama выбирается DARAVE_LLM_PROVIDER,
# см. llm_providers.py.
_llm_provider = make_provider()
sessions = SessionManager(_llm_provider)
persistence.init_db()

CHAT_HTML_PATH = _paths.resource_dir() / "static" / "chat.html"
RECORDINGS_DIR = _paths.data_subdir("recordings")

# Локальное сканирование библиотеки прямо из веб-UI (кнопка "Сканировать" во
# вкладке "Библиотека") — см. POST .../library/scan ниже. Работает, только
# когда backend и музыка на одной машине (обычный локальный запуск рядом с
# Mixxx), потому что сканирует диск ТОГО процесса, где крутится backend, а
# не диска диджея через сеть. Для облачного деплоя (backend на VPS) кнопка
# не поможет — там по-прежнему нужен CLI track_analysis.py --upload на
# машине диджея (см. README).
DEFAULT_SET_MINUTES = 90.0  # длительность сета, если диджей не задал свою
SCAN_DB_DIR = _paths.data_subdir("scan_dbs")
_scan_processes: dict[str, "subprocess.Popen"] = {}  # обычный Popen, см. start_library_scan
_scan_status: dict[str, dict] = {}
# «[17/532] Имя файла.mp3» — то, что печатает track_analysis.py --scan
# на каждый трек. Разбираем здесь, чтобы показать полосу, а не поток строк.
_SCAN_PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")

# Офлайн аудио-демо техник (кнопка "🎧 Демо" во вкладке "Техника") —
# demo_render.py реально сводит два файла диджея по логике технике и пишет
# WAV сюда; отдаётся обратно через GET .../techniques/demo/{filename}.
# Требует requirements-analysis.txt в том же Python, что и backend (та же
# оговорка, что у сканирования — см. execute_technique/library/scan).
DEMOS_DIR = _paths.data_subdir("demos")

# Выгрузка готового сета: плейлист для Mixxx, раскладка и рендер микса.
SETS_DIR = _paths.data_subdir("sets")


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
    # Длина перехода со шва, профиль ввода и луп вокала уходящего.
    # До этого демо всегда шло 38 секунд по кроссфейду, что бы ни было
    # написано на карточке, — диджей слушал не то, что одобрял.
    seam_bars = body.get("bars")
    mix_profile = body.get("mix_profile") or None
    vocal_loop = body.get("vocal_loop")
    extra_events = body.get("extra_events") or None
    # Луп вокала уходящего включается ЯВНО (кнопкой или приёмом), а не
    # сам собой: автоматика здесь один раз уже сломала все демо разом.
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
            bars=(float(seam_bars) if seam_bars else None),
            mix_profile=mix_profile,
            vocal_loop=(float(vocal_loop) if vocal_loop else None),
            extra_events=extra_events,
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
    # Отложенные переходы держат своё демо в demos/keep — там его не
    # трогает ни чистка отказов, ни уборка по расписанию. Поэтому один
    # уровень вложенности разрешён, а выход наверх — по-прежнему нет.
    parts = [x for x in filename.replace("\\", "/").split("/") if x]
    if ".." in parts or len(parts) > 2 or (len(parts) == 2 and parts[0] != "keep"):
        raise HTTPException(status_code=404, detail="Демо-файл не найден")
    path = DEMOS_DIR.joinpath(*parts)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Демо-файл не найден")
    # Демо теперь mp3; отдаём тип по расширению, чтобы старые wav из
    # прошлых прогонов продолжали открываться.
    return FileResponse(path, media_type="audio/mpeg" if path.suffix.lower() == ".mp3"
                        else "audio/wav")


@app.get("/api/llm")
async def llm_status() -> JSONResponse:
    """Состояние разговорного ассистента для UI.

    Нужен потому, что отсутствие ключа больше не роняет программу (см.
    llm_providers.NoProvider): раз она поднимается и работает, диджей
    должен где-то увидеть, что именно выключено и почему."""
    st = llm_providers.provider_status()
    st["active"] = type(sessions.provider).__name__ != "NoProvider"
    return JSONResponse(st)


@app.post("/api/llm")
async def set_llm_key(body: dict) -> JSONResponse:
    """Вставить ключ Gemini (или адрес Ollama) прямо из браузера.

    У установленной версии нет ни переменных окружения, ни консоли —
    просить диджея «задайте GEMINI_API_KEY» бессмысленно. Ключ пишется в
    darave_secrets.json папки данных, провайдер пересоздаётся на месте,
    перезапуск не нужен.

    Ключ ПРОВЕРЯЕТСЯ настоящим запросом, и нерабочий не сохраняется:
    иначе кнопка «Подключить» гасила бы баннер, а первое же сообщение в
    чат падало бы с API_KEY_INVALID — и виноватой выглядела бы программа."""
    body = body or {}
    key = str(body.get("gemini_api_key") or "").strip()
    host = str(body.get("ollama_host") or "").strip()
    if not key and not host:
        raise HTTPException(status_code=400, detail="Нужен ключ Gemini или адрес Ollama")

    # Что было до нас — чтобы вернуть, если новое не заработало.
    prev_key = llm_providers.secret("gemini_api_key")
    prev_host = llm_providers.secret("ollama_host")
    try:
        if key:
            llm_providers.save_secret("gemini_api_key", key)
        if host:
            llm_providers.save_secret("ollama_host", host)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не смог сохранить ключ: {exc}") from exc

    def _build_and_check() -> tuple[object, bool, str]:
        prov = make_provider()
        ok, why = llm_providers.verify(prov)
        return prov, ok, why

    try:
        provider, ok, why = await asyncio.to_thread(_build_and_check)
    except Exception as exc:
        provider, ok, why = None, False, str(exc)[:200]

    if not ok:
        # Откатываем, чтобы следующий запуск не подхватил заведомо
        # нерабочий ключ и не выглядел сломанным уже без объяснений.
        try:
            llm_providers.save_secret("gemini_api_key", prev_key or "")
            llm_providers.save_secret("ollama_host", prev_host or "")
        except OSError:
            pass
        # Ответ-заглушка для чата здесь не годится: диджей только что
        # вставил ключ и ждёт вердикт по НЕМУ, а не общую справку.
        if key and (not why or why == llm_providers.NO_PROVIDER_REPLY):
            why = ("Ключ сохранить получилось, а подключиться — нет. "
                   "Чаще всего это либо неполный ключ, либо отсутствующий "
                   "пакет google-genai в сборке.")
        return JSONResponse({"ok": False, "active": False,
                             "note": why or "Провайдер не поднялся."},
                            status_code=200)

    sessions.set_provider(provider)
    st = llm_providers.provider_status()
    st["active"] = True
    return JSONResponse(st)


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
    require_paid("Выгрузить сет плейлистом")
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


@app.post("/api/rooms/{session_id}/strategy/autodj")
async def export_autodj(session_id: str, body: dict | None = None) -> JSONResponse:
    """Кладёт порядок сета в очередь Auto DJ самого Mixxx.

    Единственное место в Mixxx, где есть последовательное воспроизведение.
    Плейлист .m3u8 порядок ИГРАТЬ не заставляет — он только список путей;
    ни точки, ни приёма в него положить нельзя, потому что таких полей
    нет в модели плейлиста Mixxx (см. mixxx_autodj.py)."""
    require_paid("Отправить сет в Auto DJ")
    import mixxx_autodj

    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план (кнопка «Построить план»).")
    body = body or {}
    if not body.get("dry_run") and _mixxx_running():
        raise HTTPException(status_code=409,
                            detail="Mixxx запущен — он перезапишет библиотеку при выходе. Закройте его.")
    by_name = {t.get("name"): t.get("path")
               for t in room.library_tracks if t.get("name")}
    paths = []
    for tr in (room.last_strategy.get("tracks") or []):
        p = tr.get("path") or by_name.get(tr.get("name"))
        if p:
            paths.append(p)
    if not paths:
        raise HTTPException(status_code=400, detail="в плане нет треков с путями")
    out = mixxx_autodj.export_autodj(paths, dry_run=bool(body.get("dry_run")),
                                     replace=body.get("replace", True),
                                     log=logger.info)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "не вышло")
    return JSONResponse(out)


_preview_tasks: dict[str, asyncio.Task] = {}
_preview_status: dict[str, dict] = {}


@app.post("/api/rooms/{session_id}/strategy/render")
async def render_set(session_id: str, body: dict | None = None) -> JSONResponse:
    """ВСЕ швы сета подряд одним файлом, на одной бит-сетке.

    Не выборка: в файл идёт каждый переход плана. Тел треков между ними
    нет, поэтому 46 сведений укладываются в двадцать минут вместо
    полутора часов — слышно ровно то, ради чего это слушают, и подряд,
    как один непрерывный микс.

    Фоном, как и полная сборка. Раньше здесь считалась выборка из восьми
    переходов, и ответ успевал прийти в открытый запрос. Все сорок шесть
    считаются минутами, а браузер к тому времени давно оборвёт
    соединение — и человек увидит не результат, а ошибку сети.
    Прогресс забирается GET по тому же адресу.
    """
    if demo_render is None:
        raise HTTPException(status_code=503,
                            detail="Нужны numpy/scipy/librosa: pip install -r requirements-analysis.txt")
    room = sessions.get_or_create(session_id)
    strategy = room.last_strategy
    if strategy is None:
        # Единственная кнопка ControlC, работающая без оплаты, обязана
        # работать по-настоящему. План для неё строится ЗДЕСЬ, на один
        # раз, и НЕ кладётся в room.last_strategy: в ControlC он не
        # появится, и ни одна кнопка плана от него не оживёт. Человек
        # получает файл со всеми стыками — и ничего кроме.
        if not room.library_tracks:
            raise HTTPException(status_code=400,
                                detail="Библиотека комнаты пуста — сначала разберите её во вкладке Library.")
        _kick_bpm_refine(session_id)
        import mix_profile as _mix_profile

        body = body or {}
        strategy = mix_strategist.plan_strategy(
            room.library_tracks,
            arc_shape=body.get("arc_shape", "rising"),
            only_mixable=bool(body.get("only_mixable", True)),
            fit_mode="compress",
            target_minutes=body.get("target_minutes") or DEFAULT_SET_MINUTES,
        )
        strategy["mix_profile"] = {"entry": "manual", **_mix_profile.DEFAULTS}
    task = _preview_tasks.get(session_id)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Швы уже собираются — дождитесь окончания")

    body = body or {}
    out = SETS_DIR / f"{session_id}_{secrets.token_hex(4)}.wav"
    total = len(strategy.get("transitions") or [])
    _preview_status[session_id] = {"running": True, "done": 0, "total": total}

    def _progress(done: int, all_n: int) -> None:
        _preview_status[session_id] = {"running": True, "done": done, "total": all_n}

    async def _run() -> None:
        try:
            meta = await asyncio.to_thread(
                set_export.render_set, strategy, str(out),
                44100, float(body.get("seconds_between") or 50.0),
                float(body.get("max_minutes") or 120.0),
                float(body.get("join_bars") or 1.0),
                _progress,
                int(body.get("sample") or 0),
                str(body.get("format") or "mp3"),
            )
        except Exception as exc:
            logger.exception("не смог собрать швы")
            _preview_status[session_id] = {"running": False, "error": str(exc)}
            return
        used = meta.get("techniques_used") or {}
        name = Path(meta["path"]).name      # render_set сам меняет расширение под формат
        logger.info(f"швы собраны: {meta['transitions_rendered']}/{meta['transitions_total']} переходов, "
                    f"{meta['duration_seconds']}с, сетка {'ровная' if meta.get('grid_ok') else 'СБИТА'}, "
                    f"техник использовано: {len(used)} "
                    f"({', '.join(f'{k}x{v}' for k, v in sorted(used.items()))})")
        _preview_status[session_id] = {
            "running": False, "done": meta["transitions_rendered"],
            "total": meta["transitions_total"],
            "url": f"/api/rooms/{session_id}/sets/{name}", **meta,
        }

    _preview_tasks[session_id] = asyncio.create_task(_run())
    return JSONResponse({"ok": True, "started": True, "transitions": total})


@app.get("/api/rooms/{session_id}/strategy/render")
async def render_set_status(session_id: str) -> JSONResponse:
    return JSONResponse(_preview_status.get(session_id, {"running": False}))


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
            # Ползунки приёма из панели настройки перехода идут поверх
            # того, что подставил план: демо обязано звучать теми же
            # значениями, которые диджей видит на экране, иначе он
            # слушает одно, а в сет попадает другое.
            {**set_export._blend_override(
                 {"blend_bars": body.get("blend_bars") or tr.get("blend_bars"),
                  "bars": tr.get("bars"),
                  "mid_duck": body.get("mid_duck", tr.get("mid_duck"))}),
             **{k: v for k, v in (tr.get("params") or {}).items()},
             **{k: v for k, v in (body.get("param_overrides") or {}).items()}},
            float(body.get("max_seconds") or 45.0), 44100,
            from_at, to_at, master,
            True,
            # Длина перехода — из плана: на карточке написано «16 тактов»,
            # и звучать должно ровно столько.
            float(tr.get("bars") or 0) or None,
            body.get("mix_profile") or None,
            None,
            body.get("extra_events") or None,
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
    require_paid("Свести сет целиком")
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
                # Без потерь по умолчанию: сет забирают, чтобы слушать на
                # другой системе и резать, а mp3 для этого уже
                # проигранный этап качества.
                str(body.get("format") or "flac"),
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
             "flac": "audio/flac", "txt": "text/plain; charset=utf-8"}
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


@app.get("/api/docs")
async def docs_index() -> JSONResponse:
    """Оглавление встроенной документации."""
    import docs as _docs

    return JSONResponse({"sections": _docs.index()})


@app.get("/api/docs/{slug}")
async def docs_page(slug: str) -> JSONResponse:
    """Один раздел. Теория берётся из тех же строк, что читает ассистент,
    а справочник приёмов собирается из живого словаря TECHNIQUES —
    поэтому документация не может отстать от кода."""
    import docs as _docs

    page = _docs.page(slug)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Нет раздела {slug!r}")
    return JSONResponse(page)


@app.get("/api/rooms/{session_id}/library")
async def get_library(session_id: str, q: str = "") -> JSONResponse:
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

    # Фильтр считает СЕРВЕР, а не браузер. Разбор «100-135», «174» и «Am»
    # живёт в library_query и покрыт тестами; продублировать его на
    # JavaScript значило бы завести вторую, чуть другую версию тех же
    # правил — и расхождение вылезло бы там, где его никто не ищет.
    if q:
        import library_query as _lq

        res = _lq.filter_tracks(out, q)
        return JSONResponse({"tracks": res["tracks"], "total": res["total"],
                             "found": res["found"], "query": q,
                             "filters": _lq.describe(res["filters"])})
    return JSONResponse({"tracks": out, "total": len(out), "found": len(out)})


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
            child_cmd("track_analysis", "--scan", path, "--db", db_path),
            stdout=_sp.PIPE, stderr=_sp.STDOUT,
            cwd=str(_paths.data_dir()),
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        logger.exception("не смог запустить сканер")
        raise HTTPException(status_code=500,
                            detail=f"Не смог запустить сканер: {exc}") from exc
    logger.info(f"сканирование запущено: комната '{session_id}', папка {path}, БД {db_path}")
    _scan_processes[session_id] = proc
    _scan_status[session_id] = {"running": True, "tail": [], "ok": None,
                                "returncode": None, "done": 0, "total": 0,
                                "name": "", "started_at": scan_started_at}

    def _pump() -> None:
        """Читает вывод сканера в отдельном потоке и ждёт его конца.

        Сканер печатает по строке на трек: «[17/532] Имя файла.mp3».
        Раньше эти строки просто складывались в хвост, и человек видел
        бегущие имена, но не видел, сколько осталось, — а на библиотеке
        в тысячу треков это разница между «идёт» и «идёт ещё час».
        Поэтому строку прогресса разбираем в числа и в хвост НЕ кладём:
        хвост читают, когда что-то сломалось, и настоящая причина не
        должна вытесняться оттуда именами файлов."""
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                status = _scan_status[session_id]
                m = _SCAN_PROGRESS_RE.match(line)
                if m:
                    status["done"] = int(m.group(1))
                    status["total"] = int(m.group(2))
                    status["name"] = m.group(3)
                else:
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


@app.post("/api/rooms/{session_id}/library/metadata")
async def library_metadata(session_id: str, body: dict | None = None) -> JSONResponse:
    """Дописать библиотеке стиль из открытых каталогов.

    Отдельной кнопкой, а не внутри сканирования: MusicBrainz разрешает
    один запрос в секунду, и 70 треков — это минута с лишним. Внутри
    скана это выглядело бы как «скан стал вдвое медленнее без причины».

    Токен Discogs — бесплатный (discogs.com/settings/developers) и нужен
    только ради СТИЛЕЙ: именно там написано «Neurofunk», а не
    «Electronic». Без него работает один MusicBrainz, и ответ честно
    говорит, чего не спрашивали."""
    import metadata as _metadata

    room = sessions.get_or_create(session_id)
    tracks = room.library_tracks or []
    if not tracks:
        raise HTTPException(status_code=400, detail="Библиотека пуста — сначала отсканируйте папку.")
    body = body or {}
    token = _metadata.resolve_discogs_token(body.get("discogs_token"))
    limit = int(body.get("limit") or 0)
    subset = tracks[:limit] if limit > 0 else tracks
    report = await asyncio.to_thread(
        _metadata.enrich_library, subset, token,
        bool(body.get("only_missing", True)), lambda *_a, **_k: None)
    # Поджанр из каталога дописываем только туда, где своего не было:
    # тег в файле и разбор имени — источники диджея, каталог лишь
    # заполняет пустоты.
    filled = 0
    for t in subset:
        sub = ((t.get("meta") or {}).get("subgenre"))
        if not sub:
            continue
        tags = dict(t.get("tags") or {})
        g = dict(tags.get("genre") or {})
        if not g.get("subgenre"):
            g["subgenre"] = sub
            g["subgenre_source"] = "discogs"
            tags["genre"] = g
            t["tags"] = tags
            filled += 1
    logger.info("каталоги: проверено %d, найдено %d, поджанров дописано %d",
                report.get("checked", 0), report.get("found", 0), filled)
    return JSONResponse({"ok": True, "subgenres_filled": filled,
                         "discogs": bool(token), **report})


@app.post("/api/rooms/{session_id}/library/catalog-search")
async def library_catalog_search(session_id: str, body: dict | None = None) -> JSONResponse:
    """Поиск по MusicBrainz и Discogs — строка поиска в библиотеке.

    Каталоги отдают СВЕДЕНИЯ о треках, но не файлы, и искать по темпу и
    тональности в них нельзя: там этого просто не хранят. Поэтому ответ
    несёт `note` с этой оговоркой, а не делает вид, что нашёл всё."""
    import metadata as _metadata

    body = body or {}
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Пустой запрос.")
    token = _metadata.resolve_discogs_token(body.get("discogs_token"))
    res = await asyncio.to_thread(
        _metadata.search_catalog, query, str(body.get("kind") or "release"),
        int(body.get("limit") or 10), token)
    return JSONResponse({"ok": True, **res})


@app.get("/api/rooms/{session_id}/library/scan/status")
async def library_scan_status(session_id: str) -> JSONResponse:
    status = _scan_status.get(session_id, {"running": False, "tail": [], "ok": None,
                                           "returncode": None, "done": 0, "total": 0, "name": ""})
    return JSONResponse(status)

# --- единый анализ библиотеки -----------------------------------------
#
# Раньше во вкладке "Библиотека" стояло пять кнопок: сканировать, стемы,
# метки, перемерить темп, убрать мусор. Порядок между ними был неписаным
# (стемы после скана, метки после стемов), нарушение порядка молча давало
# неверный результат, а каждая кнопка считала СВОЁ заново — добавление
# одного трека стоило столько же, сколько первый проход.
#
# Здесь они собраны в один конвейер с двумя правилами:
#   * порядок задан кодом (library_analysis.order_steps), а не памятью
#     диджея и не тем, в каком порядке клиент прислал галочки;
#   * каждый шаг сначала считает, чего не хватает, и пропускается целиком,
#     если не хватает нечего. Пропуск — это нормальный исход шага, а не
#     ошибка, и он так и показывается.
#
# Шаг "cues" (метки в Mixxx) выключен по умолчанию сознательно: Mixxx
# держит свою библиотеку в памяти и при выходе перезаписывает файл, то
# есть запись меток при запущенном Mixxx их теряет. Шаг это проверяет сам
# и отказывается, а не портит базу.

ANALYZE_STEPS = _la.ANALYZE_STEPS
ANALYZE_DEFAULT = _la.ANALYZE_DEFAULT

_analyze_tasks: dict[str, asyncio.Task] = {}
_analyze_status: dict[str, dict] = {}


def _analyze_step(session_id: str, key: str) -> dict | None:
    for st in _analyze_status.get(session_id, {}).get("steps", []):
        if st["key"] == key:
            return st
    return None


async def _analyze_push(session_id: str) -> None:
    room = sessions.get_or_create(session_id)
    await room.broadcast_to_chat({"type": "analyze_progress",
                                  **_analyze_status.get(session_id, {})})


def _parse_scan_line(line: str) -> dict | None:
    """«[17/532] Имя файла.mp3» -> числа для полосы.

    Возврат None означает «это обычная строка, положи её в хвост»."""
    m = _SCAN_PROGRESS_RE.match(line)
    if not m:
        return None
    return {"i": int(m.group(1)), "total": int(m.group(2)),
            "name": m.group(3), "stage": ""}


async def _run_subprocess_step(session_id: str, key: str, cmd: list[str],
                               parse=None) -> dict:
    """Запуск подпроцесса с живым прогрессом.

    Вывод читается в потоке (на Windows asyncio-подпроцессы требуют
    ProactorEventLoop, а uvicorn поднимает селекторный — см. историю
    /library/scan), а разобранные строки прогресса кладутся прямо в
    состояние шага."""
    import subprocess as _sp

    st = _analyze_step(session_id, key) or {}
    tail: list[str] = []
    loop = asyncio.get_running_loop()
    try:
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                         cwd=str(_paths.data_dir()),
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return {"state": "fail", "note": f"не смог запустить: {exc}"}

    def _pump() -> None:
        if proc.stdout is None:
            proc.wait()
            return
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            upd = parse(line) if parse else None
            if upd is None:
                # Машинные строки прогресса в хвост НЕ кладём: хвост
                # читает человек, когда что-то пошло не так, и JSON по
                # строке на трек вытеснял бы из него настоящую причину.
                tail.append(line)
                del tail[:-40]
            if upd:
                st.update({"done": int(upd.get("i", 0)),
                           "total": int(upd.get("total", 0)),
                           "name": str(upd.get("name", "")),
                           "stage": str(upd.get("stage", ""))})
                # Толкаем в UI из рабочего потока: событийный цикл живёт в
                # другом, и без call_soon_threadsafe это гонка.
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_analyze_push(session_id)))
        proc.wait()

    await asyncio.to_thread(_pump)
    for line in tail[-10:]:
        logger.info(f"[анализ:{key}] {line}")
    if proc.returncode != 0:
        return {"state": "fail", "note": f"код возврата {proc.returncode}",
                "tail": tail[-5:]}
    return {"state": "ok", "tail": tail[-5:]}


async def _analyze_run(session_id: str, path: str, steps: list[str],
                       force: bool, backend: str) -> None:
    import stems as _stems

    room = sessions.get_or_create(session_id)
    db_path = _room_db_path(session_id)

    async def _mark(key: str, **kw) -> None:
        st = _analyze_step(session_id, key)
        if st is not None:
            st.update(kw)
        await _analyze_push(session_id)

    try:
        for key in steps:
            _analyze_status[session_id]["current"] = key
            await _mark(key, state="run")
            try:
                res = await _analyze_one(session_id, key, path, force, backend,
                                         db_path, room, _stems)
            except asyncio.CancelledError:
                # Конвейер сняли (перезапуск backend'а, закрытие цикла).
                # Шаг обязан перестать выглядеть идущим: иначе состояние
                # навсегда остаётся running=True, кнопка заблокирована, а
                # повторный запуск отвечает 409 «уже идёт» — и выглядит
                # это как намертво зависший анализ.
                await _mark(key, state="fail", note="прервано")
                raise
            except Exception as exc:                 # noqa: BLE001
                logger.exception(f"[анализ] шаг {key} упал")
                res = {"state": "fail", "note": str(exc)[:200]}
            await _mark(key, **res)
    finally:
        _analyze_status[session_id]["running"] = False
        _analyze_status[session_id]["current"] = None
        _analyze_status[session_id]["finished_at"] = time.time()
    await _analyze_push(session_id)
    done = [s for s in _analyze_status[session_id]["steps"] if s["state"] == "ok"]
    skipped = [s for s in _analyze_status[session_id]["steps"] if s["state"] == "skip"]
    failed = [s for s in _analyze_status[session_id]["steps"] if s["state"] == "fail"]
    await room.broadcast_to_chat({
        "type": "system",
        "text": ("Анализ библиотеки закончен: сделано "
                 + (", ".join(s["title"].lower() for s in done) or "ничего")
                 + (f"; нечего делать — {len(skipped)} шагов" if skipped else "")
                 + ("; НЕ вышло: " + ", ".join(f"{s['title']} ({s['note']})" for s in failed)
                    if failed else "")),
    })


async def _analyze_one(session_id: str, key: str, path: str, force: bool,
                       backend: str, db_path: str, room, _stems) -> dict:
    """Один шаг конвейера. Возвращает {state, note, ...}.

    state: ok — сделали; skip — делать было нечего; fail — не вышло."""
    tracks = room.library_tracks or []
    paths = [t.get("path") for t in tracks if t.get("path")]

    if key == "scan":
        if not path:
            return {"state": "skip", "note": "папка не указана — беру библиотеку как есть"}
        if not Path(path).exists():
            return {"state": "fail", "note": f"папка не найдена: {path}"}
        started = time.time()
        cmd = child_cmd("track_analysis", "--scan", path, "--db", db_path)
        if force:
            cmd.append("--rescan-all")
        # parse=_parse_scan_line — без него шаг «Разбор треков» показывал
        # только бегущие имена файлов: полоса стояла пустой, и на
        # библиотеке в тысячу треков было не понять, идёт это пять минут
        # или час. Числа сканер печатает сам, оставалось их прочитать.
        res = await _run_subprocess_step(session_id, key, cmd,
                                         parse=_parse_scan_line)
        if res["state"] != "ok":
            return res
        fresh = await asyncio.to_thread(load_library_from_db, db_path, started)
        if not fresh:
            return {"state": "fail", "note": "скан не дал треков — библиотеку не трогаю"}
        room.library_tracks = fresh
        persistence.save_library(session_id, fresh)
        _bpm_refine_done.discard(session_id)
        await room.broadcast_to_chat({"type": "library_updated", "count": len(fresh)})
        return {"state": "ok", "done": len(fresh), "total": len(fresh),
                "note": f"{len(fresh)} треков в библиотеке"}

    if key == "bpm":
        if fix_library_bpm is None:
            return {"state": "skip", "note": "нет numpy/scipy/librosa"}
        if not tracks:
            return {"state": "skip", "note": "библиотека пуста"}
        if force:
            _bpm_refine_done.discard(session_id)
        await _refine_room_bpm(session_id, force=force)
        st = _bpm_refine_status.get(session_id, {})
        if st.get("skipped"):
            return {"state": "skip", "note": st["skipped"]}
        if st.get("error"):
            return {"state": "fail", "note": "перемер не отработал — см. лог"}
        return {"state": "ok", "done": st.get("done", 0), "total": st.get("done", 0),
                "note": f"темп поправлен у {st.get('changed', 0)} треков"}

    if key == "stems":
        if not paths:
            return {"state": "skip", "note": "библиотека пуста"}
        todo = [p for p in paths if not _stems.has_stems(p)]
        if not todo and not force:
            return {"state": "skip", "done": len(paths), "total": len(paths),
                    "note": f"слои есть у всех {len(paths)} треков"}
        problem = _stems.backend_problem(_stems.resolve_backend(backend))
        if problem:
            return {"state": "fail", "note": problem}
        root = path or _la.common_root(paths)
        if not root:
            return {"state": "fail", "note": "не понял, какую папку считать"}
        cmd = child_cmd("stems", "--dir", root, "--backend", backend, "--all")
        res = await _run_subprocess_step(session_id, key, cmd,
                                         parse=_la.parse_progress_line)
        cov = _stems.library_coverage(paths)
        # Карты энергии и барабанов считались при СКАНЕ, то есть до того,
        # как появились слои: тогда дроп искался по возврату баса, а в
        # фонке бас звучит непрерывно, и карта оставалась без единого
        # дропа. Теперь слои есть — пересчитываем по ним.
        #
        # Инкрементальный скан сам этого не сделает: файл не менялся,
        # отпечаток совпадает, трек пропускается. Без этой строки карта,
        # посчитанная без барабанов, оставалась бы с треком навсегда.
        if res.get("state") == "ok" and cov.get("with_stems"):
            _bpm_refine_done.discard(session_id)
            await _refine_room_bpm(session_id, force=True)
        res.setdefault("note", "")
        res["note"] = f"слои у {cov['with_stems']} из {cov['tracks']}, .stem.mp4 у {cov['live_mp4']}"
        res["done"] = cov["with_stems"]
        res["total"] = cov["tracks"]
        return res

    if key == "vocals":
        if not paths:
            return {"state": "skip", "note": "библиотека пуста"}
        todo = [p for p in paths
                if _stems.has_stems(p) and (force or _stems.vocal_map(p) is None)]
        if not todo:
            return {"state": "skip", "note": "карты вокала посчитаны у всех, у кого есть слои"}
        def _work() -> dict:
            return _stems.vocal_maps_library(todo, rebuild=force,
                                             log=lambda m: logger.info(f"[вокал] {m}"))
        r = await asyncio.to_thread(_work)
        return {"state": "ok", "done": r.get("checked", 0), "total": len(todo),
                "note": f"посчитано {r.get('checked', 0)}, с вокалом {r.get('with_vocal', 0)}"}

    if key == "catalog":
        import metadata as _metadata

        if not tracks:
            return {"state": "skip", "note": "библиотека пуста"}
        token = _metadata.resolve_discogs_token(None)
        todo = [t for t in tracks
                if force or not ((t.get("meta") or {}).get("sources"))]
        if not todo:
            return {"state": "skip", "note": "каталоги уже опрошены по всем трекам"}
        step = _analyze_step(session_id, key) or {}
        step["total"] = len(todo)
        loop = asyncio.get_running_loop()

        def _tick(i: int, total: int) -> None:
            step["done"] = i
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_analyze_push(session_id)))

        def _work() -> dict:
            return _metadata.enrich_library(
                todo, token, not force,
                lambda m: logger.info(f"[каталоги] {m}"), _tick)

        report = await asyncio.to_thread(_work)
        filled = 0
        for t in todo:
            sub = (t.get("meta") or {}).get("subgenre")
            if not sub:
                continue
            tg = dict(t.get("tags") or {})
            g = dict(tg.get("genre") or {})
            if not g.get("subgenre"):
                g["subgenre"] = sub
                g["subgenre_source"] = "discogs"
                tg["genre"] = g
                t["tags"] = tg
                filled += 1
        persistence.save_library(session_id, room.library_tracks)
        await room.broadcast_to_chat({"type": "library_updated",
                                      "count": len(room.library_tracks)})
        note = (f"опрошено {report.get('checked', 0)}, найдено {report.get('found', 0)}, "
                f"поджанров дописано {filled}")
        if not token:
            note += " — без токена Discogs стилей не будет, один MusicBrainz"
        return {"state": "ok", "done": report.get("checked", 0), "total": len(todo),
                "note": note}

    if key == "cleanup":
        import housekeeping

        r = await asyncio.to_thread(housekeeping.session_cleanup, False, logger.info)
        removed = int(r.get("removed_count", 0) or 0)
        merged = int((r.get("library_dedupe") or {}).get("merged", 0) or 0)
        if not removed and not merged:
            return {"state": "skip", "note": "мусора нет"}
        note = f"убрано {removed} файлов, {r.get('freed_mb', 0)} МБ"
        if merged:
            note += f"; схлопнуто дублей по регистру пути: {merged}"
        return {"state": "ok", "done": removed, "total": removed, "note": note}

    if key == "cues":
        import mixxx_cues
        import mixxx_process

        if not tracks:
            return {"state": "skip", "note": "библиотека пуста"}
        if mixxx_process.is_running():
            # Не «попробуем и посмотрим»: Mixxx держит библиотеку в памяти
            # и при выходе перезаписывает файл — метки, записанные сейчас,
            # он сотрёт, и выглядеть это будет как «метки не работают».
            return {"state": "fail",
                    "note": "Mixxx запущен — он перезапишет базу при выходе. Закройте его."}
        r = await asyncio.to_thread(mixxx_cues.export_library, tracks, None,
                                    False, logger.info, 8)
        if not r.get("ok"):
            return {"state": "fail", "note": str(r.get("error") or "не вышло")[:200]}
        return {"state": "ok", "done": r.get("tracks", 0), "total": len(tracks),
                "note": f"метки расставлены у {r.get('tracks', 0)} треков"}

    return {"state": "skip", "note": "шаг не знаю"}


@app.post("/api/rooms/{session_id}/library/analyze")
async def analyze_library(session_id: str, body: dict | None = None) -> JSONResponse:
    """Кнопка «Анализ библиотеки» — один проход по всем шагам.

    body: {"path": папка, "steps": [...], "force": false, "backend": "auto"}
    Без steps берётся ANALYZE_DEFAULT (всё, кроме меток в Mixxx).
    force=true считает заново даже то, что уже посчитано."""
    body = body or {}
    task = _analyze_tasks.get(session_id)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Анализ уже идёт — дождитесь конца")
    if _scan_is_running(session_id):
        raise HTTPException(status_code=409,
                            detail="Идёт отдельное сканирование — дождитесь его конца")

    # Порядок наш, а не присланный: клиент говорит ЧТО, модуль — В КАКОМ ПОРЯДКЕ.
    steps = _la.order_steps(body.get("steps") or ANALYZE_DEFAULT)
    if not steps:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного шага")
    path = str(body.get("path") or "").strip()
    if not path:
        room = sessions.get_or_create(session_id)
        path = _la.common_root([t.get("path") for t in room.library_tracks if t.get("path")])

    _analyze_status[session_id] = _la.blank_status(steps)
    _analyze_status[session_id]["started_at"] = time.time()
    _analyze_tasks[session_id] = asyncio.create_task(
        _analyze_run(session_id, path, steps, bool(body.get("force")),
                     str(body.get("backend") or "auto")))
    return JSONResponse({"ok": True, "started": True, "steps": steps, "path": path})


@app.get("/api/rooms/{session_id}/library/analyze/plan")
async def analyze_library_plan(session_id: str) -> JSONResponse:
    """Что будет сделано, ЕСЛИ нажать. Считается до запуска и ничего не трогает.

    Пять прежних кнопок спрашивали каждая про своё: уборка показывала
    список на удаление, метки — сколько записей найдено в медиатеке
    Mixxx. Сложив кнопки в одну, эти вопросы легко было потерять — а это
    ровно те два шага, которые НЕОБРАТИМЫ (удаление файлов) и пишут в
    ЧУЖУЮ базу (Mixxx). Поэтому предупреждение осталось, только теперь
    оно одно на весь проход, а не пять подряд."""
    import housekeeping
    import metadata as _metadata
    import stems as _stems

    room = sessions.get_or_create(session_id)
    tracks = room.library_tracks or []
    missing = _la.missing_report(tracks, _stems.has_stems, _stems.vocal_map)

    dry = await asyncio.to_thread(housekeeping.session_cleanup, True, None)
    missing["cleanup"] = int(dry.get("removed_count", 0) or 0)
    missing["cleanup_mb"] = dry.get("freed_mb", 0)

    # -1 = «посчитать заранее нельзя». У скана это честно: сколько файлов
    # новых, видно только обойдя папку, а обходить её дважды ради цифры на
    # кнопке — это половина стоимости самого скана. У меток — тоже: их
    # число известно только после сверки с медиатекой Mixxx.
    counts = {"scan": -1, "bpm": max(missing.get("bpm", 0), missing.get("maps", 0)),
              "stems": missing.get("stems", 0), "vocals": missing.get("vocals", 0),
              "catalog": missing.get("catalog", 0),
              "cleanup": missing["cleanup"], "cues": -1}
    steps = [{"key": k, "title": _la.ANALYZE_TITLES[k], "why": _la.ANALYZE_WHY[k],
              "missing": counts.get(k, 0), "default": k in _la.ANALYZE_DEFAULT}
             for k in _la.ANALYZE_STEPS]
    return JSONResponse({
        "tracks": len(tracks),
        "root": _la.common_root([t.get("path") for t in tracks if t.get("path")]),
        "steps": steps, "missing": missing,
        "cleanup_mb": missing["cleanup_mb"],
        "discogs": bool(_metadata.resolve_discogs_token(None)),
    })


@app.get("/api/rooms/{session_id}/library/analyze")
async def analyze_library_status(session_id: str) -> JSONResponse:
    st = _analyze_status.get(session_id)
    if st is None:
        return JSONResponse({"running": False, "steps": []})
    task = _analyze_tasks.get(session_id)
    if st.get("running") and (task is None or task.done()):
        # Состояние — источник правды для UI, а живёт оно в памяти рядом с
        # задачей. Если задача умерла, не сняв флаг (её сняли, процесс
        # перезапустили), то без этой сверки опрос крутится вечно и кнопка
        # не разблокируется никогда. Спрашиваем саму задачу, а не флаг.
        st["running"] = False
        st["current"] = None
        st.setdefault("finished_at", time.time())
        for step in st.get("steps", []):
            if step["state"] == "run":
                step.update(state="fail", note=step.get("note") or "прервано")
    return JSONResponse(st)



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
    cmd = child_cmd("stems", "--dir", path, "--backend", backend)
    if body.get("only_live"):
        cmd.append("--only-live")
    elif body.get("mode", "all") == "all":
        cmd.append("--all")

    import subprocess as _sp

    try:
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                         cwd=str(_paths.data_dir()),
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


# --- пополнение библиотеки и управление ею -----------------------------
#
# Три вещи, которые раньше делались руками вне DARAVE: взять новые треки,
# подобрать в библиотеке нужное и выкинуть лишнее.
#
# Про источник загрузки. Здесь только Jamendo — каталог под лицензиями
# Creative Commons, где право на скачивание прописано у каждого трека
# отдельным полем и проверяется перед загрузкой. Качалок из стриминговых
# сервисов в DARAVE нет и не будет: они достают файлы, которые подписка
# даёт слушать, но не отдавать. Для купленного и скачанного самому есть
# папка-приёмник — она принимает ЛЮБУЮ музыку, просто не добывает её.

_fetch_tasks: dict[str, asyncio.Task] = {}
_fetch_status: dict[str, dict] = {}
_inbox_tasks: dict[str, asyncio.Task] = {}
_inbox_state: dict[str, dict] = {}

DEFAULT_MUSIC_DIR = str(Path.home() / "Music")


@app.post("/api/rooms/{session_id}/library/fetch/search")
async def fetch_search(session_id: str, body: dict | None = None) -> JSONResponse:
    """Посмотреть, что есть в каталоге, ДО загрузки.

    Отдельным шагом, потому что «скачай сорок треков» вслепую — это
    сорок файлов, о которых диджей узнаёт постфактум. Сначала видно, что
    нашлось, потом решение."""
    import music_fetch as _mf

    body = body or {}
    client_id = _mf.resolve_client_id(body.get("client_id"))
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="Нет client_id Jamendo. Бесплатный ключ: developer.jamendo.com — "
                   f"положите его в {_mf.SECRETS_FILE} полем jamendo_client_id "
                   "или в переменную JAMENDO_CLIENT_ID.")
    lo, hi = body.get("bpm_lo"), body.get("bpm_hi")
    plan = _mf.plan_fetch(int(body.get("want") or 20),
                          float(lo) if lo else None, float(hi) if hi else None)
    tags = _mf.tags_for(body.get("genre"), body.get("tags"))
    try:
        items = await asyncio.to_thread(
            _mf.search, client_id, tags, plan["speed"],
            plan["candidates"], int(body.get("offset") or 0),
            str(body.get("order") or "popularity_total"), True,
            bool(body.get("allow_nc", True)))
    except _mf.FetchError as exc:
        raise HTTPException(status_code=502, detail=f"Каталог не ответил: {exc}") from exc
    return JSONResponse({"ok": True, "items": items, "tags": tags, **plan})


async def _fetch_run(session_id: str, items: list[dict], dest: str,
                     want: int, lo: float | None, hi: float | None) -> None:
    """Скачать и проверить темп. Не попавшее в диапазон — в корзину.

    Темп проверяется ИЗМЕРЕНИЕМ, а не полем каталога: у Jamendo его нет
    вовсе, есть только корзина скорости пятью ступенями. Меряем тем же
    анализатором, что и всю библиотеку, — иначе у скачанных треков был бы
    свой темп, а у своих свой."""
    import music_fetch as _mf
    import track_analysis as _ta

    st = _fetch_status[session_id]
    room = sessions.get_or_create(session_id)
    narrowing = lo is not None or hi is not None
    kept: list[str] = []

    async def push() -> None:
        await room.broadcast_to_chat({"type": "fetch_progress", **st})

    try:
        for i, item in enumerate(items):
            if len(kept) >= want:
                st["note"] = f"набрал {want} — остальное не качаю"
                break
            st.update(done=i, total=len(items), kept=len(kept),
                      name=f"{item.get('artist') or '?'} — {item.get('title') or '?'}")
            await push()
            r = await asyncio.to_thread(_mf.download, item, dest)
            if not r.get("ok"):
                st["failed"] = st.get("failed", 0) + 1
                st.setdefault("errors", []).append(
                    f"{item.get('title') or item.get('id')}: {r.get('error')}")
                del st["errors"][:-8]
                continue
            path = r["path"]
            if not narrowing:
                kept.append(path)
                continue
            try:
                info = await asyncio.to_thread(_ta.analyze_track, path)
                bpm = info.get("bpm")
            except Exception as exc:                 # noqa: BLE001
                logger.info(f"[загрузка] не смог померять темп {path}: {exc}")
                bpm = None
            if _mf.in_range(bpm, lo, hi):
                kept.append(path)
            else:
                # Мимо диапазона — убираем. В корзину, а не os.remove:
                # ошибка измерения не должна стоить диджею файла.
                moved = _to_trash(path)
                st["dropped"] = st.get("dropped", 0) + 1
                logger.info(f"[загрузка] {Path(path).name}: {bpm} вне {lo}-{hi} -> {moved}")
        st.update(done=len(items), kept=len(kept))
    finally:
        st["running"] = False
        st["finished_at"] = time.time()
    await push()
    await room.broadcast_to_chat({
        "type": "system",
        "text": (f"Загрузка закончена: оставлено {len(kept)} треков в {dest}"
                 + (f", отброшено по темпу {st.get('dropped', 0)}" if st.get("dropped") else "")
                 + (f", не скачалось {st.get('failed', 0)}" if st.get("failed") else "")
                 + ". Нажмите «Анализ библиотеки», чтобы они попали в подбор."),
    })


@app.post("/api/rooms/{session_id}/library/fetch")
async def fetch_start(session_id: str, body: dict | None = None) -> JSONResponse:
    """Скачать найденное. body: {items | genre/tags/want/bpm_lo/bpm_hi, dir}."""
    import music_fetch as _mf

    body = body or {}
    task = _fetch_tasks.get(session_id)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Загрузка уже идёт — дождитесь конца")

    dest = str(body.get("dir") or "").strip() or DEFAULT_MUSIC_DIR
    parent = Path(dest).parent
    if not parent.exists():
        raise HTTPException(status_code=400,
                            detail=f"Некуда класть: нет папки {parent}")
    want = max(1, int(body.get("want") or 20))
    lo = float(body["bpm_lo"]) if body.get("bpm_lo") else None
    hi = float(body["bpm_hi"]) if body.get("bpm_hi") else None

    items = body.get("items")
    if not items:
        client_id = _mf.resolve_client_id(body.get("client_id"))
        if not client_id:
            raise HTTPException(status_code=400, detail="Нет client_id Jamendo — см. поиск")
        plan = _mf.plan_fetch(want, lo, hi)
        try:
            items = await asyncio.to_thread(
                _mf.search, client_id, _mf.tags_for(body.get("genre"), body.get("tags")),
                plan["speed"], plan["candidates"], 0,
                str(body.get("order") or "popularity_total"), True,
                bool(body.get("allow_nc", True)))
        except _mf.FetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = [i for i in items if i.get("downloadable") and i.get("download_url")]
    if not items:
        raise HTTPException(status_code=400,
                            detail="Ни одного трека, который автор разрешил скачивать.")

    _fetch_status[session_id] = {"running": True, "done": 0, "total": len(items),
                                 "kept": 0, "dropped": 0, "failed": 0, "dir": dest,
                                 "name": "", "note": "", "errors": []}
    _fetch_tasks[session_id] = asyncio.create_task(
        _fetch_run(session_id, items, dest, want, lo, hi))
    return JSONResponse({"ok": True, "started": True, "candidates": len(items), "dir": dest})


@app.get("/api/rooms/{session_id}/library/fetch")
async def fetch_status(session_id: str) -> JSONResponse:
    st = _fetch_status.get(session_id)
    if st is None:
        return JSONResponse({"running": False})
    task = _fetch_tasks.get(session_id)
    if st.get("running") and (task is None or task.done()):
        st["running"] = False           # см. ту же сверку в /library/analyze
    return JSONResponse(st)


# ------------------------------------------------------ папка-приёмник

async def _inbox_watch(session_id: str, folder: str, interval: float) -> None:
    """Следит за папкой и разбирает то, что в неё положили.

    Ждёт, пока файл перестанет расти: копирование флака с флешки идёт
    секунды, а разбор половины файла даёт мусорный темп и мусорную
    структуру — и этот мусор остаётся в библиотеке навсегда, потому что
    второй раз трек уже не разберут (отпечаток совпадает)."""
    import music_fetch as _mf

    state = _inbox_state[session_id]
    room = sessions.get_or_create(session_id)
    sizes: dict[str, int] = {}
    seen: set[str] = set()
    try:
        while state.get("enabled"):
            fresh = await asyncio.to_thread(_mf.inbox_scan, folder, seen)
            ready, sizes = await asyncio.to_thread(_mf.settled, fresh, sizes)
            state["waiting"] = len(fresh) - len(ready)
            if ready:
                seen.update(ready)
                state["added"] = state.get("added", 0) + len(ready)
                state["last"] = [Path(p).name for p in ready[-5:]]
                logger.info(f"[приёмник] новых файлов: {len(ready)}")
                await room.broadcast_to_chat({
                    "type": "system",
                    "text": (f"В папке-приёмнике {len(ready)} новых треков "
                             f"({', '.join(state['last'][:3])}"
                             + ("…" if len(ready) > 3 else "") + "). Разбираю."),
                })
                # Разбираем тем же конвейером, что и всё остальное: он сам
                # пропустит уже разобранное и посчитает только новое.
                if not (_analyze_tasks.get(session_id) and
                        not _analyze_tasks[session_id].done()):
                    steps = _la.order_steps(["scan", "bpm"])
                    _analyze_status[session_id] = _la.blank_status(steps)
                    _analyze_status[session_id]["started_at"] = time.time()
                    _analyze_tasks[session_id] = asyncio.create_task(
                        _analyze_run(session_id, folder, steps, False, "auto"))
            await asyncio.sleep(max(3.0, float(interval)))
    except asyncio.CancelledError:
        pass
    finally:
        state["enabled"] = False


@app.post("/api/rooms/{session_id}/library/inbox")
async def inbox_config(session_id: str, body: dict | None = None) -> JSONResponse:
    """Включить/выключить слежение за папкой. body: {dir, enabled, interval}."""
    body = body or {}
    enabled = bool(body.get("enabled", True))
    task = _inbox_tasks.get(session_id)
    if task is not None and not task.done():
        _inbox_state.get(session_id, {})["enabled"] = False
        task.cancel()
    if not enabled:
        _inbox_state[session_id] = {"enabled": False, "dir": "", "added": 0}
        return JSONResponse({"ok": True, "enabled": False})

    folder = str(body.get("dir") or "").strip() or DEFAULT_MUSIC_DIR
    if not Path(folder).exists():
        raise HTTPException(status_code=400, detail=f"Папка не найдена: {folder}")
    _inbox_state[session_id] = {"enabled": True, "dir": folder, "added": 0,
                                "waiting": 0, "last": []}
    _inbox_tasks[session_id] = asyncio.create_task(
        _inbox_watch(session_id, folder, float(body.get("interval") or 15.0)))
    logger.info(f"[приёмник] слежу за {folder}")
    return JSONResponse({"ok": True, "enabled": True, "dir": folder})


@app.get("/api/rooms/{session_id}/library/inbox")
async def inbox_status(session_id: str) -> JSONResponse:
    return JSONResponse(_inbox_state.get(session_id, {"enabled": False}))


# ------------------------------------------------- удаление из библиотеки

def _to_trash(path: str) -> str:
    """Убрать файл, не уничтожая его.

    Не os.remove: удаление необратимо, а поводы для него бывают ошибочные
    — неверно измеренный темп, случайно отмеченная строка. Файл едет в
    папку `_корзина` рядом с ним же: место она не освобождает, но её
    видно в проводнике, и очистить её диджей может сам, когда убедится.
    Настоящее стирание — отдельный режим, и о нём спрашивают явно."""
    src = Path(path)
    trash = src.parent / "_корзина"
    try:
        trash.mkdir(exist_ok=True)
        target = trash / src.name
        n = 1
        while target.exists():
            target = trash / f"{src.stem} ({n}){src.suffix}"
            n += 1
        src.replace(target)
        return str(target)
    except OSError as exc:
        logger.info(f"[удаление] не смог убрать {path}: {exc}")
        return ""


@app.post("/api/rooms/{session_id}/library/remove")
async def library_remove(session_id: str, body: dict) -> JSONResponse:
    """Убрать треки. body: {paths: [...], mode: "library"|"trash"|"erase"}.

    Три ступени по весу, а не одна кнопка «удалить»:
      library — вынуть из библиотеки, файл не трогать. Обратимо: вернётся
                следующим сканированием. Это и есть «убрать ненужное» в
                девяти случаях из десяти.
      trash   — вдобавок отправить файл в папку `_корзина` рядом. Место
                не освобождает, зато обратимо руками.
      erase   — стереть с диска. Необратимо, и подтверждение спрашивает
                интерфейс, а не этот обработчик.
    Слои и .stem.mp4 едут за треком: иначе после удаления в папке
    остаются гигабайты осиротевших стемов, которых никто уже не найдёт."""
    import stems as _stems

    mode = str(body.get("mode") or "library")
    if mode not in ("library", "trash", "erase"):
        raise HTTPException(status_code=400, detail=f"Не знаю режим {mode!r}")
    paths = [str(p) for p in (body.get("paths") or []) if p]
    if not paths:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного трека")

    room = sessions.get_or_create(session_id)
    wanted = {_canon(p) for p in paths}
    before = len(room.library_tracks or [])
    room.library_tracks = [t for t in (room.library_tracks or [])
                           if _canon(t.get("path")) not in wanted]
    removed = before - len(room.library_tracks)
    persistence.save_library(session_id, room.library_tracks)

    # Из базы сканера тоже: иначе следующий скан вернёт трек обратно, и
    # удаление будет выглядеть как «не сработало».
    db_removed = await asyncio.to_thread(_forget_in_db, _room_db_path(session_id), paths)

    files, errors = 0, []
    if mode in ("trash", "erase"):
        for p in paths:
            targets = [p]
            try:
                d = _stems.stem_dir_for(p)
                if d and Path(d).exists():
                    targets.append(str(d))
                m = _stems.stem_mp4_for(p)
                if m:
                    targets.append(str(m))
            except Exception:                        # noqa: BLE001
                pass
            for t in targets:
                r = await asyncio.to_thread(_erase if mode == "erase" else _to_trash_any, t)
                if r:
                    files += 1
                else:
                    errors.append(Path(t).name)
    logger.info(f"[удаление] режим {mode}: из библиотеки {removed}, из базы {db_removed}, "
                f"файлов и папок {files}")
    await room.broadcast_to_chat({"type": "library_updated",
                                  "count": len(room.library_tracks)})
    return JSONResponse({"ok": True, "mode": mode, "removed": removed,
                         "db_removed": db_removed, "files": files,
                         "errors": errors[:8], "left": len(room.library_tracks)})


def _canon(path) -> str:
    """Сравнение путей без оглядки на регистр: Windows не различает
    C:\\Music и C:\\music, а в базе лежит та строка, что пришла."""
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def _forget_in_db(db_path: str, paths: list[str]) -> int:
    import sqlite3

    if not Path(db_path).exists():
        return 0
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        n = 0
        for p in paths:
            cur = conn.execute("DELETE FROM tracks WHERE lower(replace(path,'\\\\','/'))=?",
                               (_canon(p),))
            n += cur.rowcount
        conn.commit()
        conn.close()
        return n
    except sqlite3.Error as exc:
        logger.info(f"[удаление] база не отдала строки: {exc}")
        return 0


def _to_trash_any(path: str) -> str:
    """То же, что _to_trash, но и для папки слоёв."""
    src = Path(path)
    if not src.exists():
        return ""
    if src.is_dir():
        trash = src.parent / "_корзина"
        try:
            trash.mkdir(exist_ok=True)
            target = trash / src.name
            n = 1
            while target.exists():
                target = trash / f"{src.name} ({n})"
                n += 1
            src.replace(target)
            return str(target)
        except OSError:
            return ""
    return _to_trash(path)


def _erase(path: str) -> str:
    import shutil

    p = Path(path)
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        else:
            return ""
        return path
    except OSError as exc:
        logger.info(f"[удаление] не стёр {path}: {exc}")
        return ""


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
    elif reason == "junction_a":
        # Меняем ТОЛЬКО точку ухода: вход и приём диджея устраивают.
        kw = {"a_path": seam["a_path"], "b_path": seam["b_path"],
              "hold_entry": seam.get("entry_seconds"),
              "hold_technique": seam.get("technique_id")}
    elif reason == "junction_b":
        kw = {"a_path": seam["a_path"], "b_path": seam["b_path"],
              "hold_exit": seam.get("exit_seconds"),
              "hold_technique": seam.get("technique_id")}
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
    # Сужение может не оставить ни одного варианта: у пары просто нет
    # второй точки ухода с тем же приёмом. Тогда отпускаем ограничения
    # по одному, от самого узкого, — молча вернуть «швов больше нет»
    # там, где они есть, хуже, чем предложить чуть более широкий выбор.
    for drop in ("hold_technique", "hold_entry", "hold_exit"):
        if items or drop not in kw:
            continue
        kw = {k: v for k, v in kw.items() if k != drop}
        items = audition.candidates(session_id, tracks, limit=1, **kw)
    if items:
        nxt = items[0]
    return JSONResponse({"ok": True, "next": nxt,
                         "progress": audition.progress(session_id, tracks)})


@app.post("/api/rooms/{session_id}/audition/technique")
async def audition_technique(session_id: str, body: dict) -> JSONResponse:
    """Другой приём на ТОМ ЖЕ шве: {seam, technique_id?}.

    Это не отказ и не вердикт. Прежняя кнопка «другая техника» писала в
    базу отказ и пересобирала шов целиком — вместе с приёмом уезжали
    точки, а иногда и трек. Здесь треки и точки остаются, меняется
    только приём: без technique_id берётся следующий подходящий по
    кругу, с ним — ровно указанный (ручной выбор)."""
    import audition

    seam = body.get("seam") or {}
    if not seam.get("a_path") or not seam.get("b_path"):
        raise HTTPException(status_code=400, detail="в шве нужны a_path и b_path")
    by_path = {t.get("path"): t for t in _room_tracks(session_id)}
    a = by_path.get(seam["a_path"])
    b = by_path.get(seam["b_path"])
    if a is None or b is None:
        raise HTTPException(status_code=400, detail="трек шва не найден в библиотеке комнаты")
    out = audition.swap_technique(seam, a, b, body.get("technique_id") or None,
                                  session_id=session_id)
    return JSONResponse({"ok": True, "seam": out,
                         "techniques": audition.techniques_for(a, b, seam.get("pair_label"))})


@app.post("/api/rooms/{session_id}/audition/lucky")
async def audition_lucky(session_id: str, body: dict) -> JSONResponse:
    """«Повезёт»: перебрать ВСЕ стыковки этой пары на все подходящие
    приёмы и отдать лучшее. Полсотни-сотня комбинаций на пару."""
    import audition

    seam = body.get("seam") or {}
    tracks = _room_tracks(session_id)
    # seen — комбинации, которые диджей на этой паре уже видел. Без них
    # перебор детерминированный: лучшая комбинация одна, и кнопка со
    # второго нажатия ничего не меняла.
    # Seed — номер расклада. Пришёл от клиента — берём его, не пришёл —
    # выдаём новый и ВОЗВРАЩАЕМ: иначе воспроизвести находку нечем.
    raw = body.get("seed")
    try:
        seed = int(raw) if raw not in (None, "") else secrets.randbelow(10_000)
    except (TypeError, ValueError):
        seed = secrets.randbelow(10_000)

    items = audition.lucky(session_id, tracks, seam.get("a_path"),
                           seam.get("b_path"), limit=int(body.get("limit") or 1),
                           exclude=list(body.get("seen") or ()), seed=seed)
    if not items:
        raise HTTPException(status_code=404, detail="для этой пары нет содержательных стыковок")
    return JSONResponse({"ok": True, "seam": items[0], "tried": len(items),
                         "seed": seed, "combo": audition.combo_key(items[0])})


@app.post("/api/rooms/{session_id}/audition/other-sections")
async def audition_other_sections(session_id: str, body: dict) -> JSONResponse:
    """Другие точки стыка — треки и приём остаются те же.

    Отказ НЕ записывается: диджей не отверг переход, он ищет для него
    место. Записывать это как «не нравится» значит портить вкусовую
    модель ровно там, где человек сказал обратное."""
    import audition

    seam = body.get("seam") or {}
    tracks = _room_tracks(session_id)
    items = audition.other_junction(session_id, tracks, seam,
                                    exclude=list(body.get("seen") or ()))
    if not items:
        raise HTTPException(
            status_code=404,
            detail="у этой пары одна-единственная стыковка — поменяйте приём или трек")
    top = items[0]
    rec = sum(1 for x in items if x.get("junction_kind") == "recommended")
    return JSONResponse({"ok": True, "seam": top, "combo": audition.combo_key(top),
                         "kind": top.get("junction_kind"),
                         "total": len(items), "recommended": rec})


@app.post("/api/rooms/{session_id}/audition/swap-b")
async def audition_swap_b(session_id: str, body: dict) -> JSONResponse:
    """Заменить входящий на близкий по темпу, тональности и жанру."""
    import audition

    seam = body.get("seam") or {}
    tracks = _room_tracks(session_id)
    # exclude — то, что уже отобрано в пул и что диджей на этой паре уже
    # отверг. Иначе кнопка подставляет трек, который в сете уже стоит.
    cand = audition.nearest_b(tracks, seam.get("a_path"), seam.get("b_path"),
                              limit=1, exclude=list(body.get("exclude") or ()))
    if not cand:
        raise HTTPException(status_code=404, detail="не нашёл близкого трека вне пула")
    items = audition.lucky(session_id, tracks, seam.get("a_path"),
                           cand[0].get("path"), limit=1)
    if not items:
        raise HTTPException(status_code=404, detail="с этим треком не складывается ни одной стыковки")
    return JSONResponse({"ok": True, "seam": items[0]})


@app.post("/api/rooms/{session_id}/audition/other-pair")
async def audition_other_pair(session_id: str, body: dict) -> JSONResponse:
    """Другая пара целиком: два трека из библиотеки и лучший их стык."""
    import audition

    seam = body.get("seam") or {}
    tracks = _room_tracks(session_id)
    items = audition.other_pair(
        session_id, tracks,
        exclude=list(body.get("exclude") or ()),
        avoid=[p for p in (seam.get("a_path"), seam.get("b_path")) if p],
        limit=1)
    if not items:
        raise HTTPException(status_code=404,
                            detail="в библиотеке не осталось пар вне пула")
    return JSONResponse({"ok": True, "seam": items[0],
                         "combo": audition.combo_key(items[0])})


@app.get("/api/rooms/{session_id}/track-sections")
async def track_sections_short(session_id: str, path: str) -> JSONResponse:
    """Секции трека — чтобы точку входа можно было переставить не на
    «плюс четыре такта», а на «второй куплет»."""
    import cue_points

    by_path = {t.get("path"): t for t in _room_tracks(session_id)}
    tr = by_path.get(path)
    if tr is None:
        raise HTTPException(status_code=404, detail="трек не найден в библиотеке комнаты")
    data = cue_points.sections_for_track(tr)
    return JSONResponse({"sections": data.get("sections", []),
                         "duration_seconds": tr.get("duration_seconds"),
                         "anchored": data.get("anchored")})


@app.get("/api/rooms/{session_id}/audition/techniques")
async def audition_techniques(session_id: str, a_path: str, b_path: str,
                              pair_label: str = "") -> JSONResponse:
    """Список приёмов для ручного выбора на этом шве — с пометкой, где
    какой приём хорош и почему какой-то нельзя поставить."""
    import audition

    by_path = {t.get("path"): t for t in _room_tracks(session_id)}
    a, b = by_path.get(a_path), by_path.get(b_path)
    if a is None or b is None:
        raise HTTPException(status_code=400, detail="трек не найден в библиотеке комнаты")
    return JSONResponse({"techniques": audition.techniques_for(a, b, pair_label or None)})


@app.get("/api/rooms/{session_id}/technique-presets")
async def list_technique_presets(session_id: str, technique_id: str = "") -> JSONResponse:
    """Сохранённые настройки приёмов этой комнаты."""
    import presets

    return JSONResponse({"presets": presets.listing(session_id, technique_id or None)})


@app.post("/api/rooms/{session_id}/technique-presets")
async def save_technique_preset(session_id: str, body: dict) -> JSONResponse:
    """{technique_id, name, params, note} — сохранить настройку приёма.

    Повторное сохранение под тем же именем обновляет её, а не заводит
    вторую: «мягкий» у приёма ровно один."""
    import presets

    tid = str(body.get("technique_id") or "")
    if tid not in TECHNIQUES:
        raise HTTPException(status_code=404, detail=f"Неизвестная техника: {tid}")
    try:
        return JSONResponse(presets.save(session_id, tid, body.get("name") or "",
                                         body.get("params") or {}, body.get("note")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/rooms/{session_id}/technique-presets")
async def delete_technique_preset(session_id: str, technique_id: str, name: str) -> JSONResponse:
    import presets

    return JSONResponse(presets.delete(session_id, technique_id, name))


# ------------------------------------------------- библиотека переходов

@app.post("/api/rooms/{session_id}/transitions")
async def save_transition(session_id: str, body: dict) -> JSONResponse:
    """Отложить понравившийся переход: {seam, rating, note, demo_file}.

    Не то же самое, что «в пул»: пул — материал одного сета, а это
    коллекция на будущее, с оценкой и словами о том, чем переход хорош.
    Из неё же потом растут автотехники — заметка диджея единственный
    источник знания о том, ПОЧЕМУ переход сработал."""
    import transitions

    seam = body.get("seam") or {}
    if not seam.get("a_path") or not seam.get("b_path"):
        raise HTTPException(status_code=400, detail="в шве нужны a_path и b_path")
    rating = body.get("rating")
    try:
        rating = max(1, min(5, int(rating))) if rating else None
    except (TypeError, ValueError):
        rating = None
    return JSONResponse(transitions.save(
        session_id, seam, rating=rating,
        note=(body.get("note") or None), demo_file=body.get("demo_file"),
        source=body.get("source") or "audition"))


@app.get("/api/rooms/{session_id}/transitions")
async def list_transitions(session_id: str, min_rating: int = 0) -> JSONResponse:
    import transitions

    return JSONResponse({"transitions": transitions.listing(session_id, min_rating),
                         "stats": transitions.stats(session_id)})


@app.post("/api/rooms/{session_id}/transitions/{tid}")
async def update_transition(session_id: str, tid: int, body: dict) -> JSONResponse:
    """Поменять оценку или заметку у уже отложенного перехода."""
    import transitions

    rating = body.get("rating")
    try:
        rating = max(1, min(5, int(rating))) if rating else None
    except (TypeError, ValueError):
        rating = None
    return JSONResponse(transitions.update(session_id, tid, rating,
                                           body.get("note")))


@app.delete("/api/rooms/{session_id}/transitions/{tid}")
async def delete_transition(session_id: str, tid: int) -> JSONResponse:
    import transitions

    return JSONResponse(transitions.remove(session_id, tid))


@app.get("/api/rooms/{session_id}/mixxx-library")
async def mixxx_library_compare(session_id: str) -> JSONResponse:
    """Что разошлось между библиотекой DARAVE и библиотекой Mixxx.

    Ничего не меняет — только показывает. Добавить файл в медиатеку
    Mixxx снаружи нельзя, это делает только он сам пересканированием."""
    import mixxx_library

    return JSONResponse(mixxx_library.compare(_room_tracks(session_id)))


# ------------------------------------------------- профиль настроек Mixxx

def _mixxx_running() -> bool:
    """Запущен ли Mixxx прямо сейчас.

    Нужно ровно перед записью в его файлы: Mixxx держит и mixxx.cfg, и
    библиотеку в памяти и перезаписывает их при выходе, стирая всё, что
    туда положили снаружи. Ту же граблю мы уже проходили с метками."""
    try:
        import subprocess as _sp

        if os.name == "nt":
            out = _sp.run(["tasklist", "/FI", "IMAGENAME eq mixxx.exe"],
                          capture_output=True, text=True, timeout=8).stdout
            return "mixxx.exe" in out.lower()
        out = _sp.run(["pgrep", "-x", "mixxx"], capture_output=True, timeout=8)
        return out.returncode == 0
    except Exception:
        return False


@app.get("/api/rooms/{session_id}/mixxx-profile")
async def mixxx_profile_status(session_id: str) -> JSONResponse:
    import mixxx_profile

    return JSONResponse(mixxx_profile.profile_status(session_id))


@app.post("/api/rooms/{session_id}/mixxx-profile")
async def mixxx_profile_action(session_id: str, body: dict | None = None) -> JSONResponse:
    """{action: export|apply, dry_run}. Настройки Mixxx принадлежат
    комнате: снимаются с текущей установки и накладываются на любую
    другую сборку — см. mixxx_profile.py."""
    import mixxx_profile

    body = body or {}
    action = str(body.get("action") or "export")
    if action == "export":
        return JSONResponse(mixxx_profile.export_profile(session_id, log=logger.info))
    if action == "apply":
        if _mixxx_running():
            raise HTTPException(status_code=409,
                                detail="Mixxx запущен — он перезапишет mixxx.cfg при выходе. Закройте его.")
        return JSONResponse(mixxx_profile.apply_profile(
            session_id, dry_run=bool(body.get("dry_run")), log=logger.info))
    raise HTTPException(status_code=400, detail="action: export | apply")


@app.get("/api/rooms/{session_id}/audition/pool")
async def audition_pool(session_id: str) -> JSONResponse:
    import audition

    tracks = _room_tracks(session_id)
    return JSONResponse({"pool": audition.pool(session_id),
                         "rejected": audition.rejections(session_id),
                         "taste": audition.taste(session_id),
                         "progress": audition.progress(session_id, tracks)})


def _seam_for_pair(session_id: str, a_path: str, b_path: str,
                   technique_id: str | None = None,
                   exit_seconds: float | None = None,
                   entry_seconds: float | None = None,
                   bars: float | None = None) -> dict:
    """Полный шов для КОНКРЕТНОЙ пары, даже если её уже отсудили.

    Обычный перебор (audition.candidates) по построению не показывает
    отсуженное, и это правильно: он предлагает НОВОЕ. Но починка — не
    предложение: шов уже выбран, его надо открыть и поправить. Отсюда
    include_judged.

    Точки могли уехать после пересканирования (сегментация дала другие
    границы фраз), поэтому попыток три, от самой строгой к самой мягкой:
    с точками и приёмом, только с приёмом, просто по паре. Отдать шов с
    новыми точками честнее, чем отказать."""
    import audition

    tracks = _room_tracks(session_id)
    by_path = {t.get("path"): t for t in tracks}
    if a_path not in by_path or b_path not in by_path:
        raise HTTPException(
            status_code=400,
            detail="Треков этого шва нет в библиотеке — пересканировали папку после отбора?")
    tries = (
        dict(hold_exit=exit_seconds, hold_entry=entry_seconds, hold_technique=technique_id),
        dict(hold_technique=technique_id),
        dict(),
    )
    items = []
    for kw in tries:
        items = audition.candidates(session_id, tracks, a_path=a_path, b_path=b_path,
                                    limit=12, include_judged=True, **kw)
        if items:
            break
    if not items:
        raise HTTPException(status_code=400,
                            detail="Не смог собрать шов: у этой пары нет ни одного стыка")
    seam = dict(items[0])
    if technique_id:
        seam["technique_id"] = technique_id
        t = TECHNIQUES.get(technique_id)
        if t is not None:
            seam["technique"] = t.name
    if exit_seconds is not None:
        seam["exit_seconds"] = float(exit_seconds)
    if entry_seconds is not None:
        seam["entry_seconds"] = float(entry_seconds)
    if bars:
        seam["bars"] = float(bars)
    return seam


@app.get("/api/rooms/{session_id}/audition/seam")
async def audition_seam_for_pair(session_id: str, a_path: str, b_path: str,
                                 technique_id: str = "", exit_seconds: float | None = None,
                                 entry_seconds: float | None = None,
                                 bars: float | None = None) -> JSONResponse:
    """Шов по паре треков — чтобы открыть в отборе переход из готового плана.

    Нужно для правки плохих стыков прямо из ControlC: там переход виден
    как отрезок на хронометраже, а править его надо там же, где
    выбирали, — в отборе."""
    return JSONResponse({"ok": True, "seam": _seam_for_pair(
        session_id, a_path, b_path, technique_id or None,
        exit_seconds, entry_seconds, bars)})


@app.get("/api/rooms/{session_id}/audition/pool/{seam_id}/seam")
async def audition_pool_seam(session_id: str, seam_id: int) -> JSONResponse:
    """Полный шов по записи из пула — чтобы вернуть его в отбор на правку.

    Почему отдельной ручкой, а не через /audition/next. Тот перебор по
    построению НЕ показывает то, что уже отсужено: пара одобренного шва
    лежит в approved_b, сама комбинация — в dead_combos, и трек стоит в
    цепочке. То есть шов из пула не мог вернуться оттуда никогда, и
    кнопка «в отбор» честно отвечала «не смог собрать шов для этой пары».

    Здесь тот же перебор зовётся с include_judged=True и закреплённой
    парой, приёмом и точками — получается ровно тот шов, что лежит в
    пуле, но со всей обвязкой карточки: подписи секций, тональности,
    расстройка, готовность слоёв."""
    import audition

    row = next((x for x in audition.pool(session_id) if int(x["id"]) == int(seam_id)), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Такого шва в пуле нет")
    params = dict(row.get("params") or {})
    seam = _seam_for_pair(session_id, row["a_path"], row["b_path"],
                          row.get("technique_id"), row.get("exit_seconds"),
                          row.get("entry_seconds"), params.get("bars"))
    if row.get("pair_label"):
        seam["pair_label"] = row["pair_label"]
    seam["params"] = params
    return JSONResponse({"ok": True, "seam": seam, "pool_id": int(seam_id),
                         "demo_file": row.get("demo_file")})


@app.delete("/api/rooms/{session_id}/audition/pool/{seam_id}")
async def audition_pool_drop(session_id: str, seam_id: int) -> JSONResponse:
    """Убрать один шов из пула."""
    import audition

    return JSONResponse(audition.drop(session_id, seam_id))


@app.delete("/api/rooms/{session_id}/audition/pool")
async def audition_pool_clear(session_id: str, what: str = "approved") -> JSONResponse:
    """Очистить пул: approved | rejected | all."""
    import audition

    out = audition.clear(session_id, what)
    out["progress"] = audition.progress(session_id, _room_tracks(session_id))
    return JSONResponse(out)


# --- пакетная пересборка пула ------------------------------------------
#
# Демо в пуле НЕ кэшируются: «послушать» каждый раз зовёт render_demo, а
# тот собирает план заново через build_plan. Значит после правки логики
# сведения пул звучит по-новому сам, ничего инвалидировать не надо.
#
# Не хватало другого. Рендер одного шва — это секунды, и на полусотне
# отобранных швов «переслушать всё» превращается в полсотни нажатий с
# ожиданием после каждого. Поэтому здесь пакетный проход: считает все
# разом в фоне, а диджей слушает уже готовое подряд.

_pool_render_tasks: dict[str, asyncio.Task] = {}
_pool_render_status: dict[str, dict] = {}


async def _pool_rerender(session_id: str, seams: list[dict]) -> None:
    import audition

    st = _pool_render_status[session_id]
    room = sessions.get_or_create(session_id)
    by_path = {t.get("path"): t for t in room.library_tracks}

    async def push() -> None:
        await room.broadcast_to_chat({"type": "pool_render", **st})

    try:
        for i, seam in enumerate(seams):
            st.update(done=i, name=Path(str(seam.get("a_path") or "")).name[:40])
            await push()
            a = by_path.get(seam.get("a_path"))
            b = by_path.get(seam.get("b_path"))
            if a is None or b is None:
                st["failed"] = st.get("failed", 0) + 1
                st.setdefault("errors", []).append(
                    f"шов {seam.get('id')}: трека нет в библиотеке")
                continue
            params = dict(seam.get("params") or {})
            out_name = f"{session_id}_pool{seam['id']}_{secrets.token_hex(4)}.mp3"
            try:
                await asyncio.to_thread(
                    demo_render.render_demo,
                    seam["technique_id"], seam["a_path"], seam["b_path"],
                    float(a.get("bpm") or 128.0), float(b.get("bpm") or 128.0),
                    str(DEMOS_DIR / out_name),
                    param_overrides={k: v for k, v in params.items()
                                     if k not in ("bars", "mix_profile")},
                    source_at=seam.get("exit_seconds"),
                    target_at=seam.get("entry_seconds"),
                    bars=(float(params["bars"]) if params.get("bars") else None),
                    mix_profile=params.get("mix_profile") or None,
                )
            except Exception as exc:                 # noqa: BLE001
                st["failed"] = st.get("failed", 0) + 1
                st.setdefault("errors", []).append(f"шов {seam.get('id')}: {str(exc)[:90]}")
                del st["errors"][:-8]
                logger.info(f"[пул] шов {seam.get('id')} не собрался: {exc}")
                continue
            # Ссылку кладём в сам шов: страницу могут перезагрузить, и
            # отрендеренное не должно пропадать вместе с вкладкой.
            audition.set_demo(session_id, int(seam["id"]), out_name)
            st["ready"] = st.get("ready", 0) + 1
        st["done"] = len(seams)
    finally:
        st["running"] = False
    await push()
    await room.broadcast_to_chat({
        "type": "system",
        "text": (f"Пул пересобран по текущей логике: готово {st.get('ready', 0)} из "
                 f"{len(seams)}"
                 + (f", не вышло {st['failed']}" if st.get("failed") else "")
                 + ". Можно слушать подряд, рендер больше не ждём."),
    })


@app.post("/api/rooms/{session_id}/audition/pool/rerender")
async def audition_pool_rerender(session_id: str, body: dict | None = None) -> JSONResponse:
    """Пересобрать демо всех отобранных швов по ТЕКУЩЕЙ логике сведения.

    Нужно после правок в приёмах или в тракте: сами швы (пара, приём,
    точки, длина) не меняются — меняется то, как они звучат."""
    import audition

    if demo_render is None:
        raise HTTPException(status_code=503, detail="Нет numpy/scipy — рендер недоступен")
    task = _pool_render_tasks.get(session_id)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Пересборка уже идёт — дождитесь конца")
    seams = audition.pool(session_id)
    only = (body or {}).get("ids")
    if only:
        want = {int(x) for x in only}
        seams = [s for s in seams if int(s["id"]) in want]
    if not seams:
        raise HTTPException(status_code=400, detail="Пул пуст — пересобирать нечего")
    _pool_render_status[session_id] = {"running": True, "done": 0, "total": len(seams),
                                       "ready": 0, "failed": 0, "name": "", "errors": []}
    _pool_render_tasks[session_id] = asyncio.create_task(_pool_rerender(session_id, seams))
    return JSONResponse({"ok": True, "started": True, "total": len(seams)})


@app.get("/api/rooms/{session_id}/audition/pool/rerender")
async def audition_pool_rerender_status(session_id: str) -> JSONResponse:
    st = _pool_render_status.get(session_id)
    if st is None:
        return JSONResponse({"running": False})
    task = _pool_render_tasks.get(session_id)
    if st.get("running") and (task is None or task.done()):
        st["running"] = False          # см. ту же сверку в /library/analyze
    return JSONResponse(st)


@app.post("/api/rooms/{session_id}/audition/build")
async def audition_build(session_id: str) -> JSONResponse:
    """Сет из одобренных швов — самая длинная цепочка, какая складывается."""
    require_paid("Собрать сет из отобранных швов")
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
    require_paid("Построить план сета")
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
    # Как весь план ведёт фейдеры. Один ключ на весь сет, и он ОБЯЗАН
    # быть: без него превью и полный сет собирались по кроссфейду приёма,
    # а в Clipping диджей вёл два независимых фейдера — то есть половины
    # программы сводили по-разному. Правка конкретного перехода этот
    # ключ перебивает (см. set_export.set_mix_profile).
    #
    # Умолчание — ручное ведение со значениями, которые диджей нашёл сам
    # и описал как «чистый фейдер при любой технике». Режим «по приёму»
    # остаётся, но выбирается явно.
    import mix_profile as _mix_profile

    strategy["mix_profile"] = (body.get("mix_profile")
                               or {"entry": "manual", **_mix_profile.DEFAULTS})
    room.last_strategy = strategy
    # План — документ, а не результат вычисления: в нём живут правки
    # переходов. Сохраняем, чтобы он пережил и перезапуск backend'а, и
    # закрытие Mixxx ради очереди Auto DJ.
    persistence.save_strategy(session_id, strategy)

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


@app.get("/api/rooms/{session_id}/strategy")
async def get_strategy(session_id: str) -> JSONResponse:
    """Последний построенный план сета.

    Нужен фронтенду при открытии: план жил только в памяти вкладки, и
    перезагрузка страницы (или перезапуск backend'а ради очереди Auto DJ)
    означала «стройте заново» — вместе со всеми правками переходов."""
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=404, detail="План ещё не построен")
    return JSONResponse(room.last_strategy)


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
    require_paid("Подобрать длительность сета")
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
    # ВАЖНО: те же аргументы, что и у «Построить план». Раньше сюда не
    # доходили ни ручной порядок, ни фильтр жанра, ни правки переходов, ни
    # fit_mode — и подбор перебирал длительности для ДРУГОГО набора
    # треков. Снаружи это выглядело как «кнопка не работает на моей
    # сборке сета»: она честно считала, но не то.
    kwargs = dict(
        arc_shape=body.get("arc_shape", "rising"),
        track_order=body.get("track_order"),
        only_mixable=bool(body.get("only_mixable", True)),
        variant=int(body.get("variant") or 0),
        overrides=body.get("overrides"),
        exclude=exclude,
        include_genres=body.get("include_genres"),
        include_subgenres=body.get("include_subgenres"),
        fit_mode=body.get("fit_mode") or "compress",
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
    require_paid("Проиграть переход из плана")
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план (POST .../strategy)")

    index = body.get("transition_index")
    source = body.get("source_deck", "A")
    target = body.get("target_deck", "B")
    if index is None or not (0 <= index < len(room.last_strategy["transitions"])):
        raise HTTPException(status_code=400, detail="Некорректный transition_index")
    # Ползунки, настроенные у ЭТОГО перехода в плане, — часть перехода, а
    # не украшение интерфейса: живое исполнение обязано звучать так же,
    # как демо, которое диджей слушал.
    overrides = {**(room.last_strategy["transitions"][index].get("params") or {}),
                 **(body.get("param_overrides") or {})} or None

    plan = mix_strategist.build_transition_plan(
        room.last_strategy, index, source, target, f"strategy_{session_id}_{index}", overrides,
    )
    sent = await room.send_plan_to_companion(plan)
    return JSONResponse({"ok": sent, "plan_id": plan["plan_id"]})


def _strategy_paths(room, from_index: int = 0) -> list[str]:
    """Пути треков плана, начиная с трека `from_index`.

    Именно этот список должен лежать в очереди Auto DJ: автопилот
    наполняет деки ею, а очередь отдаёт треки строго по порядку."""
    by_name = {t.get("name"): t.get("path")
               for t in (room.library_tracks or []) if t.get("name")}
    out: list[str] = []
    for tr in ((room.last_strategy or {}).get("tracks") or [])[max(0, from_index):]:
        p = tr.get("path") or by_name.get(tr.get("name"))
        if p:
            out.append(p)
    return out


TELEMETRY_WAIT_SECONDS = 90.0


async def _wait_for_telemetry(room, timeout: float) -> bool:
    """Ждём, пока Mixxx снова начнёт рассказывать о деках."""
    mark = float(getattr(room, "telemetry_at", 0.0) or 0.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = float(getattr(room, "telemetry_at", 0.0) or 0.0)
        if now > mark and room.latest_telemetry:
            return True
        await asyncio.sleep(0.5)
    return False


async def _ensure_autodj_queue(room, paths: list[str]) -> str | None:
    """Привести очередь Auto DJ в соответствие с планом — сколько бы это
    ни стоило, но без единого действия от диджея.

    Очередь живёт в библиотеке Mixxx, а её он держит в памяти и пишет при
    выходе. Значит переписать очередь можно только при закрытом Mixxx.
    Раньше здесь стоял отказ «закройте Mixxx», и это складывалось в
    замкнутый круг: закрыл -> план потерян -> построил заново -> очередь
    снова не та. Теперь backend закрывает и поднимает Mixxx сам: он и так
    работает на этой машине, а план переживает перезапуск."""
    import mixxx_autodj
    import mixxx_process

    state = mixxx_autodj.queue_matches(paths)
    if state["ok"]:
        return None

    logger.info("очередь Auto DJ расходится с планом (в ней %d, нужно %d) — готовлю",
                state["queued"], state["expected"])
    was_running = await asyncio.to_thread(mixxx_process.is_running)
    exe = None
    if was_running:
        closed = await asyncio.to_thread(mixxx_process.close, 30.0, logger.info)
        if not closed.get("ok"):
            raise HTTPException(status_code=409, detail=closed.get("error") or
                                "не смог закрыть Mixxx, чтобы переписать очередь")
        exe = closed.get("path")

    out = await asyncio.to_thread(mixxx_autodj.export_autodj, paths, None, True, False,
                                  logger.info)
    if not out.get("ok"):
        if was_running:
            await asyncio.to_thread(mixxx_process.start, exe, logger.info)
        raise HTTPException(status_code=409,
                            detail=f"не смог подготовить очередь Auto DJ: {out.get('error')}")
    logger.info("очередь Auto DJ переписана под план: %d треков", out.get("queued"))

    # Обратно Mixxx поднимает _ensure_mixxx_ready: он нужен автопилоту в
    # любом случае — и когда мы его закрывали ради очереди, и когда диджей
    # просто не запускал его сам.
    return exe


async def _ensure_mixxx_ready(room, exe: str | None = None) -> None:
    """Mixxx должен работать и слать телеметрию — иначе играть некому.

    Раньше здесь была дыра: очередь готовилась, а Mixxx поднимался только
    если мы сами его закрывали. Диджей, нажавший «В Mixxx» при закрытом
    Mixxx, получал «Auto DJ не положил трек ни на одну деку» — команда
    уходила в никуда, и виноватой выглядела очередь, хотя дело было в
    том, что играть попросту нечему."""
    import mixxx_process

    if not await asyncio.to_thread(mixxx_process.is_running):
        started = await asyncio.to_thread(mixxx_process.start, exe, logger.info)
        if not started.get("ok"):
            raise HTTPException(status_code=409, detail=started.get("error") or
                                "не смог запустить Mixxx")

    # Процесс есть — это ещё не значит, что он готов принимать команды:
    # маппинг контроллера поднимается позже, и до этого момента телеметрия
    # молчит, а автопилот по ней и ориентируется.
    if room.latest_telemetry and await _wait_for_telemetry(room, 3.0):
        return
    if not await _wait_for_telemetry(room, TELEMETRY_WAIT_SECONDS):
        raise HTTPException(
            status_code=409,
            detail=("Mixxx не присылает телеметрию. Проверьте, что маппинг DARAVE "
                    "включён в Настройках -> Контроллеры, и что companion запущен."))


# ---------------------------------------------------------------- автопилот
#
# «В Mixxx» — это не выгрузка, а исполнение. Плейлист и очередь Auto DJ
# отдают Mixxx только ПОРЯДОК треков: ни точки входа, ни приёма в них
# положить некуда, таких полей в модели плейлиста нет. Поэтому сет,
# собранный в DARAVE, играет в Mixxx единственным честным способом — его
# играет DARAVE, руками своего виртуального контроллера. Зато на экране
# при этом настоящие деки: две волны, движущийся фейдер, живые ручки —
# то, ради чего это и понадобилось (на слух правку не всегда слышно).

@app.get("/api/rooms/{session_id}/mixxx-settings")
async def mixxx_settings_check(session_id: str) -> JSONResponse:
    """Настройки самого Mixxx, от которых зависит, звучит ли живое
    сведение так же, как посчитанное демо (см. mixxx_settings.py)."""
    import mixxx_settings

    out = mixxx_settings.check()
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error"))
    return JSONResponse(out)


@app.post("/api/rooms/{session_id}/mixxx-settings")
async def mixxx_settings_apply(session_id: str, body: dict | None = None) -> JSONResponse:
    import mixxx_settings

    if _mixxx_running():
        raise HTTPException(status_code=409,
                            detail="Mixxx запущен — он перезапишет mixxx.cfg при выходе. Закройте его.")
    out = mixxx_settings.apply(only_must=bool((body or {}).get("must_only")),
                               log=logger.info)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "не вышло")
    return JSONResponse(out)


@app.post("/api/rooms/{session_id}/autopilot/start")
async def autopilot_start(session_id: str, body: dict | None = None) -> JSONResponse:
    """Играть сет в Mixxx подряд, начиная с перехода `index`.

    mode="set" — весь сет до конца; mode="clip" — один переход с разбегом
    в несколько тактов и коротким хвостом (чтобы посмотреть ОДИН стык,
    не слушая сет целиком)."""
    require_paid("Автопилот")
    room = sessions.get_or_create(session_id)
    if room.last_strategy is None:
        raise HTTPException(status_code=400, detail="Сначала постройте план сета.")
    body = body or {}
    index = int(body.get("index") or 0)

    # Очередь Auto DJ — это и есть то, чем автопилот наполняет деки
    # (см. autodj_loader.py: контрола «загрузи файл по пути» в Mixxx нет).
    # Значит порядок в ней обязан совпадать с планом начиная с текущего
    # перехода, иначе на деку приедет чужой трек. Проверяем ДО старта, а
    # не ловим по длительности на третьем переходе.

    paths = _strategy_paths(room, index)
    exe = None
    if paths and not bool(body.get("skip_queue")):
        exe = await _ensure_autodj_queue(room, paths)
    await _ensure_mixxx_ready(room, exe)

    ap = room.get_autopilot(logger=logger.info)
    import autopilot as _ap
    try:
        out = await ap.start(room.last_strategy, index=index,
                             mode=str(body.get("mode") or "set"))
    except _ap.AutopilotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    logger.info(f"автопилот запущен (комната '{session_id}'): {out['mode']}, "
                f"переход №{out['index'] + 1} из {out['total']}")
    return JSONResponse(out)


@app.post("/api/rooms/{session_id}/autopilot/stop")
async def autopilot_stop(session_id: str) -> JSONResponse:
    room = sessions.get_or_create(session_id)
    if room.autopilot is None:
        return JSONResponse({"type": "autopilot", "running": False, "state": "idle"})
    return JSONResponse(await room.autopilot.stop())


@app.get("/api/rooms/{session_id}/autopilot")
async def autopilot_status(session_id: str) -> JSONResponse:
    room = sessions.get_or_create(session_id)
    if room.autopilot is None:
        return JSONResponse({"type": "autopilot", "running": False, "state": "idle",
                             "index": 0, "total": len(
                                 (room.last_strategy or {}).get("transitions") or [])})
    return JSONResponse(room.autopilot.status())


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_local_origin(origin: str) -> bool:
    """Страница, которую отдала сама программа.

    ALLOWED_ORIGINS — про САЙТ: оттуда приходит вход. Но интерфейс
    программа отдаёт сама, с 127.0.0.1, и браузер честно ставит этот
    origin на WebSocket. Список для сайта его не знал, и собственный же
    интерфейс получал 403 на /ws/chat: телеметрия и чат молча не
    подключались, а по HTTP всё работало — потому что там origin никто
    не проверяет.
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


async def ws_allowed(websocket: "WebSocket") -> bool:
    """Пустить ли WebSocket. Закрывает дыру, которую HTTP-прослойка не видит.

    `@app.middleware("http")` на WebSocket не распространяется вовсе: там
    другой протокол и другой жизненный цикл. То есть весь HTTP был закрыт
    токеном, а /ws/chat оставался открытым настежь — и через него можно
    было и читать телеметрию, и слать команды.

    Токен приходит подпротоколом (заголовки WebSocket из браузера не
    задать) либо параметром запроса. Origin проверяем отдельно: браузеры
    НЕ применяют к WebSocket правила CORS, так что список разрешённых
    сайтов здесь приходится держать своими руками.
    """
    if os.environ.get("DARAVE_NO_AUTH") == "1":
        return True
    origin = websocket.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS and not _is_local_origin(origin):
        return False
    token = websocket.query_params.get("token", "")
    if not token:
        proto = websocket.headers.get("sec-websocket-protocol", "")
        for part in proto.split(","):
            part = part.strip()
            if part.startswith("darave."):
                token = part[len("darave."):]
                break
    return cloud_auth.token_ok(token)


@app.websocket("/ws/companion/{session_id}")
async def companion_endpoint(websocket: WebSocket, session_id: str) -> None:
    if not await ws_allowed(websocket):
        # 1008 — policy violation. Закрываем ДО accept(): принятое и
        # тут же закрытое соединение браузер показывает как разрыв
        # сети, и человек идёт чинить роутер вместо того, чтобы войти.
        await websocket.close(code=1008)
        return
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
                # Отметка по МОНОТОННЫМ часам backend'а: автопилот
                # доигрывает по ней время, прошедшее с последнего тика, и
                # часы companion'а тут не годятся — они чужие.
                room.telemetry_at = time.monotonic()
                # Транслируем сырую телеметрию в чат тоже — фронтенду пригодится
                # для отрисовки BPM/waveform-позиции, не только агенту.
                payload = {"type": "telemetry", "decks": msg["decks"]}
                if "recording_status" in msg:
                    payload["recording_status"] = msg["recording_status"]
                await room.broadcast_to_chat(payload)

            elif msg_type in ("load_result", "deck_result"):
                # Ответы на «положи трек на деку» / «дёрни транспорт» —
                # их ждёт автопилот, чтобы идти по сету дальше.
                if room.autopilot is not None:
                    room.autopilot.on_load_result(msg)
                if not msg.get("ok"):
                    logger.info(f"{msg_type}: {msg.get('error')} (комната '{session_id}')")
                    await room.broadcast_to_chat(msg)

            elif msg_type in ("plan_started", "plan_finished", "plan_rejected", "control_done"):
                if room.autopilot is not None and msg_type != "control_done":
                    room.autopilot.on_plan_event(msg)
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
    if not await ws_allowed(websocket):
        # 1008 — policy violation. Закрываем ДО accept(): принятое и
        # тут же закрытое соединение браузер показывает как разрыв
        # сети, и человек идёт чинить роутер вместо того, чтобы войти.
        await websocket.close(code=1008)
        return
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
                except Exception:  # ошибка LLM не должна ронять WS-соединение
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
