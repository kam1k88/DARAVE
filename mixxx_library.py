"""
mixxx_library.py — сверка библиотеки DARAVE с библиотекой Mixxx.

## Зачем

Библиотек две, и они не знают друг о друге. Mixxx сканирует папку и
складывает найденное в `mixxxdb.sqlite`; DARAVE сканирует ту же папку и
складывает в свою `scan_dbs/<комната>.db`. Разойтись они могут в обе
стороны: диджей добавил трек в Mixxx — DARAVE его не видит; DARAVE
собрал `.stem.mp4` — Mixxx о нём не знает, пока не пересканирует.

Хуже другое. Библиотека Mixxx не различает, ЧЕМ является файл: для неё
`Track.mp3` и `Track.stem.mp4` — две независимые записи с разными id, и
что вторая является стемовой версией первой, она не знает. Отсюда и
метки, записанные на mp3, для стемовой деки не существуют (подробности
в `claude/cue-points.md`).

Этот модуль отвечает на три вопроса и ничего не меняет сам:

  * что есть у Mixxx, но нет у DARAVE (и наоборот);
  * у каких записей Mixxx есть парная стемовая запись, а у каких нет;
  * какие треки DARAVE проанализировал, но в Mixxx их нет вовсе.

Писать в базу Mixxx умеют `mixxx_cues.py` (метки) и `fix_mixxx_bpm.py`
(темп и сетка) — они и остаются единственными, кто это делает.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def mixxx_db_path(explicit: str | None = None) -> Path | None:
    """Где лежит библиотека Mixxx."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    try:
        import mixxx_profile

        d = mixxx_profile.mixxx_settings_dir()
    except Exception:
        d = None
    if d is None:
        return None
    p = d / "mixxxdb.sqlite"
    return p if p.is_file() else None


def _norm(path: str) -> str:
    """Ключ сравнения: без регистра и с одинаковыми разделителями.

    Windows отдаёт пути то с обратными слэшами, то с прямыми, а Mixxx
    хранит директорию и имя файла отдельно — без нормализации совпадений
    не находится вообще ни одного."""
    return str(path or "").replace("\\", "/").lower()


def _stem_twin(path: str) -> str:
    """Имя стемовой пары для обычного файла."""
    p = _norm(path)
    for ext in (".mp3", ".flac", ".wav", ".aiff", ".m4a", ".ogg"):
        if p.endswith(ext):
            return p[: -len(ext)] + ".stem.mp4"
    return p


def read_mixxx_tracks(db_path: str | None = None) -> list[dict]:
    """Треки из библиотеки Mixxx: путь, длина, BPM, есть ли анализ.

    Открываем ТОЛЬКО на чтение и по возможности immutable: Mixxx может
    быть запущен, и брать блокировку на его базу нельзя."""
    p = mixxx_db_path(db_path)
    if p is None:
        return []
    uri = f"file:{p.as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            """SELECT tl.location, l.duration, l.bpm, l.beats IS NOT NULL,
                      l.title, l.artist
                 FROM library l JOIN track_locations tl ON tl.id = l.location
                WHERE COALESCE(l.mixxx_deleted, 0) = 0""").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for loc, dur, bpm, has_beats, title, artist in rows:
        if not loc:
            continue
        out.append({"path": loc, "key": _norm(loc),
                    "name": os.path.basename(loc),
                    "duration_seconds": dur, "bpm": bpm,
                    "has_beatgrid": bool(has_beats),
                    "title": title, "artist": artist,
                    "is_stem": _norm(loc).endswith(".stem.mp4")})
    return out


def compare(darave_tracks: list[dict], db_path: str | None = None) -> dict:
    """Что разошлось между двумя библиотеками.

    darave_tracks — то, что отдаёт комната (список со списком path).
    """
    mixxx = read_mixxx_tracks(db_path)
    if not mixxx:
        return {"ok": False,
                "error": "не нашёл библиотеку Mixxx (mixxxdb.sqlite)"}
    m_by_key = {t["key"]: t for t in mixxx}
    d_by_key = {_norm(t.get("path")): t for t in darave_tracks if t.get("path")}

    # Пары «обычный файл ↔ его .stem.mp4». Mixxx их не связывает: для
    # него это две независимые записи, и именно поэтому метки,
    # записанные на mp3, для стемовой деки не существуют.
    plain = [t for t in mixxx if not t["is_stem"]]
    with_stem, without_stem = [], []
    for t in plain:
        (with_stem if _stem_twin(t["key"]) in m_by_key else without_stem).append(t["name"])

    only_mixxx = [t["name"] for t in mixxx
                  if not t["is_stem"] and t["key"] not in d_by_key]
    only_darave = [os.path.basename(t.get("path") or "") for k, t in d_by_key.items()
                   if k not in m_by_key]
    # Стем собран, но Mixxx о нём не знает — самая частая рассинхронизация:
    # файл лежит рядом с треком, а в медиатеке его нет, пока не пересканируют.
    stem_on_disk_not_in_mixxx = []
    for k, t in d_by_key.items():
        twin = _stem_twin(k)
        if twin in m_by_key:
            continue
        if Path(twin).exists() or Path(str(t.get("path") or "")).with_suffix("").with_suffix(".stem.mp4").exists():
            stem_on_disk_not_in_mixxx.append(os.path.basename(t.get("path") or ""))
    return {
        "ok": True,
        "mixxx_total": len(mixxx),
        "mixxx_plain": len(plain),
        "mixxx_stems": len(mixxx) - len(plain),
        "darave_total": len(d_by_key),
        "with_stem_record": len(with_stem),
        "without_stem_record": without_stem[:200],
        "only_in_mixxx": only_mixxx[:200],
        "only_in_darave": only_darave[:200],
        "stem_on_disk_not_in_mixxx": stem_on_disk_not_in_mixxx[:200],
        "hint": ("Файлы, которых нет в медиатеке Mixxx, добавляются только "
                 "пересканированием папки в самом Mixxx — снаружи это сделать "
                 "нельзя."),
    }
