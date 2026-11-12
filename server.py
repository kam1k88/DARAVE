"""FastAPI backend локального приложения DARAVE.

Обслуживает интерфейс библиотеки, анализа треков, подбора переходов,
предпрослушивания и сборки миксов. Долгие задачи выполняются отдельными
процессами, а WebSocket используется для передачи прогресса и статусов.
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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
from child import child_cmd
import library_analysis as _la
import mix_strategist
import set_export
import persistence
from track_analysis import load_library_from_db

try:
    import fix_library_bpm
except Exception:  # без numpy/librosa — просто нет автопочинки темпа
    fix_library_bpm = None
from session import SessionManager
from techniques import TECHNIQUES

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
APP_VERSION = "1.2.3"

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
#   собрать план, предпрослушать переходы и отрендерить сет
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


sessions = SessionManager()
persistence.init_db()

CHAT_HTML_PATH = _paths.resource_dir() / "static" / "chat.html"

# Локальное сканирование библиотеки прямо из веб-UI (кнопка "Сканировать" во
# вкладке "Библиотека") — см. POST .../library/scan ниже. Работает, только
# когда backend и музыка на одной машине (обычный локальный запуск рядом с
# локального приложения), потому что сканирует диск процесса backend, а
# не диска диджея через сеть. Для облачного деплоя (backend на VPS) кнопка
# не поможет — там по-прежнему нужен CLI track_analysis.py --upload на
# машине диджея (см. README).
DEFAULT_SET_MINUTES = 90.0  # длительность сета, если диджей не задал свою
SCAN_DB_DIR = _paths.data_subdir("scan_dbs")
# Значение — subprocess.Popen; модуль импортируется лениво, поэтому в
# аннотации его имени нет: строковая аннотация с неимпортированным
# модулем выглядит для линтера как необъявленное имя, а проверка на
# необъявленные имена нам нужна рабочей (см. _server_has_no_typos).
_scan_processes: dict = {}   # обычный Popen, см. start_library_scan
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

# Выгрузка готового сета: плейлист, раскладка и рендер микса.
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


@app.get("/api/rooms")
async def list_rooms() -> JSONResponse:
    """Комнаты, которые на этой машине уже открывали.

    Код комнаты живёт только в адресной строке, а за ним стоит вся
    просканированная библиотека. Закрыл вкладку — и единственным выходом
    было сканировать заново. Поэтому список: программа знает свои
    комнаты, и спрашивать код у человека незачем.

    Список локальный по построению: база `darave.db` лежит рядом с
    программой, и никакие чужие комнаты в неё попасть не могут. Отдаём
    только по локальному запросу — снаружи это был бы перечень кодов, то
    есть ровно то, чего длинный случайный код избегает."""
    rooms = await asyncio.to_thread(persistence.list_rooms, 30)
    live = {sid for sid in sessions._rooms}
    for r in rooms:
        r["open"] = r["room_id"] in live
    return JSONResponse({"rooms": rooms})


@app.delete("/api/rooms/{session_id}")
async def forget_room(session_id: str) -> JSONResponse:
    """Убрать комнату из списка. Файлы и слои не трогает — только
    запись о комнате: её библиотеку и план сета."""
    await asyncio.to_thread(persistence.forget_room, session_id)
    return JSONResponse({"ok": True})


@app.get("/api/version")
async def code_version() -> JSONResponse:
    """Какой код реально крутится в этом процессе — чтобы «перезапустил, а
    ничего не изменилось» диагностировалось за секунду, а не на слух."""
    return JSONResponse(_code_info())


@app.get("/api/techniques")
async def list_techniques() -> JSONResponse:
    """Библиотека техник сведения для веб-вкладки «Техника»."""
    return JSONResponse([t.to_dict() for t in TECHNIQUES.values()])


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
    if mix_profile is None:
        # Профиль НЕ приходит из интерфейса и не хранится в нём. Раньше
        # четыре ползунка жили в переменной экрана: подобрал форму на
        # одной паре — те же числа уехали на следующую, к другому приёму
        # и другой длине. Диджей: «на других стыках остаются на своих
        # значениях и ломают переходы».
        #
        # Теперь считаем здесь, из того, что относится к ЭТОМУ переходу:
        # длина в тактах, тип стыка, конфликт слоёв. Меняются такты —
        # меняется и форма, сама.
        import mix_profile as _mp

        _lay = None
        try:
            import stem_features as _sf

            _lay = _sf.gap(_sf.features(track_a_path), _sf.features(track_b_path))
        except Exception:                            # noqa: BLE001
            _lay = None
        mix_profile = _mp.auto(seam_bars, body.get("pair_label"), _lay,
                               technique_id)
    vocal_loop = body.get("vocal_loop")
    extra_events = list(body.get("extra_events") or [])
    # FX по сторонам приходят ИДЕНТИФИКАТОРАМИ, а не готовыми событиями:
    # словарь эффектов должен жить в одном месте, иначе клиент начнёт
    # собирать события сам и они разъедутся с рендером.
    fx_out = body.get("fx_out")
    fx_in = body.get("fx_in")
    # Ручки эффекта (сила, доля эха, хвост реверба…) идут рядом с
    # идентификатором, а не вместо него: пресет остаётся смыслом, ручки —
    # его настройкой. Клиент шлёт только то, что двигал; остальное
    # подставит сам пресет.
    fx_out_opts = body.get("fx_out_opts") or {}
    fx_in_opts = body.get("fx_in_opts") or {}
    # Ручки поверх эффекта САМОГО приёма — отдельная вещь: там не
    # добавляется второй эффект, а настраивается уже существующий.
    fx_tune = {}
    for deck, opts in (("source", body.get("fx_own_out")),
                       ("target", body.get("fx_own_in"))):
        if opts:
            fx_tune[deck] = dict(opts)
    fx_tune = fx_tune or None
    if fx_out or fx_in:
        import fx_presets

        beats = max(4.0, float(seam_bars or 16) * 4.0)
        extra_events += fx_presets.events_for(
            fx_out, fx_in, beats,
            genre_out=(track_a.get("subgenre") or track_a.get("genre")),
            genre_in=(track_b.get("subgenre") or track_b.get("genre")),
            out_opts=fx_out_opts, in_opts=fx_in_opts)
    extra_events = extra_events or None
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
            fx_tune=fx_tune,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"Файл трека не найден на диске backend'а: {exc}") from exc
    except (ValueError, NotImplementedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({
        "ok": True,
        "mix_profile": mix_profile,
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


@app.post("/api/rooms/{session_id}/strategy/export")
async def export_set(session_id: str, body: dict | None = None) -> JSONResponse:
    """Отдаёт сет наружу: универсальный M3U-плейлист и текстовая раскладка."""
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
        "how_to": "Откройте .m3u8 в совместимом DJ-плеере или медиаплеере.",
    })


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


def stems_child_cmd(paths_file: str, backend: str) -> list[str]:
    """Команда запуска разделения на слои.

    Отдельной функцией, чтобы самопроверка могла посмотреть на неё в
    СОБРАННОЙ программе. Раньше здесь стоял «--dir <угаданная папка>», и
    угадывание уводило шаг обходить весь домашний каталог; проверять
    такое чтением исходника нельзя — в сборке исходника нет."""
    return child_cmd("stems", "--paths-file", str(paths_file),
                     "--backend", backend, "--all")


def _safe_room_key(session_id: str) -> str:
    """Код комнаты, пригодный для имени файла.

    Сгенерированный код — это token_urlsafe, там только буквы, цифры,
    «-» и «_». Но код можно ВВЕСТИ руками в поле на входном экране, и
    оттуда он попадает прямо в имя файла базы. «../..» или имя со слэшем
    означали бы запись мимо папки — в лучшем случае невнятная ошибка, в
    худшем перезапись чужого файла. Сервер слушает только 127.0.0.1, то
    есть навредить может лишь сам хозяин машины, но отказ должен быть
    внятным, а не «FileNotFoundError» из глубины.
    """
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_"
                   for ch in str(session_id or ""))[:64]
    return safe or "room"


def _room_db_path(session_id: str) -> str:
    return str(SCAN_DB_DIR / f"{_safe_room_key(session_id)}.db")


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


@app.get("/api/docs")
async def docs_index() -> JSONResponse:
    """Оглавление встроенной справки."""
    import docs as _docs

    return JSONResponse({"sections": _docs.index()})


@app.get("/api/docs/{slug}")
async def docs_page(slug: str) -> JSONResponse:
    """Один раздел встроенной справки."""
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
                line = decode_child_line(raw_line).rstrip()
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

ANALYZE_STEPS = _la.ANALYZE_STEPS
ANALYZE_DEFAULT = _la.ANALYZE_DEFAULT

# --------------------------------- кодировка вывода дочерних процессов
#
# В журнале диджея строка скана выглядела так:
#
#   [анализ:scan] [����] ��� ��������� � �������� ����: 140 � �������� 0
#
# Причина: Python, печатая в ТРУБУ (а не в консоль), берёт кодировку из
# locale.getpreferredencoding() — на русской Windows это cp1251. Мы же
# читали байты как UTF-8, и каждая кириллическая буква превращалась в
# «замену». Сообщение об ошибке, напечатанное дочерним процессом,
# становилось нечитаемым ровно тогда, когда оно и нужно.
#
# Лечим с двух сторон. Первая: просим дочерний процесс печатать в UTF-8
# явно. Вторая: всё равно умеем прочитать cp1251/cp866 — потому что
# PYTHONUTF8 понимает не всякий процесс (ffmpeg, PowerShell, чужие
# утилиты), а ломаться на них нельзя.
def child_env() -> dict:
    """Окружение для дочернего процесса: печатай в UTF-8."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def decode_child_line(raw: bytes) -> str:
    """Строка из трубы. UTF-8, иначе кодировки Windows, иначе с заменами.

    Порядок не случайный: cp1251 — то, что отдаёт сам Python на русской
    Windows при печати в трубу, cp866 — то, что отдают консольные утилиты
    старой закалки. Обе однобайтовые и декодируют почти всё, поэтому
    UTF-8 обязан идти первым и СТРОГО: иначе правильный вывод молча
    превратится в кракозябры другого сорта."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Обе однобайтовые: любая последовательность байт в них «валидна», и
    # отличить их проверкой на ошибку НЕЛЬЗЯ. Поэтому не проверяем, а
    # выбираем по результату: настоящий текст — это кириллица и пробелы,
    # а не псевдографика и не надстрочные знаки. Побеждает вариант, где
    # осмысленных символов больше.
    best, best_score = None, -1
    for enc in ("cp1251", "cp866"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        score = sum(1 for ch in text
                    if ("а" <= ch <= "я" or "А" <= ch <= "Я" or ch in "ёЁ"
                        or ch.isascii() and (ch.isalnum() or ch in " .,:;!?()[]/-+%"))
                    ) - sum(1 for ch in text if "\u2500" <= ch <= "\u25ff")
        if score > best_score:
            best, best_score = text, score
    if best is not None:
        return best
    return raw.decode("utf-8", errors="replace")


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


async def _step_heartbeat(session_id: str, key: str, st: dict,
                          every: float = 60.0) -> None:
    """Раз в минуту писать в журнал, что шаг ещё жив и на чём он.

    Нужно потому, что молчание — это не отсутствие новостей, а отсутствие
    СПОСОБА узнать новости. Разделение на слои идёт часами и печатает
    строку раз в несколько минут; между строками журнал выглядел как
    оборванный, и «дальше анализ не идёт» — ровно то, что видел диджей.
    Собственных строк ребёнка для этого мало: он может молчать и по делу.
    """
    started = time.time()
    try:
        while True:
            await asyncio.sleep(every)
            mins = (time.time() - started) / 60.0
            done, total = int(st.get("done") or 0), int(st.get("total") or 0)
            where = f", {done}/{total}" if total else ""
            name = str(st.get("name") or "")
            logger.info(f"[анализ:{key}] идёт {mins:.0f} мин{where}"
                        + (f" — {name}" if name else "")
                        + " (это не зависание)")
    except asyncio.CancelledError:
        pass


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
                         cwd=str(_paths.data_dir()), env=child_env(),
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return {"state": "fail", "note": f"не смог запустить: {exc}"}

    def _pump() -> None:
        if proc.stdout is None:
            proc.wait()
            return
        for raw in proc.stdout:
            line = decode_child_line(raw).rstrip()
            if not line:
                continue
            upd = parse(line) if parse else None
            if upd is None:
                # Машинные строки прогресса в хвост НЕ кладём: хвост
                # читает человек, когда что-то пошло не так, и JSON по
                # строке на трек вытеснял бы из него настоящую причину.
                tail.append(line)
                del tail[:-40]
                # И В ЖУРНАЛ — СРАЗУ, а не после конца процесса.
                #
                # Раньше tail писался в лог одним куском, когда ребёнок
                # уже завершился. Для скана это незаметно, а для стемов
                # это четыре часа полной тишины: в журнале не появляется
                # НИ ОДНОЙ строки, и отличить «идёт» от «повисло»
                # невозможно ничем. Ровно на это диджей и жаловался.
                logger.info(f"[анализ:{key}] {line}")
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

    hb = asyncio.create_task(_step_heartbeat(session_id, key, st))
    try:
        await asyncio.to_thread(_pump)
    finally:
        hb.cancel()
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
            logger.info(f"[анализ] шаг «{_la.ANALYZE_TITLES.get(key, key)}» начат")
            _t0 = time.time()
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
            logger.info(f"[анализ] шаг «{_la.ANALYZE_TITLES.get(key, key)}» — "
                        f"{res.get('state')} за {(time.time() - _t0) / 60.0:.1f} мин"
                        + (f": {res.get('note')}" if res.get("note") else ""))
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
        # Список треков, а НЕ папку.
        #
        # Раньше сюда шла папка: указанная в поле или общий корень путей
        # библиотеки. У диджея треки лежат в «music/Phonk» и в «Music», и
        # общим корнем этих двух оказывается C:\Users\kam1k88 — весь
        # профиль целиком. Шаг уходил обходить AppData и Downloads,
        # молчал десятки минут и видеокарту не трогал вовсе: до
        # разделения дело не доходило. Именно это он и видел как
        # «анализ дальше не идёт».
        #
        # Библиотека уже отсканирована — значит список треков у нас есть,
        # и угадывать по нему папку незачем.
        todo_file = (_paths.data_subdir("scan_dbs")
                     / f"stems_todo_{_safe_room_key(session_id)}.txt")
        try:
            todo_file.write_text("\n".join(todo if not force else paths),
                                 encoding="utf-8")
        except OSError as exc:
            return {"state": "fail", "note": f"не смог записать список треков: {exc}"}
        cmd = stems_child_cmd(str(todo_file), backend)
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

    if key == "layers":
        import stem_features as _sf

        if not paths:
            return {"state": "skip", "note": "библиотека пуста"}
        todo = [p for p in paths
                if _stems.has_stems(p) and (force or _sf.features(p) is None)]
        if not todo:
            have = sum(1 for p in paths if _stems.has_stems(p))
            if not have:
                return {"state": "skip",
                        "note": "слоёв нет ни у одного трека — считать нечего"}
            return {"state": "skip", "done": have, "total": len(paths),
                    "note": f"признаки слоёв посчитаны у всех {have}"}
        step = _analyze_step(session_id, key) or {}
        step["total"] = len(todo)
        loop = asyncio.get_running_loop()

        def _tick(i: int, total: int, done: int, failed: int, name: str) -> None:
            step["done"] = i
            step["name"] = os.path.basename(name)[:60]
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_analyze_push(session_id)))

        def _work() -> dict:
            return _sf.library(todo, rebuild=force,
                               log=lambda m: logger.info(f"[слои] {m}"),
                               progress=_tick)

        r = await asyncio.to_thread(_work)
        # Разметка пары кэшируется по путям на всё время процесса: без
        # сброса подбор до перезапуска продолжал бы считать, что слоёв
        # нет, — то есть шаг отработал бы вхолостую с точки зрения
        # диджея, который сразу жмёт «подобрать».
        import audition as _aud

        _aud.invalidate_layers()
        return {"state": "ok", "done": r.get("checked", 0), "total": len(todo),
                "note": f"размечено по слоям {r.get('checked', 0)}"
                        + (f", сбоев {r['failed']}" if r.get("failed") else "")}

    if key == "catalog":
        import metadata as _metadata

        if not tracks:
            return {"state": "skip", "note": "библиотека пуста"}
        token = _metadata.resolve_discogs_token(None)
        todo = [t for t in tracks
                if force or not ((t.get("meta") or {}).get("sources"))]
        if not todo:
            return {"state": "skip", "note": "каталоги уже опрошены по всем трекам"}
        if not token:
            # Без ключа Discogs шаг опрашивает только MusicBrainz, а тот
            # по именам файлов вида «44. Lastfragment - …» почти ничего
            # не находит: у диджея за проход нашлось 0 из 140, и ни одна
            # строка в базу не легла. Значит и в следующий раз будет 140.
            # Честный пропуск лучше ежедневного холостого прохода.
            return {"state": "skip", "total": len(todo),
                    "note": f"нет ключа Discogs — по именам файлов каталоги "
                            f"почти ничего не находят. Пропускаю {len(todo)} "
                            f"треков; ключ кладётся в darave_secrets.json"}
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

        import track_analysis as _ta

        r = await asyncio.to_thread(housekeeping.session_cleanup, False, logger.info)
        removed = int(r.get("removed_count", 0) or 0)
        merged = int((r.get("library_dedupe") or {}).get("merged", 0) or 0)

        # Треки, которых нет на диске. У диджея их было 47 из 187: в базе
        # есть, файла нет. Это не косметика — подбор считает их
        # кандидатами, предлагает в пару и ставит в сет, а сыграть
        # нельзя. «Файлов не найдено 47» писал перемер темпа, и на этом
        # всё: никто их не убирал.
        gone = await asyncio.to_thread(_ta.dedupe_library_db, db_path, True)
        missing = int(gone.get("missing", 0) or 0)
        if missing:
            fresh = await asyncio.to_thread(load_library_from_db, db_path)
            if fresh:
                room.library_tracks = fresh
                persistence.save_library(session_id, fresh)
                await room.broadcast_to_chat({"type": "library_updated",
                                              "count": len(fresh)})

        if not removed and not merged and not missing:
            return {"state": "skip", "note": "мусора нет"}
        note = f"убрано {removed} файлов, {r.get('freed_mb', 0)} МБ"
        if merged:
            note += f"; схлопнуто дублей по регистру пути: {merged}"
        if missing:
            note += f"; убрано из базы треков, которых нет на диске: {missing}"
        return {"state": "ok", "done": removed, "total": removed, "note": note}

    return {"state": "skip", "note": "шаг не знаю"}


@app.post("/api/rooms/{session_id}/library/analyze")
async def analyze_library(session_id: str, body: dict | None = None) -> JSONResponse:
    """Кнопка «Анализ библиотеки» — один проход по всем шагам.

    body: {"path": папка, "steps": [...], "force": false, "backend": "auto"}
    Без steps берётся ANALYZE_DEFAULT.
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

    Перед запуском показывает, какие этапы анализа будут выполнены.
    Это позволяет заранее увидеть объём работ и отдельно подтвердить
    очистку временных файлов."""
    import housekeeping
    import metadata as _metadata
    import stems as _stems

    room = sessions.get_or_create(session_id)
    tracks = room.library_tracks or []
    import stem_features as _sf

    missing = _la.missing_report(tracks, _stems.has_stems, _stems.vocal_map,
                                 _sf.features, _stems.has_layers)

    dry = await asyncio.to_thread(housekeeping.session_cleanup, True, None)
    missing["cleanup"] = int(dry.get("removed_count", 0) or 0)
    missing["cleanup_mb"] = dry.get("freed_mb", 0)

    # -1 = «посчитать заранее нельзя». У скана это честно: сколько файлов
    # новых, видно только обойдя папку, а обходить её дважды ради цифры на
    # кнопке — это половина стоимости самого скана.
    # Каталоги без ключа Discogs не сделают ничего — значит и работой не
    # числятся. Иначе счётчик показывает «140» вечно, шаг запускается
    # каждый раз и честно находит 0: у диджея так и было.
    try:
        import metadata as _md

        if not _md.resolve_discogs_token(None):
            missing["catalog"] = 0
            missing["catalog_why"] = "нет ключа Discogs"
    except Exception:                                   # noqa: BLE001
        pass

    counts = {"scan": -1, "bpm": max(missing.get("bpm", 0), missing.get("maps", 0)),
              "stems": missing.get("stems", 0), "vocals": missing.get("vocals", 0),
              "layers": missing.get("layers", 0),
              "catalog": missing.get("catalog", 0),
              "cleanup": missing["cleanup"], "cues": -1}
    steps = [{"key": k, "title": _la.ANALYZE_TITLES[k], "why": _la.ANALYZE_WHY[k],
              "missing": counts.get(k, 0), "default": k in _la.ANALYZE_DEFAULT,
              "heavy": k in _la.ANALYZE_HEAVY}
             for k in _la.ANALYZE_STEPS]

    # На чём будут считаться слои. Раньше это знал только лог backend'а,
    # а человек видел молчащую полосу и решал, что программа зависла.
    # Разница между видеокартой и процессором тут не в процентах, а в
    # порядке: минуты против часов.
    # Слои, оставшиеся в старой папке (до 1.1.5 они лежали рядом с
    # кодом). Молча пересчитывать их заново — часы работы видеокарты и
    # ни слова о причине.
    legacy = None
    try:
        import stems as _st0

        _old = await asyncio.to_thread(_st0.legacy_stem_dir)
        if _old is not None:
            legacy = {"path": str(_old),
                      "count": await asyncio.to_thread(_st0.legacy_stem_count, _old),
                      "to": str(_st0.STEM_DIR)}
    except Exception:                                   # noqa: BLE001
        legacy = None

    dev = {}
    try:
        import stems as _st

        info = await asyncio.to_thread(_st.device_info)
        # Цифры не выдуманы: они взяты из самого stems.device_info —
        # «2-4 минуты на трек» на видеокарте и «20-40 минут» на
        # процессоре. Берём верх вилки, потому что ошибиться в меньшую
        # сторону здесь хуже: человек уйдёт, думая, что успеет.
        per_min = 4.0 if info.get("cuda") else 40.0
        n = max(1, counts.get("stems", 0) or len(tracks))
        dev = {
            "cuda": bool(info.get("cuda")),
            "name": info.get("name"),
            "torch": bool(info.get("torch")),
            "demucs": bool(info.get("demucs")),
            "note": info.get("note") or "",
            "eta_minutes": round(per_min * n),
            "tracks": n,
        }
    except Exception as exc:                            # noqa: BLE001
        dev = {"cuda": False, "torch": False, "why": f"не смог спросить torch: {exc}"}
    return JSONResponse({
        "tracks": len(tracks),
        "root": _la.common_root([t.get("path") for t in tracks if t.get("path")]),
        "steps": steps, "missing": missing, "stems_device": dev,
        "stems_legacy": legacy,
        "mp4_only": missing.get("mp4_only", 0),
        "catalog_why": missing.get("catalog_why", ""),
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
    рядом с каждым .stem.mp4 — контейнер с четырьмя аудиослоями.

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
        # Если путь не передан явно, определяем общую папку по
        # уже отсканированным трекам библиотеки.
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
                   "повторите после сканирования библиотеки.")
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
                         cwd=str(_paths.data_dir()), env=child_env(),
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не смог запустить stems.py: {exc}") from exc

    info = await asyncio.to_thread(_stems.device_info)
    logger.info(f"стемы: запуск для '{path}', способ {backend}, "
                f"устройство {'CUDA ' + str(info.get('name')) if info.get('cuda') else 'CPU'}")
    _stem_processes[session_id] = proc
    _stem_status[session_id] = {"running": True, "tail": [], "ok": None,
                                "device": info, "backend": backend}

    def _pump() -> None:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = decode_child_line(raw).rstrip()
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


# --------------------------------------------- установка пакета слоёв
#
# Пакет слоёв — это torch и demucs, 2-3 ГБ. В установщик они не входят:
# всё остальное весит 200 МБ, а слои нужны не всем (EQ- и фильтровые
# приёмы работают без них). Ставятся они в ОТДЕЛЬНЫЙ Python, не в саму
# программу, — так устроен stems.demucs_python, и так это переживает
# переустановку DARAVE.
#
# Почему это кнопка, а не строчка в документации. В интерфейсе уже была
# написана кнопка «Скачать пакет слоёв», которой не существовало: текст
# писался по комментарию-намерению. Человек с установленной версией
# оказывался в тупике — скрипта в сборке тоже не было. Теперь в сборке
# лежит setup_stems.ps1, а кнопка его запускает и показывает вывод.
#
# Ставит скрипт, а не мы: он умеет находить Python 3.10-3.12, различать
# CUDA-колёса и обычные, и ставить audio-separator так, чтобы его падение
# не уносило demucs. Дублировать это в Python значит завести вторую
# реализацию, которая разойдётся с первой.
_stem_setup_proc: dict[str, object] = {}
_stem_setup_status: dict[str, dict] = {}


def stems_setup_script() -> Path | None:
    """setup_stems.ps1 из сборки или из исходников. None — файла нет."""
    p = _paths.resource_dir() / "setup_stems.ps1"
    return p if p.is_file() else None


@app.post("/api/rooms/{session_id}/stems/install")
async def install_stems_pack(session_id: str) -> JSONResponse:
    """Поставить torch и demucs в подходящий Python.

    Долгая операция: качает из PyPI гигабайты. Поэтому запускаем и сразу
    отвечаем, а вывод копится в статусе — как у самого разделения."""
    import stems as _stems

    if os.name != "nt":
        raise HTTPException(
            status_code=400,
            detail="Автоустановка пока только для Windows. На других системах: "
                   "pip install -r requirements-stems.txt в отдельный Python 3.10-3.12.")

    proc = _stem_setup_proc.get(session_id)
    if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
        raise HTTPException(status_code=409, detail="Установка уже идёт")

    script = stems_setup_script()
    if script is None:
        raise HTTPException(
            status_code=500,
            detail="setup_stems.ps1 не найден в сборке. Это наша ошибка сборки, "
                   "а не ваша: сообщите, пожалуйста.")

    # -ExecutionPolicy Bypass — иначе на машине с политикой по умолчанию
    # скрипт не запустится вовсе, а ошибка будет выглядеть как «ничего не
    # произошло». Bypass действует ТОЛЬКО на этот процесс.
    cmd = ["powershell", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-File", str(script)]
    import subprocess as _sp

    try:
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                         cwd=str(script.parent),
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Не смог запустить PowerShell: {exc}") from exc

    _stem_setup_proc[session_id] = proc
    _stem_setup_status[session_id] = {"running": True, "tail": [], "ok": None}

    def _pump() -> None:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = decode_child_line(raw).rstrip()
                if line:
                    st = _stem_setup_status[session_id]
                    st["tail"] = (st["tail"] + [line])[-60:]
        proc.wait()

    async def _watch() -> None:
        await asyncio.to_thread(_pump)
        st = _stem_setup_status[session_id]
        st["running"] = False
        # Код возврата у скрипта не единственная правда: он ставит
        # audio-separator отдельно и намеренно не падает, если тот не
        # встал. Настоящий ответ — появился ли Python с demucs.
        _stems._demucs_py_cache = None
        _stems._ext_dev_cache.clear()
        found = bool(_stems.demucs_python())
        st["ok"] = found
        st["device"] = await asyncio.to_thread(_stems.device_info)
        for line in st["tail"][-15:]:
            logger.info(f"[пакет слоёв] {line}")
        room = sessions.get_or_create(session_id)
        await room.broadcast_to_chat({"type": "stems_pack_ready", "ok": found})

    asyncio.create_task(_watch())
    return JSONResponse({"ok": True, "started": True,
                         "detail": "Ставлю пакет слоёв. Это гигабайты из PyPI — "
                                   "20-40 минут на обычном канале."})


@app.get("/api/rooms/{session_id}/stems/install")
async def install_stems_status(session_id: str) -> JSONResponse:
    st = dict(_stem_setup_status.get(session_id,
                                     {"running": False, "tail": [], "ok": None}))
    st["available"] = stems_setup_script() is not None and os.name == "nt"
    return JSONResponse(st)


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
    tracks = _tracks_for(session_id, body)
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


def _tracks_for(session_id: str, body: dict | None) -> list[dict]:
    """Треки, среди которых искать, с учётом ПУЛА ТРЕКОВ.

    Отдельной функцией, а не строкой в каждом эндпоинте, и это не
    вкусовщина. Пул сузил поиск только на клиенте: полоса в Clipping
    честно писала «швы ищутся только среди них», а `Roll`, `Сменить
    трек B` и `Подобрать пару заново` продолжали перебирать всю
    библиотеку — сервер про `only_paths` не знал вовсе. Пять эндпоинтов
    брали `_room_tracks` напрямую, и поправить четыре из пяти было бы
    ровно тем же багом, только реже.

    Меньше двух треков после сужения — сужать бессмысленно: пара из
    одного трека не бывает. Тогда честнее искать по всей библиотеке,
    чем не найти ничего."""
    tracks = _room_tracks(session_id)
    want = {str(p) for p in ((body or {}).get("only_paths") or ()) if p}
    if not want:
        return tracks
    picked = [t for t in tracks if t.get("path") in want]
    return picked if len(picked) >= 2 else tracks


@app.post("/api/rooms/{session_id}/audition/lucky")
async def audition_lucky(session_id: str, body: dict) -> JSONResponse:
    """«Повезёт»: перебрать ВСЕ стыковки этой пары на все подходящие
    приёмы и отдать лучшее. Полсотни-сотня комбинаций на пару."""
    import audition

    seam = body.get("seam") or {}
    tracks = _tracks_for(session_id, body)
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


@app.get("/api/rooms/{session_id}/audition/fx")
async def audition_fx_presets(session_id: str, a_genre: str = "", b_genre: str = "",
                              bars: float = 16.0,
                              technique_id: str = "") -> JSONResponse:
    """Какие эффекты предложить на уход и на вход для этой пары.

    Жанр сужает список, а не задаёт его: свои идут первыми, чужие следом
    и помечены. Запрещать чужие незачем — диджей имеет право на гейтер в
    ликвиде, если он знает, зачем."""
    import fx_presets

    beats = max(4.0, float(bars) * 4.0)
    # Что приём делает с эффектами сам. Без этого панель предлагала бы
    # ВТОРОЙ эффект поверх первого, а на деке юнит один.
    own: dict = {}
    if technique_id:
        try:
            import techniques as _tq

            plan = _tq.build_plan(technique_id, "p", "source", "target", 174.0, {})
            own = fx_presets.technique_fx(
                plan["events"] if isinstance(plan, dict) else plan)
        except Exception:                            # noqa: BLE001
            own = {}
    # knobs едут вместе с пресетом: интерфейс не должен знать, какие
    # ручки у какого юнита — иначе этот список придётся держать в двух
    # местах, и он разойдётся при первой же новой ручке.
    short = lambda items: [{"id": x["id"], "name": x["name"], "unit": x["unit"],
                            "why": x["why"], "native": x["native"],
                            "knobs": x.get("knobs") or []} for x in items]
    return JSONResponse({
        "out": short(fx_presets.presets("out", beats, a_genre)),
        "in": short(fx_presets.presets("in", beats, b_genre)),
        # own.out / own.in — эффект приёма на уходящей и входящей деке.
        "own": {"out": own.get("source") or None, "in": own.get("target") or None},
        "units": fx_presets.unit_options(),
    })


# ------------------------------------------------------------- пул треков
#
# Отдельно от пула ПЕРЕХОДОВ: там одобренные швы, здесь просто отложенные
# треки. См. track_pool.py — разницу диджей назвал сам.


@app.get("/api/rooms/{session_id}/track-pool")
async def track_pool_list(session_id: str) -> JSONResponse:
    import track_pool

    # Библиотека подтягивается ЛЕНИВО и после перезапуска backend'а может
    # быть ещё пуста. Пул при этом никуда не делся, и показывать пустую
    # строку вместо шести отложенных треков — врать. Поэтому пул отдаётся
    # по СВОИМ путям, а карточка из библиотеки только дополняет их, если
    # библиотека уже загружена.
    room = sessions.get_or_create(session_id)
    by_path = {t.get("path"): t for t in (room.library_tracks or [])
               if t.get("path")}
    out = []
    for p in track_pool.paths(session_id):
        t = by_path.get(p) or {}
        out.append({
            "path": p,
            "name": t.get("name") or os.path.basename(str(p).replace("\\", "/")),
            "bpm": t.get("bpm"), "camelot": t.get("camelot"),
            "genre": t.get("genre"), "subgenre": t.get("subgenre"),
            "energy": t.get("energy"),
            "duration_seconds": t.get("duration_seconds"),
            # Трека нет в текущей библиотеке: файл перенесли, или её
            # просто ещё не загрузили. Отличать эти случаи интерфейсу не
            # нужно, а знать, что карточки нет, — нужно.
            "known": bool(t),
        })
    return JSONResponse({"ok": True, "count": len(out), "tracks": out})


@app.post("/api/rooms/{session_id}/track-pool")
async def track_pool_add(session_id: str, body: dict) -> JSONResponse:
    import track_pool

    added = track_pool.add(session_id, list(body.get("paths") or ()))
    return JSONResponse({"ok": True, "added": added,
                         "count": len(track_pool.paths(session_id))})


@app.post("/api/rooms/{session_id}/track-pool/remove")
async def track_pool_remove(session_id: str, body: dict) -> JSONResponse:
    import track_pool

    if body.get("all"):
        track_pool.clear(session_id)
        return JSONResponse({"ok": True, "count": 0})
    track_pool.remove(session_id, list(body.get("paths") or ()))
    return JSONResponse({"ok": True, "count": len(track_pool.paths(session_id))})


@app.post("/api/rooms/{session_id}/track-pool/order")
async def track_pool_order(session_id: str, body: dict) -> JSONResponse:
    import track_pool

    track_pool.reorder(session_id, list(body.get("paths") or ()))
    return JSONResponse({"ok": True, "paths": track_pool.paths(session_id)})


@app.post("/api/rooms/{session_id}/audition/code")
async def audition_code(session_id: str, body: dict) -> JSONResponse:
    """Запомнить состояние шва и вернуть его код.

    Код — короткое число, посчитанное от ВСЕГО состояния: пары треков,
    приёма, точек стыка, длины, эффектов и формы ведения. Одинаковое
    состояние даёт одинаковый код всегда, поэтому запрос идемпотентный:
    повторный вызов ничего не создаёт, а возвращает тот же номер."""
    import audition

    state = body.get("state") or body
    if not state.get("a_path") or not state.get("b_path"):
        raise HTTPException(status_code=400, detail="В состоянии нет треков")
    code = await asyncio.to_thread(audition.seam_code, session_id, state)
    return JSONResponse({"ok": True, "code": code})


@app.get("/api/rooms/{session_id}/audition/code/{code}")
async def audition_by_code(session_id: str, code: str) -> JSONResponse:
    """Состояние шва по коду. 404 — такого кода в этой комнате не видели.

    Коды не общие между комнатами сознательно: код ссылается на треки по
    путям, а у другой комнаты может быть другая библиотека."""
    import audition

    state = await asyncio.to_thread(audition.seam_by_code, session_id, code)
    if state is None:
        raise HTTPException(status_code=404,
                            detail="Такого кода в этой комнате нет. Код "
                                   "запоминается, когда переход показан, и "
                                   "живёт в базе этой комнаты.")
    return JSONResponse({"ok": True, "state": state})


@app.post("/api/rooms/{session_id}/audition/roll")
async def audition_roll(session_id: str, body: dict) -> JSONResponse:
    """Один перебор на всё: варианты этой пары, а когда кончились — новая пара.

    Раньше это были три отдельные кнопки («повезёт», окно seed, «другая
    пара»), и человек должен был сам догадываться, какая из них сейчас
    уместна. Догадка простая и всегда одна: сначала выжимаем пару, потом
    берём следующую. Значит это одна кнопка, а не три.

    Seed по умолчанию считается ОТ БИБЛИОТЕКИ: одна и та же библиотека
    даёт одну и ту же последовательность вариантов, и её можно назвать
    словами. Добавили треков — расклад честно стал другим."""
    import audition

    seam = body.get("seam") or {}
    tracks = _tracks_for(session_id, body)
    seed = _roll_seed(body.get("seed"), tracks)

    items = audition.lucky(session_id, tracks, seam.get("a_path"),
                           seam.get("b_path"), limit=1,
                           exclude=list(body.get("seen") or ()), seed=seed)
    source = "pair"
    if not items:
        items = audition.other_pair(
            session_id, tracks,
            exclude=list(body.get("exclude") or ()),
            avoid=[p for p in (seam.get("a_path"), seam.get("b_path")) if p],
            limit=1)
        source = "pair_new"
    if not items:
        raise HTTPException(status_code=404,
                            detail="в библиотеке не осталось ни вариантов, ни свободных пар")
    return JSONResponse({"ok": True, "seam": items[0], "seed": seed,
                         "source": source, "tried": len(items),
                         "combo": audition.combo_key(items[0])})


def _roll_seed(raw, tracks: list[dict]) -> int:
    """Номер расклада. Пришёл от человека — берём его; нет — считаем от
    библиотеки, чтобы он был устойчивым, а не новым на каждое нажатие."""
    try:
        if raw not in (None, ""):
            return int(raw)
    except (TypeError, ValueError):
        pass
    import hashlib

    paths = sorted(str(t.get("path") or "") for t in tracks)
    h = hashlib.sha256(("\n".join(paths)).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 10_000


@app.post("/api/rooms/{session_id}/audition/other-sections")
async def audition_other_sections(session_id: str, body: dict) -> JSONResponse:
    """Другие точки стыка — треки и приём остаются те же.

    Отказ НЕ записывается: диджей не отверг переход, он ищет для него
    место. Записывать это как «не нравится» значит портить вкусовую
    модель ровно там, где человек сказал обратное."""
    import audition

    seam = body.get("seam") or {}
    tracks = _tracks_for(session_id, body)
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
    tracks = _tracks_for(session_id, body)
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
    tracks = _tracks_for(session_id, body)
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
        source=body.get("source") or "audition",
        code=(body.get("code") or seam.get("code") or None)))


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

    # Пул треков сужает сет до отложенного. Не «фильтр жанра» — явный
    # список путей, который диджей собрал руками в Library.
    only_paths = body.get("only_paths")
    if only_paths:
        want = {str(p) for p in only_paths}
        picked = [t for t in tracks if t.get("path") in want]
        if len(picked) >= 2:
            tracks = picked
        elif picked:
            raise HTTPException(
                status_code=400,
                detail="в пуле треков меньше двух треков — сет из одного не собрать")

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
    # закрытие приложения и перезагрузку backend.
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


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_local_origin(origin: str) -> bool:
    """Страница, которую отдала сама программа.

    ALLOWED_ORIGINS — про САЙТ: оттуда приходит вход. Но интерфейс
    программа отдаёт сама, с 127.0.0.1, и браузер честно ставит этот
    origin на WebSocket. Список для сайта его не знал, и собственный же
    интерфейс получал 403 на /ws/events: обновления интерфейса молча не
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
    токеном, а WebSocket оставался открытым настежь — и через него можно было
    читать события чужой комнаты.

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


@app.websocket("/ws/events/{session_id}")
async def chat_endpoint(websocket: WebSocket, session_id: str) -> None:
    """Односторонний канал событий интерфейса: прогресс анализа и статусы."""
    if not await ws_allowed(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    room = sessions.get_or_create(session_id)
    room.chat_websockets.add(websocket)
    logger.info(f"клиент событий подключился, комната '{session_id}'")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info(f"клиент событий отключился, комната '{session_id}'")
        room.chat_websockets.discard(websocket)
        sessions.drop_if_empty(session_id)
