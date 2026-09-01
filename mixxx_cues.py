"""
mixxx_cues.py — выгрузка именованных точек DARAVE в горячие метки Mixxx.

## Почему через базу, а не через контролы

У Mixxx есть контролы `hotcue_N_set`, но ставят они метку в ТЕКУЩУЮ
позицию воспроизведения. Чтобы расставить восемь меток по треку, деку
пришлось бы восемь раз перематывать — посреди сета это неприемлемо, а вне
сета бессмысленно. Позиция метки отдельным контролом не выставляется
надёжно: единицы у неё внутренние (сэмплы, не секунды) и от версии к
версии менялись.

Библиотека Mixxx — обычный SQLite, и метки лежат в ней таблицей `cues`.
Запись туда — то же самое, что делает сам Mixxx, только без перематывания
деки. Этот путь в проекте уже проверен: `fix_mixxx_bpm.py` тем же
способом правит сетки битов.

**Mixxx при этом должен быть ЗАКРЫТ.** Он держит библиотеку в памяти и
перезапишет файл при выходе, стерев нашу работу. Резервная копия делается
всегда.

## Что и как записывается (проверено по реальной базе диджея)

Таблица `cues`: `type`, `position`, `length`, `hotcue`, `label`, `color`.

* `position` и `length` — в СЭМПЛАХ, то есть кадры × число каналов.
  Проверено на треке из его базы: length 28540996 при 44100/2 даёт 323.6 с
  при длительности трека 326.8 с;
* `type=1` — горячая метка, номер в `hotcue` считается С НУЛЯ (в базе есть
  метки 0..3, а кнопок в Mixxx 1..4);
* `type=4` — луп: position плюс length; если `hotcue` не -1, это
  сохранённый луп на кнопке;
* `type=6` — начало интро, `type=7` — аутро, и у него position = -1, а
  момент лежит в `length`. Так это записано у него в базе, и так это
  читает Mixxx;
* `color` — целое 0xRRGGBB.

Метки DARAVE ставятся только на СВОБОДНЫЕ номера, а чужие не трогаются:
если диджей расставил метки руками, они важнее любых вычисленных. Ключ
«наше или не наше» — подпись: свои мы подписываем, Mixxx по умолчанию
оставляет `label` пустым.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_MIXXXDB = Path(os.environ.get("LOCALAPPDATA", "")) / "Mixxx" / "mixxxdb.sqlite"

CUE_HOTCUE = 1
CUE_LOOP = 4
CUE_INTRO = 6
CUE_OUTRO = 7

# Цвета по роли — чтобы метки читались на волне без чтения подписей.
COLORS = {
    "first_beat": 0x2ECC71,   # зелёный: отсюда можно заводить
    "intro_end": 0x2ECC71,
    "build": 0xF1C40F,        # жёлтый: напряжение
    "drop": 0xE74C3C,         # красный: событие
    "breakdown": 0x3498DB,    # синий: пустота
    "pit": 0x9B59B6,
    "outro": 0x95A5A6,
    "loop": 0xE67E22,         # оранжевый: луп
    "phrase": 0x7F8C8D,
}
DEFAULT_COLOR = 0xFF8000


def norm_path(p: str) -> str:
    """Mixxx хранит пути через '/', сканер DARAVE — через '\\'."""
    return (p or "").replace("\\", "/").lower()


def _samples(seconds: float, samplerate: int, channels: int) -> int:
    return int(round(float(seconds) * int(samplerate or 44100) * int(channels or 2)))


def mixxx_tracks(conn: sqlite3.Connection) -> dict[str, dict]:
    """Треки библиотеки Mixxx по нормализованному пути."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT library.id, library.samplerate, library.channels, library.duration,
               track_locations.location
        FROM library JOIN track_locations ON library.location = track_locations.id
        WHERE library.mixxx_deleted = 0
    """).fetchall()
    out = {}
    for r in rows:
        out[norm_path(r["location"])] = {
            "id": r["id"], "samplerate": r["samplerate"] or 44100,
            "channels": r["channels"] or 2, "duration": r["duration"] or 0.0,
            "location": r["location"],
        }
    return out


def _existing(conn: sqlite3.Connection, track_id: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM cues WHERE track_id=?", (track_id,)).fetchall()


def write_cues_for_track(conn: sqlite3.Connection, track: dict, cues: list[dict],
                         intro_seconds: float | None = None,
                         outro_seconds: float | None = None,
                         replace_ours: bool = True) -> dict:
    """Пишет метки одного трека. Возвращает что сделано и что пропущено."""
    tid = track["id"]
    sr, ch = track["samplerate"], track["channels"]
    rows = _existing(conn, tid)

    # Свои метки узнаём по непустой подписи: Mixxx свои оставляет пустыми,
    # а ручные диджей подписывает редко и в любом случае перезаписывать их
    # нельзя — поэтому свои помечаем префиксом.
    ours = [r for r in rows if (r["label"] or "").startswith("DARAVE")]
    if replace_ours and ours:
        conn.executemany("DELETE FROM cues WHERE id=?", [(r["id"],) for r in ours])
        rows = [r for r in rows if r not in ours]

    taken = {r["hotcue"] for r in rows if r["type"] == CUE_HOTCUE and r["hotcue"] >= 0}
    free = [n for n in range(8) if n not in taken]

    added, skipped = [], []
    for cue in cues:
        if not free:
            skipped.append((cue.get("name"), "свободных кнопок не осталось"))
            continue
        slot = free.pop(0)
        pos = _samples(cue["time_seconds"], sr, ch)
        is_loop = cue.get("kind") == "loop" and cue.get("best_loop_bars")
        length = 0
        ctype = CUE_HOTCUE
        if is_loop:
            bar_seconds = 60.0 / float(cue.get("bpm") or 174.0) * 4
            length = _samples(bar_seconds * int(cue["best_loop_bars"]), sr, ch)
            ctype = CUE_LOOP
        conn.execute(
            "INSERT INTO cues (track_id, type, position, length, hotcue, label, color)"
            " VALUES (?,?,?,?,?,?,?)",
            (tid, ctype, pos, length, slot,
             "DARAVE: " + str(cue.get("name") or cue.get("label") or ""),
             COLORS.get(cue.get("kind"), DEFAULT_COLOR)))
        added.append({"hotcue": slot + 1, "name": cue.get("name"),
                      "seconds": cue["time_seconds"]})

    # Интро и аутро — отдельные маркеры Mixxx, не горячие метки. Они
    # рисуются на волне и ими пользуется Auto DJ.
    if intro_seconds is not None:
        conn.execute("DELETE FROM cues WHERE track_id=? AND type=?", (tid, CUE_INTRO))
        conn.execute(
            "INSERT INTO cues (track_id, type, position, length, hotcue, label, color)"
            " VALUES (?,?,?,?,?,?,?)",
            (tid, CUE_INTRO, _samples(intro_seconds, sr, ch), 0, -1,
             "DARAVE: интро", COLORS["first_beat"]))
    if outro_seconds is not None:
        conn.execute("DELETE FROM cues WHERE track_id=? AND type=?", (tid, CUE_OUTRO))
        # У аутро position = -1, момент лежит в length — так это записано
        # в базе самим Mixxx.
        conn.execute(
            "INSERT INTO cues (track_id, type, position, length, hotcue, label, color)"
            " VALUES (?,?,?,?,?,?,?)",
            (tid, CUE_OUTRO, -1, _samples(outro_seconds, sr, ch), -1,
             "DARAVE: аутро", COLORS["outro"]))

    return {"added": added, "skipped": skipped, "replaced": len(ours)}


def export_library(tracks: list[dict], mixxxdb: Path | None = None,
                   dry_run: bool = False, log=print, slots: int = 8) -> dict:
    """Раскладывает метки для всех треков, которые есть и у нас, и в Mixxx.

    tracks — записи анализа DARAVE (с path, bpm, structure)."""
    import cue_points

    mixxxdb = Path(mixxxdb or DEFAULT_MIXXXDB)
    if not mixxxdb.exists():
        return {"ok": False, "error": f"не нашёл библиотеку Mixxx: {mixxxdb}"}

    conn = sqlite3.connect(str(mixxxdb))
    try:
        library = mixxx_tracks(conn)
    except sqlite3.Error as exc:
        conn.close()
        return {"ok": False, "error": f"не смог прочитать библиотеку Mixxx: {exc}"}

    plan = []
    missing = 0
    for tr in tracks:
        key = norm_path(tr.get("path") or "")
        entry = library.get(key) or library.get(key.rsplit("/", 1)[-1])
        if entry is None:
            missing += 1
            continue
        data = cue_points.cues_for_track(tr)
        hot = cue_points.hotcues_for_track(tr, slots=slots)
        for h in hot:
            h["bpm"] = data.get("bpm")
        intro = next((c["time_seconds"] for c in data["cues"]
                      if c["kind"] == "first_beat"), None)
        outro = next((c["time_seconds"] for c in data["cues"]
                      if c["kind"] == "outro"), None)
        plan.append((entry, hot, intro, outro, tr.get("name")))

    log(f"метки: нашлось в Mixxx {len(plan)} треков, не нашлось {missing}")
    if dry_run:
        conn.close()
        return {"ok": True, "dry_run": True, "tracks": len(plan), "missing": missing,
                "preview": [{"track": name, "cues": [h["name"] for h in hot]}
                            for _e, hot, _i, _o, name in plan[:5]]}

    backup = mixxxdb.with_suffix(f".sqlite.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(mixxxdb, backup)
    log(f"резервная копия библиотеки Mixxx: {backup}")

    written = 0
    for entry, hot, intro, outro, name in plan:
        try:
            write_cues_for_track(conn, entry, hot, intro, outro)
            written += 1
        except sqlite3.Error as exc:
            log(f"  [!] {name}: {exc}")
    conn.commit()
    conn.close()
    return {"ok": True, "tracks": written, "missing": missing,
            "backup": str(backup),
            "note": "Mixxx должен быть закрыт во время выгрузки — иначе он "
                    "перезапишет библиотеку из памяти при выходе."}


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Выгрузка точек DARAVE в горячие метки Mixxx (Mixxx должен быть закрыт)")
    ap.add_argument("--scan-db", type=Path, required=True,
                    help="база сканера DARAVE, напр. scan_dbs\\my-room.db")
    ap.add_argument("--mixxxdb", type=Path, default=DEFAULT_MIXXXDB)
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что было бы записано, и ничего не менять")
    ap.add_argument("--slots", type=int, default=8, help="сколько кнопок занимать")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    from track_analysis import load_library_from_db

    tracks = load_library_from_db(str(args.scan_db))
    if not tracks:
        print("В базе сканера нет треков — сначала отсканируйте библиотеку.",
              file=sys.stderr)
        return 1
    r = export_library(tracks, args.mixxxdb, dry_run=args.dry_run, slots=args.slots)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
