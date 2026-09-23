"""
track_analysis.py — офлайн-сканирование музыкальной библиотеки: BPM,
тональность, энергетика, "яркость"/танцевальность трека. Работает локально
у диджея (companion-машина), не на backend'е — библиотека может быть сотни
гигабайт, гонять её в облако ради анализа не нужно.

Зависимости — requirements-analysis.txt (librosa, не входит в основной
requirements.txt, т.к. не нужен для реального времени).

Запуск:
    pip install -r requirements-analysis.txt
    python track_analysis.py --scan "C:\\Music" --db library.db

Результат — таблица tracks в library.db (см. init_db) + то же самое в JSON
на stdout при --print. Дальше эти данные можно, например, отдавать
DJAgent'у как контекст библиотеки ("что подходит по BPM/тональности к
играющему треку") — сама эта интеграция в agent.py пока не сделана, это
следующий шаг (см. README).

Что НЕ входит сюда (пока): жанр/стиль и "какие инструменты в треке" —
это требует предобученной модели аудио-тегирования (например, PANNs/YAMNet
поверх AudioSet), а не просто сигнальной обработки, как BPM/тональность/
энергетика. classify_genre_instruments() ниже — точка расширения: если
такая модель установлена, backend/companion может её вызвать; если нет —
явно возвращает None, а не выдумывает жанр.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".aiff", ".aif"}

# Всё короче этого — не трек, который можно свести: обрывки, сэмплы и,
# главное, собственные записи DARAVE (recordings/*.wav), которые лежат
# рядом с музыкой и попадали в скан. У них пустой спектр, из-за чего
# анализ выдавал energy=0.0 и мусорную тональность, а рекомендации потом
# честно ставили такой "трек" на первое место (нулевая энергия = самый
# большой "рост энергии"). Отсекаем и при сканировании, и при чтении БД.
MIN_TRACK_SECONDS = 60.0

# Профили Крумхансла-Шмуклера — эмпирические веса "устойчивости" каждой из
# 12 ступеней относительно тоники мажора/минора. Стандартный алгоритм
# key-finding по хрома-вектору: коррелируем усреднённый хрома трека с 24
# сдвигами этих профилей (12 мажорных + 12 минорных), максимум корреляции
# и есть предполагаемая тональность.
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _estimate_key(chroma_mean) -> str:
    import numpy as np

    def corr(profile, rotation):
        rotated = profile[-rotation:] + profile[:-rotation] if rotation else profile
        return float(np.corrcoef(chroma_mean, rotated)[0, 1])

    best_score, best_name = -2.0, "C major"
    for rotation in range(12):
        for profile, mode in ((_MAJOR_PROFILE, "major"), (_MINOR_PROFILE, "minor")):
            score = corr(profile, rotation)
            if score > best_score:
                best_score = score
                best_name = f"{_PITCH_NAMES[rotation]} {mode}"
    return best_name


# Camelot wheel — 12 мажоров (B) + 12 миноров (A), для гармонического
# сведения (mix_strategist.py). Индекс совпадает с _PITCH_NAMES выше.
_CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
_CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]


def key_to_camelot(key_name: str) -> str:
    """"A major" -> "11B" и т.п. — см. _CAMELOT_MAJOR/_MINOR."""
    pitch, mode = key_name.rsplit(" ", 1)
    idx = _PITCH_NAMES.index(pitch)
    return _CAMELOT_MAJOR[idx] if mode == "major" else _CAMELOT_MINOR[idx]


def who_is_it(path: str) -> dict:
    """Исполнитель и название трека: {'artist', 'title', 'source'}.

    Порядок: ID3-тег -> имя файла. Тег точнее всегда, но в библиотеке
    диджея он есть далеко не у всех: из 70 записей теги нашлись у 46.
    Имя файла — честный запасной вариант, и разбирается оно одним местом
    (`genre.artist_title_from_name`), а не тремя чуть разными."""
    artist = title = None
    source = None
    try:
        import id3_tags

        t = id3_tags.read_tags(path)
        artist, title = t.get("artist"), t.get("title")
        if artist or title:
            source = "tag"
    except Exception:                                # noqa: BLE001
        pass
    if not (artist and title):
        try:
            import genre as _genre

            a, ti = _genre.artist_title_from_name(path)
            artist = artist or a
            title = title or ti
            source = source or ("filename" if (a or ti) else None)
        except Exception:                            # noqa: BLE001
            pass
    return {"artist": artist, "title": title, "source": source}


def analyze_track(path: str) -> dict:
    """Возвращает {bpm, key, camelot, energy, brightness, danceability,
    duration_seconds, structure}. Числовые оценки 0..1, кроме bpm (уд/мин) и
    duration_seconds; structure — см. detect_structure()."""
    import librosa
    import numpy as np

    y, sr = librosa.load(path, sr=22050, mono=True)
    duration = float(len(y) / sr)

    # Огибающая атак считается ОДИН раз и переиспользуется: beat_track
    # считает её внутри себя, и ниже она считалась ещё раз — полторы
    # секунды на трек за одно и то же.
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
    bpm = float(tempo if np.isscalar(tempo) else tempo[0])
    if bpm <= 0 or len(beat_frames) == 0:
        # beat_track изредка не находит долей (тихие интро, нестандартный
        # ритм-рисунок) — не отдаём BPM=0, а пересчитываем через огибающую
        # onset'ов напрямую, это надёжнее для голого темпа (без самих долей).
        try:
            tempo_fn = librosa.feature.rhythm.tempo  # librosa >= 0.10.2
        except AttributeError:
            tempo_fn = librosa.beat.tempo  # старые версии librosa
        tempo_fallback = tempo_fn(onset_envelope=onset_env, sr=sr)
        bpm = float(tempo_fallback[0]) if len(tempo_fallback) else 0.0

    # hop 4096 вместо 512: тональность берётся как СРЕДНЕЕ по всему
    # треку, и профиль на крупном шаге совпадает с мелким до 1.000
    # (проверено), а считается втрое дешевле.
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=4096)
    key = _estimate_key(chroma.mean(axis=1))

    energy, energy_parts = _estimate_energy(y, sr, onset_env, duration)

    # Яркость — тоже одно число на трек, мелкий шаг ей не нужен.
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=4096)[0]
    brightness = float(np.clip(centroid.mean() / (sr / 4), 0.0, 1.0))
    danceability = _estimate_danceability(librosa, np, beat_frames, sr, onset_env)

    bpm = normalize_bpm(bpm)
    # librosa.beat.beat_track отдаёт темп с сетки-приора: на этой библиотеке
    # 22 трека получили РОВНО 172.30, хотя на деле там 171.00, 173.00,
    # 174.03. Ошибка в 1-2 BPM — это 1%: за 30 секунд сведения вторая дека
    # уезжает почти на целую долю, и свести такты нельзя в принципе.
    # Уточняем гребёнчатым поиском по длинному окну (beatgrid.refine_tempo).
    bpm = refine_bpm(y, sr, bpm)
    # Последнее слово — за тегом файла, если он согласуется с измерением.
    # Без этого драм-н-бейс сканировался в 2/3 темпа (174 -> 116) и
    # оставался таким навсегда: см. id3_tags.py, там разбор с цифрами.
    try:
        import id3_tags

        got = id3_tags.bpm_for_file(path, bpm)
        if got and abs(got["bpm"] - bpm) > 0.05:
            bpm = float(got["bpm"])
    except Exception:
        pass
    structure = detect_structure(y, sr, bpm)
    # Где реально играют барабаны. Без этого точки входа выбирались по
    # дропам, которые детектор находит с уверенностью 0.14-0.23, и трек
    # заводился в собственное эмбиентное интро.
    try:
        import beatgrid

        # Якорь тактовой сетки — фаза музыки, а не ноль файла, и слой
        # ударных, если он посчитан: обе карты без них систематически
        # промахиваются (подробности в beatgrid.bar_offset_for и
        # beatgrid.drums_signal).
        offset = beatgrid.bar_offset_for(y, sr, bpm)
        drums = beatgrid.drums_signal(path, sr)
        if drums is None:
            # Слоя ударных ещё нет — скан идёт ДО разделения на стемы.
            # Берём перкуссивную часть микса: это не стем, но для поиска
            # дропа её хватает, а без неё карта энергии ищет дроп как
            # «возврат баса» — и в фонке, где бас звучит непрерывно, не
            # находит НИ ОДНОГО.
            #
            # Замер на треке диджея (фонк 2:16): по басу — 0 дропов, с
            # перкуссивной частью — 0:23 и 1:26, ровно там, где их
            # показывает интерфейс. Ноль дропов заставлял mix_points
            # откатываться на дропы structure-анализа, а это 13 точек
            # ровно через 4 такта с уверенностью на уровне шума.
            try:
                import librosa as _lb

                drums = _lb.effects.percussive(y, margin=3.0)
            except Exception:                        # noqa: BLE001
                drums = None
        structure["drum_map"] = beatgrid.drum_map(y, sr, bpm, offset=offset,
                                                  drums=drums)
        structure["chroma_map"] = beatgrid.chroma_map(y, sr, bpm)
        # Карта энергии низа — дропы, брейкдауны, ямы. Раньше её считал
        # ТОЛЬКО фоновый перемер BPM, то есть сразу после сканирования у
        # трека не было ни одного дропа, и все именованные точки
        # (cue_points.py) сводились к «первый бит» и «аутро». Считается
        # она по тому же уже загруженному сигналу и стоит около секунды —
        # держать её отдельным проходом незачем.
        structure["energy_map"] = beatgrid.energy_map(y, sr, bpm,
                                                      offset=offset, drums=drums)
    except Exception:
        pass

    who = who_is_it(path)
    return {
        "bpm": round(bpm, 2),
        "genre": classify_track_genre(path, bpm),
        # Исполнитель и название — отдельными полями, а не «разберём имя
        # файла, когда понадобится». По ним работает определение поджанра
        # по артисту, поиск в библиотеке и запрос в MusicBrainz/Discogs;
        # разбирать имя файла заново в каждом из этих мест значило бы
        # получать три слегка разных ответа.
        "artist": who["artist"],
        "title": who["title"],
        "key": key,
        "camelot": key_to_camelot(key),
        "energy": round(energy, 3),
        "energy_parts": {k: round(v, 3) for k, v in energy_parts.items()},
        "brightness": round(brightness, 3),
        "danceability": round(danceability, 3),
        "duration_seconds": round(duration, 1),
        "structure": structure,
    }


# --- Mood: совместимость с rekordbox, а не замена нашей энергии ---------
#
# В rekordbox Mood — это три корзины (HIGH / MID / LOW), в которые трек
# попадает по темпу, ритму, бочке и плотности микса. Главное в нём не
# классификация энергии, а то, что ОТ НЕГО ЗАВИСИТ СЛОВАРЬ ФРАЗ: у HIGH
# доступны Intro/Up/Down/Chorus/Outro, у MID и LOW — Intro/Verse/Chorus/
# Bridge/Outro. То есть Up и Down нельзя поставить треку, который попал в
# MID, а Verse — треку из HIGH.
#
# Стоит ли переходить на эту модель? Нет, и вот почему по пунктам.
#
# 1. ИЗМЕРЯЕТ ОНА ТО ЖЕ САМОЕ. Входы Mood — темп, ритм, бочка, плотность.
#    Наш `energy` считается по трём независимым признакам ровно об этом
#    же, но НЕПРЕРЫВНО (0..1), а `EL` — квинтилями по самой библиотеке.
#    Три корзины вместо пяти квинтилей и непрерывной величины — это
#    потеря информации, а не приобретение. Для драм-н-бейсовой библиотеки
#    потеря полная: ВСЕ треки диджея попадут в HIGH, и различать их Mood
#    перестанет вовсе.
# 2. СЛОВАРЬ ФРАЗ У НАС УЖЕ ЗАВИСИТ ОТ МАТЕРИАЛА — только не от энергии,
#    а от жанра и темпа (`style.PHRASE_LADDERS`, `genre.BPM_FAMILIES`).
#    Это та же идея, применённая к более надёжному признаку: стиль
#    определяет форму аранжировки прямее, чем громкость.
# 3. СЛОВАРЬ HIGH — ЭТО ПОЧТИ НАШ СЛОВАРЬ, с точностью до названий:
#    Up ≈ build, Chorus ≈ drop, Down ≈ breakdown. Разница одна: у
#    rekordbox нет секции для ТЕЛА трека, а у нас есть groove. Переходить
#    на модель, которая беднее нашей на одну секцию, смысла нет.
# 4. А MID и LOW — это песенная схема (Verse/Chorus/Bridge), от которой
#    диджей отказался сам: «в моей библиотеке вокал — почти всегда
#    зациклённый сэмпл, а не полноценный куплет».
#
# Зачем тогда Mood здесь. Ровно за одним: СОВМЕСТИМОСТЬ. Если трек или
# набор меток поедет в rekordbox, ярлык нужен, и лучше вывести его из
# наших измерений, чем считать заново чужой формулой. Поэтому mood — это
# ПРОИЗВОДНАЯ величина, а не источник истины: наша энергия остаётся.

MOOD_HIGH_ENERGY = 0.62      # выше — плотный танцевальный материал
MOOD_LOW_ENERGY = 0.32       # ниже — спокойный, эмбиентный
MOOD_HIGH_BPM = 138.0        # быстрее — почти наверняка HIGH при любой энергии


def mood(energy: float | None, bpm: float | None = None) -> str:
    """Ярлык rekordbox (HIGH/MID/LOW), выведенный из нашей энергии.

    Нужен только для обмена с rekordbox. Внутри DARAVE решения
    принимаются по `energy` и `EL` — они непрерывные и различают там, где
    три корзины уже не различают ничего."""
    e = float(energy) if energy is not None else 0.5
    b = float(bpm) if bpm else 0.0
    if e >= MOOD_HIGH_ENERGY or b >= MOOD_HIGH_BPM:
        return "HIGH"
    if e <= MOOD_LOW_ENERGY:
        return "LOW"
    return "MID"


# Как наши секции называются в rekordbox. Односторонняя таблица: наружу
# переводим, внутрь — нет. Обратный перевод потерял бы groove, которому в
# словаре HIGH соответствия нет вовсе (ближайшее — Chorus 2, но это уже
# не перевод, а выдумка).
REKORDBOX_PHRASE = {
    "intro": "Intro",
    "build": "Up",
    "drop": "Chorus",
    "breakdown": "Down",
    "groove": "Chorus 2",     # приблизительно: точного соответствия нет
    "outro": "Outro",
}


# --- энергетика ---------------------------------------------------------
# Прежняя формула была  clip(rms.mean() * 10, 0, 1)  и давала РОВНО 1.0 всем
# 48 трекам библиотеки. Причина не в множителе: у любого современного
# мастеринга средний RMS выше 0.1, значит выражение упиралось в потолок ещё
# до clip'а. Мерить громкость в линейной амплитуде для сжатого материала
# бесполезно — вся разница живёт в логарифмической шкале. И одной громкости
# мало: в DnB-библиотеке все треки выжаты одинаково громко, поэтому берём
# три независимых признака, каждый со своим осмысленным окном.

LOUD_DB_MIN, LOUD_DB_MAX = -24.0, -6.0   # типичный разброс мастеринга танцевальной музыки
ONSET_PER_SEC_MIN, ONSET_PER_SEC_MAX = 1.5, 7.0   # плотность атак: от разрежённого до плотного
CREST_DB_MAX, CREST_DB_MIN = 14.0, 4.0   # крест-фактор: чем МЕНЬШЕ, тем сильнее сжато

ENERGY_WEIGHTS = {"loudness": 0.40, "density": 0.35, "punch": 0.25}


def _norm(value: float, lo: float, hi: float) -> float:
    import numpy as np
    return float(np.clip((value - lo) / (hi - lo + 1e-12), 0.0, 1.0))


def _estimate_energy(y, sr: int, onset_env, duration: float) -> tuple[float, dict]:
    """Составная оценка "насколько трек прёт" из трёх признаков:

    loudness — громкость 80-го перцентиля RMS в dBFS. Перцентиль, а не
        среднее: длинное тихое интро не должно занижать ощущаемую энергию.
    density  — плотность атак (онсетов в секунду): отличает плотный нейро
        от разрежённого ликвида при одинаковой громкости.
    punch    — сжатость (крест-фактор пик/RMS в dB, инвертирован): чем
        меньше динамический размах, тем жёстче звучит.

    Возвращает (0..1, разбивка по признакам) — разбивка нужна, чтобы
    метрику можно было проверить, а не принимать на веру."""
    import librosa
    import numpy as np

    rms = librosa.feature.rms(y=y)[0]
    if len(rms) == 0 or duration <= 0:
        return 0.0, {"loudness": 0.0, "density": 0.0, "punch": 0.0}

    rms_p80 = float(np.percentile(rms, 80))
    loud_db = 20.0 * np.log10(rms_p80 + 1e-9)
    loudness = _norm(loud_db, LOUD_DB_MIN, LOUD_DB_MAX)

    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="time")
    density = _norm(len(onsets) / max(duration, 1e-6), ONSET_PER_SEC_MIN, ONSET_PER_SEC_MAX)

    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    crest_db = 20.0 * np.log10((peak + 1e-9) / (rms_p80 + 1e-9))
    punch = _norm(crest_db, CREST_DB_MAX, CREST_DB_MIN)  # окно перевёрнуто намеренно

    parts = {"loudness": loudness, "density": density, "punch": punch}
    total = sum(ENERGY_WEIGHTS[k] * v for k, v in parts.items())
    return float(np.clip(total, 0.0, 1.0)), parts


def _estimate_danceability(librosa, np, beat_frames, sr: int, onset_env) -> float:
    """Ровность доли + сила атак.

    ЧЕСТНОЕ ОГРАНИЧЕНИЕ: для сведённой электронной музыки этот показатель
    почти ничего не различает — доля ровная у всех треков без исключения.
    Одна регулярность давала 0.93..0.99 на всю библиотеку; добавленная сила
    атак расширяет разброс лишь до ~0.1. Опираться на него при подборе
    треков не стоит — работают energy, BPM и тональность. Оставлен как
    справочный, а не как основание для решений."""
    if len(beat_frames) <= 4:
        return 0.0
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    intervals = np.diff(beat_times)
    regularity = 1.0 - np.clip(np.std(intervals) / (np.mean(intervals) + 1e-9), 0.0, 1.0)
    steadiness = _norm(regularity, 0.85, 1.0)
    drive = _norm(float(np.mean(onset_env)) / (float(np.max(onset_env)) + 1e-9), 0.05, 0.35)
    return float(np.clip(0.6 * steadiness + 0.4 * drive, 0.0, 1.0))


def refine_bpm(y, sr: int, bpm_hint: float, min_confidence: float = 0.25) -> float:
    """Точный темп вокруг подсказки. Октаву НЕ меняет — это дело
    normalize_bpm/tempo.py; здесь только доводка внутри неё."""
    if not bpm_hint or bpm_hint <= 20:
        return bpm_hint
    try:
        import beatgrid
    except Exception:
        return bpm_hint
    try:
        r = beatgrid.refine_tempo(y, sr, bpm_hint)
    except Exception:
        return bpm_hint
    if not r.get("refined") or r.get("confidence", 0.0) < min_confidence:
        return bpm_hint
    return float(r["bpm"])


def normalize_bpm(bpm: float, low: float = 100.0, high: float = 200.0) -> float:
    """Сворачивает темп в диапазон [low, high) удвоением/делением пополам.

    librosa.beat.beat_track регулярно отдаёт для драм-н-бейса ПОЛОВИННЫЙ
    темп (86 вместо 172) — это не баг библиотеки, а принципиальная
    октавная неоднозначность темпа: 86 и 172 одинаково хорошо объясняют
    сетку долей. Для DARAVE (инструмент именно под DnB) правильная ветка
    всегда верхняя, а половинный темп ломает вполне конкретные вещи:
    длительность техник в секундах (build_plan переводит такты в секунды
    через bpm — на 86 вместо 172 переход растягивается вдвое) и пороги
    bpm_delta_max при подборе пар. Нормализация идемпотентна: 172 -> 172."""
    if not bpm or bpm <= 0:
        return bpm
    guard = 0
    while bpm < low and guard < 8:
        bpm *= 2.0
        guard += 1
    while bpm >= high and guard < 16:
        bpm /= 2.0
        guard += 1
    return bpm


# --- поиск мест для сведения ------------------------------------------
#
# Дроп и брейкдаун ищутся ПО ТАКТАМ, а не по кадрам огибающей. Разница не
# косметическая, и вот замер, из-за которого всё переписано.
#
# Было: сглаженная RMS в дБ, «громко» = 90-й перцентиль минус 3 dB, и
# дропом считался КАЖДЫЙ переход снизу вверх через этот порог. На
# фонк-треке диджея (2:16, 153 BPM):
#
#     порог «громко»                      -4.9 dB
#     доля времени выше порога              43%
#     пересечений порога снизу вверх          36
#     из них прошло проверку на удержание     13
#     промежутки между ними        4.0, 4.0, 4.0, 4.0 такта
#
# Сорок три процента — это не событие, это половина трека; порог
# пересекался туда-сюда постоянно. А ровные четыре такта между «дропами»
# брались не из музыки, а из строки `i += min_sustain_frames`: найдя
# один, перебор перескакивал ровно на окно удержания и находил следующий.
#
# Хуже другое, и это касалось ВСЕЙ библиотеки, а не только фонка.
# «Уверенность» считалась как разница двух СОСЕДНИХ кадров:
#
#     шаг между соседними кадрами: медиана 0.101 dB, 99-й перцентиль 0.93
#
# После сглаживания окном в секунду соседние кадры отличаются на
# десятые доли децибела ВСЕГДА. То есть уверенность по построению не
# могла быть больше единицы и не значила ничего — отсюда «дропы с
# уверенностью 0.14-0.26» во всех отчётах.
#
# Стало: уровень усредняется ПО ТАКТУ (такт — наименьшая единица, в
# которой музыка вообще меняется), дроп — это скачок между СРЕДНИМ
# уровнем нескольких тактов до и нескольких после, и он обязан быть
# заметным на слух. Уверенность — величина этого скачка в децибелах,
# то есть то самое число, которое диджей и назвал бы «насколько резко».

# Насколько должен подняться уровень, чтобы это был дроп. Три децибела —
# это вдвое по мощности, порог различимости для уха на музыкальном
# материале.
DROP_MIN_STEP_DB = 3.0
# Сколько тактов смотрим до и после границы. Четыре — фраза: короче
# окно ловит отдельные удары, длиннее размазывает сам момент входа.
DROP_WINDOW_BARS = 4
# Два дропа ближе этого расстояния — это один дроп; оставляем сильнейший.
DROP_MIN_GAP_BARS = 8
# Насколько ниже «громкого» уровня трека должно быть, чтобы считать это
# ямой. Шесть децибел — вчетверо по мощности: барабаны действительно ушли.
BREAKDOWN_DROP_DB = 6.0
BREAKDOWN_MIN_BARS = 4


def _bar_levels(y, sr: int, bpm: float) -> tuple:
    """Средний уровень каждого такта в дБ. Возвращает (уровни, время начала)."""
    import librosa
    import numpy as np

    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    if len(rms) < 4 or bpm <= 0:
        return np.array([]), np.array([])
    frame_sec = hop / sr
    bar_sec = 4 * 60.0 / bpm
    n_bars = int(len(rms) * frame_sec / bar_sec)
    if n_bars < 4:
        return np.array([]), np.array([])
    per = max(1, int(round(bar_sec / frame_sec)))
    levels, starts = [], []
    for b in range(n_bars):
        seg = rms[b * per:(b + 1) * per]
        if not len(seg):
            break
        # Медиана, а не среднее: один удар в тихом такте не должен
        # объявлять его громким.
        levels.append(float(np.median(seg)))
        starts.append(b * bar_sec)
    lv = np.asarray(levels, dtype="float64")
    db = 20.0 * np.log10(lv / (lv.max() + 1e-12) + 1e-6)
    return db, np.asarray(starts, dtype="float64")


def detect_structure(y, sr: int, bpm: float) -> dict:
    """Места для сведения: дропы, ямы и сетка фраз.

    Не нейросетевая сегментация, а разбор огибающей — но ПО ТАКТАМ и по
    величине скачка, а не по пересечению порога. Почему именно так —
    замер в комментарии выше.

    Возвращает {drops, breakdowns, phrase_boundaries}: время в секундах и
    номер такта. У дропа `confidence` — величина скачка уровня в
    децибелах, нормированная к 0..1 по двенадцати децибелам (это уже
    очень резкий вход)."""
    import numpy as np

    db, starts = _bar_levels(y, sr, bpm)
    if not len(db):
        return {"drops": [], "breakdowns": [], "phrase_boundaries": []}

    beat_seconds = 60.0 / bpm
    bar_seconds = beat_seconds * 4
    w = DROP_WINDOW_BARS
    loud_ref = float(np.percentile(db, 90))

    # --- дропы: скачок среднего уровня между окном до и окном после ---
    cand = []
    for b in range(w, len(db) - w):
        before = float(np.median(db[b - w:b]))
        after = float(np.median(db[b:b + w]))
        step = after - before
        if step < DROP_MIN_STEP_DB:
            continue
        # Подняться мало — надо подняться ДО громкого. Иначе дропом
        # считается любой выход из тишины в середину.
        if after < loud_ref - DROP_MIN_STEP_DB:
            continue
        cand.append({"bar": b, "step": step, "after": after})

    # Подавление соседей: из двух близких оставляем тот, где скачок
    # больше. Раньше вместо этого стоял слепой перескок на окно
    # удержания — он и раскладывал «дропы» ровно через четыре такта.
    cand.sort(key=lambda c: -c["step"])
    taken: list[dict] = []
    for c in cand:
        if all(abs(c["bar"] - t["bar"]) >= DROP_MIN_GAP_BARS for t in taken):
            taken.append(c)
    taken.sort(key=lambda c: c["bar"])
    drops = [{
        "time_seconds": round(float(starts[c["bar"]]), 2),
        "bar_index": int(c["bar"]),
        "step_db": round(c["step"], 1),
        "confidence": round(min(1.0, c["step"] / 12.0), 3),
    } for c in taken]

    # --- ямы: подряд идущие такты заметно ниже громкого уровня ---
    low = db < (loud_ref - BREAKDOWN_DROP_DB)
    breakdowns = []
    b = 0
    while b < len(low):
        if not low[b]:
            b += 1
            continue
        e = b
        while e < len(low) and low[e]:
            e += 1
        if e - b >= BREAKDOWN_MIN_BARS:
            end_t = starts[e] if e < len(starts) else starts[-1] + bar_seconds
            breakdowns.append({
                "start_seconds": round(float(starts[b]), 2),
                "end_seconds": round(float(end_t), 2),
                "bar_index": int(b),
                "depth_db": round(loud_ref - float(np.median(db[b:e])), 1),
            })
        b = e

    # --- сетка фраз: каждые 16 тактов от начала ---
    #
    # Нужна приёмам вроде Phrase Match, которым не важен именно дроп —
    # важна чистая граница фразы. Считается от первой доли, а не от
    # найденных дропов: дроп может быть один на весь трек, а сводиться
    # надо и без него.
    duration = float(starts[-1] + bar_seconds) if len(starts) else 0.0
    phrase_boundaries = [
        round(b * bar_seconds, 2)
        for b in range(0, int(duration / bar_seconds) + 1, 16)
        if b * bar_seconds <= duration
    ]

    return {"drops": drops, "breakdowns": breakdowns, "phrase_boundaries": phrase_boundaries}


def read_file_tags(path: str) -> dict:
    """Жанр и исполнитель из самого файла. Нужны, чтобы подписать трек
    жанром, не гадая по звуку (см. genre.py: догадка по звуку проверена и
    не работает). mutagen — необязателен: без него разбираем имя файла."""
    out = {"genre": None, "artist": None, "title": None}
    try:
        from mutagen import File as MutagenFile

        m = MutagenFile(path, easy=True)
        if m:
            for field in ("genre", "artist", "title"):
                val = m.get(field)
                if val:
                    out[field] = val[0]
    except Exception:
        pass
    return out


def classify_track_genre(path: str, bpm: float | None = None) -> dict | None:
    """Жанр и, если он известен, поджанр. None — если не определилось
    вовсе: пустое поле честнее выдуманного."""
    try:
        import genre as genre_mod
    except Exception:
        return None
    tags = read_file_tags(path)
    rec = genre_mod.classify(path, tags.get("genre"), tags.get("artist"), bpm)
    if not rec.get("genre") and not rec.get("subgenre"):
        return None
    rec["title"] = tags.get("title")
    return rec


def classify_genre_instruments(path: str) -> dict | None:
    """Точка расширения под жанр/стиль/инструменты — требует внешней
    предобученной модели аудио-тегирования (не просто librosa). Пример:
    panns_inference (PANNs, AudioSet-теги) или tensorflow_hub YAMNet.
    Если ни одна не установлена — явно возвращает None (см. докстринг
    модуля): лучше честно "не умею", чем выдуманный жанр."""
    try:
        from panns_inference import AudioTagging, labels as panns_labels  # noqa: F401
    except ImportError:
        return None

    import numpy as np
    import librosa

    y, sr = librosa.load(path, sr=32000, mono=True)
    tagger = AudioTagging(checkpoint_path=None, device="cpu")
    clipwise_output, _ = tagger.inference(y[np.newaxis, :])
    top_indices = np.argsort(clipwise_output[0])[::-1][:8]
    tags = [
        {"label": panns_labels[i], "confidence": round(float(clipwise_output[0][i]), 3)}
        for i in top_indices
    ]
    return {"tags": tags}


def init_db(db_path: str) -> sqlite3.Connection:
    # WAL и длинный busy_timeout: в этот же файл пишет фоновое уточнение BPM
    # (fix_library_bpm), и на дефолтных 5 секундах ожидания скан падал с
    # «database is locked» целиком, потеряв весь проход.
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tracks (
            path TEXT PRIMARY KEY,
            bpm REAL, key TEXT, camelot TEXT, energy REAL, brightness REAL,
            danceability REAL, duration_seconds REAL,
            structure_json TEXT, tags_json TEXT,
            artist TEXT, title TEXT, meta_json TEXT,
            file_mtime REAL, file_size INTEGER,
            scanned_at REAL
        )"""
    )
    conn.commit()
    # Миграция уже существующих баз. Колонки добавляются по одной и молча:
    # у диджея база живёт с первого дня проекта, пересканировать 70 треков
    # ради трёх полей — час работы на ровном месте.
    for col, decl in (("artist", "TEXT"), ("title", "TEXT"), ("meta_json", "TEXT"),
                      ("file_mtime", "REAL"), ("file_size", "INTEGER")):
        try:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {decl}")
            conn.commit()
        except sqlite3.OperationalError:
            pass                       # колонка уже есть
    # Чистим накопленные дубли по регистру ДО прохода: иначе новый скан
    # добавит правильные строки рядом со старыми, а не вместо них.
    try:
        conn.close()
        r = dedupe_library_db(db_path)
        if r["merged"]:
            print(f"[скан] схлопнуто дублей по регистру пути: {r['merged']} "
                  f"({r['before']} -> {r['after']})")
        conn = sqlite3.connect(db_path, timeout=60.0)
        conn.execute("PRAGMA busy_timeout=60000")
    except sqlite3.Error:
        pass
    return conn


