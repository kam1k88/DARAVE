"""
music_fetch.py — пополнение библиотеки треками, которые разрешено скачивать.

## Откуда берём и почему именно оттуда

Jamendo — каталог музыки под лицензиями Creative Commons, с открытым API
и правом на скачивание, прописанным у КАЖДОГО трека полем
`audiodownload_allowed`. Мы это поле не игнорируем: если автор скачивание
не разрешил, трек в выдаче остаётся (его видно), но кнопки загрузки у
него нет.

Чего здесь СОЗНАТЕЛЬНО нет: качалок из стриминговых сервисов. Они тянут
файлы, которые подписка даёт слушать, но не отдавать, выдавая себя за
приложение сервиса. Это обход защиты, а не бесплатный софт, и в DARAVE
этого не будет. Для купленного и скачанного самостоятельно есть второй
путь — папка-приёмник (см. `inbox_scan` ниже): кладёте файлы руками,
DARAVE их подхватывает и разбирает.

## Про BPM: у Jamendo его нет

В API есть только `speed` пятью корзинами (verylow…veryhigh) — ни
точного темпа, ни диапазона. Поэтому «хочу 40 треков 170-180» работает
так: корзина сужает перебор, а темп МЕРЯЕТСЯ нашим же анализатором после
загрузки, и не попавшее в диапазон удаляется. Это дороже по трафику, но
честнее: тот же анализатор считает темп всей библиотеке, и трек,
пришедший через загрузку, ничем не отличается от прочих.

Из этого следует неприятное, о чём лучше знать заранее: чтобы набрать N
треков в узком диапазоне, скачать придётся больше N. `plan_fetch`
говорит, сколько кандидатов возьмёт, до начала загрузки.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

JAMENDO_ROOT = "https://api.jamendo.com/v3.0"
SECRETS_FILE = "darave_secrets.json"
USER_AGENT = "DARAVE/1.0 (DJ library tool)"

# Jamendo отдаёт быстро, но это чужой бесплатный сервис — не долбим.
MIN_INTERVAL = 0.35
_last_call = 0.0

# Сколько кандидатов брать на один нужный трек, когда задан диапазон
# темпа. Два — не с потолка: корзина `speed` у Jamendo широкая, и в
# выдаче «veryhigh» половина треков мимо 170-180.
BPM_OVERFETCH = 2.5

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".aiff", ".aif"}


class FetchError(RuntimeError):
    """Не вышло сходить в каталог. Причина — человеческим текстом."""


# --------------------------------------------------------------- ключ

def resolve_client_id(explicit: str | None = None) -> str | None:
    """client_id Jamendo: аргумент -> переменная окружения -> локальный файл.

    Тот же порядок, что у токена Discogs, и по той же причине: ключ не
    должен жить в репозитории. Бесплатный, регистрация на
    developer.jamendo.com."""
    if explicit:
        return str(explicit).strip() or None
    env = os.environ.get("JAMENDO_CLIENT_ID")
    if env:
        return env.strip() or None
    f = Path(__file__).resolve().parent / SECRETS_FILE
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("jamendo_client_id")
    return str(v).strip() or None if v else None


# ------------------------------------------------- жанры и темп наружу

# Наши жанры -> теги Jamendo. Слева то, чем оперирует весь остальной
# DARAVE (см. genre.py), справа — как это называется в каталоге.
#
# Фонка здесь нет намеренно: это жанр, выросший на SoundCloud и
# коммерческих релизах, и в CC-каталоге его попросту не водится. Пустая
# выдача честнее, чем подсунуть вместо него «trap» и сделать вид, что
# нашли.
GENRE_TAGS: dict[str, tuple[str, ...]] = {
    "dnb": ("drumnbass", "dnb", "jungle", "breakbeat"),
    "liquid": ("drumnbass", "chillout"),
    "neurofunk": ("drumnbass", "darkambient"),
    "jungle": ("jungle", "breakbeat"),
    "dubstep": ("dubstep", "bass"),
    "garage": ("garage", "ukgarage", "2step"),
    "breaks": ("breakbeat", "breaks"),
    "house": ("house", "deephouse"),
    "techno": ("techno", "minimal"),
    "trance": ("trance", "psytrance"),
    "hiphop": ("hiphop", "rap", "trap"),
    "ambient": ("ambient", "chillout"),
    "electronic": ("electronic",),
}

# Корзины скорости Jamendo в темпах. Границы приблизительные — это и
# есть причина, по которой темп потом меряется, а не берётся отсюда.
SPEED_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("verylow", 0.0, 70.0),
    ("low", 70.0, 100.0),
    ("medium", 100.0, 130.0),
    ("high", 130.0, 160.0),
    ("veryhigh", 160.0, 999.0),
)


def speed_for_bpm(lo: float | None, hi: float | None) -> str | None:
    """Корзина скорости, накрывающая запрошенный диапазон темпа.

    Возвращает None, если диапазон шире одной корзины: тогда сужать
    нечем и фильтровать будем только измерением. Никогда не возвращает
    корзину, которая ОТРЕЗАЛА бы часть запрошенного диапазона — лучше
    скачать лишнего, чем молча не найти то, что просили."""
    if lo is None and hi is None:
        return None
    a = float(lo) if lo is not None else 0.0
    b = float(hi) if hi is not None else 999.0
    if b < a:
        a, b = b, a
    covering = [name for name, x, y in SPEED_BUCKETS if x <= a and b <= y]
    return covering[0] if len(covering) == 1 else None


def tags_for(genre: str | None, extra: str | None = None) -> list[str]:
    """Теги запроса: наш жанр плюс то, что диджей дописал руками."""
    out: list[str] = []
    if genre:
        out.extend(GENRE_TAGS.get(str(genre).strip().lower(), (str(genre).strip().lower(),)))
    for word in re.split(r"[\s,;]+", str(extra or "")):
        w = word.strip().lower()
        if w and w not in out:
            out.append(w)
    return [t for t in out if t]


# ------------------------------------------------------------- запросы

def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:                         # noqa: BLE001
        raise FetchError(str(exc)) from exc


def normalize_item(raw: dict) -> dict:
    """Ответ Jamendo -> запись, понятная остальному DARAVE.

    Лицензия тащится в карточку трека НЕ для красоты: CC-BY требует
    указать автора, а CC-BY-NC запрещает коммерческое использование. Без
    этого поля диджей не знает, можно ли играть трек на платной
    вечеринке, и узнаёт это не от нас."""
    mi = raw.get("musicinfo") or {}
    tags = mi.get("tags") or {}
    return {
        "id": str(raw.get("id") or ""),
        "artist": raw.get("artist_name") or None,
        "title": raw.get("name") or None,
        "duration_seconds": float(raw.get("duration") or 0.0) or None,
        "album": raw.get("album_name") or None,
        "license": raw.get("license_ccurl") or None,
        "downloadable": bool(raw.get("audiodownload_allowed")),
        "download_url": raw.get("audiodownload") or None,
        "page_url": raw.get("shareurl") or raw.get("shorturl") or None,
        "tags": [t for t in (tags.get("genres") or []) + (tags.get("vartags") or []) if t],
        "speed": mi.get("speed") or None,
        "source": "jamendo",
    }


def search(client_id: str, tags: list[str] | None = None, speed: str | None = None,
           limit: int = 40, offset: int = 0, order: str = "popularity_total",
           fuzzy: bool = True, allow_nc: bool = True) -> list[dict]:
    """Поиск треков. Возвращает нормализованные записи.

    fuzzy=True — теги по ИЛИ: «drumnbass ИЛИ jungle ИЛИ breakbeat». По И
    (fuzzy=False) выдача почти всегда пустая: в каталоге редко вешают на
    трек сразу три жанровых тега, и запрос выглядел бы сломанным."""
    if not client_id:
        raise FetchError("нет client_id Jamendo. Бесплатный: developer.jamendo.com")
    params = {
        "client_id": client_id, "format": "json",
        "limit": str(max(1, min(int(limit), 200))), "offset": str(max(0, int(offset))),
        "include": "musicinfo licenses", "order": order,
        # Просим у каталога только то, что вообще можно скачать: иначе
        # половина выдачи — треки, которые видно и нельзя взять.
        "audiodownload_allowed": "true",
    }
    if tags:
        params["fuzzytags" if fuzzy else "tags"] = "+".join(tags)
    if speed:
        params["speed"] = speed
    if not allow_nc:
        # Запрет коммерческого использования снимаем только по просьбе:
        # играть на платной вечеринке под CC-BY-NC нельзя.
        params["ccnc"] = "false"
    data = _throttled_get(f"{JAMENDO_ROOT}/tracks/?{urllib.parse.urlencode(params)}")
    head = data.get("headers") or {}
    if head.get("status") != "success":
        raise FetchError(f"Jamendo: {head.get('error_message') or head.get('status')}")
    return [normalize_item(r) for r in (data.get("results") or [])]


def _throttled_get(url: str) -> dict:
    _throttle()
    return _get_json(url)


# ------------------------------------------------------------ загрузка

def safe_filename(item: dict) -> str:
    """Имя файла, из которого DARAVE потом разберёт исполнителя и название.

    Формат «Исполнитель - Название.mp3» выбран не ради красоты: ровно его
    разбирает genre.artist_title_from_name, и трек из загрузки попадает в
    библиотеку с теми же полями, что и трек из папки диджея."""
    who = re.sub(r"[^\w\s.,'()\[\]&-]", "", str(item.get("artist") or "Unknown")).strip()
    what = re.sub(r"[^\w\s.,'()\[\]&-]", "", str(item.get("title") or item.get("id") or "track")).strip()
    name = f"{who} - {what}"[:120].strip(" .") or f"jamendo-{item.get('id')}"
    return name + ".mp3"


def download(item: dict, dest_dir: str, timeout: float = 120.0) -> dict:
    """Скачать один трек. Уже лежащий на месте файл не перекачиваем."""
    if not item.get("downloadable") or not item.get("download_url"):
        return {"ok": False, "error": "автор не разрешил скачивание"}
    dest = Path(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"не смог создать папку: {exc}"}
    target = dest / safe_filename(item)
    if target.exists() and target.stat().st_size > 0:
        return {"ok": True, "path": str(target), "cached": True,
                "bytes": target.stat().st_size}
    # Пишем во временное имя и переименовываем в конце: иначе оборванная
    # загрузка оставляет в папке битый .mp3, и он поедет в библиотеку как
    # настоящий трек. Папка-приёмник ждёт, пока файл перестанет расти, —
    # временное имя вне списка расширений делает это надёжным.
    tmp = target.with_suffix(".part")
    req = urllib.request.Request(str(item["download_url"]),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
    except Exception as exc:                         # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": str(exc)[:200]}
    size = tmp.stat().st_size
    if size < 20000:
        # Каталог иногда отдаёт страницу ошибки с кодом 200. Двадцать
        # килобайт — это заведомо не трек, а такой «трек» в библиотеке
        # выглядел бы сбоем анализатора, а не сбоем загрузки.
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"пришло {size} байт — это не трек"}
    tmp.replace(target)
    return {"ok": True, "path": str(target), "cached": False, "bytes": size}


def plan_fetch(want: int, bpm_lo: float | None, bpm_hi: float | None) -> dict:
    """Сколько кандидатов запросить, чтобы набрать `want` треков.

    Отдельной функцией, чтобы цифра была ВИДНА диджею до начала:
    «нужно 40, скачаю до 100, лишнее удалю» — это разный расход трафика и
    разное время, и узнавать об этом постфактум неприятно."""
    want = max(1, int(want))
    narrowing = bpm_lo is not None or bpm_hi is not None
    candidates = int(round(want * BPM_OVERFETCH)) if narrowing else want
    return {"want": want, "candidates": min(candidates, 200),
            "speed": speed_for_bpm(bpm_lo, bpm_hi),
            "note": (f"темпа в каталоге нет — скачаю до {candidates} и померяю сам, "
                     f"не попавшее в диапазон удалю" if narrowing
                     else "диапазон темпа не задан — меряю, но ничего не отбрасываю")}


def in_range(bpm: float | None, lo: float | None, hi: float | None) -> bool:
    """Попадает ли измеренный темп в запрошенный диапазон.

    Точное значение («хочу 174») — это НЕ равенство: анализатор отдаёт
    173.97, и строгое сравнение отбросило бы всё. Точный запрос приходит
    сюда уже как узкий диапазон (см. library_query.parse_bpm)."""
    if bpm is None:
        return False
    v = float(bpm)
    if lo is not None and v < float(lo):
        return False
    if hi is not None and v > float(hi):
        return False
    return True


# ------------------------------------------------------- папка-приёмник

def inbox_scan(folder: str, known: set[str] | None = None) -> list[str]:
    """Что нового появилось в папке-приёмнике.

    Отдаёт только файлы, которые ДОСКАЧАЛИСЬ. Проверка на «файл ещё
    растёт» здесь не по паранойе: копирование флака с флешки идёт
    секунды, а разбор половины файла даёт мусорный темп и мусорную
    структуру — и этот мусор потом лежит в библиотеке как настоящий
    анализ, потому что второй раз трек уже не разберут (отпечаток
    совпадает)."""
    root = Path(folder)
    if not root.exists():
        return []
    known = known or set()
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if str(p) in known:
            continue
        out.append(str(p))
    return out


def settled(paths: list[str], sizes: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    """Разделить «дописались» и «ещё растут», сравнив размер с прошлым разом.

    Возвращает (готовые, новый снимок размеров). Файл считается готовым,
    когда его размер не изменился между двумя опросами: это дёшево,
    работает на любой файловой системе и не требует открывать файл на
    запись, чтобы проверить блокировку."""
    ready, snapshot = [], {}
    for p in paths:
        try:
            size = Path(p).stat().st_size
        except OSError:
            continue                                  # исчез между опросами
        snapshot[p] = size
        if size > 0 and sizes.get(p) == size:
            ready.append(p)
    return ready, snapshot


# ------------------------------------------------------------------ CLI

def _check(client_id: str | None, genre: str, want: int) -> int:
    """Проверка ключа и живой выдачи каталога.

    Существует потому, что проверить ключ можно только оттуда, где есть
    выход в интернет. У сборщика этого проекта его нет, а разбор ответа
    без живого ответа не проверишь — поэтому команда отдаётся диджею, и
    она печатает не «ОК», а то, ЧТО именно пришло: ключ бывает верным, а
    выдача пустой, и это разные беды с разными ответами."""
    cid = resolve_client_id(client_id)
    if not cid:
        print("Нет client_id. Бесплатный: developer.jamendo.com")
        print(f"Положите его в {SECRETS_FILE} полем jamendo_client_id "
              "или в переменную JAMENDO_CLIENT_ID.")
        return 1
    print(f"client_id: …{cid[-4:]} ({len(cid)} символов)")
    tags = tags_for(genre)
    print(f"жанр {genre!r} -> теги {tags}")
    try:
        items = search(cid, tags, None, want)
    except FetchError as exc:
        print(f"НЕ ОТВЕТИЛ: {exc}")
        print("Если это 'Your credential is not valid' — ключ неверный или не активирован.")
        return 1
    ok = [i for i in items if i["downloadable"]]
    print(f"нашлось {len(items)}, из них скачиваемых {len(ok)}")
    for i in items[:want]:
        lic = (i["license"] or "").replace("https://creativecommons.org/licenses/", "").strip("/")
        print(f"  {'✓' if i['downloadable'] else '✕'} {str(i['artist'])[:24]:24} — "
              f"{str(i['title'])[:30]:30} {lic or '—':12} {', '.join(i['tags'][:3])}")
    if items and not ok:
        print("\nВыдача есть, но скачивать нельзя ничего: авторы не дали разрешения.")
        print("Это не поломка — попробуйте другой жанр или теги.")
    return 0 if items else 2


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Пополнение библиотеки из каталога Jamendo")
    ap.add_argument("--check", action="store_true",
                    help="проверить ключ и посмотреть, что отдаёт каталог")
    ap.add_argument("--client-id", default=None,
                    help=f"по умолчанию из JAMENDO_CLIENT_ID или {SECRETS_FILE}")
    ap.add_argument("--genre", default="dnb", help="жанр (dnb, house, techno, …)")
    ap.add_argument("--tags", default="", help="дополнительные теги через пробел")
    ap.add_argument("--want", type=int, default=8, help="сколько показать/скачать")
    ap.add_argument("--bpm", default="", help="диапазон темпа, например 170-180")
    ap.add_argument("--dir", default="", help="куда скачивать (без него — только показать)")
    args = ap.parse_args()

    if args.check or not args.dir:
        return _check(args.client_id, args.genre, args.want)

    cid = resolve_client_id(args.client_id)
    if not cid:
        print("Нет client_id — см. --check")
        return 1
    lo = hi = None
    if args.bpm:
        try:
            a, b = str(args.bpm).replace("–", "-").split("-", 1)
            lo, hi = float(a), float(b)
        except ValueError:
            print(f"Не понял диапазон {args.bpm!r}, жду вида 170-180")
            return 1
    plan = plan_fetch(args.want, lo, hi)
    print(f"{plan['note']}")
    items = [i for i in search(cid, tags_for(args.genre, args.tags), plan["speed"],
                               plan["candidates"]) if i["downloadable"]]
    print(f"скачиваемых кандидатов: {len(items)}")
    kept = 0
    for i in items:
        if kept >= plan["want"]:
            break
        r = download(i, args.dir)
        if not r.get("ok"):
            print(f"  ✕ {str(i['title'])[:40]}: {r.get('error')}")
            continue
        kept += 1
        print(f"  ✓ {Path(r['path']).name} ({r['bytes'] / 1e6:.1f} МБ)")
    print(f"\nскачано {kept} в {args.dir}")
    print("Темп здесь НЕ проверяется: меряет его backend при загрузке через интерфейс. "
          "Нажмите «Анализ библиотеки».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
