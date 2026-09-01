"""
housekeeping.py — уборка того, что DARAVE наделал за сессию.

За один вечер работы в папке DARAVE оседает три разных вида мусора, и
все три растут без предела:

* `demos/` — по два файла (wav и mp3) на каждое нажатие «послушать», а
  нажимают его десятками: перебор техник на слух — это и есть работа;
* `sets/` — собранные сеты. Полный 90-минутный сет в mp3 320 это ~200 МБ,
  и каждая пересборка кладёт рядом новый файл со случайным именем;
* `recordings/` — записи из Mixxx;
* `stems/<ключ>/` — папки, оставшиеся от прерванных проходов. Пустая
  папка занимает ноль байт, но 68 пустых папок означают «стемы вроде
  считались», хотя не посчиталось ничего, и понять это по виду нельзя.

Правило уборки — не «удалить всё», а «оставить последнее». Диджей часто
возвращается к сету, собранному полчаса назад, и вычистить его вместе с
мусором значило бы отнять работу. Поэтому у каждой папки два ограничения:
сколько последних файлов держать и сколько часов считать свежими; файл
переживает уборку, если проходит ХОТЯ БЫ по одному.

Ничего не удаляется молча: любая уборка возвращает список того, что
убрала, и сколько места освободила, — и то же самое пишется в лог.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

HERE = Path(__file__).parent

# папка -> (сколько последних файлов оставить, сколько часов считать свежими)
SWEEP_RULES = {
    "demos": (6, 2.0),
    "sets": (3, 24.0),
    "recordings": (3, 24.0),
}

# Файлы, которые в этих папках не наши и трогать их нельзя.
KEEP_NAMES = {".gitignore", "readme.md", "readme.txt", ".gitkeep"}


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def sweep_dir(folder: Path, keep_last: int, keep_hours: float,
              dry_run: bool = False) -> dict:
    """Чистит одну папку по правилу «оставить последнее»."""
    if not folder.is_dir():
        return {"folder": folder.name, "removed": [], "freed_bytes": 0, "kept": 0}

    files = [f for f in folder.iterdir()
             if f.is_file() and f.name.lower() not in KEEP_NAMES]
    files.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)

    now = time.time()
    fresh_until = now - keep_hours * 3600.0
    removed, freed = [], 0
    for i, f in enumerate(files):
        try:
            recent_enough = f.stat().st_mtime >= fresh_until
        except OSError:
            recent_enough = False
        if i < keep_last or recent_enough:
            continue
        size = _size(f)
        if not dry_run:
            try:
                f.unlink()
            except OSError:
                continue
        removed.append(f.name)
        freed += size
    return {"folder": folder.name, "removed": removed, "freed_bytes": freed,
            "kept": len(files) - len(removed)}


def sweep_stem_cache(root: Path | None = None, dry_run: bool = False) -> dict:
    """Убирает папки стемов, в которых ничего нет или проход оборвался.

    Готовность стемов определяет meta.json с complete=true (см. stems.py).
    Папка без него — это не «частично готово», а след прерванного прохода:
    пересчитается она всё равно с нуля, а места занимает до 280 МБ."""
    root = root or (HERE / "stems")
    if not root.is_dir():
        return {"folder": "stems", "removed": [], "freed_bytes": 0, "kept": 0}

    removed, freed, kept = [], 0, 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = d / "meta.json"
        complete = False
        if meta.exists():
            try:
                import json

                complete = bool(json.loads(meta.read_text(encoding="utf-8")).get("complete"))
            except (OSError, ValueError):
                complete = False
        if complete:
            kept += 1
            continue
        size = sum(_size(f) for f in d.rglob("*") if f.is_file())
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():
                continue
        removed.append(d.name)
        freed += size
    return {"folder": "stems", "removed": removed, "freed_bytes": freed, "kept": kept}


def session_cleanup(dry_run: bool = False, log=None) -> dict:
    """Полная уборка после сессии. Возвращает отчёт, ничего не скрывает."""
    parts = [sweep_dir(HERE / name, keep_last, keep_hours, dry_run)
             for name, (keep_last, keep_hours) in SWEEP_RULES.items()]
    parts.append(sweep_stem_cache(dry_run=dry_run))

    freed = sum(p["freed_bytes"] for p in parts)
    count = sum(len(p["removed"]) for p in parts)
    report = {"dry_run": dry_run, "removed_count": count,
              "freed_mb": round(freed / 1e6, 1), "by_folder": parts}
    if log:
        if count:
            detail = ", ".join(f"{p['folder']}: {len(p['removed'])}"
                               for p in parts if p["removed"])
            log(f"[уборка] удалено {count} ({report['freed_mb']} МБ) — {detail}"
                + (" [проверка, ничего не тронуто]" if dry_run else ""))
        else:
            log("[уборка] чистить нечего")
    return report


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Уборка рабочих папок DARAVE")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что было бы удалено, и не удалять")
    ap.add_argument("--all", action="store_true",
                    help="убрать ВСЁ, не оставляя последних файлов")
    args = ap.parse_args()

    if args.all:
        for name in SWEEP_RULES:
            SWEEP_RULES[name] = (0, 0.0)
    print(json.dumps(session_cleanup(dry_run=args.dry_run, log=print),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
