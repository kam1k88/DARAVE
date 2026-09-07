"""
presets.py — сохранённые настройки приёмов.

## Зачем

У приёма 2-5 ползунков, и подобранные значения — это работа: «вот такой
дабл-дроп с пересечением в полдоли и возвратом верха через два такта»
находится на слух за десяток прослушиваний. Без сохранения диджей
настраивал одно и то же заново каждый раз, а в сет попадали значения по
умолчанию — то есть та самая версия приёма, которую он и не выбирал.

Пресет — это имя плюс `technique_id` плюс значения ползунков. Ничего
больше: треки и точки к приёму не относятся, они у каждого перехода
свои.

## Где хранится

Таблица `technique_presets` в той же `darave.db`, что история комнаты,
библиотека и приговоры отбора. Пресеты живут ПО КОМНАТЕ: у разных сетов
разные настройки одного и того же приёма, и общий список смешал бы их.

Имя уникально в пределах комнаты и приёма: сохранить «мягкий» дважды —
это обновить его, а не завести второй с тем же именем.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

import persistence


def _init(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS technique_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            technique_id TEXT NOT NULL,
            name TEXT NOT NULL,
            params_json TEXT NOT NULL,
            note TEXT,
            updated_at REAL NOT NULL,
            UNIQUE(session_id, technique_id, name)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS presets_room "
                 "ON technique_presets(session_id, technique_id)")
    conn.commit()


@contextmanager
def _db():
    conn = persistence._connect()
    _init(conn)
    yield conn


def save(session_id: str, technique_id: str, name: str,
         params: dict, note: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("у пресета должно быть имя")
    if len(name) > 60:
        name = name[:60]
    clean = {}
    for k, v in (params or {}).items():
        try:
            clean[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    with _db() as conn:
        conn.execute(
            """INSERT INTO technique_presets
                   (session_id, technique_id, name, params_json, note, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id, technique_id, name) DO UPDATE SET
                   params_json = excluded.params_json,
                   note = excluded.note,
                   updated_at = excluded.updated_at""",
            (session_id, technique_id, name, json.dumps(clean, ensure_ascii=False),
             note, time.time()))
        conn.commit()
    return {"ok": True, "technique_id": technique_id, "name": name, "params": clean}


def listing(session_id: str, technique_id: str | None = None) -> list[dict]:
    sql = ("SELECT technique_id, name, params_json, note, updated_at "
           "FROM technique_presets WHERE session_id = ?")
    args: list = [session_id]
    if technique_id:
        sql += " AND technique_id = ?"
        args.append(technique_id)
    sql += " ORDER BY technique_id, name"
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"technique_id": r[0], "name": r[1], "params": json.loads(r[2] or "{}"),
             "note": r[3], "updated_at": r[4]} for r in rows]


def delete(session_id: str, technique_id: str, name: str) -> dict:
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM technique_presets WHERE session_id=? AND technique_id=? AND name=?",
            (session_id, technique_id, name))
        conn.commit()
    return {"ok": True, "deleted": cur.rowcount}
