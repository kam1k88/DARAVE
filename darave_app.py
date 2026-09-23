"""Точка входа собранного DARAVE.exe.

Отличается от `uvicorn server:app` тремя вещами, каждая из которых нужна
именно установленной версии, а не запуску из исходников.

1. Режим «за скрипт» (`--child`). Бэкенд запускает скан библиотеки и
   разделение на слои отдельными процессами; в сборке запускать нечего —
   .py-файлов рядом нет. Здесь мы ловим --child и отрабатываем за нужный
   модуль. См. child.py.

2. Рабочая папка. Из исходников DARAVE пишет базу, логи и наборы рядом с
   собой. Установленная программа лежит в Program Files, куда без прав
   администратора не записать ни байта, поэтому всё пользовательское
   переезжает в %LOCALAPPDATA%\\DARAVE.

3. ffmpeg. Он нужен для .stem.mp4 и для честных 320 кбит/с на экспорте.
   Установщик кладёт его рядом с exe — добавляем эту папку в PATH, чтобы
   shutil.which его увидел без возни с настройками.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "DARAVE"
DEFAULT_PORT = 8765


def app_dir() -> Path:
    """Папка, где лежит exe (или исходники)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def prepare_env() -> None:
    """Всё, что должно случиться ДО импорта server."""
    # Кириллица в логах дочерних процессов: без этого на русской Windows
    # print() уезжает в cp1251 и в лог приходит каша.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    here = app_dir()
    # ffmpeg рядом с exe: ffmpeg\bin\ffmpeg.exe (как его кладёт установщик)
    # либо просто ffmpeg.exe в той же папке.
    for rel in ("ffmpeg/bin", "ffmpeg", "."):
        cand = here / rel
        if (cand / "ffmpeg.exe").exists() or (cand / "ffmpeg").exists():
            os.environ["PATH"] = str(cand) + os.pathsep + os.environ.get("PATH", "")
            break

    import paths

    data = paths.data_dir()
    os.environ.setdefault("DARAVE_DATA_DIR", str(data))
    # persistence.py открывает darave.db по относительному пути, и ещё
    # несколько модулей пишут рядом с собой. Уводим текущую папку в
    # данные — иначе первая же запись упрётся в Program Files.
    try:
        os.chdir(data)
    except OSError:
        pass


def own_console() -> bool:
    """Консоль принадлежит НАМ, а не тому, кто нас запустил?

    Разница решающая. При двойном клике Windows создаёт консоль под наш
    процесс — её можно прятать. При запуске из PowerShell мы делим окно
    С НИМ, и ShowWindow(SW_HIDE) прячет окно пользователя вместе с нашим:
    человек теряет свою сессию и не видит ни одной строчки ошибки. Ровно
    это и произошло — программа падала молча, потому что окно, куда она
    писала, пряталось первым.

    GetConsoleProcessList возвращает число процессов, привязанных к
    консоли. Один — наш собственный, больше — чужой."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        buf = (ctypes.c_uint * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        return n == 1
    except Exception:
        return False


def hide_console() -> None:
    """Спрятать чёрное окно консоли — только если оно наше.

    Сборка идёт с console=True не ради окна, а ради stdout: без него
    дочерние процессы (скан, слои) не могут отдать прогресс — см.
    комментарий в DARAVE.spec."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    if not own_console():
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


class _Tee:
    """Пишет и в исходный поток, и в файл журнала запуска.

    Нужен потому, что окно консоли у собранной программы живёт ровно до
    падения: traceback печатается в окно, окно закрывается, и узнать,
    что случилось, неоткуда. Файл остаётся."""

    def __init__(self, stream, fh) -> None:
        self._stream = stream
        self._fh = fh

    def write(self, text):
        try:
            if self._stream is not None:
                self._stream.write(text)
        except Exception:
            pass
        try:
            self._fh.write(text)
            self._fh.flush()
        except Exception:
            pass
        return len(text)

    def flush(self):
        for t in (self._stream, self._fh):
            try:
                if t is not None:
                    t.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._stream) and self._stream.isatty()
        except Exception:
            return False


_launch_log_path: Path | None = None


def start_launch_log() -> None:
    """Дублировать весь вывод в %LOCALAPPDATA%\\DARAVE\\darave_launch.log.

    Открывается ДО импорта server: половина поломок сборки случается
    именно на импорте, и журнал бэкенда к тому моменту ещё не заведён."""
    global _launch_log_path
    try:
        import paths

        path = paths.data_dir() / "darave_launch.log"
        fh = open(path, "a", encoding="utf-8", errors="replace")
    except Exception:
        return
    _launch_log_path = path
    fh.write("\n" + "=" * 70 + "\n")
    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  запуск: {sys.argv}\n")
    fh.write(f"exe: {sys.executable}\n")
    fh.flush()
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)


def open_browser_later(url: str, delay: float = 1.5) -> None:
    def go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


