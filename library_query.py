"""
library_query.py — поиск и отбор в библиотеке: жанр, темп, тональность, имя.

## Зачем отдельным модулем

Фильтр библиотеки нужен в трёх местах сразу: строка поиска в UI, отбор
кандидатов на загрузку («докачай ещё двадцать таких же») и удаление
пачкой («убрать всё, что короче минуты»). Раскопировать разбор «100-135»
по трём местам — это три чуть разных поведения и три отдельных бага;
здесь оно одно, и его видно тестами.

## Почему точный темп — это диапазон

«Хочу 174» строгим сравнением не работает НИКОГДА: анализатор отдаёт
173.97, и равенство отбрасывает всё подряд. Поэтому точный запрос
превращается в узкий диапазон (±BPM_EXACT_TOLERANCE), и «174» находит
173.97 — как и ожидает человек, который это набрал.

## Почему тональность сравнивается буквально

Фильтр «8A» отдаёт треки в 8A, а не «совместимые с 8A». Совместимость —
это отдельное знание (колесо Камелота, см. music_theory), и оно уже
работает при подборе пары. Смешивать их в одной строке поиска нельзя:
диджей, ищущий «что у меня есть в 8A», получил бы половину библиотеки и
решил бы, что фильтр сломан.
"""
from __future__ import annotations

import re

# «174» — это 173..175, а не равенство. См. заголовок модуля.
BPM_EXACT_TOLERANCE = 1.0

# Тональности: и по-камелотовски (8A), и по-нотному (Am, F#m, C).
_PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLAT_TO_SHARP = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}
_CAMELOT_MAJOR = ("8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B")
_CAMELOT_MINOR = ("5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A")

_CAMELOT_RE = re.compile(r"^(1[0-2]|[1-9])\s*([ABab])$")
_NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)\s*(m|min|minor|maj|major)?$")
_RANGE_RE = re.compile(r"^(\d{2,3}(?:[.,]\d+)?)\s*(?:-|–|—|\.\.|to)\s*(\d{2,3}(?:[.,]\d+)?)$")
_PLUS_RE = re.compile(r"^(\d{2,3}(?:[.,]\d+)?)\s*\+$")
_MINUS_RE = re.compile(r"^(\d{2,3}(?:[.,]\d+)?)\s*-$")
_EXACT_RE = re.compile(r"^(\d{2,3}(?:[.,]\d+)?)$")

# Нижняя граница «на что это вообще похоже как темп». Ниже 40 — это уже
# не BPM, а номер трека в имени файла, и принимать его за темп нельзя.
BPM_MIN, BPM_MAX = 40.0, 300.0


def normalize_key(token: str | None) -> str | None:
    """Тональность в камелот: '8a' -> '8A', 'Am' -> '8A', 'C' -> '8B'.

    Возвращает None, если это не тональность — так вызывающий отличает
    «не нашёл» от «не тональность» и не превращает слово 'Am' из названия
    трека в фильтр."""
    if not token:
        return None
    t = str(token).strip()
    m = _CAMELOT_RE.match(t)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m = _NOTE_RE.match(t)
    if not m:
        return None
    pitch = (m.group(1).upper() + (m.group(2) or "")).upper()
    pitch = _FLAT_TO_SHARP.get(pitch, pitch)
    pitch = pitch if pitch in _PITCHES else None
    if pitch is None:
        return None
    idx = _PITCHES.index(pitch)
    mode = (m.group(3) or "").lower()
    minor = mode.startswith("m") and not mode.startswith("maj")
    return _CAMELOT_MINOR[idx] if minor else _CAMELOT_MAJOR[idx]


