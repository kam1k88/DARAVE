"""
mixxx_profile.py — настройки Mixxx принадлежат КОМНАТЕ DARAVE, а не
установленной копии Mixxx.

## Зачем

Mixxx хранит всё своё в одной папке (`%LOCALAPPDATA%\\Mixxx` на Windows,
`~/.mixxx` на Linux и macOS): mixxx.cfg, маппинги контроллеров, цепочки
эффектов, профили вещания. Там же лежит библиотека и результаты анализа —
сотни мегабайт, которые к настройкам отношения не имеют.

Из-за этого «переехать на другую сборку Mixxx» означает руками собрать
десяток файлов из кучи мусора. А диджей ставит другую сборку регулярно:
он собирает Mixxx из форка, и каждая пересборка — новая установка.

Здесь настройки описаны как ПРОФИЛЬ КОМНАТЫ: небольшой набор файлов,
который DARAVE хранит у себя (`profiles/<комната>/`), умеет снять с
текущей установки и наложить на любую другую. Версия Mixxx перестаёт
иметь значение: берётся любая, профиль применяется поверх.

## Что входит и чего нет

Входит то, что диджей настраивал руками: `mixxx.cfg`, маппинги и скрипты
контроллеров, `effects.xml` и цепочки эффектов, сэмплеры, конфигурация
звуковых карт, профили вещания, свои скины и раскладка клавиатуры.

НЕ входит то, что пересчитывается само и весит на три порядка больше:
библиотека `mixxxdb.sqlite` (её содержимое — треки, а треки живут в
файлах), папка `analysis` (188 МБ волновых форм), логи, дампы контролов,
резервные копии. Профиль целиком — около полумегабайта, его можно
держать в git и переносить как текст.

Библиотека сюда не входит СОЗНАТЕЛЬНО, а не по недосмотру: метки, BPM и
сетку DARAVE и так умеет писать в базу Mixxx сам (`mixxx_cues.py`,
`fix_mixxx_bpm.py`), и делает это из своего анализа. Копировать базу
между установками значит копировать чужие идентификаторы треков.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import time
from pathlib import Path

# Файлы в корне папки настроек, которые составляют профиль.
PROFILE_FILES = (
    "mixxx.cfg",
    "effects.xml",
    "samplers.xml",
    "soundconfig.xml",
)

# Папки целиком (за вычетом SKIP_PATTERNS).
PROFILE_DIRS = (
    "controllers",          # маппинги и JS — главное, ради чего всё это
    "effects",              # цепочки эффектов
    "broadcast_profiles",
    "skins",                # свои скины, если диджей их правил
    "keyboard",             # своя раскладка клавиатуры
)

# Что не берём никогда — по маске имени.
SKIP_PATTERNS = (
    "*.bak", "*.bak-*", "*~",
    "*.log", "*.log.*",
    "mixxxdb.sqlite*",
    "co_dump_*.csv",
    "sandbox.cfg",          # пути песочницы конкретной машины
    "*.sqlite-wal", "*.sqlite-shm",
)

# Папки, которые не берём, даже если они попались внутри профильных.
SKIP_DIRS = ("analysis", "lut", "cache", "__pycache__")


# Профиль лежит в git и уезжает на другую машину, поэтому две вещи с ним
# делаются на выходе: убираются пароли и обезличиваются пути.
SETTINGS_TOKEN = "${MIXXX_SETTINGS}"
HOME_TOKEN = "${HOME}"
SECRET_TAGS = ("Password", "Login")
TEXT_SUFFIXES = (".cfg", ".xml", ".js", ".json", ".txt", ".kbd.cfg")


def _sanitize(text: str, name: str, settings_dir: Path) -> str:
    """Вычищает из файла профиля пароли и абсолютные пути.

    Пароли — потому что профиль уезжает в репозиторий: в
    broadcast_profiles Mixxx держит логин и пароль от стрим-сервера
    открытым текстом, и попасть на GitHub они не должны ни при каких
    условиях.

    Пути — потому что профиль должен ложиться на ЛЮБУЮ установку. В
    mixxx.cfg маппинги контроллеров прописаны абсолютным путём вида
    C:/Users/<имя>/AppData/Local/Mixxx/controllers/... — на другой машине
    такого пути нет, и контроллер молча не подключится."""
    for tag in SECRET_TAGS:
        text = re.sub(rf"(<{tag}>)(.*?)(</{tag}>)", r"\1\3", text,
                      flags=re.S | re.I)
    for form in {str(settings_dir), str(settings_dir).replace("\\", "/")}:
        if form:
            text = text.replace(form, SETTINGS_TOKEN)
    # И на всякий случай — по форме пути, а не только по строке: профиль
    # могли снять с одной машины, указав --mixxx-dir, а пути внутри
    # ссылаются на другую (так бывает после переустановки Windows).
    text = re.sub(r"[A-Za-z]:[\\/](?:[^\\/\n\"<>]+[\\/])*?AppData[\\/]Local[\\/]Mixxx",
                  SETTINGS_TOKEN, text)
    text = re.sub(r"/(?:home|Users)/[^/\n\"<>]+/\.mixxx", SETTINGS_TOKEN, text)
    # Домашняя папка — после папки настроек, она общее. Заодно из профиля
    # исчезает имя пользователя: профиль уезжает в публичный репозиторий.
    text = re.sub(r"[A-Za-z]:[\\/]Users[\\/][^\\/\n\"<>]+", HOME_TOKEN, text)
    text = re.sub(r"/(?:home|Users)/[^/\n\"<>]+", HOME_TOKEN, text)
    return text


def _localize(text: str, settings_dir: Path) -> str:
    """Обратная замена при наложении профиля на конкретную установку."""
    text = text.replace(SETTINGS_TOKEN, str(settings_dir).replace("\\", "/"))
    return text.replace(HOME_TOKEN, str(Path.home()).replace("\\", "/"))


def _is_text(name: str) -> bool:
    return name.lower().endswith(TEXT_SUFFIXES)


def _write_text_file(src: Path, dst: Path, transform) -> None:
    try:
        text = src.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        shutil.copy2(src, dst)
        return
    dst.write_text(transform(text), encoding="utf-8")
    shutil.copystat(src, dst)


def _skip(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in SKIP_PATTERNS)


def mixxx_settings_dir(explicit: str | None = None) -> Path | None:
    """Где Mixxx держит свои настройки на этой машине."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Mixxx")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Mixxx")
    home = Path.home()
    candidates += [home / ".mixxx",
                   home / "Library" / "Application Support" / "Mixxx"]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def profiles_root() -> Path:
    return Path(__file__).resolve().parent / "profiles"


