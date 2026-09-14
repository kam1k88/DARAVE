"""
mixxx_autodj.py — порядок сета едет в очередь Auto DJ самого Mixxx.

## Зачем это понадобилось

Диджей выгрузил план плейлистом `.m3u8`, импортировал его в контейнер
Mixxx и увидел ровно то, что и должен был увидеть: список треков и ни
намёка на последовательное воспроизведение. Это не поломка экспорта —
это предел формата. В `.m3u8` есть только пути к файлам; ни точки входа,
ни точки ухода, ни приёма туда положить нельзя, и Mixxx их оттуда не
возьмёт, потому что в его модели плейлиста таких полей нет вовсе.

Место, где у Mixxx ЕСТЬ последовательное воспроизведение, ровно одно —
очередь **Auto DJ**. Это обычный плейлист с `id = 1` и `hidden = 1`;
Mixxx играет его сверху вниз, сам заводя следующий трек и сводя своим
кроссфейдом. Порядок туда положить можно — и это единственное, что из
плана в Mixxx переносится машинно.

## Что переносится, а что нет

| Из плана DARAVE            | Куда попадает в Mixxx        |
|----------------------------|------------------------------|
| порядок треков             | очередь Auto DJ (этот модуль)|
| точки входа и ухода        | горячие метки (`mixxx_cues`) |
| приём, длина, ползунки     | НИКУДА                       |

Приём переносить некуда, и это не обходится хитростью: в базе Mixxx нет
поля «свести эту пару через ST-15 за 32 такта». Приёмы исполняет либо
сам DARAVE через свой контроллер в реальном времени, либо офлайн-рендер
(«собрать сет целиком») — там переход уже внутри звука.

Поэтому Auto DJ — это «сет играет сам, но сводит его Mixxx своим
кроссфейдом», а не «играет так, как вы отобрали». Разница честная, и
интерфейс обязан её называть.

**Mixxx должен быть ЗАКРЫТ.** Он держит библиотеку в памяти и
перезаписывает её при выходе — та же история, что с метками и mixxx.cfg.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from mixxx_cues import DEFAULT_MIXXXDB, norm_path

AUTODJ_PLAYLIST_ID = 1


def _library_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Путь (в нормальном виде) -> id записи в библиотеке Mixxx."""
    rows = conn.execute(
        """SELECT l.id, tl.location FROM library l
             JOIN track_locations tl ON tl.id = l.location
            WHERE COALESCE(l.mixxx_deleted, 0) = 0""").fetchall()
    return {norm_path(loc): int(tid) for tid, loc in rows if loc}


def _prefer_stem(path: str) -> list[str]:
    """Сначала стемовый файл, потом исходный.

    На деку диджей заводит `.stem.mp4` — ради стемов он их и собирал.
    В библиотеке Mixxx это отдельная запись, и если она есть, в очередь
    должна попасть именно она."""
    out = []
    try:
        import stem_mp4 as _sm

        sp = str(_sm.stem_mp4_path(path))
        if sp != path:
            out.append(sp)
    except Exception:
        pass
    out.append(path)
    return out


