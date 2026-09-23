"""
track_pool.py — ПУЛ ТРЕКОВ. Не путать с пулом переходов.

Две разные вещи, и диджей назвал разницу точнее любого комментария:

    как только выделенные файлы добавить в clipping и в ControlC через
    какой-нибудь пул треков, но не тот, что пул переходов для сета,
    собираемый в Clipping

Пул ПЕРЕХОДОВ (`transitions.py`) — это одобренные ШВЫ: пара треков плюс
приём, точки и настройки. Он растёт в Clipping и из него собирается сет.

Пул ТРЕКОВ (этот модуль) — просто отложенные треки. Короткий список, с
которым диджей хочет поработать: отобрал в Library шесть вещей и хочет,
чтобы Clipping искал швы только между ними, а ControlC собирал сет из
них. Никаких швов, приёмов и точек здесь нет и быть не должно.

Порядок хранится: диджей раскладывает треки руками, и этот порядок
ControlC берёт как готовую последовательность.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

import persistence


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS track_pool (
            session_id TEXT NOT NULL,
            path TEXT NOT NULL,
            position REAL NOT NULL,
            added_at REAL NOT NULL,
            PRIMARY KEY (session_id, path)
        )"""
    )
    conn.commit()


@contextmanager
def _db():
    conn = persistence._connect()
    _init(conn)
    yield conn


def paths(session_id: str) -> list[str]:
    """Пути в том порядке, в каком их разложил диджей."""
    with _db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT path FROM track_pool WHERE session_id=? ORDER BY position, added_at",
            (session_id,))]


def add(session_id: str, items: list[str]) -> int:
    """Добавить, не трогая тех, кто уже в пуле. Возвращает, сколько новых.

    Повторное добавление не переставляет трек в конец: диджей мог
    разложить порядок руками, и «добавил ещё раз» не повод его ломать."""
    items = [p for p in (items or []) if p]
    if not items:
        return 0
    now = time.time()
    with _db() as conn:
        have = {r[0] for r in conn.execute(
            "SELECT path FROM track_pool WHERE session_id=?", (session_id,))}
        row = conn.execute(
            "SELECT MAX(position) FROM track_pool WHERE session_id=?",
            (session_id,)).fetchone()
        pos = float(row[0] or 0.0)
        fresh = [p for p in items if p not in have]
        for p in fresh:
            pos += 1.0
            conn.execute(
                "INSERT OR IGNORE INTO track_pool(session_id, path, position, added_at) "
                "VALUES (?,?,?,?)", (session_id, p, pos, now))
        conn.commit()
        return len(fresh)


def remove(session_id: str, items: list[str]) -> int:
    items = [p for p in (items or []) if p]
    if not items:
        return 0
    with _db() as conn:
        cur = conn.executemany(
            "DELETE FROM track_pool WHERE session_id=? AND path=?",
            [(session_id, p) for p in items])
        conn.commit()
        return cur.rowcount or 0


def clear(session_id: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM track_pool WHERE session_id=?", (session_id,))
        conn.commit()


def reorder(session_id: str, ordered: list[str]) -> None:
    """Разложить пул в заданном порядке.

    Треки, которых в присланном списке нет, остаются в пуле и уезжают в
    конец: диджей переставлял видимую часть, а не удалял невидимую."""
    with _db() as conn:
        pos = 0.0
        for p in (ordered or []):
            pos += 1.0
            conn.execute("UPDATE track_pool SET position=? WHERE session_id=? AND path=?",
                         (pos, session_id, p))
        conn.execute(
            "UPDATE track_pool SET position=position+? WHERE session_id=? AND position<=?",
            (pos + 1000.0, session_id, 0.0))
        conn.commit()


def tracks(session_id: str, library: list[dict]) -> list[dict]:
    """Карточки треков пула — в порядке пула.

    Путь, которого больше нет в библиотеке, молча пропускается: файл
    могли удалить или перенести, и падать из-за этого пулу незачем."""
    by_path = {t.get("path"): t for t in (library or []) if t.get("path")}
    return [by_path[p] for p in paths(session_id) if p in by_path]