def port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def smoke_test() -> int:
    """Проверка собранной сборки: DARAVE.exe --selftest.

    Три поломки PyInstaller'а, которые ловятся ТОЛЬКО так. Все три не
    видны при старте: программа поднимается, страница открывается, и
    падает она через несколько минут — на первом же анализе трека.

      librosa читает таблицы фильтров файлами из своего пакета;
      у scipy расширения грузятся по имени, статический анализ их не видит;
      soundfile тащит libsndfile отдельным бинарником.

    Поэтому здесь не "импортируется ли", а "считает ли": берём секунду
    шума и прогоняем через настоящий ресемплинг и настоящий mel.
    """
    import traceback

    checks: list[tuple[str, str]] = []

    def run(name: str, fn, optional: bool = False) -> None:
        """optional=True — проверка на то, без чего программа живёт.

        Без google-genai работает всё, кроме чат-ассистента (и остаётся
        Ollama). Валить из-за этого сборку нельзя, а молчать — тем более:
        именно так и появляются сборки, где кнопка «Подключить» не
        работает, а узнаётся об этом от пользователя."""
        try:
            fn()
            checks.append((name, "OK"))
        except Exception as exc:  # noqa: BLE001 — нам нужен ЛЮБОЙ отказ
            tag = "ПРЕДУПРЕЖДЕНИЕ" if optional else "ОШИБКА"
            checks.append((name, f"{tag}: {exc.__class__.__name__}: {exc}"))
            if not optional:
                traceback.print_exc()

    def _librosa() -> None:
        import numpy as np
        import librosa

        y = np.random.default_rng(0).standard_normal(22050).astype("float32")
        librosa.resample(y, orig_sr=22050, target_sr=44100)
        librosa.feature.melspectrogram(y=y, sr=22050)
        librosa.beat.beat_track(y=y, sr=22050)

    def _scipy() -> None:
        import numpy as np
        from scipy import signal

        b, a = signal.butter(4, 0.25)
        signal.sosfilt(signal.butter(4, 0.25, output="sos"), np.zeros(1024))
        signal.lfilter(b, a, np.zeros(1024))

    def _soundfile() -> None:
        import io

        import numpy as np
        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, np.zeros((1024, 2), dtype="float32"), 44100, format="WAV")

    def _static() -> None:
        import paths

        page = paths.resource_dir() / "static" / "chat.html"
        if not page.exists():
            raise FileNotFoundError(str(page))

    def _ffmpeg() -> None:
        import shutil

        if not shutil.which("ffmpeg"):
            raise FileNotFoundError("ffmpeg не найден в PATH и рядом с exe")

    def _children() -> None:
        import child

        for mod in child.CHILD_MODULES:
            __import__(mod)

    def _genai() -> None:
        # Namespace-пакет: PyInstaller его не видит без явной подсказки в
        # .spec, и ломается это не при старте, а когда пользователь
        # нажимает «Подключить» с ключом на руках.
        from google import genai  # noqa: F401

    def _backend_builds() -> None:
        # САМАЯ ВАЖНАЯ проверка, и добавлена она задним числом. FastAPI
        # разбирает подписи обработчиков ПРЯМО В ДЕКОРАТОРЕ: не хватает
        # python-multipart для UploadFile — и падает не загрузка файла, а
        # импорт server.py целиком, то есть программа не стартует вовсе.
        # Проверить это можно ровно одним способом — собрать приложение.
        import server

        routes = len(getattr(server.app, "routes", []))
        if routes < 10:
            raise RuntimeError(f"у приложения всего {routes} маршрутов — похоже, собралось не всё")

    def _llm_starts_without_key() -> None:
        # Главная поломка первого запуска: провайдер создавался при
        # импорте server.py и бросал RuntimeError, если ключа нет. Первый
        # же запуск установленной версии заканчивался трейсбеком.
        import llm_providers

        prov = llm_providers.make_provider()
        if prov is None:
            raise RuntimeError("make_provider вернул None")

    def _fx_units_exist() -> None:
        # Словарь эффектов (fx_presets.py) и рендер (demo_render.py) —
        # разные файлы, и разъехаться им ничего не мешает. Пообещать в
        # карточке «фленджер», которого рендер не знает, — значит отдать
        # человеку тишину вместо эффекта и ни строчки в лог.
        #
        # Проверяем ровно это: каждый юнит из пресетов рендер умеет, и
        # на сторону приходится не больше одного эффекта.
        import demo_render
        import fx_presets

        known = set(demo_render.FX_ALL)
        for side in ("out", "in"):
            for p in fx_presets.presets(side, 64.0, "techno"):
                if p["unit"] and p["unit"] not in known:
                    raise RuntimeError(
                        f"пресет «{p['name']}» просит эффект {p['unit']}, "
                        f"которого рендер не знает")
                decks = {e["deck"] for e in p["events"]}
                if len(decks) > 1:
                    raise RuntimeError(f"пресет «{p['name']}» лезет на обе деки")
                units = {(e.get("params") or {}).get("unit") for e in p["events"]}
                if len(units) > 1:
                    raise RuntimeError(f"в пресете «{p['name']}» больше одного эффекта")
        # И через общий вход: два пресета дают ровно два события, по
        # одному на деку.
        ev = fx_presets.events_for("echo_tail", "wobble_in", 64.0)
        if len(ev) != 2 or {e["deck"] for e in ev} != {"source", "target"}:
            raise RuntimeError("events_for отдаёт не по одному эффекту на сторону")

        # --- ручки ---
        #
        # Ручка, которую никто не слушает, хуже отсутствующей: она
        # обещает и врёт. Поэтому каждая ручка обязана доезжать до
        # события — именно оттуда её читает рендер.
        for unit in demo_render.FX_ALL:
            if not fx_presets.knobs_for(unit):
                raise RuntimeError(f"у эффекта {unit} нет ни одной ручки, даже силы")
        for side in ("out", "in"):
            for p in fx_presets.presets(side, 64.0, "techno"):
                for k in p["knobs"]:
                    if not (k["min"] <= k["default"] <= k["max"]):
                        raise RuntimeError(
                            f"«{p['name']}»: ручка «{k['name']}» открывается "
                            f"на {k['default']} вне диапазона {k['min']}..{k['max']}")
                if p["unit"] and not p["knobs"]:
                    raise RuntimeError(f"«{p['name']}» нечем настроить")
                if not p["unit"] and p["knobs"]:
                    raise RuntimeError("у «чисто» появились ручки — настраивать нечего")

        # Сила масштабирует ход, а не подменяет его.
        full = fx_presets.events_for("echo_tail", None, 64.0)
        half = fx_presets.events_for("echo_tail", None, 64.0,
                                     out_opts={"strength": 50})
        if not full or not half:
            raise RuntimeError("пресет ухода перестал давать события")
        if abs(half[0]["value_to"] - full[0]["value_to"] / 2.0) > 1e-6:
            raise RuntimeError("сила не масштабирует глубину эффекта")
        if fx_presets.events_for("echo_tail", None, 64.0, out_opts={"strength": 0}):
            raise RuntimeError("сила 0 обязана быть тем же, что «чисто»")

        # Ручки юнита доезжают до params — там их и читает рендер.
        tuned = fx_presets.events_for(
            "echo_tail", None, 64.0,
            out_opts={"delay_beats": 0.25, "feedback": 75})
        prm = tuned[0].get("params") or {}
        if prm.get("delay_beats") != 0.25 or prm.get("feedback") != 75.0:
            raise RuntimeError(f"ручки не доехали до события: {prm}")
        # …и значения по умолчанию совпадают с тем, что звучит без правок.
        base = (full[0].get("params") or {})
        for key, val in fx_presets.defaults_for("out", "echo_tail").items():
            if key == "strength":
                continue
            if abs(float(base.get(key, val)) - float(val)) > 1e-6:
                raise RuntimeError(
                    f"ручка «{key}» открывается на {val}, а звучит {base.get(key)}")

        # --- эффект, которым играет САМ приём ---
        #
        # Пятнадцать приёмов открывают «ручку FX», не называя юнит
        # (fx_meta/fx_mix), и рендер подставлял им эхо жёстко: уйти в
        # реверб на DNB-25 было невозможно физически. Семь приёмов юнит
        # называют — этим менять его нельзя, это и есть приём.
        import techniques as _tq

        import demo_render as _dr

        free, named = [], []
        for tid in sorted(_tq.TECHNIQUES):
            if tid == "CUSTOM":
                continue
            try:
                plan = _tq.build_plan(tid, "p", "source", "target", 174.0, {})
            except ValueError:
                continue
            own = fx_presets.technique_fx(
                plan["events"] if isinstance(plan, dict) else plan)
            for deck, slot in own.items():
                (free if slot["free"] else named).append((tid, deck, slot))
        if not free or not named:
            raise RuntimeError(
                f"каталог перестал делиться на два случая: "
                f"безымянных {len(free)}, названных {len(named)}")

        # Безымянной ручке юнит подменяется…
        tid, deck, _ = free[0]
        plan = _tq.build_plan(tid, "p", "source", "target", 174.0, {})
        ev = plan["events"] if isinstance(plan, dict) else plan
        tuned = _dr._tune_own_fx(list(ev), {deck: {"unit": "reverb", "strength": 50}})
        units = {(e.get("params") or {}).get("unit")
                 for e in tuned
                 if e["action"] in ("fx_meta", "fx_mix") and e["deck"] == deck}
        if units != {"reverb"}:
            raise RuntimeError(f"«{tid}»: юнит не подменился — {units}")
        # …и сила масштабирует ход этой ручки.
        было = max(float(e.get("value_to") or 0.0) for e in ev
                   if e["action"] in ("fx_meta", "fx_mix") and e["deck"] == deck)
        стало = max(float(e.get("value_to") or 0.0) for e in tuned
                    if e["action"] in ("fx_meta", "fx_mix") and e["deck"] == deck)
        if abs(стало - было / 2.0) > 1e-6:
            raise RuntimeError(f"«{tid}»: сила не масштабирует эффект приёма ({было} -> {стало})")

        # А названному юниту — НЕ подменяется.
        tid2, deck2, slot2 = named[0]
        plan2 = _tq.build_plan(tid2, "p", "source", "target", 174.0, {})
        ev2 = plan2["events"] if isinstance(plan2, dict) else plan2
        t2 = _dr._tune_own_fx(list(ev2), {deck2: {"unit": "wobble"}})
        got = {(e.get("params") or {}).get("unit")
               for e in t2 if e["action"] in ("fx", "stem_fx") and e["deck"] == deck2}
        if "wobble" in got and "wobble" not in slot2["units"]:
            raise RuntimeError(f"«{tid2}»: юнит приёма подменили — это уже другой приём")

    def _layer_bias_works() -> None:
        # Признаки по слоям влияют на ПОРЯДОК приёмов, а не только
        # лежат в meta.json. Проверка поведенческая: строим пару, у
        # которой два непрерывных баса в одной полосе, и требуем, чтобы
        # обмен низом обогнал длинное наложение.
        #
        # Именно поведенческая, а не «функция существует»: разъехаться
        # тут может что угодно — имя ключа в gap(), состав множеств
        # BASS_HANDLERS/BASS_BLIND, зажим множителя. Все три поломки
        # выглядят как молча вернувшийся 1.0, и тест на существование
        # функции их не поймает.
        import stem_features
        import style

        # Пара, которая спорит ТОЛЬКО БАСОМ: гармонии разведены по
        # яркости нарочно. Иначе штраф за гармонию подпирает штраф за
        # бас, и проверка проходит, даже когда басовый штраф снят вовсе
        # — на это я уже попался один раз, когда она так и была
        # написана.
        bass_only = {"drums_bright": 0.20, "bass_density": 0.90, "bass_sub": 0.94,
                     "harm_bright": 0.20, "harm_density": 0.90, "vocal_share": 0.0}
        other = dict(bass_only, drums_bright=0.80, harm_bright=0.60)
        g = stem_features.gap(bass_only, other)
        if g is None or g["bass"] < stem_features.BASS_CLASH:
            raise RuntimeError("два непрерывных баса в одной полосе не считаются конфликтом")
        if g["harm"] >= stem_features.HARM_CLASH:
            raise RuntimeError("гармонии разведены по яркости, а считаются конфликтом")

        swap, _ = style.layer_technique_bias("DNB-03", g, True)     # Bass Swap
        blend, _ = style.layer_technique_bias("DNB-00", g, True)    # Long Blend
        if not swap > 1.0:
            raise RuntimeError(f"обмен низом не получил надбавки: {swap}")
        if not blend < 1.0:
            raise RuntimeError(f"длинное наложение не оштрафовано за два баса: {blend}")

        # И то же самое отдельно по гармонии: бас у второго редкий,
        # спорит только верх.
        harm_only = {"drums_bright": 0.50, "bass_density": 0.90, "bass_sub": 0.94,
                     "harm_bright": 0.35, "harm_density": 0.90, "vocal_share": 0.0}
        hg = stem_features.gap(harm_only, dict(harm_only, bass_density=0.10))
        if hg["bass"] >= stem_features.BASS_CLASH:
            raise RuntimeError("редкий бас у одного из треков считается конфликтом")
        if hg["harm"] < stem_features.HARM_CLASH:
            raise RuntimeError("две плотные гармонии одной яркости не считаются конфликтом")
        if not style.layer_technique_bias("DNB-01", hg, True)[0] < 1.0:
            raise RuntimeError("гармоническое наложение не оштрафовано за кашу в верхах")
        if not style.layer_technique_bias("DNB-04", hg, True)[0] > 1.0:
            raise RuntimeError("фильтр не получил надбавки на конфликте гармоний")

        # И обратное: без признаков поправки нет вовсе. Молчание здесь
        # обязательно — библиотека без стемов не должна получать
        # выдуманный порядок приёмов.
        if style.layer_technique_bias("DNB-00", None, True)[0] != 1.0:
            raise RuntimeError("без признаков слоёв приём всё равно штрафуется")
        if stem_features.gap(bass_only, None) is not None:
            raise RuntimeError("половина пары без признаков — должно быть None")

        # Пороги обязаны что-то делить. Пара, одинаковая по всем осям,
        # не должна давать одновременно и конфликт, и «барабаны разные».
        same = stem_features.gap(bass_only, dict(bass_only))
        if same["drum_fit"] > stem_features.DRUM_SAME:
            raise RuntimeError("одинаковые барабаны посчитаны разными")

        # Середина шкалы обязана оставаться НЕЙТРАЛЬНОЙ. Это и есть
        # смысл порога: надбавка, которую получают две трети пар, не
        # отличает ничего — она просто поднимает общий уровень. Первая
        # версия этого признака срабатывала на 71% пар, и поймал её
        # замер, а не тест. Теперь ловит тест.
        mid = stem_features.gap(bass_only, dict(bass_only, drums_bright=0.35))
        if style.layer_technique_bias("ST-02", mid, True)[0] != 1.0:
            raise RuntimeError(
                f"барабаны разошлись на 0.15, а приём уже подвинут "
                f"(drum_fit={mid['drum_fit']}) — порог ничего не делит")

    def _stems_setup_shipped() -> None:
        # setup_stems.ps1 не входил в сборку ВООБЩЕ, а интерфейс при этом
        # отправлял человека к кнопке, которой тоже не было. Установленная
        # версия не могла получить слои никак — и узналось это от диджея,
        # а не от проверки.
        #
        # Проверка дешёвая и ловит ровно этот класс поломки: файл есть в
        # исходниках, но забыт в datas. В собранной программе
        # resource_dir() — это _internal, то есть спрашиваем ровно то, что
        # реально уехало в установщик.
        import paths as _p

        for name in ("setup_stems.ps1", "requirements-stems.txt"):
            f = _p.resource_dir() / name
            if not f.is_file():
                raise RuntimeError(
                    f"{name} нет в сборке ({f}) — установить пакет слоёв "
                    f"будет нечем. Проверьте datas в DARAVE.spec")

    def _profile_follows_seam() -> None:
        # Форма ведения обязана ЗАВИСЕТЬ от шва и НЕ иметь состояния.
        #
        # Ползунки убраны не потому, что числа были плохие, а потому что
        # они жили в переменной экрана: подобранное на одной паре ехало
        # на следующую. Проверяем ровно два свойства, которых у прежней
        # схемы не было.
        import mix_profile as _mp

        # 1. Длина меняет форму. Если это перестанет быть правдой,
        #    кнопка «такты» станет украшением: длина поменяется, а
        #    ведение останется от прежней.
        short = _mp.auto(8, "groove→groove")
        long_ = _mp.auto(32, "groove→groove")
        if not short["in_len"] < long_["in_len"]:
            raise RuntimeError(
                f"длина перехода не меняет форму: 8 тактов {short['in_len']}, "
                f"32 такта {long_['in_len']}")

        # 2. Стык меняет форму: дроп в дроп сводят резче, чем брейкдаун
        #    в брейкдаун. Иначе таблица JUNCTION_SHAPE ни на что не влияет.
        drop = _mp.auto(16, "drop→drop")
        bd = _mp.auto(16, "breakdown→breakdown")
        if not drop["in_len"] < bd["in_len"]:
            raise RuntimeError("тип стыка не меняет форму")

        # 3. Чистая функция: два вызова с теми же доводами дают то же
        #    самое, и ничего не помнится между ними.
        if _mp.auto(16, "drop→drop") != drop:
            raise RuntimeError("auto() возвращает разное на одних доводах")

        # 4. Границы. Форма уезжает в рендер, и значение вне 0..1 там
        #    означает тишину или обрыв, а не «странный звук».
        for bars in (1, 2, 4, 8, 16, 32, 64, 128, 256, 0):
            for lab in list(_mp.JUNCTION_SHAPE) + [None, "чушь"]:
                p = _mp.auto(bars, lab)
                for k in ("in_len", "out_len", "in_smooth", "out_smooth"):
                    if not 0.0 <= p[k] <= 1.0:
                        raise RuntimeError(f"{k}={p[k]} вне 0..1 при bars={bars}, стык={lab}")
                if p.get("entry") != "manual":
                    raise RuntimeError("профиль перестал вести фейдеры сам")

        # 5. Кнопка «такты» не должна выбрасывать из текущей длины:
        #    нажал — и не вернуться, потому что твоего значения в списке нет.
        import audition as _aud

        for cur in (3, 12, 16, 48):
            opts = _aud.bars_options({"bpm": 174}, {"bpm": 174, "genre": "dnb"}, cur)
            if cur not in opts:
                raise RuntimeError(f"текущая длина {cur} не попала в список {opts}")
            if len(opts) < 2:
                raise RuntimeError("в списке длин нечего перебирать")

    def _stems_take_a_list() -> None:
        # Шаг «слои» обязан получать СПИСОК ТРЕКОВ, а не папку.
        #
        # У диджея треки лежали в «music/Phonk» и в «Music». Общий корень
        # этих двух — C:\Users\kam1k88, весь профиль. Шаг уходил обходить
        # AppData и Downloads: десятки минут тишины и ни процента
        # нагрузки на видеокарту, потому что до разделения дело не
        # доходило. Выглядело как «анализ дальше не идёт».
        import server
        import stems

        cmd = server.stems_child_cmd("список.txt", "demucs")
        if "--paths-file" not in cmd:
            raise RuntimeError(f"разделение зовут без списка треков: {cmd}")
        if "--dir" in cmd:
            raise RuntimeError(f"разделение снова зовут папкой: {cmd}")

        # Обход --dir остался для ручного запуска, и он не должен
        # затягивать то, что DARAVE сделал сам: слои слоёв на библиотеке
        # диджея это 93 папки по четыре файла — 372 лишних «трека».
        for own in ("stems", "demos", "sets", "recordings"):
            if own not in stems._OWN_DIRS:
                raise RuntimeError(f"обход --dir зайдёт в свою папку «{own}»")

        # Код комнаты идёт в имя файла, а ввести его можно руками.
        if "/" in server._safe_room_key("a/b") or ".." in server._safe_room_key("../x"):
            raise RuntimeError("код комнаты попадает в путь как есть")

    def _progress_survives_encoding() -> None:
        # Строка прогресса обязана печататься при ЛЮБОЙ кодировке stdout.
        #
        # 1.1.3, журнал диджея: «Ü» в имени трека, stdout дочернего
        # процесса в cp1251 —
        #   File "stems.py", line 1885, in emit_progress
        #   UnicodeEncodeError: 'charmap' codec can't encode '\xdc'
        # и разделение на слои умерло на первом же треке, не тронув
        # видеокарту. Снаружи: «шаг идёт, карта не грузится».
        import io as _io
        import library_analysis as _la
        import stems as _stems

        class _Narrow(_io.TextIOBase):
            """stdout, который умеет только свою кодировку."""

            def __init__(self, enc):
                self.enc, self.buf = enc, []

            def write(self, s):
                s.encode(self.enc)                   # как настоящий поток
                self.buf.append(s)
                return len(s)

            def flush(self):
                pass

        name = "Münchener — Ü.mp3"
        for enc in ("utf-8", "cp1251", "cp866", "ascii"):
            real, sys.stdout = sys.stdout, _Narrow(enc)
            try:
                _stems.emit_progress("separate", 3, 13, name=name)
                out = "".join(sys.stdout.buf).strip()
            except UnicodeEncodeError as exc:
                sys.stdout = real
                raise RuntimeError(f"прогресс уронил проход на stdout={enc}: {exc}") from exc
            finally:
                sys.stdout = real
            got = _la.parse_progress_line(out)
            if not got or got.get("i") != 3 or got.get("total") != 13:
                raise RuntimeError(f"на stdout={enc} полоса не читается: {out!r}")

        # Маркер протокола обязан быть ASCII: иначе на узком stdout не
        # пролезает даже он, и спасать имя файла уже бессмысленно.
        try:
            _la.PROGRESS_PREFIX.encode("ascii")
        except UnicodeEncodeError:
            raise RuntimeError("маркер прогресса снова не ASCII") from None

    def _writes_go_to_data_dir() -> None:
        # Всё, куда программа ПИШЕТ, обязано лежать в data_dir и быть
        # доступным на запись.
        #
        # 1.1.4, журнал диджея:
        #   PermissionError: [WinError 5] Отказано в доступе:
        #   'C:\\Program Files\\DARAVE\\_internal\\stems'
        # Слои писались рядом с кодом (`Path(__file__).parent / "stems"`).
        # Из исходников это верно и потому не замечалось годами; в
        # установленной версии `__file__` — это _internal внутри Program
        # Files, куда без администратора не записать.
        #
        # Той же болезнью болела уборка: она мела `_internal/demos`,
        # которых там нет, и честно сообщала «чистить нечего», пока
        # настоящие демо копились и не убирались никогда.
        import housekeeping
        import paths as _p
        import stems

        data = _p.data_dir().resolve()
        res = _p.resource_dir().resolve()

        for name, path in (("слои", stems.STEM_DIR),
                           ("уборка", housekeeping.HERE)):
            p = Path(path).resolve()
            if not str(p).startswith(str(data)):
                raise RuntimeError(f"{name}: пишем вне data_dir — {p}")
            # В сборке ресурсы и данные это РАЗНЫЕ папки; из исходников
            # они совпадают, и тогда проверять нечего.
            if res != data and str(p).startswith(str(res)):
                raise RuntimeError(f"{name}: пишем в папку с кодом — {p}")

        # И самое простое: туда правда можно записать.
        #
        # Уборка — в finally, и это не педантизм: если запись упала ПОСЛЕ
        # mkdir (а именно так и бывает — права на папку есть, на файл
        # нет), прежний код до rmdir не доходил и оставлял «проба-записи»
        # лежать в репозитории. Диджей видел её в git status как мусор
        # неизвестного происхождения.
        import shutil as _shutil

        probe = data / "проба-записи"
        try:
            probe.mkdir(parents=True, exist_ok=True)
            (probe / "x").write_text("x", encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"в {data} не записать: {exc}") from exc
        finally:
            _shutil.rmtree(probe, ignore_errors=True)

    def _seam_code_covers_everything() -> None:
        # Код шва обязан меняться от КАЖДОГО поля состояния.
        #
        # Жалоба диджея была ровно про это: «seed не показывает значения
        # всех параметров — треки, стыки, техники, FX и прочее». Прежний
        # seed был номером расклада и про содержимое карточки не говорил
        # ничего.
        #
        # Проверка идёт по самому списку CODE_FIELDS, а не по выписанным
        # руками полям: добавят поле в состояние и забудут в коде — тест
        # промолчит; уберут поле из кода — тест упадёт. Второе и есть та
        # поломка, которую надо ловить.
        import audition

        room = "__selftest__"
        base = {"a_path": "C:/m/a.mp3", "b_path": "C:/m/b.mp3",
                "technique_id": "ST-08", "pair_label": "drop→drop",
                "exit_seconds": 109.0, "entry_seconds": 13.0, "bars": 32,
                "fx_out": "echo_tail", "fx_in": "clean",
                "fx_out_opts": {"strength": 60, "delay_beats": 0.5},
                "fx_in_opts": {},
                "fx_own_out": {"unit": "echo", "strength": 100},
                "fx_own_in": None,
                "mix_profile": {"in_len": 0.55}, "params": {"blend_bars": 8}}
        other = {"a_path": "C:/m/z.mp3", "b_path": "C:/m/z.mp3",
                 "technique_id": "DNB-03", "pair_label": "groove→groove",
                 "exit_seconds": 200.0, "entry_seconds": 40.0, "bars": 16,
                 "fx_out": "clean", "fx_in": "filter_in",
                 "fx_out_opts": {"strength": 100, "delay_beats": 0.75},
                 "fx_in_opts": {"strength": 40},
                 "fx_own_out": {"unit": "reverb", "strength": 70},
                 "fx_own_in": {"strength": 50},
                 "mix_profile": {"in_len": 0.9}, "params": {"blend_bars": 4}}

        code = audition.seam_code(room, base)
        if not 0 < code < 1_000_000_000:
            raise RuntimeError(f"код вне девяти цифр: {code}")
        if audition.seam_code(room, dict(base)) != code:
            raise RuntimeError("одно и то же состояние дало разные коды")

        for field in audition.CODE_FIELDS:
            changed = dict(base)
            changed[field] = other[field]
            if audition.seam_code(room, changed) == code:
                raise RuntimeError(f"код не замечает поле «{field}»")

        # Дробь секунды — это та же точка стыка, а не другая.
        if audition.seam_code(room, dict(base, exit_seconds=109.04)) != code:
            raise RuntimeError("код меняется от сотых долей секунды")

        # Возврат: по коду собирается ровно то, что закодировали.
        back = audition.seam_by_code(room, code)
        if not back:
            raise RuntimeError("код не расшифровывается")
        for field in ("technique_id", "fx_out", "params"):
            if back.get(field) != base[field]:
                raise RuntimeError(f"после возврата поле «{field}» не то: {back.get(field)}")

        # Коды не общие между комнатами: код ссылается на треки путями, а
        # у другой комнаты библиотека может быть другой.
        if audition.seam_by_code(room + "-чужая", code) is not None:
            raise RuntimeError("код виден из чужой комнаты")

    def _counters_can_reach_zero() -> None:
        # Счётчик шага обязан показывать то, что шаг МОЖЕТ сделать.
        #
        # Диджей: «почему каждую новую сборку приходится делать новый
        # анализ? разве это всё не лежит в разметке в .db?» Лежало. В его
        # базе все 140 треков были с темпом, тональностью, структурой и
        # обеими картами, а скан честно писал «разбираю 0». Врали
        # СЧЁТЧИКИ, и четыре из них не могли дойти до нуля никогда:
        #
        #   вокал 54, слои 127 — считались по has_stems, а он отвечает
        #     «да» и на голый .stem.mp4, для которого результат записать
        #     некуда;
        #   темп 15 — «одинаковый BPM у многих» считалось заглушкой, а
        #     это был драм-н-бейс на 174;
        #   каталоги 140 — шаг без ключа Discogs не пишет ничего.
        #
        # Панель показывала работу, работа не делалась, и выглядело это
        # как «ничего не сохраняется».
        import library_analysis as _la

        # 1. Темп: жанровая библиотека — не заглушка.
        dnb = [{"path": f"t{i}", "bpm": 174.0,
                "structure": {"drum_map": [1], "energy_map": [1]}}
               for i in range(15)]
        dnb += [{"path": f"u{i}", "bpm": 120.0 + i,
                 "structure": {"drum_map": [1], "energy_map": [1]}}
                for i in range(125)]
        r = _la.missing_report(dnb)
        if r["bpm"]:
            raise RuntimeError(f"15 треков на 174 сочтены заглушкой: {r['bpm']}")

        # А вот когда ОДНО значение занимает больше половины — заглушка.
        stub = [{"path": f"s{i}", "bpm": 128.0,
                 "structure": {"drum_map": [1], "energy_map": [1]}} for i in range(20)]
        if not _la.missing_report(stub)["bpm"]:
            raise RuntimeError("настоящая заглушка темпа не замечена")

        # Трек вообще без темпа считается всегда.
        none_bpm = [{"path": "x", "bpm": 0,
                     "structure": {"drum_map": [1], "energy_map": [1]}}]
        if not _la.missing_report(none_bpm)["bpm"]:
            raise RuntimeError("трек без темпа не посчитан")

        # 2. Вокал и слои считаются по ПАПКЕ слоёв, а не по has_stems.
        paths = [f"p{i}" for i in range(10)]
        tracks = [{"path": p, "bpm": 174.0 + i,
                   "structure": {"drum_map": [1], "energy_map": [1]}}
                  for i, p in enumerate(paths)]
        folder = set(paths[:4])                      # у четырёх есть папка
        rep = _la.missing_report(
            tracks,
            has_stems=lambda p: True,                # у всех есть .stem.mp4
            vocal_map=lambda p: None,
            layer_features=lambda p: None,
            has_layers=lambda p: p in folder)
        # Считаем по has_stems — включая голый .stem.mp4. Это НЕ откат к
        # прежней ошибке: тогда считать их было можно, а записать
        # результат некуда, и счётчик не мог дойти до нуля. Теперь
        # разметка для них ложится спутником рядом с mp4
        # (stems.write_meta), и счёт по has_stems снова честен.
        if rep["vocals"] != 10 or rep["layers"] != 10:
            raise RuntimeError(
                f"счётчики не видят треки с одним .stem.mp4: {rep}")
        if rep.get("mp4_only") != 6:
            raise RuntimeError(f"треки с одним .stem.mp4 не показаны: {rep}")

        # И главное: разметке ЕСТЬ КУДА лечь. Без этого счёт по has_stems
        # был бы обещанием, которого программа не выполняет.
        import stems as _stems

        if not hasattr(_stems, "write_meta") or not hasattr(_stems, "sidecar_for"):
            raise RuntimeError(
                "счёт идёт по .stem.mp4, а писать разметку для него нечем")

        # 3. Досчитанное уходит из счётчика — то есть ноль достижим.
        rep2 = _la.missing_report(
            tracks, has_stems=lambda p: True,
            vocal_map=lambda p: [],                  # посчитано у всех
            layer_features=lambda p: {"version": 1},
            has_layers=lambda p: p in folder)
        if rep2["vocals"] or rep2["layers"]:
            raise RuntimeError(f"счётчик не обнуляется после работы: {rep2}")

    def _cuts_enter_at_full() -> None:
        # У резов и бэкспинов входящий вступает СРАЗУ И ЦЕЛИКОМ.
        #
        # Диджей про «Луп в бэкспин» (TT-09): «после того, как уходящий A
        # уходит в бэкспин, входящий B начинает без басса, басс
        # подтягивается через какое-то время, и поэтому звучит провал».
        #
        # Причина была в mix_profile.auto: форма считалась для ВСЕХ
        # приёмов одинаково, и на 8 тактах входящий поднимался за 38%
        # перехода — три такта с нуля. Для наложения верно, для бэкспина
        # это дыра. «Без баса» — как звучит поднимающийся фейдер на треке
        # с сабом: низ доходит последним.
        import mix_profile as _mp

        for tid in ("TT-09", "TT-01", "DNB-02", "ST-08"):
            p = _mp.auto(8, "drop→drop", None, tid)
            if _mp.is_manual(p):
                raise RuntimeError(
                    f"{tid}: входящему всё ещё навязывают подъём фейдера "
                    f"({p.get('in_len')})")

        # Регистр не должен ничего ломать: идентификатор приходит и из
        # шва, и из адреса, и из кода шва.
        if _mp.is_manual(_mp.auto(8, None, None, "tt-09")):
            raise RuntimeError("регистр идентификатора приёма не учитывается")

        # А наложениям форма нужна — иначе правка лечит одно и ломает
        # другое.
        for tid in ("DNB-25", "DNB-00", "ST-03", None):
            if not _mp.is_manual(_mp.auto(8, "groove→groove", None, tid)):
                raise RuntimeError(f"{tid}: наложение осталось без формы ведения")

        # И самое важное — что это доходит до РЕНДЕРА. apply обязан
        # оставить события приёма нетронутыми.
        events = [{"action": "crossfade", "deck": "target", "beat_offset": 0.0,
                   "duration_beats": 4.0, "value_from": 0.0, "value_to": 1.0}]
        cut = _mp.auto(8, "drop→drop", None, "TT-09")
        if _mp.apply(list(events), cut) != events:
            raise RuntimeError("apply всё равно переписал события бэкспина")

    def _incoming_gets_its_bass() -> None:
        # Входящий не должен входить без барабанов и без низа.
        #
        # Диджей: «многие техники будто сломались… остальные ломают
        # входящий трек, где он звучит будто только в слое others, без
        # басса драмсов и вокала». Слой others — это ровно то, что
        # остаётся от деки, когда у неё закрыты барабаны и бас.
        #
        # Причина: ворота барабанов («одна установка за раз») считались
        # от СЫРЫХ событий crossfade, а mix_profile.apply их выбрасывает
        # — в этом весь его смысл. Событий нет, огибающая падает на
        # умолчание 0.0, ворота входящего не открываются ни разу. Низ
        # ломался так же, только через cf_env.
        #
        # Замер по каталогу: под профилем без crossfade и без eq_low шли
        # 20 приёмов, у всех двадцати барабаны входящего молчали весь
        # переход. Рабочими оставались те же три, что назвал диджей.
        import numpy as _np

        import demo_render as _dr
        import mix_profile as _mp
        import techniques as _tq

        n, sr, spb = 2000, 100, 1.0

        # 1. Форма берётся из САМОГО профиля, а не выдумывается.
        for bars, label in ((4, "drop→drop"), (16, "groove→groove"),
                            (32, "groove→groove"), (8, "breakdown→drop")):
            prof = _mp.auto(bars, label)
            il, ol = float(prof["in_len"]), float(prof["out_len"])
            x = _np.linspace(0.0, 1.0, n)
            vol = _np.array([_mp.fader_shape(min(1.0, t / il), prof["in_smooth"])
                             for t in x])
            src = _np.array([1.0 - _mp.fader_shape(t, prof["out_smooth"])
                             for t in _np.clip((x - (1.0 - ol)) / ol, 0.0, 1.0)])
            hand = _dr._handover_position(vol, src)
            gate = _dr._smoothstep(hand)

            where = f"{bars} тактов, {label}"
            if float(gate.max()) < 0.99:
                raise RuntimeError(f"{where}: барабаны входящего так и не открылись")
            if float(gate[0]) > 0.01:
                raise RuntimeError(f"{where}: барабаны обеих дек звучат с самого начала")

            swap = int(_np.argmax(gate > 0.5))
            leave = int(_np.argmax(src < 0.99)) if (src < 0.99).any() else n - 1
            if swap > leave:
                raise RuntimeError(
                    f"{where}: обмен ({swap / n:.2f}) позже ухода старого ({leave / n:.2f})")
            # И не слишком рано: отдавать низ треку, который ещё тише
            # половины, незачем — это и есть провал с другой стороны.
            if float(vol[swap]) < 0.5:
                raise RuntimeError(
                    f"{where}: обмен на входящем уровня {vol[swap]:.2f} — слишком рано")
            # «Одна установка за раз» не должна сломаться.
            if float((gate + (1.0 - gate)).max()) > 1.01:
                raise RuntimeError(f"{where}: обе установки звучат разом")

        # 2. Главное: на РЕАЛЬНОМ каталоге. Форму можно подогнать под
        # проверку, а каталог подогнать нельзя — именно он и показал, что
        # сломано ровно двадцать приёмов.
        silent = []
        for tid in sorted(_tq.TECHNIQUES):
            if tid == "CUSTOM":
                continue
            try:
                plan = _tq.build_plan(tid, "p", "source", "target", 174.0, {})
            except ValueError:
                continue          # трёхдековые: пары им мало
            ev = plan["events"] if isinstance(plan, dict) else plan
            for bars, label in ((16, "groove→groove"), (4, "drop→drop")):
                out = _mp.apply(list(ev), _mp.auto(bars, label, None, tid))
                if any(e["action"] == "crossfade" for e in out):
                    continue      # ведёт кроссфейд — путь другой
                decks = {e["deck"] for e in out if e["action"] == "stem_gain"}
                if decks == {"source", "target"}:
                    continue      # слои ведёт сама техника
                low = {e["deck"] for e in out if e["action"] == "eq_low"}
                if low == {"source", "target"}:
                    continue      # низом управляет сама техника
                hand = _dr._manual_handover(out, spb, 0.0, n, sr)
                if hand is None or float(_np.max(hand)) < 0.99:
                    silent.append(f"{tid} ({bars} тактов)")
        if silent:
            raise RuntimeError(
                f"входящий идёт без барабанов и низа у {len(silent)} приёмов: "
                + ", ".join(silent[:6]))

        # 3. Кроссфейдный путь трогать было нельзя: там обмен по-прежнему
        # считает сам кроссфейд, а не этот механизм.
        cf_only = [{"beat_offset": 0.0, "action": "crossfade", "deck": "master",
                    "duration_beats": 16.0, "value_from": 0.0, "value_to": 1.0}]
        if _dr._manual_handover(cf_only, spb, 0.0, n, sr) is not None:
            raise RuntimeError("кроссфейду подменили механизм обмена")

    def _catalog_rules_reach_clipping() -> None:
        # Требования, записанные в самом каталоге, обязаны влиять на то,
        # что Clipping предлагает.
        #
        # У каждого приёма есть порог по темпу (в ПРОЦЕНТАХ), требование
        # к тональности и направление энергии. Читал их только живой
        # стратег; подбор приёма в Clipping не знал про них вовсе и
        # предлагал Long Blend между 174 и 128 как ни в чём не бывало.
        #
        # Проверка идёт по САМОМУ каталогу, а не по выписанным руками
        # приёмам: появится новый с порогом — он проверится сам.
        import audition
        import techniques as _tq

        far = ({"bpm": 174.0, "camelot": "8A", "path": "a"},
               {"bpm": 128.0, "camelot": "9A", "path": "b"})
        near = ({"bpm": 174.0, "camelot": "8A", "path": "a"},
                {"bpm": 176.0, "camelot": "9A", "path": "b"})

        by_far = {x["id"]: x for x in audition.techniques_for(*far, "groove→groove")}
        by_near = {x["id"]: x for x in audition.techniques_for(*near, "groove→groove")}

        strict = [tid for tid, t in _tq.TECHNIQUES.items()
                  if t.bpm_delta_max is not None and t.bpm_delta_max <= 6]
        if not strict:
            raise RuntimeError("в каталоге не осталось приёмов с порогом по темпу")
        for tid in strict:
            if by_far.get(tid, {}).get("ok"):
                raise RuntimeError(
                    f"«{tid}» с порогом {_tq.TECHNIQUES[tid].bpm_delta_max}% "
                    f"предлагают на разницу темпа 26%")
        # …и на близком темпе он же обязан вернуться: порог не должен
        # превращаться в вечный запрет.
        back = [tid for tid in strict
                if by_near.get(tid, {}).get("ok")
                or "темп" not in (by_near.get(tid, {}).get("problem") or "")]
        if not back:
            raise RuntimeError("порог по темпу отсекает приёмы и на сходящемся темпе")

        # Энергия: приём, который тянет вверх, на спаде должен весить
        # меньше, чем на подъёме.
        up = [tid for tid, t in _tq.TECHNIQUES.items() if t.energy_direction == "up"]
        if not up:
            raise RuntimeError("в каталоге не осталось приёмов с направлением энергии")
        tid = up[0]
        m_up, _, _ = audition.catalog_fit(tid, *near, "breakdown→drop")
        m_down, _, _ = audition.catalog_fit(tid, *near, "drop→breakdown")
        if not m_up > m_down:
            raise RuntimeError(
                f"«{tid}» ведёт энергию вверх, но на подъёме ({m_up:.2f}) "
                f"весит не больше, чем на спаде ({m_down:.2f})")

        # Оси едут наружу — по ним фильтруют в интерфейсе.
        for key in ("cut", "energy", "key_rule", "bpm_delta_max", "decks"):
            if key not in (by_near.get("DNB-25") or {}):
                raise RuntimeError(f"ось «{key}» не отдаётся наружу")

        # --- тональность и энергия: тоже ОТКАЗ, а не понижение веса ---
        #
        # Диджей: «если тональности сильно различаются, нужно, чтобы на
        # выбор давались только те техники, что не требуют одной
        # тональности, так же можно сказать и про BPM и про энергию».
        same = ({"bpm": 138.0, "camelot": "9B", "path": "a"},
                {"bpm": 138.0, "camelot": "9B", "path": "b"})
        far_key = ({"bpm": 138.0, "camelot": "9B", "path": "a"},
                   {"bpm": 138.0, "camelot": "2A", "path": "b"})

        need_key = [tid for tid, t in _tq.TECHNIQUES.items()
                    if t.key_rule == "compatible" and not t.requires_stems
                    and t.requires_decks <= 2]
        if not need_key:
            raise RuntimeError("в каталоге не осталось приёмов, которым нужна тональность")
        by_far_key = {x["id"]: x for x in audition.techniques_for(*far_key, "groove→groove")}
        by_same = {x["id"]: x for x in audition.techniques_for(*same, "groove→groove")}
        for tid in need_key:
            if by_far_key.get(tid, {}).get("ok"):
                raise RuntimeError(
                    f"«{tid}» держит тональности вместе, но его предлагают "
                    f"на расстоянии 6 по Камелоту")
            if not by_same.get(tid, {}).get("ok"):
                raise RuntimeError(
                    f"«{tid}» не предлагают даже на одной тональности — "
                    f"отказ превратился в вечный запрет")

        # Энергия: приём, который тянет вверх, на спаде не предлагается.
        up2 = [tid for tid, t in _tq.TECHNIQUES.items()
               if t.energy_direction == "up" and not t.requires_stems
               and t.requires_decks <= 2]
        if not up2:
            raise RuntimeError("в каталоге не осталось приёмов, ведущих энергию вверх")
        by_down = {x["id"]: x for x in audition.techniques_for(*same, "drop→breakdown")}
        by_up = {x["id"]: x for x in audition.techniques_for(*same, "breakdown→drop")}
        for tid in up2:
            if by_down.get(tid, {}).get("ok"):
                raise RuntimeError(f"«{tid}» ведёт энергию вверх, но его предлагают на спаде")
            if not by_up.get(tid, {}).get("ok"):
                raise RuntimeError(f"«{tid}» не предлагают даже на подъёме")

        # Названия — на одном языке. Было вперемешку: DNB-* по-английски,
        # ST/SF/TT/FX по-русски.
        import fx_presets as _fx

        def _cyr(text):
            return any("а" <= c <= "я" or "А" <= c <= "Я" for c in str(text))

        mixed = [tid for tid, t in _tq.TECHNIQUES.items() if _cyr(t.name)]
        if mixed:
            raise RuntimeError(f"названия приёмов снова вперемешку: {mixed[:5]}")
        for side in ("out", "in"):
            bad_names = [p["name"] for p in _fx.presets(side, 64.0) if _cyr(p["name"])]
            if bad_names:
                raise RuntimeError(f"названия эффектов снова вперемешку: {bad_names[:5]}")

    def _bars_are_the_bars_promised() -> None:
        # На карточке написано «32 такта» — столько и должно прозвучать.
        #
        # Масштаб приёма округлялся до степени двойки: рендер брал первую
        # ступень лесенки (×4, ×2, ×1, ×½, ×¼), которая «влезает» в окно.
        # Влезает — не значит совпадает. Замер на 138 BPM: из 180
        # сочетаний «приём × 16/32/64 такта» в заказанную длину попадали
        # 10, медианный промах 44%. Переход начинался и кончался посреди
        # фразы, в каждой технике. Диджей:
        #
        #     не сходятся такты и доли теперь, ни в одной технике
        #
        # Проверка идёт по каталогу и БЕЗ звука: масштаб — чистая
        # арифметика над событиями, слушать для этого нечего.
        import audition as _aud
        import demo_render as _dr
        import techniques as _tq

        bpm = 138.0
        spb = 60.0 / bpm
        bar = spb * 4.0
        miss = []
        capped = 0
        for tid in sorted(_tq.TECHNIQUES):
            if tid == "CUSTOM":
                continue
            try:
                plan = _tq.build_plan(tid, "p", "source", "target", bpm, {})
            except ValueError:
                continue
            ev = plan["events"] if isinstance(plan, dict) else plan
            raw = max([4.0 * spb] + [_dr._event_window(e, spb)[1] for e in ev])
            for bars in (16, 32, 64):
                budget = max(2.0 * bar, bars * bar)
                # Масштаб спрашиваем У РЕНДЕРА, а не считаем тут заново:
                # своя арифметика молча согласилась бы с любой его
                # правкой, и проверка стала бы декорацией.
                scale = _dr.technique_scale(raw, budget, True)
                got = raw * scale / bar
                if abs(got - bars) / bars <= 0.02:
                    continue
                # Упёрлись в потолок растяжения — это честный случай, но
                # тогда кнопка «такты» не имеет права такую длину
                # предлагать.
                if scale >= _dr.technique_scale(raw, 1e9, True) - 1e-9:
                    capped += 1
                    opts = _aud.bars_options({"bpm": bpm},
                                             {"bpm": bpm, "genre": "trance"},
                                             None, tid)
                    if bars in opts:
                        raise RuntimeError(
                            f"«{tid}» не может заполнить {bars} тактов "
                            f"(растянется до {got:.0f}), а кнопка их предлагает")
                    continue
                miss.append(f"{tid}: заказано {bars}, прозвучит {got:.0f}")
        if miss:
            raise RuntimeError(
                f"длина не совпадает у {len(miss)} сочетаний: " + ", ".join(miss[:5]))
        if not capped:
            raise RuntimeError(
                "ни один приём не упёрся в потолок растяжения — проверка "
                "перестала трогать короткие резы, и её вторая половина мертва")

    def _gaps_have_a_way_out() -> None:
        # Подбор научился ОТКАЗЫВАТЬ по темпу и тональности — значит он
        # обязан предложить, чем разрыв закрыть. Иначе диджей остаётся с
        # отказом и без выхода, а это хуже, чем плохой вариант.
        #
        # Каталог был разложен по ЖАНРУ (DNB/ST/SF/TT/FX), и жанр не
        # отвечает на вопрос у пульта: «два баса спорят — чем лечить?».
        # Отсюда 107 пар приёмов со схожестью действий ≥0.7 при 60
        # приёмах: одно решение переизобретено в каждом семействе.
        # Ось `solves` — ответ на этот вопрос, и проверка идёт по ней.
        import audition
        import techniques as _tq

        far_bpm = ({"bpm": 174.0, "camelot": "9B", "path": "a"},
                   {"bpm": 128.0, "camelot": "9B", "path": "b"})
        far_key = ({"bpm": 138.0, "camelot": "9B", "path": "a"},
                   {"bpm": 138.0, "camelot": "2A", "path": "b"})

        # 1. Приём, СДЕЛАННЫЙ ради разрыва, этим же разрывом не отсекается.
        for (a, b), need in ((far_bpm, "tempo_gap"), (far_key, "key_gap")):
            solvers = [tid for tid, t in _tq.TECHNIQUES.items()
                       if need in t.solves and not t.requires_stems
                       and t.requires_decks <= 2 and t.energy_direction == "any"]
            if not solvers:
                raise RuntimeError(f"в каталоге некому закрыть {need}")
            by = {x["id"]: x for x in audition.techniques_for(a, b, "breakdown→drop")}
            alive = [tid for tid in solvers if by.get(tid, {}).get("ok")]
            if not alive:
                raise RuntimeError(
                    f"{need}: все решатели отсеклись тем самым разрывом, "
                    f"ради которого сделаны — {solvers}")
            # 2. …и он же поднимается, а не лежит мёртвым грузом.
            best = max(by.values(), key=lambda x: float(x.get("fit") or 0.0))
            if best["id"] not in solvers:
                raise RuntimeError(
                    f"{need}: первым идёт «{best['id']}» (fit {best['fit']}), "
                    f"а не решатель разрыва")

        # 3. Измеренный конфликт слоёв поднимает того, кто его лечит.
        clash = {"bass": 0.9, "harm": 0.1, "drum_fit": 1.0, "vocal": 0.0}
        fixers = [tid for tid, t in _tq.TECHNIQUES.items() if "bass_clash" in t.solves]
        if not fixers:
            raise RuntimeError("в каталоге некому развести два баса")
        for tid in fixers[:3]:
            if not audition._solve_bias(tid, clash) > 1.0:
                raise RuntimeError(f"«{tid}» лечит спор басов, но замер его не поднимает")
        plain = [tid for tid, t in _tq.TECHNIQUES.items() if not t.solves]
        if plain and audition._solve_bias(plain[0], clash) != 1.0:
            raise RuntimeError("приём, который ничего не решает, поднимают за чужую заслугу")
        if audition._solve_bias(fixers[0], None) != 1.0:
            raise RuntimeError("без замера слоёв приём всё равно поднимают — это угадывание")

        # 4. Ярусы и органы. Органы ВЫВОДЯТСЯ из событий: рукописный
        # список разошёлся бы с каталогом в первый же день.
        tiers = {t.tier for t in _tq.TECHNIQUES.values()}
        if not {"core", "solver", "trick"} <= tiers:
            raise RuntimeError(f"ярусы каталога потерялись: {tiers}")
        if "transport" not in _tq.organs_of("TT-09"):
            raise RuntimeError("у «Loop To Backspin» пропал транспорт — органы врут")
        if "stems" not in _tq.organs_of("ST-10"):
            raise RuntimeError("у «Stem Crossfade» пропали слои — органы врут")

        # 5. Жанр называет свои стыки ВСЕГДА. Транс сводят в брейкдаунах.
        import style as _st

        pref = _st.preferred_junctions({"genre": "trance"})
        if not pref or not any("breakdown" in p for p in pref):
            raise RuntimeError(f"транс больше не сводят в брейкдаунах: {pref}")
        if _st.genre_junction_bias({"genre": "trance"}, "breakdown→breakdown")[0] <= \
           _st.genre_junction_bias({"genre": "trance"}, "groove→groove")[0]:
            raise RuntimeError("трансу groove→groove нравится не меньше брейкдауна")
        if not _st.preferred_junctions({"genre": "___нет такого___"}):
            raise RuntimeError("для неизвестного жанра карточка молчит вместо общего случая")

        # 6. Half-time — про ПЛОТНОСТЬ атак, а не про уровень.
        import cue_points as _cp

        if _cp._half_time_bars({"hits": ""}):
            raise RuntimeError("half-time находят там, где плотность не мерили")
        dense_then_thin = {"hits": "4" * 8 + "2" * 10 + "4" * 8}
        found = _cp._half_time_bars(dense_then_thin)
        if not (8 in found and 0 not in found):
            raise RuntimeError(f"half-time размечен неверно: {sorted(found)[:6]}")
        # Одна редкая фраза — это сбивка, а не секция.
        if _cp._half_time_bars({"hits": "4" * 8 + "22" + "4" * 8}):
            raise RuntimeError("сбивку в два такта считают half-time секцией")

    def _pool_and_fit_order() -> None:
        # Пул ТРЕКОВ и раскладка ПО СХОДИМОСТИ.
        #
        # Диджей: «как выделенные файлы добавить в clipping и в ControlC
        # через какой-нибудь пул треков, но не тот, что пул переходов» и
        # «план микса будет составляться не по какой-то дуге, а по
        # сходимости в первую очередь, чтобы максимальное количество
        # треков сошлись».
        import mix_strategist as _ms
        import track_pool as _tp

        room = "__selftest_pool__"
        _tp.clear(room)
        if _tp.paths(room):
            raise RuntimeError("пул не очищается")
        if _tp.add(room, ["a.mp3", "b.mp3", "c.mp3"]) != 3:
            raise RuntimeError("пул не принимает треки")
        # Повторное добавление не переставляет уже лежащее в конец:
        # диджей мог разложить порядок руками.
        if _tp.add(room, ["b.mp3", "d.mp3"]) != 1:
            raise RuntimeError("повторное добавление считается новым")
        if _tp.paths(room) != ["a.mp3", "b.mp3", "c.mp3", "d.mp3"]:
            raise RuntimeError(f"порядок пула сбился: {_tp.paths(room)}")
        _tp.reorder(room, ["d.mp3", "a.mp3"])
        if _tp.paths(room)[:2] != ["d.mp3", "a.mp3"]:
            raise RuntimeError("перестановка не сохраняется")
        if len(_tp.paths(room)) != 4:
            raise RuntimeError("перестановка потеряла треки, которых не было в списке")
        _tp.remove(room, ["c.mp3"])
        if "c.mp3" in _tp.paths(room):
            raise RuntimeError("удаление из пула не работает")
        _tp.clear(room)

        # --- раскладка по сходимости ---
        #
        # Набор построен так, что жадный отбор обязан споткнуться: два
        # плотных куста по темпу и один трек-мост между ними. Если
        # алгоритм ходит по графу, мост он пройдёт один раз; если гребёт
        # по весу — будет прыгать туда-сюда.
        def _t(name, bpm, cam, energy):
            return {"name": name, "path": name, "bpm": bpm, "camelot": cam,
                    "energy": energy, "duration_seconds": 300.0}

        # Энергия РАЗНАЯ — иначе дуге нечего вести, она совпадает со
        # сходимостью, и сравнивать нечего. На это проверка и поймала
        # первую версию фикстуры.
        pack = ([_t(f"a{i}", 174.0, "8A", 0.2 + i * 0.13) for i in range(6)]
                + [_t(f"b{i}", 140.0, "8A", 0.9 - i * 0.13) for i in range(6)]
                + [_t("bridge", 174.0, "9A", 0.5)])
        fit = _ms.order_tracks(pack, "fit", _ms.library_energy_levels(pack))
        arc = _ms.order_tracks(pack, "rising", _ms.library_energy_levels(pack))
        if len(fit) != len(pack) or {t["name"] for t in fit} != {t["name"] for t in pack}:
            raise RuntimeError("раскладка по сходимости теряет или дублирует треки")
        g_fit, total = _ms.joins_ok(fit)
        g_arc, _ = _ms.joins_ok(arc)
        if g_fit < g_arc:
            raise RuntimeError(
                f"«по сходимости» сошлось хуже дуги: {g_fit} против {g_arc} из {total}")
        # Потолок считается по компонентам связности: между двумя
        # кустами переход обязан стоить один несошедшийся стык.
        adj = _ms._compat_graph(fit)
        seen, comps = set(), 0
        for i in range(len(fit)):
            if i in seen:
                continue
            comps += 1
            stack = [i]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(adj[x] - seen)
        ceiling = total - (comps - 1)
        if g_fit < ceiling:
            raise RuntimeError(
                f"сошлось {g_fit} при потолке {ceiling} — алгоритм упирается в тупики")

        # Дуге сходимость не навязываем: она выбрана ради энергии.
        if _ms.order_tracks(pack, "rising", None) == _ms.order_tracks(pack, "fit", None):
            raise RuntimeError("дуга и сходимость дают одно и то же — один из режимов мёртв")

    def _no_undefined_names() -> None:
        # Необъявленные имена во ВСЕХ модулях программы.
        #
        # Поймано на живом: эндпоинт пула треков звал `_room(session_id)`
        # — функции с таким именем в server.py нет вовсе. Python
        # разрешает имена в момент ВЫЗОВА, поэтому ни импорт, ни запуск
        # backend'а этого не замечают: падает только сам запрос, 500 на
        # каждое обращение. Снаружи это выглядело как «пул треков не
        # появляется», и час ушёл бы на поиск в интерфейсе.
        #
        # Проверка синтаксисом такое не ловит по определению — нужен
        # разбор области видимости. Берём pyflakes: он есть в сборочном
        # окружении, а без него проверка честно предупреждает, а не
        # делает вид, что всё хорошо.
        import pathlib

        try:
            from pyflakes.api import check
            from pyflakes.reporter import Reporter
        except ImportError:
            raise Warning("pyflakes не установлен — необъявленные имена не проверены")

        import io as _io

        here = pathlib.Path(__file__).resolve().parent
        out, err = _io.StringIO(), _io.StringIO()
        rep = Reporter(out, err)
        bad: list[str] = []
        for path in sorted(here.glob("*.py")):
            out.truncate(0)
            out.seek(0)
            try:
                text = path.read_text(encoding="utf-8")
                check(text, path.name, rep)
            except Exception:                        # noqa: BLE001
                continue
            src_lines = text.splitlines()
            for line in out.getvalue().splitlines():
                if "undefined name" not in line:
                    continue
                # Строковая аннотация на лениво импортируемый модуль —
                # законная запись, и она помечена noqa прямо в коде.
                # Уважаем пометку, иначе проверку заглушат целиком, и
                # она перестанет ловить настоящие опечатки.
                try:
                    num = int(line.split(":")[1])
                    if "noqa: F821" in src_lines[num - 1]:
                        continue
                except (IndexError, ValueError):
                    pass
                bad.append(line.strip())
        if bad:
            raise RuntimeError(
                f"необъявленные имена ({len(bad)}): " + "; ".join(bad[:5]))

    def _ui_touches_only_real_elements() -> None:
        # В интерфейсе нет обращений к элементам, которых на странице нет.
        #
        # Поймано при переделке шапки ControlC: убрал из разметки четыре
        # кнопки, а `document.getElementById("mixxx-play-btn").add…`
        # остался. Такой вызов бросает TypeError и роняет ВЕСЬ блок
        # скрипта ниже по файлу — то есть половину интерфейса, а не одну
        # кнопку. Снаружи это выглядит как «вкладка перестала работать»,
        # и связь с удалённой кнопкой не видна ниоткуда.
        #
        # Ищем только НЕЗАЩИЩЁННЫЕ обращения — `getElementById("x").`
        # сразу с точкой. Форма `const el = getElementById("x"); if (el)`
        # законна: элемент может отсутствовать намеренно.
        import pathlib
        import re as _re

        page = (pathlib.Path(__file__).resolve().parent / "static" / "chat.html")
        if not page.is_file():
            raise Warning("static/chat.html не найден — интерфейс не проверен")
        text = page.read_text(encoding="utf-8")
        ids = set(_re.findall(r'\bid="([^"]+)"', text))
        js = "\n".join(_re.findall(r"<script[^>]*>(.*?)</script>", text, _re.S))
        used = _re.findall(r'getElementById\("([^"]+)"\)\s*\.', js)
        missing = sorted({u for u in used if u not in ids})
        if missing:
            raise RuntimeError(
                f"интерфейс дёргает несуществующие элементы ({len(missing)}): "
                + ", ".join(missing[:6]))


    def _genre_picks_the_technique() -> None:
        # Жанр обязан влиять на выбор приёма В CLIPPING, а не только при
        # сборке сета.
        #
        # `style.bias()` (основной ход стиля ×1.35) звался ТОЛЬКО из
        # mix_strategist. Подбор в Clipping про жанр не знал вовсе, и на
        # трансовой паре со стемами выдавал ST-12, ST-07, ST-04, ST-08 —
        # четыре реза и дабл-дропа подряд. Диджей: «всегда рекомендуется
        # ST-08 вне зависимости от жанра» и «входящий трек слишком резко
        # входит»: рез входит сразу и целиком, по построению.
        from unittest.mock import patch

        import audition
        import mix_profile as _mp
        import style as _st

        def pair(genre):
            t = {"bpm": 138.0, "camelot": "9B", "genre": genre, "name": "x"}
            return dict(t, path="a"), dict(t, path="b")

        # Стемы есть — иначе половина каталога отсеется и проверка будет
        # мерить не то. Именно со стемами диджей это и наблюдал.
        with patch.object(_st, "has_stems", lambda t: True), \
             patch.object(_st, "has_vocals", lambda t: True):
            # 1. Жанр вообще участвует. Самая прямая формулировка: убери
            # множитель стиля — и порядок обязан измениться. Требовать
            # «основной ход первым на каждом стыке» было бы моим мнением,
            # а не требованием: на groove→groove у техно законно выигрывает
            # SE-01 из таблицы стыков, и проверка на этом попалась.
            def top_of(genre, label):
                a, b = pair(genre)
                got = audition.techniques_ranked(
                    a, b, {"pair_label": label, "key_distance": 0})
                if not got:
                    raise RuntimeError(f"{genre}/{label}: подбор не дал ничего")
                return got[0]

            probes = [(g, lab) for g in ("trance", "techno", "neurofunk")
                      for lab in ("drop→drop", "breakdown→drop", "groove→groove")]
            with_genre = {p: top_of(*p) for p in probes}
            with patch.object(_st, "bias", lambda tid, a, b, need="hold": (1.0, "")):
                without = {p: top_of(*p) for p in probes}
            if with_genre == without:
                raise RuntimeError(
                    "порядок приёмов не меняется от стиля — style.bias снова "
                    "не участвует в подборе Clipping")

            # 2. Разные жанры дают разное. Один ответ на всё — это ровно
            # то, что диджей и видел: «всегда ST-08 вне зависимости от
            # жанра».
            firsts = {with_genre[(g, "drop→drop")] for g in
                      ("trance", "techno", "neurofunk")}
            if len(firsts) < 2:
                raise RuntimeError(f"все жанры дают один приём: {firsts}")

            # 3. Главное, ради чего это затевалось: на трансе входящий не
            # вступает резом. Рез — это `entry: technique`, то есть
            # «сразу и целиком, без подъёма фейдера».
            a, b = pair("trance")
            bars = _st.transition_bars(a, b, None, 138.0)
            for label in ("drop→drop", "breakdown→drop", "groove→groove"):
                tid = with_genre[("trance", label)]
                prof = _mp.auto(bars, label, None, tid)
                if str(prof.get("entry")) == "technique":
                    raise RuntimeError(
                        f"транс/{label}: «{tid}» вводит трек резом — "
                        f"входящий вступает сразу и целиком")
                if float(prof.get("in_len") or 0.0) < 0.4:
                    raise RuntimeError(
                        f"транс/{label}: фейдер входящего поднимается за "
                        f"{prof['in_len'] * 100:.0f}% — для транса это рывок")

        # Длина сведения в трансе — 64 такта, а не 32: брейкдауны там
        # идут по 64-96, и переход в 32 кончается раньше, чем брейкдаун
        # успел развернуться.
        if _st.transition_bars({}, {"genre": "trance"}, None, 138.0) < 64:
            raise RuntimeError("транс снова сводится короткими переходами")
        if 96 not in _st.phrase_ladder({"genre": "trance"}):
            raise RuntimeError("из трансовой лесенки пропали 96 тактов")

        # И каталог обязан отдаваться в JSON: `solves` — множество, а
        # JSON множеств не знает. Из-за этого /api/techniques падал с 500
        # на каждый запрос, и Technician выглядел пустым.
        import json as _json
        import techniques as _tq

        _json.dumps([t.to_dict() for t in _tq.TECHNIQUES.values()])

    def _local_ui_allowed() -> None:
        # Регрессия 1.0.3: ALLOWED_ORIGINS — это список для САЙТА, а
        # интерфейс программа отдаёт сама с 127.0.0.1. Проверка origin у
        # WebSocket не знала про собственный адрес, и /ws/chat отвечал
        # 403: телеметрия и чат молча не подключались, а по HTTP всё
        # работало — там origin никто не смотрит. Наружу это выглядело
        # как «кнопки не нажимаются».
        import server

        for origin in ("http://127.0.0.1:8765", "http://localhost:8765"):
            if not server._is_local_origin(origin):
                raise RuntimeError(f"собственный интерфейс с {origin} не пускают в WebSocket")
        for origin in ("http://evil.example", "https://darave.ru.evil.example"):
            if server._is_local_origin(origin):
                raise RuntimeError(f"чужой адрес {origin} считается своим")

    def _media_needs_no_header() -> None:
        # Вторая половина той же регрессии: <audio src> и ссылку
        # «скачать» грузит браузер, заголовок Authorization он не ставит,
        # и файл приходил с 401. Звука не было, а подпись говорила
        # «играет». Значит middleware обязан принимать токен параметром —
        # но только на GET.
        #
        # Проверяем ПОВЕДЕНИЕ, а не текст исходника: inspect.getsource
        # внутри собранного exe не работает — там байт-код, и первая
        # версия этой проверки честно валила сборку сама на себе.
        import asyncio

        import cloud_auth
        import server
        from starlette.requests import Request

        GOOD = "x" * 64

        def fake_request(method: str, query: str) -> Request:
            return Request({
                "type": "http", "http_version": "1.1", "method": method,
                "scheme": "http", "path": "/api/rooms/1/techniques/demo/x.mp3",
                "raw_path": b"/api/rooms/1/techniques/demo/x.mp3",
                "query_string": query.encode(), "headers": [],
                "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 8765),
                "root_path": "",
            })

        async def passed(_request):
            return "ПРОПУЩЕН"

        async def call(method: str, query: str):
            return await server.require_account(fake_request(method, query), passed)

        real = cloud_auth.token_ok
        no_auth = os.environ.pop("DARAVE_NO_AUTH", None)
        try:
            cloud_auth.token_ok = lambda t: t == GOOD

            if asyncio.run(call("GET", f"token={GOOD}")) != "ПРОПУЩЕН":
                raise RuntimeError("GET с токеном в адресе не пускают — файлы будут отдавать 401")

            bad = asyncio.run(call("GET", "token=nope"))
            if bad == "ПРОПУЩЕН" or getattr(bad, "status_code", None) != 401:
                raise RuntimeError("GET с ЧУЖИМ токеном в адресе пускают")

            post = asyncio.run(call("POST", f"token={GOOD}"))
            if post == "ПРОПУЩЕН" or getattr(post, "status_code", None) != 401:
                raise RuntimeError("токен в адресе принимают не только на GET — так менять данные нельзя")
        finally:
            cloud_auth.token_ok = real
            if no_auth is not None:
                os.environ["DARAVE_NO_AUTH"] = no_auth

    run("librosa считает", _librosa)
    run("scipy фильтрует", _scipy)
    run("soundfile пишет", _soundfile)
    run("страница чата на месте", _static)
    run("ffmpeg доступен", _ffmpeg)
    run("дочерние модули импортируются", _children)
    run("google-genai на месте (нужен только для чата)", _genai, optional=True)
    run("старт без ключа LLM не падает", _llm_starts_without_key)
    run("бэкенд собирается целиком", _backend_builds)
    run("свой интерфейс пускают в WebSocket", _local_ui_allowed)
    run("эффекты по сторонам рендер понимает", _fx_units_exist)
    run("признаки слоёв меняют порядок приёмов", _layer_bias_works)
    run("скрипт установки слоёв уехал в сборку", _stems_setup_shipped)
    run("форма сведения считается по шву", _profile_follows_seam)
    run("разделение получает список треков, а не папку", _stems_take_a_list)
    run("прогресс печатается при любой кодировке", _progress_survives_encoding)
    run("пишем в папку данных, а не в Program Files", _writes_go_to_data_dir)
    run("код шва отражает все параметры", _seam_code_covers_everything)
    run("счётчики анализа доходят до нуля", _counters_can_reach_zero)
    run("рез и бэкспин вводят трек целиком", _cuts_enter_at_full)
    run("входящий приходит со своим низом", _incoming_gets_its_bass)
    run("каталог влияет на подбор приёма", _catalog_rules_reach_clipping)
    run("такты звучат ровно столько, сколько обещано", _bars_are_the_bars_promised)
    run("у разрыва темпа и тональности есть выход", _gaps_have_a_way_out)
    run("пул треков и раскладка по сходимости", _pool_and_fit_order)
    run("в коде нет необъявленных имён", _no_undefined_names)
    run("интерфейс не дёргает пустоту", _ui_touches_only_real_elements)
    run("жанр выбирает приём", _genre_picks_the_technique)
    run("файлы отдаются браузеру без заголовка", _media_needs_no_header)

    bad = [c for c in checks if c[1].startswith("ОШИБКА")]
    warn = [c for c in checks if c[1].startswith("ПРЕДУПРЕЖДЕНИЕ")]
    print()
    for name, res in checks:
        print(f"  {res:<12} {name}" if res == "OK" else f"  {res}  ({name})")
    print()
    if bad:
        print(f"ПРОВЕРКА НЕ ПРОШЛА: {len(bad)} из {len(checks)}")
        return 1
    if warn:
        print(f"Проверка пройдена с оговорками: {len(warn)} предупреждение(й) из {len(checks)}")
        return 0
    print(f"Проверка пройдена: {len(checks)} из {len(checks)}")
    return 0


