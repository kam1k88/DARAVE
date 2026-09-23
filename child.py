"""Запуск вспомогательных скриптов и из исходников, и из собранного .exe.

Бэкенд считает библиотеку и слои НЕ внутри себя, а отдельными процессами:
скан на 50 треков занимает минуты, и если крутить его в том же процессе,
браузер всё это время не получает ни прогресса, ни ответа. Из исходников
это тривиально — `[sys.executable, "track_analysis.py", ...]`.

В собранной сборке эта строка ломается молча и неприятно. У замороженного
PyInstaller'ом приложения `sys.executable` — это сам DARAVE.exe, а
track_analysis.py рядом с ним просто не лежит (он внутри архива). Запуск
формально проходит: exe стартует, видит незнакомый первый аргумент,
поднимает ВТОРОЙ веб-сервер, тот падает на занятом порту — а пользователь
видит «скан не дал треков».

Поэтому собранный exe умеет работать «за скрипт»: DARAVE.exe --child
track_analysis --scan ... Точка входа (darave_app.py) ловит --child и
передаёт управление main() нужного модуля. Здесь — только то, как эту
команду собрать, чтобы в двух режимах она была одна и та же.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Модули, которые бэкенд имеет право запускать как подпроцесс. Список
# закрытый: первый аргумент --child приходит из нашего же кода, но пусть
# опечатка падает сразу и понятно, а не поднимает что-то постороннее.
CHILD_MODULES = ("track_analysis", "stems", "fix_library_bpm", "autopilot_selftest")


def frozen() -> bool:
    """Мы внутри собранного PyInstaller'ом приложения?"""
    return bool(getattr(sys, "frozen", False))


def child_cmd(module: str, *args: str) -> list[str]:
    """Команда запуска вспомогательного модуля — в любом режиме.

    В исходниках: [python, /путь/module.py, *args]
    В сборке:     [DARAVE.exe, --child, module, *args]
    """
    if module not in CHILD_MODULES:
        raise ValueError(f"неизвестный дочерний модуль: {module}")
    if frozen():
        return [sys.executable, "--child", module, *args]
    script = Path(__file__).resolve().parent / f"{module}.py"
    return [sys.executable, str(script), *args]


def force_utf8_output() -> None:
    """Заставить stdout/stderr этого процесса говорить в UTF-8.

    Зачем, хотя родитель уже выставляет PYTHONUTF8 и PYTHONIOENCODING.
    Потому что переменные окружения читает ИНТЕРПРЕТАТОР при старте, а
    дочерний процесс здесь — это сам DARAVE.exe, собранный PyInstaller'ом.
    Его загрузчик поднимает CPython со своей конфигурацией, и полагаться
    на то, что он передаст туда переменные, нельзя.

    Что было без этого (журнал диджея, 1.1.3):

        File "stems.py", line 1885, in emit_progress
        File "encodings\cp1251.py", line 19, in encode
        UnicodeEncodeError: 'charmap' codec can't encode character
        '\xdc' in position 85: character maps to <undefined>

    В имени трека стояла «Ü», stdout был cp1251, кодировать её там нечем —
    и разделение на слои умирало НА ПЕРВОЙ ЖЕ строке прогресса, до
    единого обращения к видеокарте. Снаружи это выглядело как «шаг идёт,
    но карта не грузится».

    errors="replace" — второй пояс. UTF-8 кодирует что угодно, так что
    он не нужен; но фоновая задача на несколько часов не имеет права
    падать из-за символа в имени файла ни при каких обстоятельствах.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                            # noqa: BLE001
            pass                                     # питон старый или поток подменён


def run_child(argv: list[str]) -> int:
    """Отработать за вспомогательный скрипт. argv = ["track_analysis", ...].

    Вызывается из точки входа собранного приложения."""
    # ДО всего остального: дочерние модули печатают прогресс с именами
    # файлов, а имена бывают какими угодно.
    force_utf8_output()
    if not argv:
        print("--child: не указан модуль", file=sys.stderr)
        return 2
    module, rest = argv[0], argv[1:]
    if module not in CHILD_MODULES:
        print(f"--child: неизвестный модуль {module}", file=sys.stderr)
        return 2
    import importlib

    mod = importlib.import_module(module)
    main = getattr(mod, "main", None)
    if main is None:
        print(f"--child: у модуля {module} нет main()", file=sys.stderr)
        return 2
    # Скрипты разбирают argparse из sys.argv — подменяем его так, как будто
    # запустили именно их.
    sys.argv = [f"{module}.py", *rest]
    rc = main()
    return int(rc) if isinstance(rc, int) else 0
