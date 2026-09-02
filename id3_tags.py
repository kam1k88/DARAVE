"""
id3_tags.py — темп и тональность из тегов файла, и примирение их с
измерением.

## Зачем это понадобилось

Библиотека диджея показывала 115-117 BPM там, где играет 173-175. Ошибка
не в одном треке: так определилось 40 треков из 67. Причина —
`librosa.beat.beat_track` устойчиво находит в драм-н-бейсе долю в 2/3 от
настоящей (174 -> 116, 170 -> 113): рисунок 174 BPM с двухдольным басом
даёт сетке на 116 почти такой же контраст, как настоящей.

Дальше ошибка сама себя защищала. `fix_library_bpm.py` умеет проверять
гипотезы x1.5 и x2, но выбирает между ними по «полосе исполнения
библиотеки», а полосу считал по тем же самым испорченным значениям:
центр 116, полоса 95..139, и настоящие 174 в неё не попадали. Инструмент
чинил библиотеку по её же ошибке и не менял ничего.

## Что здесь есть

Внешний якорь — тег TBPM. Он стоит у 64 треков из 67 и ставится не
трекером, а магазином или самим диджеем. Сам по себе тег тоже врёт (у
восьми треков там половина или две трети), поэтому правило такое:

    тег ПРЕДЛАГАЕТ гипотезу, измерение её ПОДТВЕРЖДАЕТ.

Гипотеза принимается, только если найденный темп связан с ней простым
отношением (1, 4/3, 3/2, 2, 3 и обратные) с точностью 2.5%. На этой
библиотеке правило дало 60 верных значений из 67, а оставшиеся семь —
это треки без тега или с жанром, не попавшим в полосу; их разбирает
контраст гребёнчатого фильтра в fix_library_bpm.py.

Читать теги умеет и mutagen, но он ставится не везде, а нужен здесь
ровно один кадр ID3. Поэтому есть и свой разбор — он не зависит ни от
чего и работает на любой машине.
"""
from __future__ import annotations

import re
import struct

# Отношения, которыми детектор темпа промахивается. 2/3 и 3/2 —
# самая частая пара в драм-н-бейсе, 3/4 и 4/3 — на треках с триольным
# рисунком, 1/2 и 2 — обычная октавная ошибка.
DETECT_RATIOS = (1.0, 4.0 / 3.0, 3.0 / 2.0, 2.0, 3.0, 3.0 / 4.0, 2.0 / 3.0, 0.5)

# В каком порядке пробовать множители самого тега. Сначала тег как есть
# (обычно он верен), потом удвоенный (в теге половинный темп), потом
# полуторный (в теге две трети — так помечают драм-н-бейс некоторые
# магазины).
TAG_MULTIPLIERS = (1.0, 2.0, 1.5, 3.0, 4.0 / 3.0, 0.5, 0.75, 2.0 / 3.0)

TOLERANCE = 0.025
BPM_MIN, BPM_MAX = 70.0, 200.0

_TEXT_ENC = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}


def _id3_frames(path: str, want: tuple[str, ...]) -> dict:
    """Текстовые кадры ID3v2 без внешних библиотек.

    Читается только заголовок тега — это первые килобайты файла, а не
    весь трек. Разбор нарочно простой: любой непонятный кадр обрывает
    чтение, потому что нам нужны только TBPM/TKEY/TCON, и они всегда в
    начале."""
    out: dict[str, str] = {}
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                return out
            size = 0
            for b in head[6:10]:
                size = (size << 7) | (b & 0x7F)
            data = f.read(size)
    except OSError:
        return out
    ver = head[3]
    i = 0
    while i + 10 <= len(data):
        fid = data[i:i + 4].decode("latin-1", "replace")
        if not re.fullmatch(r"[A-Z0-9]{4}", fid):
            break
        if ver >= 4:
            fs = 0
            for b in data[i + 4:i + 8]:
                fs = (fs << 7) | (b & 0x7F)
        else:
            fs = struct.unpack(">I", data[i + 4:i + 8])[0]
        if fs <= 0 or i + 10 + fs > len(data):
            break
        if fid in want:
            body = data[i + 10:i + 10 + fs]
            if body:
                enc = _TEXT_ENC.get(body[0], "latin-1")
                try:
                    out[fid] = body[1:].decode(enc, "replace").strip("\x00").strip()
                except Exception:
                    pass
        i += 10 + fs
    return out


def read_tags(path: str) -> dict:
    """{'bpm': float|None, 'key': str|None, 'genre': str|None}."""
    bpm = key = gen = None
    try:
        from mutagen import File as MutagenFile

        m = MutagenFile(path)
        if m is not None and getattr(m, "tags", None):
            tags = m.tags
            for k, dst in (("TBPM", "bpm"), ("TKEY", "key"), ("TCON", "genre")):
                try:
                    v = tags.get(k)
                except Exception:
                    v = None
                if v is None:
                    continue
                v = str(v.text[0]) if hasattr(v, "text") and v.text else str(v)
                if dst == "bpm":
                    bpm = v
                elif dst == "key":
                    key = v
                else:
                    gen = v
    except Exception:
        pass
    if bpm is None and key is None and gen is None:
        fr = _id3_frames(path, ("TBPM", "TKEY", "TCON"))
        bpm, key, gen = fr.get("TBPM"), fr.get("TKEY"), fr.get("TCON")
    try:
        bpm_f = float(str(bpm).strip()) if bpm else None
    except ValueError:
        bpm_f = None
    if bpm_f is not None and not (20.0 <= bpm_f <= 400.0):
        bpm_f = None
    return {"bpm": bpm_f, "key": (str(key).strip() or None) if key else None,
            "genre": (str(gen).strip() or None) if gen else None}