def parse_bpm(token: str | None) -> tuple[float, float] | None:
    """Темп из строки: '174' -> (173,175), '100-135' -> (100,135),
    '170+' -> (170, inf), '140-' -> (0, 140).

    Возвращает None, если это не про темп."""
    if token is None:
        return None
    t = str(token).strip().replace(",", ".")
    m = _RANGE_RE.match(t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return (min(a, b), max(a, b))
    m = _PLUS_RE.match(t)
    if m:
        return (float(m.group(1)), BPM_MAX)
    m = _MINUS_RE.match(t)
    if m:
        return (BPM_MIN, float(m.group(1)))
    m = _EXACT_RE.match(t)
    if m:
        v = float(m.group(1))
        if not (BPM_MIN <= v <= BPM_MAX):
            return None
        return (v - BPM_EXACT_TOLERANCE, v + BPM_EXACT_TOLERANCE)
    return None


# Слова-флаги, которые значат не жанр, а свойство трека.
FLAG_WORDS = {
    "vocal": ("vocal", True), "вокал": ("vocal", True),
    "novocal": ("vocal", False), "инструментал": ("vocal", False),
    "instrumental": ("vocal", False),
    "stems": ("stems", True), "стемы": ("stems", True),
    "nostems": ("stems", False), "безстемов": ("stems", False),
}


def parse(text: str | None) -> dict:
    """Свободная строка -> фильтры. 'dnb 170-180 8A liquid vocal'.

    Всё, что не опознано как темп, тональность или флаг, складывается в
    текстовый поиск — он идёт по имени, исполнителю, названию И жанру
    сразу. Так строка «shadow» находит трек по имени, а «neurofunk» — по
    жанру, и диджею не надо помнить, какое поле он ищет."""
    out: dict = {"bpm": None, "key": None, "text": [], "vocal": None, "stems": None}
    for raw in re.split(r"[\s,;]+", str(text or "")):
        tok = raw.strip()
        if not tok:
            continue
        low = tok.lower()
        if low in FLAG_WORDS:
            field, value = FLAG_WORDS[low]
            out[field] = value
            continue
        rng = parse_bpm(tok)
        if rng is not None:
            out["bpm"] = rng
            continue
        # Тональность проверяем ПОСЛЕ темпа: '8A' не число, но и не
        # название. А вот одиночные буквы вроде 'a' в тональность не
        # превращаем — иначе поиск по слову ломается на первой же букве.
        if len(tok) >= 2:
            key = normalize_key(tok)
            if key is not None:
                out["key"] = key
                continue
        out["text"].append(low)
    return out


def _haystack(track: dict) -> str:
    """Всё текстовое про трек одной строкой — по ней и ищем."""
    tags = track.get("tags") or {}
    g = tags.get("genre") if isinstance(tags, dict) else None
    parts = [track.get("name"), track.get("artist"), track.get("title"),
             track.get("genre"), track.get("subgenre"), track.get("path")]
    if isinstance(g, dict):
        parts += [g.get("genre"), g.get("subgenre")]
    meta = track.get("meta")
    if isinstance(meta, dict):
        parts.append(meta.get("subgenre"))
        parts += list(meta.get("styles") or [])
    return " ".join(str(p) for p in parts if p).lower()


def match(track: dict, filters: dict) -> bool:
    """Подходит ли трек под фильтры. Пустой фильтр пропускает всё."""
    rng = filters.get("bpm")
    if rng:
        bpm = track.get("bpm")
        if not bpm or not (float(rng[0]) <= float(bpm) <= float(rng[1])):
            return False
    want_key = filters.get("key")
    if want_key:
        have = track.get("camelot") or normalize_key(track.get("key"))
        if not have or str(have).upper() != want_key:
            return False
    if filters.get("vocal") is not None:
        if bool(track.get("vocal")) is not bool(filters["vocal"]):
            return False
    if filters.get("stems") is not None:
        if bool(track.get("stems")) is not bool(filters["stems"]):
            return False
    words = filters.get("text") or []
    if words:
        hay = _haystack(track)
        if not all(w in hay for w in words):
            return False
    return True


def filter_tracks(tracks, query: str | None = None, **explicit) -> dict:
    """Отобрать треки. query — свободная строка, explicit перекрывает её.

    Возвращает и сами треки, и разобранные фильтры: интерфейсу надо
    показать, КАК он понял запрос. Иначе «почему ничего не нашлось» —
    это гадание, а не вопрос с ответом."""
    filters = parse(query)
    for k, v in explicit.items():
        if v is not None:
            filters[k] = v
    items = [t for t in (tracks or []) if match(t, filters)]
    return {"tracks": items, "filters": filters,
            "found": len(items), "total": len(tracks or [])}


def describe(filters: dict) -> str:
    """Фильтры человеческим текстом — то, что показывается под строкой поиска."""
    bits = []
    rng = filters.get("bpm")
    if rng:
        lo, hi = rng
        bits.append(f"темп {lo:g}-{hi:g}" if hi - lo > 2 * BPM_EXACT_TOLERANCE
                    else f"темп около {(lo + hi) / 2:g}")
    if filters.get("key"):
        bits.append(f"тональность {filters['key']}")
    if filters.get("vocal") is not None:
        bits.append("с вокалом" if filters["vocal"] else "без вокала")
    if filters.get("stems") is not None:
        bits.append("со слоями" if filters["stems"] else "без слоёв")
    if filters.get("text"):
        bits.append("слова: " + ", ".join(filters["text"]))
    return "; ".join(bits) or "без фильтров"
