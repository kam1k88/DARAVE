"""
fix_library_bpm.py — чинит BPM в уже отсканированной библиотеке, не
пересканируя её заново.

Зачем: librosa.beat.beat_track отдаёт темп с сетки-приора, а не измеренный.
На библиотеке этого пользователя это дало две поломки сразу:

  1. 22 трека получили РОВНО 172.30, хотя на деле там 171.00, 173.00,
     174.03. Ошибка 1-2 BPM = 1%: за 30 секунд сведения вторая дека уезжает
     почти на целую долю. Именно поэтому «синхронизирует криво и
     разъезжается» — темп в базе совпадал, а музыка нет.
  2. 25 треков определились как 2/3 своего темпа (117.5 вместо 176,
     112.3 вместо 168). Такие треки выпадали из семьи темпов и вообще не
     попадали в план, хотя сводятся идеально.

Измеряем контрастом гребёнчатого фильтра на длинном окне (beatgrid.py):
сетка с верным периодом попадает на удары на всём окне, с чуть неверным —
уезжает и контраст падает. Проверено: при 172.3 контраст 1.14 (сетки нет),
при уточнённом темпе — 2.5..7.1.

Полный анализ трека занимает минуты; здесь читается 45-90 секунд аудио,
поэтому вся библиотека чинится за минуту-две.

Запуск:
    python fix_library_bpm.py                    # все комнаты в scan_dbs/
    python fix_library_bpm.py --db scan_dbs/my-room.db
    python fix_library_bpm.py --dry-run          # только показать, что изменится
    python fix_library_bpm.py --no-octave        # не трогать 2/3-ошибки
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PROBE_SECONDS = 90.0
# Кандидаты на исправление октавы/доли. 1.5 и 2/3 — потому что трекер
# регулярно путает драм-н-бейс с хаус-темпом (176 -> 117.5).
OCTAVE_CANDIDATES = (1.0, 1.5, 2.0, 4.0 / 3.0, 0.5, 2.0 / 3.0, 0.75)
MIN_CONFIDENCE = 0.20


def _load_whole(path: str, sr: int = 11025):
    """Грузим трек целиком один раз: и для темпа, и для карты барабанов.
    Два отдельных чтения одного файла — самая дорогая часть прохода."""
    import librosa

    y, _ = librosa.load(path, sr=sr, mono=True)
    return y, sr


def refine_one(path: str, bpm_hint: float, band: tuple[float, float],
               allow_octave: bool = True, y=None, sr: int = 11025) -> dict:
    """Возвращает {bpm, changed, ratio, contrast, note}."""
    import numpy as np

    import beatgrid

    if y is None:
        y, sr = _load_whole(path)
    if len(y) < sr * 8:
        return {"bpm": bpm_hint, "changed": False, "note": "слишком короткий фрагмент"}

    # темп меряем по телу трека: в интро часто нет барабанов вообще
    mid = len(y) // 2
    half = int(PROBE_SECONDS * sr / 2)
    probe = y[max(0, mid - half):mid + half]
    wide, _lows, frame_sec = beatgrid._onset_envelopes(probe, sr)

    ratios = OCTAVE_CANDIDATES if allow_octave else (1.0,)
    best = None
    for r in ratios:
        cand_hint = bpm_hint * r
        if not (band[0] * 0.75 <= cand_hint <= band[1] * 1.25):
            continue
        res = beatgrid.refine_tempo(probe, sr, cand_hint)
        bpm = float(res["bpm"])
        contrast = beatgrid._comb_best(wide, (60.0 / bpm) / frame_sec, n_phases=256)
        in_band = band[0] <= bpm <= band[1]
        # приоритет: сначала попадание в полосу исполнения библиотеки,
        # потом контраст. Без полосы «половинный темп» почти всегда
        # выигрывает по контрасту — см. комментарий в beatgrid.refine_tempo.
        key = (1 if in_band else 0, contrast)
        if best is None or key > best[0]:
            best = (key, bpm, r, contrast, res.get("confidence", 0.0))

    if best is None:
        return {"bpm": bpm_hint, "changed": False, "note": "нет кандидатов в полосе"}

    _key, bpm, ratio, contrast, conf = best
    if conf < MIN_CONFIDENCE and abs(ratio - 1.0) > 1e-9:
        return {"bpm": bpm_hint, "changed": False, "note": f"низкая уверенность {conf:.2f}"}

    changed = abs(bpm - bpm_hint) > 0.05
    note = ""
    if abs(ratio - 1.0) > 1e-9:
        note = f"исправлена доля x{ratio:.4g}"
    elif changed:
        note = "уточнён темп"
    return {"bpm": round(bpm, 2), "changed": changed, "ratio": ratio,
            "contrast": round(contrast, 2), "confidence": conf, "note": note}


def _drum_map_for(y, sr: int, bpm: float) -> dict | None:
    """Карта «где играют барабаны». Без неё точки входа выбирались по
    ненадёжным дропам structure-анализа, и половина переходов заводила
    новый трек в его эмбиентное интро."""
    import beatgrid

    try:
        return beatgrid.drum_map(y, sr, bpm)
    except Exception:
        return None


def _energy_map_for(y, sr: int, bpm: float) -> dict | None:
    """Карта энергии низа по тактам: брейкдауны, ямы, дропы.

    Считается здесь же, в том же фоновом проходе, что и сетка битов, —
    диджей не должен запускать ради этого отдельную утилиту. Заменяет
    дропы structure-анализа: те находились с уверенностью 0.14-0.26, и
    точки входа по ним попадали мимо.
    """
    try:
        import beatgrid

        return beatgrid.energy_map(y, sr, bpm)
    except Exception:
        return None


def _chroma_map_for(y, sr: int, bpm: float) -> dict | None:
    """Гармонический профиль по фразам. Camelot считается по всему треку и
    не говорит ничего о том, спорят ли те 30 секунд, которые реально
    накладываются — измерено, связи между ними нет."""
    import beatgrid

    try:
        return beatgrid.chroma_map(y, sr, bpm)
    except Exception:
        return None


def _store_maps(con, path: str, dmap: dict | None, cmap: dict | None,
                emap: dict | None = None) -> None:
    """Кладём карты в structure_json рядом с дропами и брейкдаунами."""
    import json

    row = con.execute("SELECT structure_json FROM tracks WHERE path=?", (path,)).fetchone()
    try:
        structure = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        structure = {}
    if not isinstance(structure, dict):
        structure = {}
    if dmap:
        structure["drum_map"] = dmap
    if cmap:
        structure["chroma_map"] = cmap
    if emap:
        structure["energy_map"] = emap
    con.execute("UPDATE tracks SET structure_json=? WHERE path=?",
                (json.dumps(structure, ensure_ascii=False), path))


def library_band(bpms: list[float]) -> tuple[float, float]:
    """Полоса исполнения библиотеки — вокруг её собственного центра, а не
    зашитая константа: у диджея техно-библиотека и драм-н-бейсовая имеют
    разные полосы, и жёсткий диапазон сломал бы одну из них."""
    import tempo as _tempo

    center = _tempo.library_center([b for b in bpms if b and b > 20]) or 174.0
    return (center * 0.82, center * 1.20)


def stale_bpm_report(bpms: list[float]) -> dict:
    """Признак того, что BPM в базе — заглушка трекера, а не измерение.

    Настоящие темпы у 50 разных релизов не совпадают до сотых. Если один
    и тот же BPM стоит у трети библиотеки — это значение с сетки-приора
    librosa, и сводить по нему нельзя."""
    from collections import Counter

    vals = [round(float(b), 2) for b in bpms if b and b > 20]
    if not vals:
        return {"stale": False, "share": 0.0, "value": None, "total": 0, "distinct": 0}
    value, count = Counter(vals).most_common(1)[0]
    share = count / len(vals)
    distinct = len(set(vals))
    # Два независимых признака заглушки, любого достаточно:
    #  - один и тот же темп у заметной доли библиотеки;
    #  - мало РАЗНЫХ значений вообще (у 48 реальных релизов их почти 48,
    #    а на сетке-приоре получается 6-7).
    stale = (share >= 0.20 and count >= 3) or (len(vals) >= 5 and distinct / len(vals) < 0.5)
    return {"stale": bool(stale), "share": round(share, 3), "value": value,
            "count": count, "total": len(vals), "distinct": distinct}


def fix_db(db_path: str, dry_run: bool = False, allow_octave: bool = True,
           limit: int | None = None, progress=None, log=print) -> dict:
    # WAL + длинный busy_timeout. В ЭТОТ ЖЕ файл базы пишет сканер
    # библиотеки, и они запросто идут одновременно: диджей нажал
    # «Сканировать», потом открыл «Стратегию» — и та кикнула уточнение BPM.
    # В журнале по умолчанию писатели блокируют друг друга намертво, а
    # таймаут sqlite — 5 секунд: сканер получал «database is locked» и падал
    # с кодом 1 посреди прохода (в UI — «при сканировании вылазит ошибка»).
    con = sqlite3.connect(db_path, timeout=60.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT path, bpm FROM tracks ORDER BY path").fetchall()
    if not rows:
        con.close()
        return {"db": db_path, "tracks": 0, "changes": []}

    band = library_band([r["bpm"] for r in rows])
    log(f"\n=== {db_path} ===")
    log(f"треков: {len(rows)}, полоса исполнения библиотеки: {band[0]:.0f}..{band[1]:.0f} BPM")

    backup = None
    if not dry_run:
        backup = f"{db_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(db_path, backup)
        log(f"резервная копия: {backup}")

    changes: list[dict] = []
    changed = 0
    missing = 0
    t0 = time.time()
    for i, row in enumerate(rows if limit is None else rows[:limit]):
        path, old = row["path"], float(row["bpm"] or 0)
        if not os.path.exists(path):
            missing += 1
            continue
        try:
            y, sr_l = _load_whole(path)
            res = refine_one(path, old, band, allow_octave=allow_octave, y=y, sr=sr_l)
            dmap = _drum_map_for(y, sr_l, res.get("bpm") or old)
            cmap = _chroma_map_for(y, sr_l, res.get("bpm") or old)
            emap = _energy_map_for(y, sr_l, res.get("bpm") or old)
        except Exception as exc:  # один битый файл не должен ронять проход
            log(f"  [!] {os.path.basename(path)[:46]:<46} {type(exc).__name__}: {exc}")
            continue
        new = res["bpm"]
        if res.get("changed"):
            changed += 1
            mark = "**" if abs(new - old) > 3 else "  "
            log(f"{mark} {os.path.basename(path)[:46]:<46} {old:7.2f} -> {new:7.2f}  {res.get('note','')}")
            changes.append({"name": os.path.basename(path), "old": round(old, 2),
                            "new": new, "note": res.get("note", "")})
            if not dry_run:
                con.execute("UPDATE tracks SET bpm=? WHERE path=?", (new, path))
        if not dry_run and (dmap or cmap):
            _store_maps(con, path, dmap, cmap, emap)
        if not dry_run:
            # Коммитим ПОТРЕКОВО, а не одним куском в конце. Общий коммит
            # держал транзакцию записи все 5-7 минут перемера — за это время
            # любой другой писатель гарантированно упирался в блокировку.
            # Плюс прерванный проход больше не теряет всю проделанную работу.
            con.commit()
        if progress:
            progress(i + 1, len(rows))
        if (i + 1) % 10 == 0:
            log(f"   ... {i+1}/{len(rows)}  ({time.time()-t0:.0f}с)")

    if not dry_run:
        con.commit()
    con.close()
    log(f"итого: изменено {changed} из {len(rows)}"
        + (f", файлов не найдено {missing}" if missing else "")
        + (" (пробный запуск, ничего не записано)" if dry_run else ""))
    return {"db": db_path, "tracks": len(rows), "changed": changed, "missing": missing,
            "backup": backup, "changes": changes,
            "band": [round(band[0], 1), round(band[1], 1)]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Уточнить BPM в отсканированной библиотеке DARAVE")
    ap.add_argument("--db", help="конкретный файл базы; по умолчанию все в scan_dbs/")
    ap.add_argument("--dry-run", action="store_true", help="показать изменения, но не записывать")
    ap.add_argument("--no-octave", action="store_true", help="не исправлять 2/3- и половинные ошибки")
    ap.add_argument("--limit", type=int, help="обработать только первые N треков (для проверки)")
    args = ap.parse_args()

    dbs = [args.db] if args.db else sorted(
        os.path.join(HERE, "scan_dbs", f) for f in os.listdir(os.path.join(HERE, "scan_dbs"))
        if f.endswith(".db")) if os.path.isdir(os.path.join(HERE, "scan_dbs")) else []
    if not dbs:
        print("Не найдено ни одной базы. Укажите --db путь/к/базе.db")
        return 1
    for db in dbs:
        fix_db(db, dry_run=args.dry_run, allow_octave=not args.no_octave, limit=args.limit)
    print("\nГотово. Перестройте план в веб-интерфейсе — BPM обновится.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
