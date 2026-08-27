"""
Темповые отношения: половинный/двойной счёт как ОДНО семейство.

Проблема, которую это решает. Драм-н-бейс считают и как 172, и как 86 —
это один и тот же трек, просто доля выбрана вдвое реже. Анализаторы
(и librosa, и сам Mixxx) регулярно выдают то одно, то другое. Пока каждый
трек «нормализовали» в фиксированное окно [90,180), возникали два изъяна:

  1. Граница окна РАЗРЫВАЛА семейство. 85 -> 170 (удвоили), а 95
     оставалось 95. Два трека, отличающиеся на 12%, оказывались «170 и 95»
     — то есть якобы несовместимыми вдвое, хотя это соседний темп.
  2. Абсолютное окно ничего не знает о конкретной библиотеке. Для
     хип-хоп-сета центр 95, для DnB — 174; одно окно на всех неверно.

Правильная модель — ОТНОСИТЕЛЬНАЯ. У пары треков ищем множитель из
{1/4, 1/3, 1/2, 1, 2, 3, 4}, при котором темпы сходятся ближе всего.
Сошлись в пределах допуска — темпы совместимы, и мы знаем, играть ли
второй трек в половинном/двойном счёте. Не сошлись ни при каком — вот
это и есть настоящая несовместимость.

Для отображения и для энергодуги библиотека приводится к своему
СОБСТВЕННОМУ центру (медиане доминирующего семейства), а не к [90,180).
"""
from __future__ import annotations

import math
from collections import Counter

# ТОЛЬКО степени двойки — и это принципиально. Половинный/двойной счёт не
# требует менять скорость воспроизведения: трек звучит ровно так же, просто
# долю считают вдвое реже или чаще. А вот отношение 3/2 (174 против 116 —
# тоже встречается в DnB) требует изменить скорость на 50%, чего питч-фейдер
# не даёт и на слух это уже другой трек. Такие пары честно считаем
# несовместимыми, а не делаем вид, что их можно свести.
RATIOS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)

# Насколько темпы должны сойтись после умножения, чтобы считать их
# совместимыми. 6% — примерно предел того, что вытягивает питч-фейдер
# Mixxx (±8% по умолчанию) с запасом на неточность анализа.
# Сколько процентов расхождения ещё считается «один темп».
# Было 6% — при таком допуске 112 и 118 объявлялись совпадающими, хотя
# это 5.4%: питч-фейдер такое вытянет, но слышно, что трек ускорили, и в
# раскладке подпись «темпы совпадают» просто врала. 4% — примерно тот
# предел, за которым тембр уезжает заметно.
DEFAULT_TOLERANCE_PCT = 4.0

RATIO_NAMES = {
    0.25: "в четверть счёта",
    0.5: "в половинном счёте",
    1.0: "тот же темп",
    2.0: "в двойном счёте",
    4.0: "в четверном счёте",
}