def canon_track_path(path) -> str:
    """Канонический путь к треку — один файл, одна строка в базе.

    Ключом в таблице `tracks` стоит САМА строка пути, а Windows на
    регистр не смотрит: `C:\\Users\\...\\music\\x.mp3` и
    `...\\Music\\x.mp3` — один и тот же файл, но два разных ключа.
    Именно так библиотека из 65 треков превратилась в 115: часть строк
    осталась от прохода, запущенного с другим написанием папки, и в
    интерфейсе они выглядели дублями с пустым жанром.

    `Path.resolve()` на Windows возвращает НАСТОЯЩИЙ регистр с диска,
    поэтому это одновременно и нормализация, и красивое отображение."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _dedupe_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def dedupe_library_db(db_path: str, drop_missing: bool = False) -> dict:
    """Схлопнуть строки, различающиеся только регистром пути.

    Из группы остаётся САМАЯ СВЕЖАЯ запись (у неё есть жанр и прочее, что
    добавили позже), а путь переписывается в канонический вид."""
    if not os.path.exists(db_path):
        return {"before": 0, "after": 0, "merged": 0, "missing": 0}
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        rows = conn.execute("SELECT path, scanned_at FROM tracks").fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {"before": 0, "after": 0, "merged": 0, "missing": 0}
    before = len(rows)
    best: dict[str, tuple[str, float]] = {}
    for path, ts in rows:
        k = _dedupe_key(path)
        cur = best.get(k)
        if cur is None or (ts or 0) > (cur[1] or 0):
            best[k] = (path, ts)
    keep = {v[0] for v in best.values()}
    merged = missing = 0
    for path, _ts in rows:
        if path not in keep:
            conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
            merged += 1
    if drop_missing:
        # Предохранитель: строку убираем, только если ПАПКА файла на
        # месте. Внешний диск, отключённый сетевой каталог, не
        # примонтированный раздел — это «сейчас не видно», а не «файла
        # нет», и вычистить по такому поводу полбиблиотеки нельзя.
        for path in list(keep):
            if os.path.exists(path):
                continue
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                continue                     # папки нет — судить не о чем
            conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
            keep.discard(path)
            missing += 1
    for path in list(keep):
        canon = canon_track_path(path)
        if canon != path:
            try:
                conn.execute("UPDATE tracks SET path = ? WHERE path = ?", (canon, path))
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
                merged += 1
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    conn.close()
    return {"before": before, "after": after, "merged": merged, "missing": missing}


def load_library_from_db(db_path: str, since_ts: float | None = None) -> list[dict]:
    """Читает результаты сканирования ПРЯМО из SQLite — в том же формате,
    что возвращает scan_library() и что принимает
    server.py::POST /api/rooms/{id}/library.

    Зачем: когда сканер запущен backend'ом как подпроцесс (кнопка
    "Сканировать"), они работают на ОДНОЙ машине и делят один файл БД —
    гонять результат обратно по HTTP через localhost бессмысленно и
    хрупко (у пользователя системный прокси перехватывал localhost и
    отдавал пустой 503, из-за чего библиотека молча оставалась пустой).
    Backend просто читает файл. HTTP-путь (--upload) остаётся только для
    случая, когда backend реально на другой машине/VPS.

    since_ts: если задан, вернуть только треки, отсканированные не раньше
    этого момента — так результат совпадает с "этим прогоном" сканера, а
    не со всем накопленным в БД за прошлые сканирования других папок."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    try:
        if since_ts is not None:
            rows = conn.execute(
                "SELECT * FROM tracks WHERE scanned_at >= ? ORDER BY path", (since_ts,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tracks ORDER BY path").fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        d = dict(row)
        if (d["duration_seconds"] or 0.0) < MIN_TRACK_SECONDS:
            continue  # см. MIN_TRACK_SECONDS — чиним и уже отсканированные БД
        # (нормализация темпа — ниже, по всей библиотеке сразу)
        path_str = d["path"]
        out.append({
            "path": path_str,
            # Не Path(path_str).name: БД могла быть записана на Windows, а
            # прочитана на другой ОС (тесты/VPS) — posixpath не режет "\\".
            "name": path_str.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1],
            "bpm": float(d["bpm"] or 0.0),
            "key": d["key"],
            "camelot": d["camelot"],
            "energy": d["energy"],
            "brightness": d["brightness"],
            "danceability": d["danceability"],
            "duration_seconds": d["duration_seconds"],
            "structure": json.loads(d["structure_json"]) if d["structure_json"] else {
                "drops": [], "breakdowns": [], "phrase_boundaries": [],
            },
            "tags": json.loads(d["tags_json"]) if d["tags_json"] else None,
        })
        tg = (out[-1]["tags"] or {}).get("genre") if out[-1]["tags"] else None
        if tg:
            out[-1]["genre"] = tg.get("genre")
            out[-1]["subgenre"] = tg.get("subgenre")
            out[-1]["genre_source"] = tg.get("genre_source")
            out[-1]["subgenre_source"] = tg.get("subgenre_source")
            out[-1]["artist"] = tg.get("artist")

    # Темп приводим ПО ВСЕЙ БИБЛИОТЕКЕ СРАЗУ, а не по одному треку.
    # Раньше каждый трек независимо сворачивался в фиксированное окно
    # [90,180) — и окно разрезало темповое семейство: 70 превращалось в
    # 140, а 95 оставалось 95, хотя это соседние темпы. Теперь центр
    # берётся из самой библиотеки, а трек подтягивается к его октаве,
    # только если он туда реально попадает (см. tempo.fold_to_center).
    import tempo as _tempo
    _tempo.normalize_library(out)
    return out


def _upsert_track(conn: sqlite3.Connection, path: Path, info: dict, tags: dict | None) -> None:
    """Запись одного трека с повтором: файл базы делят сканер и фоновое
    уточнение BPM, и короткая блокировка — норма, а не повод падать."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            conn.execute(
                """INSERT INTO tracks (path, bpm, key, camelot, energy, brightness, danceability,
                                        duration_seconds, structure_json, tags_json,
                                        artist, title, file_mtime, file_size, scanned_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       bpm=excluded.bpm, key=excluded.key, camelot=excluded.camelot,
                       energy=excluded.energy, brightness=excluded.brightness,
                       danceability=excluded.danceability, duration_seconds=excluded.duration_seconds,
                       structure_json=excluded.structure_json, tags_json=excluded.tags_json,
                       artist=excluded.artist, title=excluded.title,
                       file_mtime=excluded.file_mtime, file_size=excluded.file_size,
                       scanned_at=excluded.scanned_at""",
                (
                    canon_track_path(path), info["bpm"], info["key"], info["camelot"], info["energy"],
                    info["brightness"], info["danceability"], info["duration_seconds"],
                    json.dumps(info["structure"]), json.dumps(tags) if tags else None,
                    info.get("artist"), info.get("title"),
                    *file_fingerprint(path), time.time(),
                ),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            last = exc
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            time.sleep(2.0 * (attempt + 1))
    raise last if last is not None else sqlite3.OperationalError("не смог записать трек")


# Папки и имена, которые производит сам DARAVE. В медиатеке им не место:
# отрендеренный сет — это не трек, сводить его не с чем и незачем.
#
# Диджей нашёл это дважды. Сперва: «в библиотеке есть какие-то треки,
# которых нет в папке Музыка — my-room_*.mp3». Потом в его библиотеке из
# 70 записей нашлись четыре таких, и одна из них — полный сет на 117.5
# BPM, который перекашивал и статистику темпа, и выбор пар.
OWN_OUTPUT_DIRS = ("sets", "demos", "recordings", "stems", "scan_dbs",
                   "mixxx-export")
OWN_OUTPUT_PREFIXES = ("my-room_", "my-room-")


def is_own_output(path) -> bool:
    """Файл, который DARAVE сам и сделал.

    Путь разбираем по ОБОИМ разделителям, а не через Path.parts: бэкенд
    крутится на Windows, а тесты и часть инструментов — нет, и на Linux
    «C:\\...\\sets\\x.mp3» превращается в одно длинное имя файла без
    единой части. Проверка молча переставала работать ровно там, где
    проверять и надо."""
    raw = str(path).replace("\\", "/")
    parts = [x for x in raw.split("/") if x]
    if not parts:
        return False
    name = parts[-1].lower()
    if any(name.startswith(x) for x in OWN_OUTPUT_PREFIXES):
        return True
    return any(x.lower() in OWN_OUTPUT_DIRS for x in parts[:-1])


def file_fingerprint(path) -> tuple[float, int]:
    """Отпечаток файла: время правки и размер.

    По нему скан отличает «этот трек уже разобран» от «этот трек новый или
    переписан». Хеш содержимого был бы честнее, но он читает файл целиком —
    то есть стоит ровно столько же, сколько сам разбор, и экономить было бы
    нечего. mtime+размер врут только если файл подменили с сохранением обоих;
    для музыкальной папки это не тот случай, а принудительный пересчёт всё
    равно есть (force)."""
    try:
        st = os.stat(path)
        return (round(st.st_mtime, 3), int(st.st_size))
    except OSError:
        return (0.0, 0)


def scanned_fingerprints(db_path: str) -> dict[str, tuple[float, int]]:
    """Что уже разобрано и с каким отпечатком. Ключ — канонический путь,
    тот же, которым пишет _upsert_track."""
    out: dict[str, tuple[float, int]] = {}
    try:
        conn = sqlite3.connect(db_path, timeout=60.0)
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            rows = conn.execute(
                "SELECT path, file_mtime, file_size FROM tracks").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for path_str, mtime, size in rows:
        if mtime is None or size is None:
            continue            # запись до этой версии — отпечатка нет, разберём заново
        out[str(path_str)] = (float(mtime), int(size))
    return out


def touch_scanned(db_path: str, paths: list[str]) -> None:
    """Пометить пропущенные треки как «участвовали в этом проходе».

    Без этого шага инкрементальный скан ломал библиотеку: backend забирает
    из базы строки с scanned_at >= начала прохода (чтобы не подцепить
    остатки сканов других папок), и все пропущенные треки просто исчезали
    из комнаты. Пропуск экономит РАЗБОР, а не участие в результате."""
    if not paths:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=60.0)
        conn.execute("PRAGMA busy_timeout=60000")
        now = time.time()
        conn.executemany("UPDATE tracks SET scanned_at=? WHERE path=?",
                         [(now, p) for p in paths])
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[скан] не смог отметить пропущенные ({exc!r})", file=sys.stderr)


def scan_library(root: str, db_path: str, with_tags: bool = False,
                 only_new: bool = True) -> list[dict]:
    # Быстрая проверка ДО цикла по файлам: если librosa не установлена,
    # каждый файл провалится с одной и той же ModuleNotFoundError — вместо
    # N одинаковых строк в логе печатаем одну понятную и выходим сразу.
    try:
        import librosa  # noqa: F401
    except ImportError as exc:
        print(f"librosa не установлена ({exc!r}).", file=sys.stderr)
        print(f"Выполните:  {sys.executable} -m pip install -r requirements-analysis.txt", file=sys.stderr)
        return []

    conn = init_db(db_path)
    files = [
        p for p in Path(root).rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        and not is_own_output(p)
    ]
    # Разбираем только то, чего не хватает. Раньше каждый прогон считал
    # заново ВСЮ папку: 70 треков — это десятки минут librosa на файлах,
    # которые не менялись с прошлого раза, и добавление одного нового трека
    # стоило столько же, сколько первый скан.
    known = scanned_fingerprints(db_path) if only_new else {}
    skipped: list[str] = []
    if known:
        fresh = []
        for path in files:
            canon = canon_track_path(path)
            if known.get(canon) == file_fingerprint(path):
                skipped.append(canon)
            else:
                fresh.append(path)
        files = fresh
        if skipped:
            print(f"[скан] без изменений с прошлого раза: {len(skipped)} — "
                  f"разбираю {len(files)}", file=sys.stderr)
            touch_scanned(db_path, skipped)
    results = []
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}", file=sys.stderr)
        try:
            info = analyze_track(str(path))
        except Exception as exc:
            print(f"  пропущен ({exc!r})", file=sys.stderr)
            continue
        if (info.get("duration_seconds") or 0.0) < MIN_TRACK_SECONDS:
            print(
                f"  пропущен: {info.get('duration_seconds', 0):.1f}с — короче {MIN_TRACK_SECONDS:.0f}с "
                f"(обрывок/сэмпл/запись, не сводимый трек)",
                file=sys.stderr,
            )
            continue
        tags = classify_genre_instruments(str(path)) if with_tags else None
        if info.get("genre"):
            tags = dict(tags or {})
            tags["genre"] = info["genre"]
        try:
            _upsert_track(conn, path, info, tags)
        except sqlite3.Error as exc:
            # База занята другим писателем (фоновое уточнение BPM пишет в тот
            # же файл) или иначе не приняла запись. Раньше исключение летело
            # наверх и убивало ВЕСЬ скан с кодом 1 — диджей видел «ошибку при
            # сканировании» и терял весь проход из-за одного трека.
            print(f"  не записан в БД ({exc!r})", file=sys.stderr)
            continue
        # energy_parts — разбивка энергии по признакам, полезна при ручном
        # запуске (--print), но в БД её колонки нет: добавлять колонку в уже
        # существующие базы пришлось бы миграцией, а читать её всё равно
        # некому. Чтобы путь «через БД» и путь «через --upload» отдавали
        # ОДИНАКОВЫЙ набор полей, здесь её убираем.
        row = {k: v for k, v in info.items() if k != "energy_parts"}
        results.append({"path": str(path), "name": path.name, **row, "tags": tags})
    return results


def upload_library(http_base_url: str, session_id: str, results: list[dict]) -> bool:
    """Шлёт результаты сканирования backend'у комнаты — см.
    server.py::POST /api/rooms/{session_id}/library. Только так и попадает
    в веб-чат (Библиотека/Стратегия) — сам track_analysis.py работает
    локально у диджея, backend не видит его диск напрямую."""
    import httpx  # локальный импорт: не требуем httpx, если --upload не используется

    url = f"{http_base_url.rstrip('/')}/api/rooms/{session_id}/library"

    # ВАЖНО: не пускаем запрос к своему же localhost через системный прокси.
    # httpx по умолчанию (trust_env=True) читает HTTP_PROXY/ALL_PROXY из
    # окружения. У пользователя с VPN/прокси-клиентом (типичная ситуация,
    # когда Gemini доступен только через прокси) запрос на
    # http://localhost:8765 уходил в прокси, тот не мог достучаться до
    # "localhost" со своей стороны и отдавал 503 с ПУСТЫМ телом — выглядело
    # как "backend сломался", хотя backend был жив и отвечал браузеру.
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    is_local = host in ("localhost", "127.0.0.1", "::1", "[::1]")

    try:
        with httpx.Client(timeout=60.0, trust_env=not is_local) as client:
            response = client.post(url, json={"tracks": results})
        response.raise_for_status()
        print(f"Библиотека ({len(results)} треков) загружена в комнату '{session_id}'", file=sys.stderr)
        return True
    except httpx.HTTPStatusError as exc:
        resp = exc.response
        body = (resp.text[:500] if resp is not None else "") or "(пустое тело)"
        server_hdr = resp.headers.get("server", "?") if resp is not None else "?"
        print(
            f"Не удалось загрузить библиотеку: {exc!r} | Server: {server_hdr} | ответ: {body}",
            file=sys.stderr,
        )
        if resp is not None and resp.status_code in (502, 503, 504) and not resp.text.strip():
            print(
                "Пустой 5xx без тела на localhost — почти наверняка запрос перехватил системный "
                "прокси/VPN (HTTP_PROXY/ALL_PROXY). Backend тут ни при чём.",
                file=sys.stderr,
            )
        return False
    except Exception as exc:
        print(f"Не удалось загрузить библиотеку: {exc!r}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="DARAVE — сканер библиотеки (BPM/тональность/энергетика)")
    parser.add_argument("--scan", required=True, help="Папка с треками для сканирования (рекурсивно)")
    parser.add_argument("--db", default="library.db", help="Куда писать результаты (SQLite)")
    parser.add_argument(
        "--with-tags", action="store_true",
        help="Пытаться также определить жанр/инструменты (требует panns_inference, см. classify_genre_instruments)",
    )
    parser.add_argument("--print", dest="print_json", action="store_true", help="Печатать результаты как JSON")
    parser.add_argument(
        "--upload", default=None, metavar="HTTP_URL",
        help="После сканирования загрузить результаты в комнату backend'а (например http://localhost:8765)",
    )
    parser.add_argument("--room", default=None, help="Код комнаты для --upload (тот же, что --companion-id)")
    parser.add_argument("--rescan-all", action="store_true",
                        help="Разобрать заново ВСЕ файлы, а не только новые и изменившиеся")
    args = parser.parse_args()

    run_started = time.time()
    results = scan_library(args.scan, args.db, with_tags=args.with_tags,
                           only_new=not args.rescan_all)
    print(f"Готово: разобрано {len(results)} треков -> {args.db}", file=sys.stderr)
    # results — только РАЗОБРАННОЕ в этом проходе. Наружу (и в --print, и в
    # --upload) отдаём всю папку: инкрементальный скан не должен выглядеть
    # как «библиотека усохла до трёх треков».
    library = load_library_from_db(args.db, run_started) or results
    if args.print_json:
        print(json.dumps(library, ensure_ascii=False, indent=2))
    if args.upload:
        if not args.room:
            print("--upload требует --room", file=sys.stderr)
        elif not library:
            # Пустой результат — почти всегда ошибка сканирования (см. лог
            # выше), а не "в папке правда нет треков". НЕ загружаем: иначе
            # молча затираем уже загруженную ранее библиотеку комнаты.
            print("Треков не найдено/не обработано — библиотеку НЕ загружаю (не хочу затирать то, что уже было).", file=sys.stderr)
        else:
            upload_library(args.upload, args.room, library)


if __name__ == "__main__":
    main()