def port_owner(port: int) -> str:
    """Кто держит порт. Пустая строка, если выяснить не вышло.

    Без этого «порт занят» звучит как поломка программы, хотя чаще всего
    это её же вторая копия — или backend, поднятый из исходников."""
    if os.name != "nt":
        return ""
    import subprocess

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    pid = ""
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line.upper():
            pid = line.split()[-1]
            break
    if not pid:
        return ""
    try:
        t = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        name = t.split(",")[0].strip('"') if t else ""
    except (OSError, subprocess.SubprocessError):
        name = ""
    return f"{name} (PID {pid})" if name else f"PID {pid}"


def doctor(port: int) -> int:
    """DARAVE.exe --doctor: почему не запускается.

    Отдельный режим, потому что «окно мигнуло и закрылось» — это не одна
    поломка, а пять разных, и отличаются они ровно тем, что печатается
    ниже."""
    import shutil

    print(f"\n{APP_NAME} — проверка запуска\n")
    print(f"  exe            {sys.executable}")
    print(f"  папка данных   {data_dir_safe()}")
    print(f"  журнал запуска {_launch_log_path or '—'}")
    print(f"  ffmpeg         {shutil.which('ffmpeg') or 'НЕ НАЙДЕН'}")
    try:
        import paths

        page = paths.resource_dir() / "static" / "chat.html"
        print(f"  страница чата  {'есть' if page.is_file() else 'НЕТ: ' + str(page)}")
    except Exception as exc:
        print(f"  страница чата  не смог проверить: {exc}")

    free = port_free(port)
    print(f"  порт {port}      {'свободен' if free else 'ЗАНЯТ'}")
    if not free:
        who = port_owner(port)
        print(f"                 держит: {who or 'выяснить не вышло'}")
        print("                 это и есть причина: программа видит занятый порт,")
        print("                 считает, что уже запущена, и сразу выходит.")
        print("                 Закройте ту копию (или DARAVE, поднятый из исходников).")

    try:
        import llm_providers

        st = llm_providers.provider_status()
        print(f"  ассистент      {'подключён: ' + str(st.get('provider')) if st.get('ok') else 'выключен (программе не мешает)'}")
    except Exception as exc:
        print(f"  ассистент      не смог проверить: {exc}")

    # Собрать приложение — единственный способ поймать нехватку пакета,
    # которую FastAPI проверяет в декораторе маршрута. Без этой строки
    # --doctor рапортовал «всё в порядке» ровно в тот раз, когда
    # программа не запускалась.
    try:
        import server

        print(f"  бэкенд         собирается, маршрутов: {len(server.app.routes)}")
    except Exception as exc:
        print(f"  бэкенд         НЕ СОБИРАЕТСЯ: {exc.__class__.__name__}: {exc}")
        print(f"                 это и есть причина. Подробности в {_launch_log_path or 'журнале запуска'}.")
    print()
    return 0