def relate(bpm_a: float, bpm_b: float,
           tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> dict:
    """Как темп B соотносится с темпом A.

    Возвращает:
      ratio          — множитель, при котором B ближе всего к A
      effective_bpm  — темп B, приведённый к счёту A (bpm_b * ratio)
      error_pct      — насколько не сошлось после приведения, в процентах
      compatible     — уложились ли в допуск
      label          — человекочитаемо ("в половинном счёте")
      needs_recount  — нужно ли пересчитывать долю (ratio != 1)
    """
    if bpm_a <= 0 or bpm_b <= 0:
        return {"ratio": 1.0, "effective_bpm": bpm_b, "error_pct": 100.0,
                "compatible": False, "label": "темп неизвестен",
                "needs_recount": False}

    best = min(RATIOS, key=lambda r: abs(bpm_b * r - bpm_a) / bpm_a)
    effective = bpm_b * best
    error = abs(effective - bpm_a) / bpm_a * 100.0
    return {
        "ratio": best,
        "effective_bpm": round(effective, 1),
        "error_pct": round(error, 2),
        "compatible": error <= tolerance_pct,
        "label": RATIO_NAMES.get(best, f"x{best:g}"),
        "needs_recount": abs(best - 1.0) > 1e-9,
    }


def same_family(bpm_a: float, bpm_b: float,
                tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> bool:
    """Один ли это темп с точностью до половинного/двойного счёта.
    70 и 140 — да. 105 и 172 — нет (отношение 1.64, ни на что не похоже)."""
    return relate(bpm_a, bpm_b, tolerance_pct)["compatible"]


# Полоса «темпа исполнения»: как диджей реально считает танцевальную
# музыку. Нужна как ПРИОР — из одних данных октаву не выбрать: 86 и 172
# описывают библиотеку одинаково хорошо. Приор прикладывается ОДИН раз к
# центру библиотеки, а не к каждому треку, поэтому семейство не режет.
PERFORMANCE_BAND = (100.0, 200.0)

# Насколько близко к октаве центра должен лечь трек, чтобы счесть, что
# анализатор ошибся октавой. Дальше — это просто ДРУГОЙ темп, и трогать
# его нельзя: 117 в библиотеке с центром 172 — не «недосчитанные 234», а
# самостоятельный темп.
OCTAVE_SNAP_PCT = 15.0


def fold_into_band(bpm: float, low: float = PERFORMANCE_BAND[0],
                   high: float = PERFORMANCE_BAND[1]) -> float:
    if bpm <= 0:
        return bpm
    guard = 0
    while bpm < low and guard < 8:
        bpm *= 2
        guard += 1
    while bpm >= high and guard < 16:
        bpm /= 2
        guard += 1
    return bpm


def nearest_octave(bpm: float, center: float) -> float:
    """Октава bpm, ближайшая к center в логарифмической шкале."""
    if bpm <= 0 or center <= 0:
        return bpm
    return bpm * (2.0 ** round(math.log2(center / bpm)))


def fold_to_center(bpm: float, center: float,
                   snap_pct: float = OCTAVE_SNAP_PCT) -> float:
    """Приводит темп к октаве центра — но ТОЛЬКО если он туда попадает.
    Иначе оставляет как есть (приводя лишь в разумную полосу): трек с
    другим темпом не должен «притягиваться» к центру библиотеки."""
    if bpm <= 0 or center <= 0:
        return bpm
    cand = nearest_octave(bpm, center)
    if abs(cand - center) / center * 100.0 <= snap_pct:
        return cand
    return fold_into_band(bpm)


def library_center(bpms: list[float], fallback: float = 174.0) -> float:
    """Доминирующий темп библиотеки.

    Считаем так: каждый темп сворачиваем во ВСЕ его октавы, голосуем за
    попадание в корзины по 5 BPM, берём самую населённую корзину и внутри
    неё — медиану. Для DnB-библиотеки это даст ~174 независимо от того,
    сколько треков анализатор посчитал как 87."""
    usable = [b for b in bpms if b and b > 0]
    if not usable:
        return fallback

    votes: Counter = Counter()
    for b in usable:
        x = b
        while x < 60:
            x *= 2
        while x >= 200:
            x /= 2
        # трек голосует за свою октаву и за соседние — так половинные и
        # полные варианты одного трека складываются в одну корзину
        for cand in (x / 2, x, x * 2):
            if 60 <= cand < 200:
                votes[int(cand // 5) * 5] += 1
    if not votes:
        return fallback

    top_bucket = max(votes, key=lambda k: (votes[k], k))
    inside = []
    for b in usable:
        folded = b
        while folded < top_bucket - 2.5:
            folded *= 2
        while folded >= top_bucket + 7.5:
            folded /= 2
        if top_bucket - 2.5 <= folded < top_bucket + 7.5:
            inside.append(folded)
    if not inside:
        return round(fold_into_band(float(top_bucket + 2.5)), 1)
    inside.sort()
    median = inside[len(inside) // 2]
    # Октаву центра выбирает приор «темпа исполнения»: по данным 86 и 172
    # описывают библиотеку одинаково хорошо, объективно их не различить,
    # но диджей считает драм-н-бейс как 172, а не как 86. Приор
    # прикладывается ОДИН раз здесь, к центру, а не к каждому треку —
    # поэтому разрезать темповое семейство он не может.
    return round(fold_into_band(median), 1)


def normalize_library(tracks: list[dict], bpm_key: str = "bpm") -> float:
    """Приводит поле bpm всех треков к общему центру библиотеки НА МЕСТЕ.
    Возвращает найденный центр. Идемпотентна."""
    center = library_center([t.get(bpm_key) or 0.0 for t in tracks])
    for t in tracks:
        b = t.get(bpm_key)
        if b:
            t[bpm_key] = round(fold_to_center(b, center), 1)
    return center