def read_queue(mixxxdb: Path | None = None) -> list[str]:
    """Что сейчас лежит в очереди Auto DJ, по порядку.

    Читаем в режиме read-only, поэтому безопасно и при ЗАПУЩЕННОМ Mixxx:
    смотреть можно всегда, писать — только при закрытом."""
    mixxxdb = Path(mixxxdb or DEFAULT_MIXXXDB)
    if not mixxxdb.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{mixxxdb}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            """SELECT tl.location FROM PlaylistTracks pt
                 JOIN library l ON l.id = pt.track_id
                 JOIN track_locations tl ON tl.id = l.location
                WHERE pt.playlist_id = ? ORDER BY pt.position""",
            (AUTODJ_PLAYLIST_ID,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def queue_matches(paths: list[str], mixxxdb: Path | None = None) -> dict:
    """Совпадает ли очередь Auto DJ с порядком сета.

    Сравниваем по нормализованному пути и допускаем стемовую замену:
    на деку диджей заводит `.stem.mp4`, и в очереди законно лежит именно
    он, а в плане — исходный файл."""
    queue = [norm_path(p) for p in read_queue(mixxxdb)]
    want: list[set[str]] = []
    for p in paths:
        want.append({norm_path(c) for c in _prefer_stem(p)})

    ok = len(queue) >= len(want) and all(queue[i] in want[i] for i in range(len(want)))
    first_bad = None
    for i in range(min(len(queue), len(want))):
        if queue[i] not in want[i]:
            first_bad = i
            break
    if first_bad is None and len(queue) < len(want):
        first_bad = len(queue)
    return {"ok": ok, "queued": len(queue), "expected": len(want),
            "first_mismatch": first_bad}


def export_autodj(paths: list[str], mixxxdb: Path | None = None,
                  replace: bool = True, dry_run: bool = False,
                  log=print) -> dict:
    """Положить порядок сета в очередь Auto DJ.

    replace=True очищает очередь перед записью — иначе новый сет
    дописался бы к остаткам предыдущего, и играло бы не то.
    """
    mixxxdb = Path(mixxxdb or DEFAULT_MIXXXDB)
    if not mixxxdb.exists():
        return {"ok": False, "error": f"не нашёл библиотеку Mixxx: {mixxxdb}"}

    conn = sqlite3.connect(str(mixxxdb))
    try:
        index = _library_index(conn)
    except sqlite3.Error as exc:
        conn.close()
        return {"ok": False, "error": f"не смог прочитать библиотеку Mixxx: {exc}"}

    queued: list[tuple[int, str]] = []
    missing: list[str] = []
    stems = 0
    for p in paths:
        tid = None
        for cand in _prefer_stem(p):
            tid = index.get(norm_path(cand))
            if tid is not None:
                if cand != p:
                    stems += 1
                break
        if tid is None:
            missing.append(Path(p).name)
        else:
            queued.append((tid, p))

    if dry_run:
        conn.close()
        return {"ok": True, "dry_run": True, "queued": len(queued),
                "stems": stems, "missing": missing}
    if not queued:
        conn.close()
        return {"ok": False, "error": "ни одного трека сета нет в медиатеке Mixxx. "
                                      "Добавить файл снаружи нельзя — пересканируйте папку в Mixxx.",
                "missing": missing}

    backup = mixxxdb.with_name(mixxxdb.name + time.strftime(".bak-%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(mixxxdb, backup)
    except OSError as exc:
        conn.close()
        return {"ok": False, "error": f"не смог сделать резервную копию базы: {exc}"}

    try:
        conn.execute(
            """INSERT OR IGNORE INTO Playlists (id, name, position, hidden, locked)
               VALUES (?, 'Auto DJ', 1, 1, 0)""", (AUTODJ_PLAYLIST_ID,))
        if replace:
            conn.execute("DELETE FROM PlaylistTracks WHERE playlist_id = ?",
                         (AUTODJ_PLAYLIST_ID,))
            start = 0
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) FROM PlaylistTracks WHERE playlist_id = ?",
                (AUTODJ_PLAYLIST_ID,)).fetchone()
            start = int(row[0] or 0)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.executemany(
            """INSERT INTO PlaylistTracks (playlist_id, track_id, position, pl_datetime_added)
               VALUES (?,?,?,?)""",
            [(AUTODJ_PLAYLIST_ID, tid, start + i + 1, now)
             for i, (tid, _p) in enumerate(queued)])
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        conn.close()
        return {"ok": False, "error": f"не смог записать очередь Auto DJ: {exc}",
                "backup": str(backup)}
    conn.close()
    log(f"в очередь Auto DJ положено {len(queued)} треков"
        + (f", из них стемовых {stems}" if stems else "")
        + (f"; не нашлось в медиатеке: {len(missing)}" if missing else ""))
    return {"ok": True, "queued": len(queued), "stems": stems,
            "missing": missing, "backup": str(backup),
            "note": ("Auto DJ играет очередь сверху вниз и сводит СВОИМ кроссфейдом. "
                     "Точки входа и ухода живут в горячих метках («Метки в Mixxx»), "
                     "а приёмы исполняет DARAVE через свой контроллер или "
                     "офлайн-рендер «собрать сет целиком».")}
