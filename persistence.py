"""
persistence.py — лёгкая персистентность backend'а поверх SQLite (один файл,
без внешних сервисов — сознательный выбор для MVP, см. README "Что дальше").

Хранит по каждой комнате (session_id):
  - историю диалога с DJAgent (роль + текст), чтобы при рестарте backend
    ассистент "помнил" контекст текущего сета
  - последний отправленный MixPlan (для кнопки "повторить микс")
  - загруженную библиотеку треков (track_analysis.py --upload) — ВАЖНО:
    комната (SessionRoom) удаляется из памяти, когда из неё уходят все
    (см. session.py::drop_if_empty), а следующее подключение с тем же
    кодом создаёт её заново с нуля — без этой персистентности библиотека
    "терялась" бы при каждом кратком обрыве связи в браузере, что и
    обнаружилось при UI-тестировании (см. README).

Это НЕ хранилище телеметрии в реальном времени — та живёт только в памяти
процесса (SessionRoom), терять её при рестарте не страшно: она
переотправляется companion'ом раз в TELEMETRY_SEND_INTERVAL_SECONDS.
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
            """CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id, id)")
        cur.execute(
            """CREATE TABLE IF NOT EXISTS rooms (
                session_id TEXT PRIMARY KEY,
                last_plan_json TEXT,
                library_json TEXT,
                updated_at REAL NOT NULL
            )"""
        )
        # На случай апгрейда существующей базы без library_json (созданной
        # до этого поля) — ALTER TABLE ADD COLUMN безопасно падает, если
        # колонка уже есть, поэтому оборачиваем в try.
        try:
            cur.execute("ALTER TABLE rooms ADD COLUMN library_json TEXT")
        except sqlite3.OperationalError:
            pass


def append_history(session_id: str, role: str, text: str, keep_last: int = 40) -> None:
    """role — 'user' или 'model' (терминология Gemini, не 'assistant')."""
    import time

    with _cursor() as cur:
        cur.execute(
            "INSERT INTO history (session_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, text, time.time()),
        )
        # Обрезаем хвост здесь же, а не в Python — проще держать таблицу
        # маленькой без отдельной фоновой задачи.
        cur.execute(
            """DELETE FROM history WHERE session_id = ? AND id NOT IN (
                SELECT id FROM history WHERE session_id = ? ORDER BY id DESC LIMIT ?
            )""",
            (session_id, session_id, keep_last),
        )


def load_history(session_id: str) -> list[tuple[str, str]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT role, text FROM history WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        return cur.fetchall()


def save_last_plan(session_id: str, plan: dict) -> None:
    import time

    with _cursor() as cur:
        cur.execute(
            """INSERT INTO rooms (session_id, last_plan_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET last_plan_json = excluded.last_plan_json,
                                                       updated_at = excluded.updated_at""",
            (session_id, json.dumps(plan), time.time()),
        )


def load_last_plan(session_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute("SELECT last_plan_json FROM rooms WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0])


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
