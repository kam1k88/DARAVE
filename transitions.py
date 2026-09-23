"""
transitions.py — библиотека понравившихся переходов: оценка, заметка,
сохранённое демо.

## Зачем это отдельно от пула отбора

Пул в `audition.py` — рабочий материал ОДНОГО сета: из него собирается
цепочка, и после сборки он свою роль отыграл. Здесь другое — то, что
диджей хочет сохранить НАДОЛГО: «вот так хорошо, запомни». У такой
записи есть оценка и словами написанная причина, и живёт она дольше
любого конкретного сета.

Ради причины всё и затевалось. Алгоритм умеет померить темп, тональность
и стыковку секций; он не умеет понять, ПОЧЕМУ переход хорош. Заметка
диджея — единственный источник этого знания, и она же материал для
будущих автотехник: набор из полусотни переходов с оценками и
объяснениями — это уже обучающая выборка, а не коллекция ссылок.

## Что хранится

Всё, что нужно, чтобы переход воспроизвести дословно: пара треков,
приём, обе точки, длина в тактах и параметры приёма. Плюс оценка 1-5,
заметка и имя сохранённого демо — чтобы переслушать, не пересчитывая.

Демо КОПИРУЕТСЯ в `demos/keep/`: обычные демо чистятся при отказе и
уборкой по расписанию, а отложенное должно пережить и то, и другое.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import paths as _paths
import persistence

# Демо лежат в ПИСЧЕЙ папке (%LOCALAPPDATA%\\DARAVE\\demos), а не рядом с
# исходниками. Здесь стояло `Path(__file__).parent / "demos"` — в сборке
# это `C:\\Program Files\\DARAVE\\_internal\\demos`, где файла нет и
# писать нельзя. `_keep_demo` молча не находил исходник и возвращал
# None, поэтому у КАЖДОГО отложенного перехода стояло «демо не
# сохранено». Ровно та же ошибка, что была у папки стемов.
DEMOS_DIR = _paths.data_subdir("demos")
KEEP_DIR = DEMOS_DIR / "keep"


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            a_path TEXT, b_path TEXT,
            a_name TEXT, b_name TEXT,
            technique_id TEXT, technique TEXT, pair_label TEXT,
            exit_seconds REAL, entry_seconds REAL, bars REAL,
            params_json TEXT,
            rating INTEGER,
            note TEXT,
            demo_file TEXT,
            source TEXT,
            code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS transitions_room "
                 "ON transitions(session_id, rating)")
    # Таблица старше поля `code` — добавляем на месте, чтобы уже
    # отложенные переходы не пришлось терять.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transitions)")}
    if "code" not in cols:
        conn.execute("ALTER TABLE transitions ADD COLUMN code TEXT")
    conn.commit()


@contextmanager
def _db():
    conn = persistence._connect()
    _init(conn)
    yield conn


def _keep_demo(demo_file: str | None) -> str | None:
    """Копирует демо в demos/keep — там его не тронет уборка."""
    if not demo_file:
        return None
    src = DEMOS_DIR / Path(demo_file).name
    if not src.is_file():
        return None
    KEEP_DIR.mkdir(parents=True, exist_ok=True)
    dst = KEEP_DIR / src.name
    try:
        if not dst.exists():
            shutil.copy2(src, dst)
    except OSError:
        return None
    return "keep/" + dst.name


def save(session_id: str, seam: dict, rating: int | None = None,
         note: str | None = None, demo_file: str | None = None,
         source: str = "audition", code: str | None = None) -> dict:
    """Отложить переход. Повторное сохранение той же связки обновляет
    оценку и заметку, а не плодит дубли: диджей чаще уточняет мнение,
    чем сохраняет второй раз то же самое."""
    now = time.time()
    kept = _keep_demo(demo_file)
    # Код шва. Если клиент его не прислал — считаем сами: отложенный
    # переход без кода нельзя ни вернуть в Clipping, ни передать словами,
    # а именно ради этого код и заводили.
    if not code:
        try:
            import audition as _aud

            state = {k: seam.get(k) for k in _aud.CODE_FIELDS}
            state["a_path"] = seam.get("a_path")
            state["b_path"] = seam.get("b_path")
            code = str(_aud.seam_code(session_id, state))
        except Exception:                            # noqa: BLE001
            code = None
    params = dict(seam.get("params") or {})
    if seam.get("bars") and "bars" not in params:
        params["bars"] = float(seam["bars"])
    key = (session_id, seam.get("a_path"), seam.get("b_path"),
           seam.get("technique_id"), round(float(seam.get("exit_seconds") or 0), 1))
    with _db() as conn:
        row = conn.execute(
            """SELECT id FROM transitions WHERE session_id=? AND a_path=? AND b_path=?
               AND technique_id=? AND ROUND(COALESCE(exit_seconds,0),1)=?""", key).fetchone()
        if row:
            conn.execute(
                """UPDATE transitions SET rating=COALESCE(?, rating),
                          note=COALESCE(?, note), demo_file=COALESCE(?, demo_file),
                          entry_seconds=?, bars=?, params_json=?,
                          code=COALESCE(?, code), updated_at=?
                   WHERE id=?""",
                (rating, note, kept, seam.get("entry_seconds"), seam.get("bars"),
                 json.dumps(params, ensure_ascii=False), code, now, row[0]))
            conn.commit()
            return {"ok": True, "id": row[0], "updated": True}
        cur = conn.execute(
            """INSERT INTO transitions (session_id, a_path, b_path, a_name, b_name,
                    technique_id, technique, pair_label, exit_seconds, entry_seconds,
                    bars, params_json, rating, note, demo_file, source, code,
                    created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, seam.get("a_path"), seam.get("b_path"),
             seam.get("a_name"), seam.get("b_name"),
             seam.get("technique_id"), seam.get("technique"), seam.get("pair_label"),
             seam.get("exit_seconds"), seam.get("entry_seconds"), seam.get("bars"),
             json.dumps(params, ensure_ascii=False), rating, note, kept, source,
             code, now, now))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "updated": False}


