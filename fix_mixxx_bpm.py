"""
Починка половинного темпа в библиотеке Mixxx (DnB detected as 86 instead of 172).

Зачем: анализатор Mixxx (Queen Mary) для драм-н-бейса регулярно фиксирует
ПОЛОВИННЫЙ темп — 84-87 вместо 168-175. Это октавная неоднозначность:
обе сетки одинаково хорошо ложатся на доли. Последствие не косметическое:
sync между треком «86» и треком «175» требует изменения скорости вдвое, а
питч-фейдер даёт ±8% — выровнять физически невозможно, деки разъезжаются.
Именно это ощущается как «sync криво синхронизирует».

Что делает скрипт: находит треки, где Mixxx определил примерно вдвое
меньший темп, чем анализ DARAVE (scan_dbs/<room>.db), и удваивает BPM —
ровно то же, что кнопка "x2" в бит-сетке Mixxx, только не по одному треку.
Точка первой доли НЕ трогается: удвоение лишь добавляет долю между
существующими, сетка остаётся на месте.

Формат бит-сетки (library.beats, beats_version='BeatGrid-2.0') — protobuf:
    field 1: Bpm      { field 1: double bpm }
    field 2: FirstBeat{ field 1: int32 frame }
Меняем только 8 байт double'а и колонку library.bpm.

БЕЗОПАСНОСТЬ:
  * MIXXX ДОЛЖЕН БЫТЬ ЗАКРЫТ — иначе он держит библиотеку в памяти и
    перезапишет файл при выходе. Скрипт сам проверяет блокировку БД.
  * Всегда делается резервная копия рядом (mixxxdb.sqlite.bak-<время>).
  * Без --apply только показывает, что будет изменено.
  * Меняются ТОЛЬКО треки, где оба анализатора согласны, что это
    удвоение (отношение темпов 1.8..2.2). Ничего «на глаз».

Запуск:
    python fix_mixxx_bpm.py                 # показать, что будет сделано
    python fix_mixxx_bpm.py --apply         # применить
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

DEFAULT_MIXXXDB = Path(os.environ.get("LOCALAPPDATA", "")) / "Mixxx" / "mixxxdb.sqlite"
RATIO_LOW, RATIO_HIGH = 1.8, 2.2
# Половина реального DnB-темпа (165-180). См. --assume-dnb.
DNB_HALF_LOW, DNB_HALF_HIGH = 82.0, 95.0
# Результат удвоения должен попадать в осмысленный для DnB/джангла темп.
# Честная оговорка: по одному темпу НЕЛЬЗЯ отличить DnB, определённый как
# половинный (86 -> 172), от настоящего хип-хопа на 86. Поэтому скрипт
# всегда показывает полный список ПЕРЕД изменением и требует --apply.
PLAUSIBLE_LOW, PLAUSIBLE_HIGH = 140.0, 200.0


# ---------- минимальный protobuf для BeatGrid-2.0 ----------

def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        b = buf[i]; i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def parse_beatgrid(blob: bytes) -> tuple[float | None, int | None]:
    """-> (bpm, first_beat_frame). None, если поле отсутствует."""
    bpm = first = None
    i = 0
    while i < len(blob):
        key, i = _read_varint(blob, i)
        field, wire = key >> 3, key & 7
        if wire == 2:
            ln, i = _read_varint(blob, i)
            sub, i = blob[i:i + ln], i + ln
            if field == 1:                       # Bpm
                j = 0
                while j < len(sub):
                    k, j = _read_varint(sub, j)
                    if (k >> 3) == 1 and (k & 7) == 1:
                        bpm = struct.unpack("<d", sub[j:j + 8])[0]; j += 8
                    else:
                        raise ValueError("неожиданное поле в Bpm")
            elif field == 2:                     # FirstBeat
                j = 0
                while j < len(sub):
                    k, j = _read_varint(sub, j)
                    if (k >> 3) == 1 and (k & 7) == 0:
                        first, j = _read_varint(sub, j)
                    else:
                        raise ValueError("неожиданное поле в FirstBeat")
        elif wire == 0:
            _, i = _read_varint(blob, i)
        else:
            raise ValueError(f"неподдерживаемый wire type {wire}")
    return bpm, first


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def build_beatgrid(bpm: float, first_beat: int | None) -> bytes:
    inner = b"\x09" + struct.pack("<d", bpm)
    out = b"\x0a" + _varint(len(inner)) + inner
    if first_beat is not None:
        fb = b"\x08" + _varint(first_beat)
        out += b"\x12" + _varint(len(fb)) + fb
    return out


# ---------- основная логика ----------

def norm_path(p: str) -> str:
    """Mixxx хранит пути через '/' и с регистром как в проводнике
    ('C:/Users/.../Music/x.mp3'), сканер DARAVE — через '\\' и как ввёл
    пользователь ('C:\\Users\\...\\music\\x.mp3'). Сравнивать надо
    приведённые: иначе ни один трек не находится и скрипт молча решает,
    что менять нечего."""
    return p.replace("\\", "/").lower()


def load_darave_bpm(scan_db: Path) -> tuple[dict[str, float], dict[str, float]]:
    """(по полному пути, по имени файла) -> BPM по анализу DARAVE, приведённый к
    диапазону [90,180). librosa промахивается ровно так же, как Mixxx, и в
    БД сканера лежит тот же половинный темп — без нормализации сравнивать
    было бы не с чем (оба сказали бы 86)."""
    if not scan_db.exists():
        return {}, {}
    from track_analysis import normalize_bpm
    conn = sqlite3.connect(f"file:{scan_db}?mode=ro", uri=True)
    try:
        by_path, by_name = {}, {}
        for p, b in conn.execute("SELECT path, bpm FROM tracks WHERE bpm > 0"):
            np_ = norm_path(p)
            by_path[np_] = normalize_bpm(b)
            by_name.setdefault(np_.rsplit("/", 1)[-1], normalize_bpm(b))
        return by_path, by_name
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mixxxdb", type=Path, default=DEFAULT_MIXXXDB)
    ap.add_argument("--scan-db", type=Path, default=None,
                    help="БД сканера DARAVE (по умолчанию — самая свежая в scan_dbs/)")
    ap.add_argument("--apply", action="store_true", help="реально записать изменения")
    ap.add_argument(
        "--assume-dnb", action="store_true",
        help="удваивать ЛЮБОЙ трек, который Mixxx определил в 82-95 BPM, даже если "
             "анализ DARAVE с этим не согласен. Настоящий драм-н-бейс — 165-180, и "
             "82-95 это ровно его половина; трека, который РЕАЛЬНО играет на 88, в "
             "DnB-библиотеке практически не бывает. Для не-DnB библиотеки не включать.",
    )
    args = ap.parse_args()

    if not args.mixxxdb.exists():
        print(f"Не нашёл библиотеку Mixxx: {args.mixxxdb}", file=sys.stderr)
        return 1

    scan_db = args.scan_db
    if scan_db is None:
        candidates = sorted((Path(__file__).parent / "scan_dbs").glob("*.db"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print("Нет ни одной БД сканера в scan_dbs/ — сначала отсканируйте библиотеку "
                  "во вкладке «Библиотека».", file=sys.stderr)
            return 1
        scan_db = candidates[0]
    darave, darave_by_name = load_darave_bpm(scan_db)
    print(f"Анализ DARAVE: {scan_db} ({len(darave)} треков)")

    conn = sqlite3.connect(str(args.mixxxdb))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT library.id, library.bpm, library.beats, library.bpm_lock,
               track_locations.location
        FROM library JOIN track_locations ON library.location = track_locations.id
        WHERE library.beats_version = 'BeatGrid-2.0' AND library.bpm > 0
    """).fetchall()

    plan, skipped, three_halves = [], [], []
    for r in rows:
        loc = norm_path(r["location"] or "")
        # Сначала по полному пути; если папка в Mixxx и в сканере записана
        # по-разному (буква диска, симлинк, другая точка монтирования) —
        # по имени файла.
        ours = darave.get(loc) or darave_by_name.get(loc.rsplit("/", 1)[-1])
        if ours is None:
            skipped.append((r["location"], "нет в анализе DARAVE")); continue
        if r["bpm_lock"]:
            skipped.append((r["location"], "BPM залочен в Mixxx")); continue
        ratio = ours / r["bpm"] if r["bpm"] else 0
        agree_double = RATIO_LOW <= ratio <= RATIO_HIGH
        dnb_halftime = args.assume_dnb and DNB_HALF_LOW <= r["bpm"] <= DNB_HALF_HIGH
        if not (agree_double or dnb_halftime):
            # Отдельно отметим классическую ошибку «в полтора раза» (116 вместо
            # 174): удвоение тут дало бы 232, поэтому автоматом не трогаем.
            if (ours and 1.4 <= ratio <= 1.6
                    and PLAUSIBLE_LOW <= r["bpm"] * 1.5 <= PLAUSIBLE_HIGH):
                three_halves.append((r["location"], r["bpm"], r["bpm"] * 1.5))
            continue
        if not (PLAUSIBLE_LOW <= r["bpm"] * 2.0 <= PLAUSIBLE_HIGH):
            skipped.append((r["location"], f"удвоение дало бы {r['bpm']*2:.0f} — вне разумного диапазона"))
            continue
        try:
            grid_bpm, first = parse_beatgrid(r["beats"]) if r["beats"] else (None, None)
        except Exception as exc:
            skipped.append((r["location"], f"не разобрал бит-сетку: {exc}")); continue
        plan.append((r["id"], r["location"], r["bpm"], r["bpm"] * 2.0, grid_bpm, first,
                     "оба анализа согласны" if agree_double else "DnB-полутемп"))

    print(f"\nК удвоению: {len(plan)} треков (из {len(rows)} в библиотеке Mixxx)")
    for _, loc, old, new, _, _, why in plan[:60]:
        print(f"  {old:6.1f} -> {new:6.1f}  [{why:18}] {Path(loc).name[:48]}")
    if len(plan) > 60:
        print(f"  ... и ещё {len(plan) - 60}")
    if three_halves:
        print(f"\nОтдельно: {len(three_halves)} треков похожи на ошибку «в полтора раза» "
              f"(116 вместо 174 — типично для DnB с триольным ощущением).")
        print("Автоматом не трогаю: удвоение дало бы вдвое больше нужного. "
              "Поправьте вручную в Mixxx (бит-сетка -> подогнать), если они вам нужны:")
        for loc, old, want in three_halves[:10]:
            print(f"  {old:6.1f} -> ~{want:5.1f}?  {Path(loc).name[:50]}")
    if skipped:
        print(f"\nПропущено {len(skipped)}:")
        for loc, why in skipped[:10]:
            print(f"  {Path(loc).name[:52]:54} {why}")

    if not plan:
        print("\nНечего менять.")
        return 0
    if not args.apply:
        print("\nЭто предпросмотр. Запустите с --apply, чтобы применить (Mixxx должен быть ЗАКРЫТ).")
        return 0

    # Mixxx открыт? SQLite не даст взять эксклюзивную блокировку.
    try:
        conn.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError:
        print("\nБаза занята — похоже, Mixxx запущен. Закройте Mixxx полностью и повторите.",
              file=sys.stderr)
        return 2

    backup = args.mixxxdb.with_suffix(f".sqlite.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    conn.rollback()
    shutil.copy2(args.mixxxdb, backup)
    print(f"\nРезервная копия: {backup}")

    conn.execute("BEGIN EXCLUSIVE")
    for track_id, _, _, new_bpm, _, first, _ in plan:
        conn.execute("UPDATE library SET bpm = ?, beats = ? WHERE id = ?",
                     (new_bpm, build_beatgrid(new_bpm, first), track_id))
    conn.commit()
    print(f"Готово: обновлено {len(plan)} треков. Запускайте Mixxx.")
    print("Если что-то пошло не так — верните резервную копию поверх mixxxdb.sqlite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
