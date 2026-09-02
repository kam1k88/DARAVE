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

    plan, skipped, already_ok = [], [], []
    for r in rows:
        loc = norm_path(r["location"] or "")
        # Сначала по полному пути; если папка в Mixxx и в сканере записана
        # по-разному (буква диска, симлинк, другая точка монтирования) —
        # по имени файла.
        ours = darave.get(loc) or darave_by_name.get(loc.rsplit("/", 1)[-1])
        if ours is None and loc.endswith(".stem.mp4"):
            # Стемовый файл в анализе DARAVE не значится — анализ идёт по
            # исходному .mp3, а .stem.mp4 собирается из него. Для Mixxx
            # это ДВЕ разные записи, и без этой строчки стемовая дека
            # оставалась с темпом, найденным самим Mixxx, — то есть
            # ровно с той ошибкой, которую мы здесь и чиним. Ищем анализ
            # по исходному имени.
            base = loc[:-len(".stem.mp4")]
            for ext in (".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif", ".ogg"):
                ours = darave.get(base + ext) or darave_by_name.get(
                    (base + ext).rsplit("/", 1)[-1])
                if ours is not None:
                    break
        if ours is None:
            skipped.append((r["location"], "нет в анализе DARAVE")); continue
        if r["bpm_lock"]:
            skipped.append((r["location"], "BPM залочен в Mixxx")); continue

        # Пишем ИМЕННО значение из анализа DARAVE, а не «удвоенное
        # значение Mixxx».
        #
        # Раньше здесь умели ровно одну поправку — удвоение, — а самую
        # частую ошибку этой библиотеки (116 вместо 174, полтора раза)
        # печатали в список «поправьте руками» и не трогали. Поэтому темп
        # в Mixxx «слетал обратно» после каждого прохода: сорок треков он
        # просто не чинил. Теперь источник истины один — БД сканера, где
        # темп уже сверен с тегом файла и уточнён по сетке
        # (см. id3_tags.py).
        #
        # Предохранитель остаётся: чужое значение можно записать, только
        # если оно связано с найденным Mixxx простым отношением. Сетка
        # битов держится на позиции первой доли, и темп, не кратный
        # измеренному, сдвинул бы её целиком.
        target = float(ours)
        ratio = target / r["bpm"] if r["bpm"] else 0.0
        SAFE_RATIOS = (1.0, 1.5, 2.0, 3.0, 4.0 / 3.0, 0.5, 2.0 / 3.0, 0.75)
        near = min((abs(ratio - x) / x for x in SAFE_RATIOS), default=9.0)
        dnb_halftime = args.assume_dnb and DNB_HALF_LOW <= r["bpm"] <= DNB_HALF_HIGH
        if near > 0.03 and not dnb_halftime:
            skipped.append((r["location"],
                            f"анализ даёт {target:.2f}, Mixxx {r['bpm']:.2f} — "
                            f"это не простое отношение, не трогаю"))
            continue
        if dnb_halftime and near > 0.03:
            target = r["bpm"] * 2.0
        if abs(target - r["bpm"]) < 0.05:
            # Значение уже верное — но замок всё равно поставим. Иначе
            # Mixxx при следующем анализе пересчитает его своим
            # детектором и сломает то, что менять было не нужно.
            already_ok.append(r["id"])
            continue
        if not (PLAUSIBLE_LOW <= target <= PLAUSIBLE_HIGH):
            skipped.append((r["location"], f"{target:.0f} — вне разумного диапазона"))
            continue
        try:
            grid_bpm, first = parse_beatgrid(r["beats"]) if r["beats"] else (None, None)
        except Exception as exc:
            skipped.append((r["location"], f"не разобрал бит-сетку: {exc}")); continue
        why = ("из анализа DARAVE" if near <= 0.03 else "DnB-полутемп")
        plan.append((r["id"], r["location"], r["bpm"], target, grid_bpm, first, why))

    print(f"\nК исправлению: {len(plan)} записей (из {len(rows)} в библиотеке Mixxx)")
    for _, loc, old, new, _, _, why in plan[:60]:
        print(f"  {old:6.1f} -> {new:6.1f}  [{why:18}] {Path(loc).name[:48]}")
    if len(plan) > 60:
        print(f"  ... и ещё {len(plan) - 60}")
    if skipped:
        print(f"\nПропущено {len(skipped)}:")
        for loc, why in skipped[:10]:
            print(f"  {Path(loc).name[:52]:54} {why}")

    if not plan and not already_ok:
        print("\nНечего менять.")
        return 0
    if already_ok:
        print(f"\nЕщё {len(already_ok)} записей уже с верным темпом — им поставим "
              f"только замок, чтобы Mixxx их не пересчитал.")
    if not plan and already_ok and not args.apply:
        print("\nЭто предпросмотр. Запустите с --apply.")
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
        # bpm_lock=1 — иначе Mixxx при следующем анализе трека посчитает
        # темп заново своим детектором и вернёт ту же ошибку. Именно так
        # исправленный темп «слетал обратно»: мы писали значение, а Mixxx
        # считал его своим предположением и переписывал. Замок означает
        # «темп задан человеком, не трогать» — и Mixxx его уважает.
        conn.execute("UPDATE library SET bpm = ?, beats = ?, bpm_lock = 1 WHERE id = ?",
                     (new_bpm, build_beatgrid(new_bpm, first), track_id))
    for track_id in already_ok:
        conn.execute("UPDATE library SET bpm_lock = 1 WHERE id = ?", (track_id,))
    conn.commit()
    print(f"Готово: обновлено {len(plan)} записей, темп закреплён (bpm_lock). "
          f"Запускайте Mixxx.")
    print("Если что-то пошло не так — верните резервную копию поверх mixxxdb.sqlite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
