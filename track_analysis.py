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


def analyze_track(path: str) -> dict:
    """Возвращает {bpm, key, camelot, energy, brightness, danceability,
    duration_seconds, structure}. Числовые оценки 0..1, кроме bpm (уд/мин) и
    duration_seconds; structure — см. detect_structure()."""
    import librosa
    import numpy as np

    y, sr = librosa.load(path, sr=22050, mono=True)
    duration = float(len(y) / sr)

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(tempo if np.isscalar(tempo) else tempo[0])
    if bpm <= 0 or len(beat_frames) == 0:
        # beat_track изредка не находит долей (тихие интро, нестандартный
        # ритм-рисунок) — не отдаём BPM=0, а пересчитываем через огибающую
        # onset'ов напрямую, это надёжнее для голого темпа (без самих долей).
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        try:
            tempo_fn = librosa.feature.rhythm.tempo  # librosa >= 0.10.2
        except AttributeError:
            tempo_fn = librosa.beat.tempo  # старые версии librosa
        tempo_fallback = tempo_fn(onset_envelope=onset_env, sr=sr)
        bpm = float(tempo_fallback[0]) if len(tempo_fallback) else 0.0

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key = _estimate_key(chroma.mean(axis=1))

    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    energy, energy_parts = _estimate_energy(y, sr, onset_env, duration)

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    brightness = float(np.clip(centroid.mean() / (sr / 4), 0.0, 1.0))
    danceability = _estimate_danceability(librosa, np, beat_frames, sr, onset_env)

    bpm = normalize_bpm(bpm)
    # librosa.beat.beat_track отдаёт темп с сетки-приора: на этой библиотеке
    # 22 трека получили РОВНО 172.30, хотя на деле там 171.00, 173.00,
    # 174.03. Ошибка в 1-2 BPM — это 1%: за 30 секунд сведения вторая дека
    # уезжает почти на целую долю, и свести такты нельзя в принципе.
    # Уточняем гребёнчатым поиском по длинному окну (beatgrid.refine_tempo).
    bpm = refine_bpm(y, sr, bpm)
    structure = detect_structure(y, sr, bpm)
    # Где реально играют барабаны. Без этого точки входа выбирались по
    # дропам, которые детектор находит с уверенностью 0.14-0.23, и трек
    # заводился в собственное эмбиентное интро.
    try:
        import beatgrid

        structure["drum_map"] = beatgrid.drum_map(y, sr, bpm)
        structure["chroma_map"] = beatgrid.chroma_map(y, sr, bpm)
    except Exception:
        pass

    return {
        "bpm": round(bpm, 2),
        "key": key,
        "camelot": key_to_camelot(key),
        "energy": round(energy, 3),
        "energy_parts": {k: round(v, 3) for k, v in energy_parts.items()},
        "brightness": round(brightness, 3),
        "danceability": round(danceability, 3),
        "duration_seconds": round(duration, 1),
        "structure": structure,
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


def detect_structure(y, sr: int, bpm: float) -> dict:
    """Эвристический поиск "мест для сведения" — НЕ нейросетевая структурная
    сегментация (это отдельная, более тяжёлая задача), а анализ огибающей
    энергии: резкий скачок энергии, удержанный N+ тактов = "drop"; устойчиво
    тихий участок N+ тактов = "breakdown"/яма. Плюс равномерная сетка
    "фразовых" точек (каждые 8/16 тактов от первой доли) — для техник вроде
    Phrase Match, которым не нужен именно дроп, просто чистая граница фразы.

    Возвращает {drops, breakdowns, phrase_boundaries} — время в секундах +
    номер такта (bar_index, от начала трека, 4/4 по умолчанию)."""
    import librosa
    import numpy as np

    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(range(len(rms)), sr=sr, hop_length=hop_length)
    if len(rms) < 8 or bpm <= 0:
        return {"drops": [], "breakdowns": [], "phrase_boundaries": []}

    # Сглаживание — простое скользящее среднее на ~1 секунду, чтобы не ловить
    # отдельные удары как "дропы".
    win = max(1, int(sr / hop_length))
    kernel = np.ones(win) / win
    smoothed = np.convolve(rms, kernel, mode="same")
    norm = smoothed / (smoothed.max() + 1e-9)

    beat_seconds = 60.0 / bpm
    bar_seconds = beat_seconds * 4
    min_sustain_bars = 4
    min_sustain_frames = max(1, int(min_sustain_bars * bar_seconds / (hop_length / sr)))

    # Пороги — в ДЕЦИБЕЛАХ и ОТНОСИТЕЛЬНО громких мест самого трека.
    #
    # Почему не линейно (было 0.65/0.25 от пика): мастеринг подтягивает тихие
    # места вверх, и брейкдаун, задуманный как -10 dB, после компрессии
    # оказывается на -8 dB, то есть линейно 0.38 — выше порога 0.25. Порог
    # не срабатывал никогда, отсюда "Брейкдаунов: 0" у каждого трека.
    #
    # Почему относительно трека, а не абсолютные -6/-9: у сжатого в кашу
    # мастеринга и у динамичного трека разный размах, один и тот же
    # абсолютный порог для обоих не годится. За "громко" берём 90-й
    # перцентиль уровня, дальше: дроп — не ниже -3 dB от него, брейкдаун —
    # ниже -6 dB (перцептивно это и есть "барабаны ушли").
    db = 20.0 * np.log10(norm + 1e-6)
    loud_ref = float(np.percentile(db, 90))
    high_thresh = loud_ref - 3.0
    low_thresh = loud_ref - 6.0
    norm = db  # дальше все сравнения идут в dB

    def bar_index(t: float) -> int:
        return int(t / bar_seconds)

    drops, breakdowns = [], []
    i = 1
    while i < len(norm):
        # Drop: переход из "тихо" в "громко", удержанный min_sustain_frames.
        if norm[i] >= high_thresh and norm[i - 1] < high_thresh:
            if i + min_sustain_frames < len(norm) and np.mean(norm[i:i + min_sustain_frames]) >= high_thresh - 2.0:
                drops.append({
                    "time_seconds": round(float(times[i]), 2),
                    "bar_index": bar_index(times[i]),
                    "confidence": round(float(norm[i] - norm[i - 1]), 3),
                })
                i += min_sustain_frames
                continue
        i += 1

    i = 1
    in_breakdown = False
    breakdown_start = 0.0
    while i < len(norm):
        if norm[i] < low_thresh and not in_breakdown:
            in_breakdown = True
            breakdown_start = times[i]
        elif norm[i] >= low_thresh and in_breakdown:
            in_breakdown = False
            if times[i] - breakdown_start >= min_sustain_bars * bar_seconds:
                breakdowns.append({
                    "start_seconds": round(float(breakdown_start), 2),
                    "end_seconds": round(float(times[i]), 2),
                    "bar_index": bar_index(breakdown_start),
                })
        i += 1
    # Брейкдаун может тянуться до самого конца трека (аутро) — цикл выше
    # фиксирует его только при переходе обратно наверх, здесь дописываем
    # "хвостовой" случай.
    if in_breakdown and (times[-1] - breakdown_start) >= min_sustain_bars * bar_seconds:
        breakdowns.append({
            "start_seconds": round(float(breakdown_start), 2),
            "end_seconds": round(float(times[-1]), 2),
            "bar_index": bar_index(breakdown_start),
        })

    duration = float(times[-1]) if len(times) else 0.0
    phrase_boundaries = [
        round(b * bar_seconds, 2)
        for b in range(0, bar_index(duration) + 1, 16)
        if b * bar_seconds <= duration
    ]

    return {"drops": drops, "breakdowns": breakdowns, "phrase_boundaries": phrase_boundaries}


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
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tracks (
            path TEXT PRIMARY KEY,
            bpm REAL, key TEXT, camelot TEXT, energy REAL, brightness REAL,
            danceability REAL, duration_seconds REAL,
            structure_json TEXT, tags_json TEXT,
            scanned_at REAL
        )"""
    )
    conn.commit()
    return conn


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
    conn = sqlite3.connect(db_path)
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

    # Темп приводим ПО ВСЕЙ БИБЛИОТЕКЕ СРАЗУ, а не по одному треку.
    # Раньше каждый трек независимо сворачивался в фиксированное окно
    # [90,180) — и окно разрезало темповое семейство: 70 превращалось в
    # 140, а 95 оставалось 95, хотя это соседние темпы. Теперь центр
    # берётся из самой библиотеки, а трек подтягивается к его октаве,
    # только если он туда реально попадает (см. tempo.fold_to_center).
    import tempo as _tempo
    _tempo.normalize_library(out)
    return out


def scan_library(root: str, db_path: str, with_tags: bool = False) -> list[dict]:
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
    ]
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
        conn.execute(
            """INSERT INTO tracks (path, bpm, key, camelot, energy, brightness, danceability,
                                    duration_seconds, structure_json, tags_json, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   bpm=excluded.bpm, key=excluded.key, camelot=excluded.camelot,
                   energy=excluded.energy, brightness=excluded.brightness,
                   danceability=excluded.danceability, duration_seconds=excluded.duration_seconds,
                   structure_json=excluded.structure_json, tags_json=excluded.tags_json,
                   scanned_at=excluded.scanned_at""",
            (
                str(path), info["bpm"], info["key"], info["camelot"], info["energy"], info["brightness"],
                info["danceability"], info["duration_seconds"],
                json.dumps(info["structure"]), json.dumps(tags) if tags else None, time.time(),
            ),
        )
        conn.commit()
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
    args = parser.parse_args()

    results = scan_library(args.scan, args.db, with_tags=args.with_tags)
    print(f"Готово: {len(results)} треков -> {args.db}", file=sys.stderr)
    if args.print_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    if args.upload:
        if not args.room:
            print("--upload требует --room", file=sys.stderr)
        elif not results:
            # Пустой результат — почти всегда ошибка сканирования (см. лог
            # выше), а не "в папке правда нет треков". НЕ загружаем: иначе
            # молча затираем уже загруженную ранее библиотеку комнаты.
            print("Треков не найдено/не обработано — библиотеку НЕ загружаю (не хочу затирать то, что уже было).", file=sys.stderr)
        else:
            upload_library(args.upload, args.room, results)


if __name__ == "__main__":
    main()
