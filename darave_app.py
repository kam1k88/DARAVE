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


def hide_console() -> None:
    """Спрятать чёрное окно консоли собранного exe.

    Сборка идёт с console=True не ради окна, а ради stdout: без него
    дочерние процессы (скан, слои) не могут отдать прогресс — см.
    комментарий в DARAVE.spec. Само окно пользователю не нужно."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


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

    def _llm_starts_without_key() -> None:
        # Главная поломка первого запуска: провайдер создавался при
        # импорте server.py и бросал RuntimeError, если ключа нет. Первый
        # же запуск установленной версии заканчивался трейсбеком.
        import llm_providers

        prov = llm_providers.make_provider()
        if prov is None:
            raise RuntimeError("make_provider вернул None")

    run("librosa считает", _librosa)
    run("scipy фильтрует", _scipy)
    run("soundfile пишет", _soundfile)
    run("страница чата на месте", _static)
    run("ffmpeg доступен", _ffmpeg)
    run("дочерние модули импортируются", _children)
    run("google-genai на месте (нужен только для чата)", _genai, optional=True)
    run("старт без ключа LLM не падает", _llm_starts_without_key)

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


def main() -> int:
    argv = sys.argv[1:]

    # --- режим «за скрипт»: DARAVE.exe --child stems --dir ... ---
    if argv and argv[0] == "--child":
        prepare_env()
        from child import run_child

        return run_child(argv[1:])

    prepare_env()

    if "--selftest" in argv:
        return smoke_test()

    if "--console" not in argv:
        hide_console()

    port = DEFAULT_PORT
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass

    # Вторая копия программы не должна поднимать второй сервер и падать на
    # занятом порту: почти всегда это значит «DARAVE уже запущен» — просто
    # открываем окно.
    if not port_free(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"{APP_NAME} уже запущен — открываю {url}")
        webbrowser.open(url)
        return 0

    import uvicorn

    from server import app  # noqa: F401  — импорт поднимает провайдера LLM

    if "--no-browser" not in argv:
        open_browser_later(f"http://127.0.0.1:{port}/")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info",
                access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