def profile_dir(room: str) -> Path:
    safe = "".join(ch for ch in str(room or "default")
                   if ch.isalnum() or ch in "-_") or "default"
    return profiles_root() / safe


def _copy_tree(src: Path, dst: Path, log=None, transform=None) -> tuple[int, int]:
    """Копирует папку, пропуская мусор. Возвращает (файлов, байт)."""
    files = size = 0
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not _skip(d)]
        rel = Path(root).relative_to(src)
        for name in names:
            if _skip(name):
                continue
            s = Path(root) / name
            d = dst / rel / name
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                if transform is not None and _is_text(name):
                    _write_text_file(s, d, transform)
                else:
                    shutil.copy2(s, d)
            except OSError as exc:
                if log:
                    log(f"  [!] {s.name}: {exc}")
                continue
            files += 1
            size += s.stat().st_size
    return files, size


def export_profile(room: str, mixxx_dir: str | None = None, log=print) -> dict:
    """Снять настройки текущей установки Mixxx в профиль комнаты."""
    src = mixxx_settings_dir(mixxx_dir)
    if src is None:
        return {"ok": False, "error": "не нашёл папку настроек Mixxx"}
    dst = profile_dir(room)
    dst.mkdir(parents=True, exist_ok=True)
    files = size = 0
    clean = lambda t: _sanitize(t, "", src)          # noqa: E731
    for name in PROFILE_FILES:
        s = src / name
        if s.is_file():
            if _is_text(name):
                _write_text_file(s, dst / name, clean)
            else:
                shutil.copy2(s, dst / name)
            files += 1
            size += s.stat().st_size
    for name in PROFILE_DIRS:
        s = src / name
        if s.is_dir():
            f, b = _copy_tree(s, dst / name, log, transform=clean)
            files += f
            size += b
    (dst / "PROFILE.txt").write_text(
        "Профиль настроек Mixxx для комнаты DARAVE «%s»\n"
        "Снят: %s\nИсточник: %s\n\n"
        "Применяется командой:  python mixxx_profile.py --apply --room %s\n"
        "Mixxx при этом должен быть ЗАКРЫТ — он перезаписывает mixxx.cfg\n"
        "при выходе и сотрёт наложенное.\n"
        % (room, time.strftime("%Y-%m-%d %H:%M"), src, room),
        encoding="utf-8")
    log(f"профиль «{room}»: {files} файлов, {size / 1024:.0f} КБ -> {dst}")
    return {"ok": True, "files": files, "bytes": size,
            "profile_dir": str(dst), "source": str(src)}


