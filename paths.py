"""Где лежат ресурсы программы и где — данные пользователя.

Из исходников это одна и та же папка, и вопроса не возникает. У
установленной версии они расходятся, и перепутать их дорого:

  ресурсы (например, static/chat.html) лежат рядом с exe, в
  Program Files, и доступны только на чтение;

  данные (darave.db, логи, записи, наборы, демо) должны писаться в
  %LOCALAPPDATA%\\DARAVE — в Program Files обычный пользователь не
  запишет ни байта, и попытка кончается не понятной ошибкой, а пустым
  500 в браузере.

Переменная DARAVE_DATA_DIR перекрывает выбор — ей пользуется и точка
входа, и тот, кто хочет держать данные на другом диске.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DARAVE"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Папка с ресурсами, которые кладёт сборка (только чтение)."""
    if frozen():
        # onedir: PyInstaller распаковывает данные в _internal и указывает
        # на неё через _MEIPASS. onefile — во временную папку, туда же.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Папка, куда можно писать. Создаётся при первом обращении."""
    override = os.environ.get("DARAVE_DATA_DIR", "").strip()
    if override:
        d = Path(override)
    elif frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = Path(base) / APP_NAME
    else:
        d = Path(__file__).resolve().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_subdir(name: str) -> Path:
    d = data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d
