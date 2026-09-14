"""
mixxx_process.py — закрыть и снова запустить Mixxx, ничего не потеряв.

## Зачем это понадобилось

Треки на деки кладёт очередь Auto DJ (см. `claude/deck-loading.md`:
контрола «загрузи файл по пути» в Mixxx нет). Очередь — это плейлист в
библиотеке Mixxx, а её он держит в памяти и пишет при выходе. Значит
переписать очередь можно только при закрытом Mixxx.

Руками это складывалось в замкнутый круг: закрыть Mixxx -> записать
очередь -> открыть Mixxx -> а план в вебе уже потерян, строить заново ->
снова не та очередь. Круг разрывается с двух сторон: план теперь
переживает перезапуск (persistence.save_strategy), а закрытие и запуск
Mixxx делает сам backend — он и так живёт на этой машине.

## Почему закрываем мягко и не форсим

`taskkill` без `/F` шлёт окну WM_CLOSE — то же самое, что крестик в
углу: Mixxx успевает дописать библиотеку и mixxx.cfg. `/F` убил бы
процесс на месте, и мы получили бы ровно ту потерю данных, от которой
защищаемся. Поэтому если Mixxx не закрылся сам за отведённое время, мы
честно сдаёмся и говорим об этом, а не добиваем.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

CLOSE_TIMEOUT_SECONDS = 30.0
START_TIMEOUT_SECONDS = 25.0
POLL_SECONDS = 0.5

# Куда Mixxx ставится по умолчанию. Нужно только как запасной вариант:
# обычно путь берётся у ЖИВОГО процесса, и это надёжнее любого списка —
# диджей собирает Mixxx из форка и держит его где угодно.
FALLBACK_PATHS = (
    r"C:\Mixxx\mixxx.exe",
    r"C:\Program Files\Mixxx\mixxx.exe",
    r"C:\Program Files (x86)\Mixxx\mixxx.exe",
)


def _run(args: list[str], timeout: float = 10.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout or ""
    except Exception:
        return ""


def is_running() -> bool:
    if os.name == "nt":
        return "mixxx.exe" in _run(
            ["tasklist", "/FI", "IMAGENAME eq mixxx.exe"]).lower()
    return subprocess.run(["pgrep", "-x", "mixxx"],
                          capture_output=True).returncode == 0


def executable_path() -> str | None:
    """Путь к mixxx.exe. Сначала спрашиваем у живого процесса."""
    if os.name != "nt":
        return None
    out = _run(["powershell", "-NoProfile", "-Command",
                "(Get-Process mixxx -ErrorAction SilentlyContinue "
                "| Select-Object -First 1 -ExpandProperty Path)"]).strip()
    if out and Path(out).exists():
        return out
    for candidate in FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def close(timeout: float = CLOSE_TIMEOUT_SECONDS, log=print) -> dict:
    """Закрыть Mixxx как крестиком в углу и дождаться, пока он выйдет."""
    if not is_running():
        return {"ok": True, "was_running": False}
    path = executable_path()
    if os.name == "nt":
        _run(["taskkill", "/IM", "mixxx.exe"])
    else:
        subprocess.run(["pkill", "-x", "mixxx"], capture_output=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            # Библиотека дописывается уже после закрытия окна — даём
            # файлу успокоиться, иначе наша запись очереди и его
            # финальный сброс наложатся друг на друга.
            time.sleep(1.2)
            log("Mixxx закрыт")
            return {"ok": True, "was_running": True, "path": path}
        time.sleep(POLL_SECONDS)
    return {"ok": False, "was_running": True, "path": path,
            "error": ("Mixxx не закрылся за отведённое время. Возможно, он ждёт "
                      "ответа в диалоге — закройте его сами и повторите. "
                      "Принудительно снимать процесс не буду: он не успеет "
                      "дописать библиотеку.")}


def start(path: str | None = None, log=print) -> dict:
    """Запустить Mixxx и дождаться появления процесса."""
    exe = path or executable_path()
    if not exe:
        return {"ok": False, "error": "не нашёл mixxx.exe — запустите Mixxx сами"}
    try:
        flags = 0
        if os.name == "nt":
            # DETACHED_PROCESS: Mixxx должен пережить перезапуск backend'а,
            # а не уйти вместе с ним.
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen([exe], cwd=str(Path(exe).parent),
                         creationflags=flags, close_fds=True)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"не смог запустить Mixxx: {exc}"}

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_running():
            log(f"Mixxx запущен: {exe}")
            return {"ok": True, "path": exe}
        time.sleep(POLL_SECONDS)
    return {"ok": False, "path": exe,
            "error": "Mixxx запущен, но процесс не появился в списке"}