def confirmed_bpm(detected: float | None, tag_bpm: float | None,
                  band: tuple[float, float] | None = None) -> dict | None:
    """Темп, на который согласны тег и измерение.

    Возвращает {'bpm', 'why', 'in_band'} или None, если согласия нет —
    тогда решать должен контраст сетки, а не догадка.

    ## Два случая, и они разные

    **Источники согласны** (тег и найденный темп отличаются меньше чем на
    2.5%). Тогда ответ — тег: он же округляет 135.02 до 135. Искать здесь
    «подтверждённые» кратные НЕЛЬЗЯ, и это стоило двенадцати испорченных
    треков: при tag == detected любое общее кратное формально
    «подтверждается» обоими, потому что подтверждает само себя, и 135
    превращалось в 202.5. Подтверждение имеет смысл только когда
    источники независимы, то есть когда они РАСХОДЯТСЯ.

    **Источники расходятся.** Тогда ищем значение, которое их мирит
    простым отношением, и берём БОЛЬШЕЕ из двух. Обе типичные ошибки
    занижают темп: детектор находит 2/3 или половину настоящего (174 ->
    116, 174 -> 87), а в теге магазины пишут половинный темп (174 -> 87).
    Ошибки, которые темп ЗАВЫШАЮТ, на реальной библиотеке не встретились
    ни разу. Проверено на обоих направлениях:

        найдено 115.97, тег 174  ->  174   (детектор дал 2/3)
        найдено 174.97, тег  88  ->  176   (в теге половина)
        найдено 135.02, тег 135  ->  135   (согласны — не трогаем)

    Жанровая полоса, если она есть, сильнее: драм-н-бейса на 116 не
    бывает, даже когда тег и детектор дружно на этом настаивают.
    """
    if not detected or detected <= 0 or not tag_bpm or tag_bpm <= 0:
        return None

    def supported(v: float) -> bool:
        return any(abs(detected * r - v) <= TOLERANCE * v for r in DETECT_RATIOS)

    def in_range(v: float) -> bool:
        return BPM_MIN <= v <= BPM_MAX

    agree = abs(detected - tag_bpm) <= TOLERANCE * tag_bpm
    if agree:
        v = float(tag_bpm)
        if band and not (band[0] <= v <= band[1]):
            # Оба ошиблись одинаково — вытягиваем в жанровую полосу.
            for m in TAG_MULTIPLIERS:
                lifted = v * m
                if in_range(lifted) and band[0] <= lifted <= band[1]:
                    return {"bpm": round(lifted, 2), "why": f"тег x{m:.4g} (в полосу жанра)",
                            "in_band": True}
            return {"bpm": round(v, 2), "why": "тег (вне полосы жанра)", "in_band": False}
        return {"bpm": round(v, 2), "why": "тег", "in_band": True}

    # Расходятся: собираем всё, что мирит источники.
    cands: list[tuple[float, str]] = []
    for m in TAG_MULTIPLIERS:
        v = tag_bpm * m
        if in_range(v) and supported(v):
            cands.append((v, "тег" if abs(m - 1.0) < 1e-9 else f"тег x{m:.4g}"))
    if in_range(detected) and any(abs(tag_bpm * m - detected) <= TOLERANCE * detected
                                  for m in TAG_MULTIPLIERS):
        cands.append((float(detected), "найденный темп"))
    if not cands:
        return None
    if band:
        for v, why in sorted(cands, key=lambda x: -x[0]):
            if band[0] <= v <= band[1]:
                return {"bpm": round(v, 2), "why": why, "in_band": True}
    v, why = max(cands, key=lambda x: x[0])
    return {"bpm": round(v, 2), "why": why, "in_band": band is None}


# Полосы исполнения по жанру. Нужны ровно там, где тег и измерение
# СОГЛАСНЫ и оба неверны: у драм-н-бейса магазины сплошь и рядом пишут
# в TBPM две трети настоящего темпа (113 вместо 170, 116 вместо 174), и
# детектор ошибается ровно так же — согласие двух одинаковых ошибок
# доказательством не является. Жанровый тег в этом случае и есть третий
# независимый голос: драм-н-бейса на 116 не бывает.
#
# Полоса по библиотеке для этого НЕ годится, проверено: её центр
# получается 174, и в неё перестают попадать гэридж и бас на 135-140,
# которые лежат в той же папке и определены верно.
GENRE_BANDS = (
    (("драм", "drum", "dnb", "d&b", "jungle", "neuro", "liquid", "jump"), (160.0, 190.0)),
    (("hardcore", "хардкор", "gabber"), (160.0, 200.0)),
    (("dubstep", "дабстеп", "halftime"), (130.0, 150.0)),
)


def band_for_genre(genre_text: str | None, file_name: str | None = None) -> tuple[float, float] | None:
    """Полоса темпа по жанровому тегу (и по имени файла — «[DnB]» в
    названии встречается чаще, чем правильный TCON)."""
    hay = f"{genre_text or ''} {file_name or ''}".lower()
    for words, band in GENRE_BANDS:
        if any(w in hay for w in words):
            return band
    return None


def bpm_for_file(path: str, detected: float | None) -> dict | None:
    """Всё вместе: прочитать теги файла и примирить их с измерением."""
    import os

    tags = read_tags(path)
    band = band_for_genre(tags.get("genre"), os.path.basename(path))
    got = confirmed_bpm(detected, tags.get("bpm"), band)
    if got:
        got["tag_bpm"] = tags.get("bpm")
        got["tag_key"] = tags.get("key")
    return got
