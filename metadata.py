"""
metadata.py — исполнитель, название и СТИЛЬ из открытых каталогов.

## Зачем

Библиотека знает про трек всё, что можно измерить: темп, тональность,
энергию, структуру, слои. Чего измерить нельзя — это как трек называется
и к какому стилю его относят люди. Поджанр у DARAVE сейчас берётся из
тега, из имени файла или по списку артистов (`genre.ARTIST_SUBGENRE`), и
последний способ работает ровно до первого незнакомого имени.

Два каталога закрывают это и оба бесплатны:

* **MusicBrainz** — канонические исполнитель, название, год, длительность,
  MBID. Ключа не требует вовсе, только честный User-Agent. Ограничение —
  один запрос в секунду, и оно соблюдается здесь, а не «по возможности»:
  за превышение банят по IP.
* **Discogs** — СТИЛИ, и это главное. Для электронной музыки разметка
  Discogs подробнее любой другой открытой: не «Electronic», а «Drum n
  Bass» + «Liquid» / «Neurofunk» / «Jungle» / «Techstep». Ровно тот
  уровень, на котором у DARAVE различаются лестницы фраз и приёмы.
  Поиску Discogs нужен бесплатный персональный токен
  (discogs.com/settings/developers) — без него модуль работает на одном
  MusicBrainz и говорит об этом прямо, а не молча отдаёт пустоту.

## Чего здесь НЕТ и не будет

Скачивания музыки. Ни один из этих каталогов её не отдаёт, и обходить
чужие условия ради файлов модуль не станет. Он отвечает на вопрос «что
это за трек и какого он стиля», а файлы диджей приносит сам — DARAVE
разберёт их, как только они окажутся в папке.

Темпа и тональности здесь тоже нет, и это не недоработка: в MusicBrainz
их не хранят, а в Discogs они попадаются у одной записи из сотни.
Меряем сами — это и точнее, и уже сделано.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

# MusicBrainz требует User-Agent с контактом и банит за его отсутствие.
# Версия и адрес — не украшение, а условие доступа.
USER_AGENT = "DARAVE/1.0 (https://github.com/kam1k88/darave)"

MB_ROOT = "https://musicbrainz.org/ws/2"
DISCOGS_ROOT = "https://api.discogs.com"

# Один запрос в секунду — официальное ограничение MusicBrainz. Discogs
# разрешает 60 в минуту с токеном, то есть тот же порядок.
MB_MIN_INTERVAL = 1.05
DISCOGS_MIN_INTERVAL = 1.05

TIMEOUT = 12.0

_last_call: dict[str, float] = {}


class LookupError_(RuntimeError):
    """Каталог не ответил или ответил не тем. Отдельный тип, чтобы
    вызывающий код мог отличить «не нашли» от «не смогли спросить»."""


def _throttle(key: str, interval: float) -> None:
    prev = _last_call.get(key, 0.0)
    wait = interval - (time.monotonic() - prev)
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.monotonic()


def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:                         # noqa: BLE001
        raise LookupError_(str(exc)) from exc


# --------------------------------------------------- токен Discogs
#
# Токен — это секрет, и в репозитории ему не место: папка DARAVE под git,
# и один `git add -A` отправил бы его в историю навсегда. Поэтому он
# живёт в файле рядом с кодом, который занесён в .gitignore, либо в
# переменной окружения. Порядок поиска — от явного к общему, чтобы
# вызывающий код всегда мог перебить настройку.

SECRETS_FILE = "darave_secrets.json"


def _secrets_paths() -> list:
    """Где искать файл с ключами.

    Из исходников он лежит рядом с кодом. У установленной версии рядом с
    кодом — Program Files, куда пользователь свой ключ не положит; там
    файл живёт в папке данных. Проверяем оба места, папку данных первой:
    если человек правил ключ, он правил именно её."""
    out = []
    try:
        import paths as _p
        out.append(_p.data_dir() / SECRETS_FILE)
    except Exception:
        pass
    out.append(Path(__file__).resolve().parent / SECRETS_FILE)
    return out


def _read_secret(field: str) -> str | None:
    for f in _secrets_paths():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        v = data.get(field)
        if v and str(v).strip():
            return str(v).strip()
    return None


def resolve_discogs_token(explicit: str | None = None) -> str | None:
    """Токен Discogs: аргумент -> переменная окружения -> локальный файл.

    Возвращает None, если токена нет нигде. Это не ошибка: без него
    работает MusicBrainz, а стилей просто не будет — и об этом скажут
    вслух, а не молча отдадут пустоту."""
    import os

    if explicit:
        return str(explicit).strip() or None
    env = os.environ.get("DISCOGS_TOKEN")
    if env:
        return env.strip() or None
    return _read_secret("discogs_token")


# --------------------------------------------------- нормализация имён

# Хвосты, которые мешают совпадению: каталог знает трек как «Summer Rain»,
# а файл называется «Summer Rain (Original Mix)». Убираем их ТОЛЬКО для
# запроса — в библиотеке название остаётся тем, что написано в файле.
_NOISE = re.compile(
    r"\s*[\(\[]\s*(original|extended|radio|club|vip|instrumental|clean|dirty)"
    r"[^\)\]]*[\)\]]\s*", re.I)
# \b обязательна. Без неё «ft» находилось ВНУТРИ слова: «Left in Dust
# (feat. Tyhh)» превращалось в «Le» — «ft» нашлось в «Left», а дальше
# выражение съело всё до конца строки.
_FEAT = re.compile(r"\s*[\(\[]?\s*\b(feat|ft|featuring)\b\.?\s+[^\)\]]*[\)\]]?", re.I)


def clean_title(title: str | None) -> str:
    t = _NOISE.sub(" ", str(title or ""))
    t = _FEAT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_artist(artist: str | None) -> str:
    """Первый исполнитель. «Black Sun Empire, Concord Dawn» -> первый.

    Каталоги хранят совместные работы как отдельные записи со списком
    артистов, и поиск по всей строке целиком чаще всего не находит
    ничего. Первого имени достаточно, чтобы попасть в запись, а
    остальных мы потом прочитаем из ответа."""
    a = _FEAT.sub(" ", str(artist or ""))
    a = re.split(r"\s*(?:,|&|\bvs\.?\b|\bx\b|\bwith\b)\s*", a, maxsplit=1)[0]
    return re.sub(r"\s+", " ", a).strip()


# --------------------------------------------------- MusicBrainz

def musicbrainz_lookup(artist: str | None, title: str | None,
                       duration_seconds: float | None = None,
                       limit: int = 5) -> dict | None:
    """Канонические исполнитель, название, год. Ключ не нужен."""
    a, t = clean_artist(artist), clean_title(title)
    if not t:
        return None
    terms = [f'recording:"{t}"']
    if a:
        terms.append(f'artist:"{a}"')
    q = urllib.parse.quote(" AND ".join(terms))
    url = f"{MB_ROOT}/recording?query={q}&fmt=json&limit={int(limit)}"
    _throttle("mb", MB_MIN_INTERVAL)
    data = _get_json(url)
    return _pick_recording(data.get("recordings") or [], duration_seconds)


def _pick_recording(recordings: list, duration_seconds: float | None) -> dict | None:
    """Лучшая запись из выдачи.

    Оценка каталога (`score`) — про совпадение текста, а у нас есть то,
    чего каталог не знает: ДЛИТЕЛЬНОСТЬ файла. Для драм-н-бейса это
    сильный признак — расхождение в пару секунд означает тот же микс, а
    в минуту — другой (радиоверсия, VIP, ремикс). Поэтому длительность
    и решает, когда текстовые оценки близки."""
    best = None
    for r in recordings:
        score = float(r.get("score") or 0) / 100.0
        if duration_seconds and r.get("length"):
            diff = abs(float(r["length"]) / 1000.0 - float(duration_seconds))
            # ±3 секунды — тот же микс; больше минуты — точно другой.
            score *= 1.25 if diff <= 3.0 else (0.9 if diff <= 15.0 else 0.4)
        if best is None or score > best[0]:
            best = (score, r)
    if best is None:
        return None
    score, r = best
    credits = r.get("artist-credit") or []
    artists = [c.get("name") for c in credits if isinstance(c, dict) and c.get("name")]
    releases = r.get("releases") or []
    year = None
    for rel in releases:
        d = str(rel.get("date") or "")
        if len(d) >= 4 and d[:4].isdigit():
            year = int(d[:4])
            break
    return {
        "source": "musicbrainz",
        "mbid": r.get("id"),
        "artist": ", ".join(artists) or None,
        "title": r.get("title"),
        "length_seconds": (round(float(r["length"]) / 1000.0, 1)
                           if r.get("length") else None),
        "year": year,
        "release": (releases[0].get("title") if releases else None),
        "confidence": round(min(1.0, score), 3),
    }


# --------------------------------------------------- Discogs

def discogs_search(artist: str | None, title: str | None, token: str | None,
                   limit: int = 5) -> dict | None:
    """Стили с Discogs. Нужен бесплатный персональный токен.

    Без токена возвращает None и НЕ делает запрос: Discogs ответил бы
    401, и отличить «не нашли» от «не спросили» стало бы нельзя."""
    if not token:
        return None
    a, t = clean_artist(artist), clean_title(title)
    if not t:
        return None
    params = {"track": t, "type": "release", "per_page": str(int(limit)),
              "token": token}
    if a:
        params["artist"] = a
    url = f"{DISCOGS_ROOT}/database/search?{urllib.parse.urlencode(params)}"
    _throttle("discogs", DISCOGS_MIN_INTERVAL)
    data = _get_json(url)
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    return {
        "source": "discogs",
        "discogs_id": r.get("id"),
        "year": (int(r["year"]) if str(r.get("year") or "").isdigit() else None),
        "label": ((r.get("label") or [None])[0] if isinstance(r.get("label"), list)
                  else r.get("label")),
        "genres": [str(x) for x in (r.get("genre") or [])],
        "styles": [str(x) for x in (r.get("style") or [])],
        "release": r.get("title"),
    }


# --------------------------------------------------- стиль -> наш поджанр

# Как стили Discogs ложатся на то, чем DARAVE различает фразы и приёмы.
# Слева — как пишет Discogs, справа — наш канон (`genre.SUBGENRES`).
# Порядок важен: проверяем сверху вниз и берём первое совпадение, потому
# что у релиза стилей обычно несколько и они не равнозначны —
# «Neurofunk» говорит о треке больше, чем «Drum n Bass» рядом с ним.
STYLE_TO_SUBGENRE: tuple[tuple[str, str], ...] = (
    ("neurofunk", "neurofunk"),
    ("techstep", "neurofunk"),
    ("darkstep", "neurofunk"),
    ("liquid funk", "liquid"),
    ("liquid", "liquid"),
    ("atmospheric", "liquid"),
    ("jump up", "jumpup"),
    ("jungle", "jungle"),
    ("ragga", "jungle"),
    ("halftime", "halftime"),
    ("drumfunk", "jungle"),
    ("drum n bass", "dnb"),
    ("drum and bass", "dnb"),
    ("breakcore", "jungle"),
    ("speed garage", "speed garage"),
    ("bassline", "bassline"),
    ("uk garage", "garage"),
    ("2-step", "garage"),
    ("garage house", "garage"),
    ("dubstep", "dubstep"),
    ("big room", "bigroom"),
    ("electro house", "house"),
    ("tech house", "house"),
    ("deep house", "house"),
    ("progressive house", "house"),
    ("house", "house"),
    ("techno", "techno"),
    ("trance", "trance"),
    ("breaks", "breaks"),
    ("breakbeat", "breaks"),
)


def subgenre_from_styles(styles: list | None, genres: list | None = None) -> str | None:
    """Наш поджанр из стилей Discogs. None — если ничего не узнали."""
    hay = [str(x).lower() for x in (styles or [])] + \
          [str(x).lower() for x in (genres or [])]
    for needle, canon in STYLE_TO_SUBGENRE:
        for h in hay:
            if needle in h:
                return canon
    return None


# --------------------------------------------------- общий вход

def lookup(artist: str | None, title: str | None,
           duration_seconds: float | None = None,
           discogs_token: str | None = None) -> dict:
    """Всё, что знают о треке оба каталога, одним словарём.

    Ни одна ошибка сети не считается фатальной: каталог — это украшение
    библиотеки, а не её условие. Что не ответило, то попадает в `errors`,
    и трек остаётся с тем, что у него было."""
    out: dict = {"artist": None, "title": None, "year": None, "label": None,
                 "genres": [], "styles": [], "subgenre": None,
                 "mbid": None, "discogs_id": None, "release": None,
                 "sources": [], "errors": []}
    try:
        mb = musicbrainz_lookup(artist, title, duration_seconds)
    except LookupError_ as exc:
        mb = None
        out["errors"].append(f"musicbrainz: {exc}")
    if mb:
        out.update({k: mb.get(k) for k in ("artist", "title", "year",
                                           "mbid", "release")})
        out["confidence"] = mb.get("confidence")
        out["sources"].append("musicbrainz")

    token = resolve_discogs_token(discogs_token)
    try:
        dc = discogs_search(artist, title, token)
    except LookupError_ as exc:
        dc = None
        out["errors"].append(f"discogs: {exc}")
    if dc:
        out["genres"] = dc.get("genres") or []
        out["styles"] = dc.get("styles") or []
        out["discogs_id"] = dc.get("discogs_id")
        out["label"] = dc.get("label")
        out["year"] = out["year"] or dc.get("year")
        out["release"] = out["release"] or dc.get("release")
        out["sources"].append("discogs")
    elif not token:
        out["errors"].append(
            "discogs: нет токена — стили не спрашивали. Бесплатный токен: "
            "discogs.com/settings/developers")

    out["subgenre"] = subgenre_from_styles(out["styles"], out["genres"])
    return out


def enrich_track(track: dict, discogs_token: str | None = None) -> dict:
    """Один трек библиотеки -> что про него говорят каталоги.

    Берёт исполнителя и название из самой записи (они там уже есть
    отдельными полями) и не разбирает имя файла заново."""
    return lookup(track.get("artist"), track.get("title"),
                  track.get("duration_seconds"),
                  resolve_discogs_token(discogs_token))


def enrich_library(tracks: list[dict], discogs_token: str | None = None,
                   only_missing: bool = True, log=print, progress=None) -> dict:
    """Проход по библиотеке. Медленный по построению — один запрос в
    секунду, — поэтому запускается отдельно, а не внутри сканирования.

    progress(i, total) зовётся на каждом треке, а не только на найденных:
    именно из-за секунды на запрос этот шаг и надо показывать полосой.
    Через log это не сделать — он молчит на треках, которых в каталогах
    нет, а таких как раз большинство, и полоса стояла бы на месте."""
    done = found = skipped = 0
    for i, t in enumerate(tracks, 1):
        if progress:
            progress(i, len(tracks))
        if only_missing and (t.get("meta") or {}).get("sources"):
            skipped += 1
            continue
        meta = enrich_track(t, discogs_token)
        t["meta"] = meta
        done += 1
        if meta.get("sources"):
            found += 1
            log(f"[{i}/{len(tracks)}] {str(t.get('title'))[:40]}: "
                f"{meta.get('subgenre') or '—'} "
                f"({', '.join(meta['styles'][:3]) or 'стилей нет'})")
    return {"checked": done, "found": found, "skipped": skipped,
            "total": len(tracks)}


def search_catalog(query: str, kind: str = "release", limit: int = 10,
                   discogs_token: str | None = None) -> dict:
    """Поиск по каталогам — то, что стоит за строкой поиска в библиотеке.

    ВАЖНО про то, чего этот поиск НЕ умеет: искать по темпу и
    тональности. В MusicBrainz их не хранят, в Discogs они попадаются у
    считанных записей. Поэтому запрос вида «ликвид на 174 в 8A»
    честно распадается надвое: стиль ищется здесь, а темп и тональность
    DARAVE померяет сам, когда файл окажется в папке.

    Файлы каталоги не отдают — только сведения о них."""
    out: dict = {"query": query, "items": [], "errors": [],
                 "note": ("каталоги отдают сведения о треках, но не сами "
                          "файлы; темп и тональность DARAVE меряет сам")}
    discogs_token = resolve_discogs_token(discogs_token)
    if discogs_token:
        params = {"q": query, "type": kind, "per_page": str(int(limit)),
                  "token": discogs_token}
        try:
            _throttle("discogs", DISCOGS_MIN_INTERVAL)
            data = _get_json(
                f"{DISCOGS_ROOT}/database/search?{urllib.parse.urlencode(params)}")
            for r in (data.get("results") or [])[:limit]:
                out["items"].append({
                    "source": "discogs",
                    "title": r.get("title"),
                    "year": r.get("year"),
                    "styles": r.get("style") or [],
                    "genres": r.get("genre") or [],
                    "label": ((r.get("label") or [None])[0]
                              if isinstance(r.get("label"), list) else r.get("label")),
                    "url": (f"https://www.discogs.com{r['uri']}"
                            if r.get("uri") else None),
                    "subgenre": subgenre_from_styles(r.get("style"), r.get("genre")),
                })
        except LookupError_ as exc:
            out["errors"].append(f"discogs: {exc}")
    else:
        out["errors"].append(
            "discogs: нет токена. Бесплатный: discogs.com/settings/developers")

    try:
        _throttle("mb", MB_MIN_INTERVAL)
        q = urllib.parse.quote(query)
        data = _get_json(f"{MB_ROOT}/recording?query={q}&fmt=json&limit={int(limit)}")
        for r in (data.get("recordings") or [])[:limit]:
            credits = r.get("artist-credit") or []
            out["items"].append({
                "source": "musicbrainz",
                "artist": ", ".join(c.get("name") for c in credits
                                    if isinstance(c, dict) and c.get("name")) or None,
                "title": r.get("title"),
                "length_seconds": (round(float(r["length"]) / 1000.0, 1)
                                   if r.get("length") else None),
                "url": (f"https://musicbrainz.org/recording/{r['id']}"
                        if r.get("id") else None),
            })
    except LookupError_ as exc:
        out["errors"].append(f"musicbrainz: {exc}")
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="DARAVE — исполнитель, название и стиль из MusicBrainz и Discogs")
    ap.add_argument("--artist")
    ap.add_argument("--title")
    ap.add_argument("--search", help="свободный запрос по каталогам")
    ap.add_argument("--check", action="store_true",
                    help="проверить связь и токен одной командой")
    ap.add_argument("--seconds", type=float, default=None,
                    help="длительность файла — помогает выбрать нужный микс")
    ap.add_argument("--discogs-token", default=None, dest="token",
                    help=f"по умолчанию берётся из DISCOGS_TOKEN или {SECRETS_FILE}")
    args = ap.parse_args()

    if args.check:
        # Проверка связи одной командой. Нужна потому, что «ничего не
        # нашлось» и «не достучались» снаружи выглядят одинаково, а чинятся
        # по-разному.
        tok = resolve_discogs_token(args.token)
        print(f"токен Discogs: {'есть' if tok else 'НЕТ'}"
              + (f" ({tok[:4]}…{tok[-4:]})" if tok else
                 f" — положите в {SECRETS_FILE} или DISCOGS_TOKEN"))
        try:
            _throttle("mb", MB_MIN_INTERVAL)
            d = _get_json(f"{MB_ROOT}/recording?query=recording:%22Summer%20Rain%22"
                          f"&fmt=json&limit=1")
            print(f"MusicBrainz: ОК, записей в ответе {len(d.get('recordings') or [])}")
        except LookupError_ as exc:
            print(f"MusicBrainz: НЕ ОТВЕТИЛ — {exc}")
        if tok:
            try:
                r = discogs_search("Hybrid Minds", "Summer Rain", tok)
                if r:
                    print(f"Discogs: ОК — стили {r.get('styles')}, "
                          f"поджанр {subgenre_from_styles(r.get('styles'), r.get('genres'))}")
                else:
                    print("Discogs: ответил, но ничего не нашёл по пробному запросу")
            except LookupError_ as exc:
                print(f"Discogs: НЕ ОТВЕТИЛ — {exc}"
                      + ("  (401 значит, что токен неверный или отозван)"
                         if "401" in str(exc) else ""))
        return 0

    if args.search:
        print(json.dumps(search_catalog(args.search, discogs_token=args.token),
                         ensure_ascii=False, indent=1))
        return 0
    if not (args.artist or args.title):
        print("Укажите --artist/--title или --search")
        return 1
    print(json.dumps(lookup(args.artist, args.title, args.seconds, args.token),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
