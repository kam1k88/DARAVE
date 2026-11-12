"""
persistence.py — лёгкая персистентность backend'а поверх SQLite (один файл,
без внешних сервисов — сознательный выбор для MVP, см. README "Что дальше").

Хранит по каждой комнате (session_id):
   - последний ПОСТРОЕННЫЙ ПЛАН СЕТА (mix_strategist.plan_strategy). Раньше
    он жил только в памяти комнаты как "дёшево пересчитать". Дёшево — по
    процессору, но не по работе диджея: план несёт ПРАВКИ (техника, точки
    ухода и входа, ползунки приёмов у конкретных переходов), а их
    пересчёт не вернёт. Любой перезапуск backend'а — и час работы
    выброшен. Теперь переживает и рестарт, и перезагрузку страницы.
  - загруженную библиотеку треков (track_analysis.py --upload) — ВАЖНО:
    комната (SessionRoom) удаляется из памяти, когда из неё уходят все
    (см. session.py::drop_if_empty), а следующее подключение с тем же
    кодом создаёт её заново с нуля — без этой персистентности библиотека
    "терялась" бы при каждом кратком обрыве связи в браузере, что и
    обнаружилось при UI-тестировании (см. README).

Данные хранятся локально в SQLite и переживают перезапуск приложения.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DARAVE_DB_PATH", "darave.db")

_lock = threading.Lock()
_local = threading.local()


def _connect() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


@contextmanager
def _cursor():
    conn = _connect()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()


def init_db() -> None:
    with _cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS rooms (
                session_id TEXT PRIMARY KEY,
                library_json TEXT,
                strategy_json TEXT,
                updated_at REAL NOT NULL
            )"""
        )
        # На случай апгрейда существующей базы без library_json (созданной
        # до этого поля) — ALTER TABLE ADD COLUMN безопасно падает, если
        # колонка уже есть, поэтому оборачиваем в try.
        for column in ("library_json", "strategy_json"):
            try:
                cur.execute(f"ALTER TABLE rooms ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass


def list_rooms(limit: int = 30) -> list[dict]:
    """Комнаты, о которых мы что-то помним, свежие сверху.

    Зачем это вообще нужно. Код комнаты — случайные 16 символов, он
    нигде не записан и держится только в адресной строке. Но за ним
    стоит ВСЯ просканированная библиотека: она лежит в rooms.library_json
    по этому коду. Человек, закрывший вкладку и открывший программу
    заново, получал новую пустую комнату — и единственным выходом было
    сканировать библиотеку с нуля. Час работы из-за потерянной строки в
    адресе.

    Поэтому список. Показываем то, по чему комнату можно узнать, не
    открывая: сколько в ней треков и когда её трогали в последний раз.
    Кода достаточно, истории и планов здесь не читаем — на списке из
    тридцати комнат это были бы тридцать разборов JSON ради двух чисел.
    """
    with _cursor() as cur:
        cur.execute(
            """SELECT session_id, updated_at,
                      LENGTH(COALESCE(library_json, '')) AS lib_len,
                      strategy_json IS NOT NULL AS has_strategy
                 FROM rooms
                ORDER BY updated_at DESC
                LIMIT ?""",
            (int(limit),),
        )
        rows = cur.fetchall()

    out = []
    for sid, updated, lib_len, has_strategy in rows:
        tracks = 0
        if lib_len:
            # Число треков — по самой библиотеке, а не по длине строки.
            # Разбор тут дешевле, чем кажется: комнат тридцать, а не
            # тридцать тысяч, и пустые (lib_len = 0) даже не читаются.
            try:
                tracks = len(load_library(sid))
            except (ValueError, TypeError):
                tracks = 0
        out.append({
            "room_id": sid,
            "tracks": tracks,
            "updated_at": float(updated or 0.0),
            "has_strategy": bool(has_strategy),
        })
    return out


def forget_room(session_id: str) -> None:
    """Убрать комнату и связанные с ней сохранённые данные."""
    with _cursor() as cur:
        cur.execute("DELETE FROM rooms WHERE session_id = ?", (session_id,))


def save_library(session_id: str, tracks: list[dict]) -> None:
    import time

    with _cursor() as cur:
        cur.execute(
            """INSERT INTO rooms (session_id, library_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET library_json = excluded.library_json,
                                                       updated_at = excluded.updated_at""",
            (session_id, json.dumps(tracks), time.time()),
        )


def load_library(session_id: str) -> list[dict]:
    with _cursor() as cur:
        cur.execute("SELECT library_json FROM rooms WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return []
        return json.loads(row[0])


def save_strategy(session_id: str, strategy: dict | None) -> None:
    """Последний построенный план сета.

    Хранится целиком, вместе с правками переходов: план — это не
    результат вычисления, а документ, над которым диджей работал."""
    import time

    with _cursor() as cur:
        cur.execute(
            """INSERT INTO rooms (session_id, strategy_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET strategy_json = excluded.strategy_json,
                                                       updated_at = excluded.updated_at""",
            (session_id, json.dumps(strategy) if strategy is not None else None, time.time()),
        )


def load_strategy(session_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT strategy_json FROM rooms WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None