_COLUMNS = ("id", "a_path", "b_path", "a_name", "b_name", "technique_id",
            "technique", "pair_label", "exit_seconds", "entry_seconds", "bars",
            "params_json", "rating", "note", "demo_file", "source", "code",
            "created_at", "updated_at")


def listing(session_id: str, min_rating: int = 0) -> list[dict]:
    """Отложенные переходы: сначала высокие оценки, потом свежие."""
    with _db() as conn:
        rows = conn.execute(
            f"""SELECT {', '.join(_COLUMNS)} FROM transitions
                WHERE session_id=? AND COALESCE(rating,0) >= ?
                ORDER BY COALESCE(rating,0) DESC, updated_at DESC""",
            (session_id, int(min_rating))).fetchall()
    out = []
    for r in rows:
        d = dict(zip(_COLUMNS, r))
        try:
            d["params"] = json.loads(d.pop("params_json") or "{}")
        except ValueError:
            d["params"] = {}
        out.append(d)
    return out


def update(session_id: str, tid: int, rating: int | None = None,
           note: str | None = None) -> dict:
    with _db() as conn:
        cur = conn.execute(
            """UPDATE transitions SET rating=COALESCE(?, rating),
                      note=COALESCE(?, note), updated_at=?
               WHERE id=? AND session_id=?""",
            (rating, note, time.time(), int(tid), session_id))
        conn.commit()
    return {"ok": cur.rowcount > 0}


def remove(session_id: str, tid: int) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT demo_file FROM transitions WHERE id=? AND session_id=?",
                           (int(tid), session_id)).fetchone()
        cur = conn.execute("DELETE FROM transitions WHERE id=? AND session_id=?",
                           (int(tid), session_id))
        conn.commit()
    if row and row[0]:
        f = Path(__file__).resolve().parent / "demos" / row[0]
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass
    return {"ok": cur.rowcount > 0}


def stats(session_id: str) -> dict:
    """Сводка для интерфейса: сколько отложено и как оценено."""
    items = listing(session_id)
    by_rating: dict[int, int] = {}
    by_tech: dict[str, int] = {}
    for it in items:
        by_rating[int(it.get("rating") or 0)] = by_rating.get(int(it.get("rating") or 0), 0) + 1
        tid = it.get("technique_id") or "?"
        by_tech[tid] = by_tech.get(tid, 0) + 1
    return {"total": len(items), "by_rating": by_rating, "by_technique": by_tech,
            "with_note": sum(1 for it in items if (it.get("note") or "").strip())}
