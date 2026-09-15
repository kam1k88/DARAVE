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

    run("librosa считает", _librosa)
    run("scipy фильтрует", _scipy)
    run("soundfile пишет", _soundfile)
    run("страница чата на месте", _static)
    run("ffmpeg доступен", _ffmpeg)
    run("дочерние модули импортируются", _children)
    run("google-genai на месте (нужен только для чата)", _genai, optional=True)
    run("старт без ключа LLM не падает", _llm_starts_without_key)
    run("бэкенд собирается целиком", _backend_builds)

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