def data_dir_safe() -> str:
    try:
        import paths

        return str(paths.data_dir())
    except Exception:
        return "—"


def serve(argv: list[str]) -> int:
    port = DEFAULT_PORT
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass

    if "--doctor" in argv:
        return doctor(port)

    # Вторая копия программы не должна поднимать второй сервер и падать на
    # занятом порту: почти всегда это значит «DARAVE уже запущен» — просто
    # открываем окно. Сообщение подробное намеренно: раньше здесь была одна
    # строка в спрятанное окно, и снаружи это выглядело как «программа
    # мигнула и закрылась, ничего не открыв».
    if not port_free(port):
        url = f"http://127.0.0.1:{port}/"
        who = port_owner(port)
        print(f"Порт {port} уже занят: {who or 'неизвестно кем'}.")
        print(f"Считаю, что {APP_NAME} уже запущен, и просто открываю {url}")
        print("Если окно так и не открылось — закройте ту копию и запустите снова.")
        webbrowser.open(url)
        # Даём браузеру стартовать: процесс завершится сразу после return,
        # а os.startfile асинхронный.
        time.sleep(2.0)
        return 0

    print(f"{APP_NAME}: поднимаю сервер на 127.0.0.1:{port}")
    import uvicorn

    from server import app  # noqa: F401  — импорт поднимает провайдера LLM

    print(f"{APP_NAME}: бэкенд собран, открываю браузер")
    if "--no-browser" not in argv:
        open_browser_later(f"http://127.0.0.1:{port}/")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info",
                access_log=False)
    return 0