def apply_profile(room: str, mixxx_dir: str | None = None,
                  dry_run: bool = False, log=print) -> dict:
    """Наложить профиль комнаты на установку Mixxx.

    Mixxx должен быть ЗАКРЫТ: он держит mixxx.cfg в памяти и перезаписывает
    его при выходе — ровно та же история, что с библиотекой и метками.
    Прежние файлы не удаляются, а сохраняются рядом с суффиксом .bak-<время>.
    Пути вида ${MIXXX_SETTINGS} разворачиваются в папку настроек ЭТОЙ
    установки — из-за этого профиль и переносится между сборками.
    """
    src = profile_dir(room)
    if not src.is_dir():
        return {"ok": False, "error": f"профиля «{room}» нет: {src}"}
    dst = mixxx_settings_dir(mixxx_dir)
    if dst is None:
        return {"ok": False, "error": "не нашёл папку настроек Mixxx"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    written: list[str] = []
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(src)
        for name in names:
            if name == "PROFILE.txt" or _skip(name):
                continue
            s = Path(root) / name
            d = dst / rel / name
            written.append(str(Path(rel) / name))
            if dry_run:
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.exists():
                try:
                    shutil.copy2(d, d.with_name(d.name + f".bak-{stamp}"))
                except OSError:
                    pass
            if _is_text(name):
                _write_text_file(s, d, lambda t: _localize(t, dst))
            else:
                shutil.copy2(s, d)
    log(("показал бы" if dry_run else "наложил") +
        f" {len(written)} файлов профиля «{room}» на {dst}")
    return {"ok": True, "files": written, "target": str(dst),
            "dry_run": bool(dry_run)}


def profile_status(room: str, mixxx_dir: str | None = None) -> dict:
    """Что в профиле и совпадает ли он с установкой."""
    src = profile_dir(room)
    dst = mixxx_settings_dir(mixxx_dir)
    out = {"room": room, "profile_dir": str(src), "exists": src.is_dir(),
           "mixxx_dir": str(dst) if dst else None, "files": 0, "bytes": 0,
           "differs": []}
    if not src.is_dir():
        return out
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(src)
        for name in names:
            if name == "PROFILE.txt":
                continue
            s = Path(root) / name
            out["files"] += 1
            out["bytes"] += s.stat().st_size
            if dst is None:
                continue
            d = dst / rel / name
            if not d.exists():
                out["differs"].append(str(Path(rel) / name))
                continue
            # Сравниваем ПОСЛЕ подстановки путей: в профиле они
            # обезличены, и побайтовое сравнение показало бы разницу у
            # каждого текстового файла.
            if _is_text(name):
                try:
                    same = _localize(s.read_text(encoding="utf-8"), dst) == \
                        d.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    same = d.stat().st_size == s.stat().st_size
            else:
                same = d.stat().st_size == s.stat().st_size
            if not same:
                out["differs"].append(str(Path(rel) / name))
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Профиль настроек Mixxx для комнаты DARAVE")
    ap.add_argument("--room", default="my-room")
    ap.add_argument("--mixxx-dir", default=None, help="папка настроек Mixxx, если не находится сама")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", action="store_true", help="снять настройки в профиль")
    g.add_argument("--apply", action="store_true", help="наложить профиль на Mixxx")
    g.add_argument("--status", action="store_true", help="что в профиле и что разошлось")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.export:
        r = export_profile(a.room, a.mixxx_dir)
    elif a.apply:
        r = apply_profile(a.room, a.mixxx_dir, dry_run=a.dry_run)
    else:
        r = profile_status(a.room, a.mixxx_dir)
        print(f"профиль {r['profile_dir']}: {r['files']} файлов, {r['bytes'] / 1024:.0f} КБ")
        if r["differs"]:
            print("разошлись с установкой:", ", ".join(r["differs"][:20]))
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