def main() -> int:
    argv = sys.argv[1:]

    # --- режим «за скрипт»: DARAVE.exe --child stems --dir ... ---
    if argv and argv[0] == "--child":
        prepare_env()
        from child import run_child

        return run_child(argv[1:])

    prepare_env()
    start_launch_log()

    if "--selftest" in argv:
        return smoke_test()

    if "--console" not in argv and "--doctor" not in argv:
        hide_console()

    return serve(argv)


def _fatal(exc: BaseException) -> int:
    """Показать и СОХРАНИТЬ падение, а не закрыть окно молча.

    Тот самый случай: «открывается и сразу закрывается терминал, логи
    хз». Traceback печатался в окно, которое закрывалось вместе с
    процессом."""
    import traceback

    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        print("\n" + "=" * 70)
        print(f"{APP_NAME} не смог запуститься:\n")
        print(text)
        if _launch_log_path:
            print(f"Это же записано в {_launch_log_path}")
        print("=" * 70)
    except Exception:
        pass
    # Если окно наше — держим его открытым, иначе прочитать ничего нельзя.
    if own_console() and getattr(sys, "frozen", False):
        try:
            input("\nНажмите Enter, чтобы закрыть…")
        except Exception:
            time.sleep(30)
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit:
        raise
    except BaseException as _exc:      # noqa: BLE001 — сюда не должно долетать ничего
        rc = _fatal(_exc)
    sys.exit(rc)
