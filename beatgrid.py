"""
beatgrid.py — где в аудио-буфере находятся доли и, главное, ПЕРВАЯ доля
такта (даунбит).

Зачем отдельный модуль: BPM у нас уже есть (из анализа библиотеки) и ему
можно верить. Чего у нас НЕ было — фазы. Рендер брал «последние 25 секунд
трека A» и «первые 25 секунд трека B», то есть обрезал оба в случайной
точке цикла. Даже при идеально совпадающем темпе бочки двух дек попадали
в разные места такта — это и есть «неверно синхронизирует такты»: не
темп разъезжается, а фаза изначально не совпадала.

Метод — гребёнчатый фильтр по известному периоду, а не полный
beat-tracking:
  * период доли P = 60/BPM известен => искать надо ОДНО число, фазу
    phi in [0, P). Это устойчивее, чем позволять трекеру заново выбирать
    темп (на DnB он регулярно выбирает половинный).
  * для каждой phi суммируем onset-энергию во всех моментах phi + k*P.
    Максимум суммы = сетка попала на удары.
  * даунбит выбираем отдельным голосованием среди 4 долей такта по
    НИЗУ спектра: в drum'n'bass первая доля такта почти всегда бочка,
    а бочка — это низ. Широкополосный onset тут врёт (снейр на 3-й доле
    громче бочки).

Всё в секундах от начала переданного буфера.
"""
from __future__ import annotations

import numpy as np

HOP = 256  # ~5.8 мс при 44100 — точность фазы заведомо лучше слухового порога (~10 мс)


def _onset_envelopes(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, float]:
    """(широкополосный onset, низовой onset, секунд на кадр)."""
    import librosa

    if len(y) < sr // 4:
        empty = np.zeros(1)
        return empty, empty, HOP / sr

    mel = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=96, fmax=sr / 2)
    db = librosa.power_to_db(mel, ref=np.max)
    # onset = положительная разность по времени (spectral flux)
    flux = np.diff(db, axis=1, prepend=db[:, :1])
    flux = np.maximum(0.0, flux)

    mel_f = librosa.mel_frequencies(n_mels=96, fmax=sr / 2)
    low = mel_f <= 160.0  # бочка/саб — то, по чему слышно «раз»

    wide = flux.sum(axis=0)
    lows = flux[low].sum(axis=0)
    return wide, lows, HOP / sr


def _comb_phase(env: np.ndarray, period_frames: float, n_phases: int = 240) -> tuple[float, np.ndarray]:
    """Возвращает (лучшая фаза в кадрах, вектор откликов по всем фазам)."""
    if len(env) < period_frames * 2:
        return 0.0, np.zeros(n_phases)
    n_hits = int(len(env) / period_frames)
    phases = np.linspace(0.0, period_frames, n_phases, endpoint=False)
    k = np.arange(n_hits)
    # матрица индексов [n_phases, n_hits]
    idx = np.rint(phases[:, None] + k[None, :] * period_frames).astype(int)
    idx = np.clip(idx, 0, len(env) - 1)
    # ±1 кадр допуска — фаза непрерывна, а сетка кадров дискретна
    resp = env[idx] + 0.5 * env[np.clip(idx - 1, 0, len(env) - 1)] + 0.5 * env[np.clip(idx + 1, 0, len(env) - 1)]
    scores = resp.sum(axis=1)
    return float(phases[int(np.argmax(scores))]), scores


def beat_phase(y: np.ndarray, sr: int, bpm: float) -> dict:
    """Фаза сетки в буфере y при известном bpm.

    Возвращает:
      beat_offset — секунды от начала буфера до первой доли,
      downbeat_offset — секунды до первой доли ТАКТА (4 доли),
      confidence — 0..1, насколько гребёнка выделилась над средним,
      beat_seconds / bar_seconds.
    """
    bpm = float(bpm) if bpm and bpm > 20 else 172.0
    beat_sec = 60.0 / bpm
    bar_sec = beat_sec * 4

    wide, lows, frame_sec = _onset_envelopes(y, sr)
    if len(wide) < 8:
        return {"beat_offset": 0.0, "downbeat_offset": 0.0, "confidence": 0.0,
                "beat_seconds": beat_sec, "bar_seconds": bar_sec}

    period_frames = beat_sec / frame_sec
    phi_frames, scores = _comb_phase(wide, period_frames)
    beat_offset = phi_frames * frame_sec

    mean, peak = float(np.mean(scores)), float(np.max(scores))
    confidence = 0.0 if peak <= 0 else float(np.clip((peak - mean) / (peak + 1e-9), 0.0, 1.0))

    # --- какая из 4 долей такта — «раз»: голосуем низом ---
    best_b, best_score = 0, -1.0
    for b in range(4):
        t0 = beat_offset + b * beat_sec
        idx = np.rint((t0 + np.arange(0, max(1, int((len(lows) * frame_sec - t0) / bar_sec))) * bar_sec) / frame_sec)
        if len(idx) == 0:
            continue
        idx = np.clip(idx.astype(int), 0, len(lows) - 1)
        s = float(lows[idx].sum() + 0.5 * lows[np.clip(idx - 1, 0, len(lows) - 1)].sum())
        if s > best_score:
            best_score, best_b = s, b

    return {
        "beat_offset": round(beat_offset, 4),
        "downbeat_offset": round(beat_offset + best_b * beat_sec, 4),
        "confidence": round(confidence, 3),
        "beat_seconds": beat_sec,
        "bar_seconds": bar_sec,
        "downbeat_index": best_b,
    }


def snap_to_downbeat(offset_seconds: float, grid: dict, mode: str = "nearest") -> float:
    """Двигает произвольную точку на ближайший (или следующий) даунбит
    той же сетки. grid — результат beat_phase() для ТОГО ЖЕ буфера."""
    bar = grid["bar_seconds"]
    d0 = grid["downbeat_offset"]
    k = (offset_seconds - d0) / bar
    if mode == "next":
        k = np.ceil(k - 1e-6)
    elif mode == "prev":
        k = np.floor(k + 1e-6)
    else:
        k = np.round(k)
    return max(0.0, d0 + float(k) * bar)


def first_musical_downbeat(y: np.ndarray, sr: int, bpm: float,
                           silence_db: float = -45.0) -> dict:
    """То же, что beat_phase, но сначала пропускает тишину/фейд-ин в начале
    файла: интро у многих релизов начинается с 1-3 секунд почти нуля, и
    «грузим трек B с offset=0» означало грузить эту тишину."""
    import librosa

    grid = beat_phase(y, sr, bpm)
    if len(y) == 0:
        return grid
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    db = librosa.amplitude_to_db(rms, ref=np.max(rms) if np.max(rms) > 0 else 1.0)
    voiced = np.nonzero(db > silence_db)[0]
    start = float(voiced[0] * HOP / sr) if len(voiced) else 0.0
    grid = dict(grid)
    grid["audio_start"] = round(start, 3)
    grid["downbeat_offset"] = round(snap_to_downbeat(start, grid, mode="next"), 4)
    return grid


# ---------------------------------------------------------------------------
# Точный темп
# ---------------------------------------------------------------------------
# Зачем это здесь, если BPM «уже есть в библиотеке»: librosa.beat.beat_track
# отдаёт темп с сетки-приора, поэтому в базе у 22 треков стоит РОВНО 172.30,
# хотя на деле там 171.5, 172.2, 173.2. Ошибка в 1 BPM — это 0.6%: за 30
# секунд сведения сетка уезжает на 150 мс, почти на полдоли. Темп «совпадает»,
# а такты разъезжаются — ровно то, что слышно в миксе.
# Плюс тот же трекер регулярно отдаёт 2/3 темпа (176 -> 117.5), и такие
# треки выпадают из семьи темпов, хотя сводятся идеально.

SEARCH_RATIOS = (1.0, 2.0, 0.5, 1.5, 2.0 / 3.0, 4.0 / 3.0, 0.75, 3.0)


def _comb_best(env: np.ndarray, period_frames: float, n_phases: int = 128) -> float:
    """«Резкость» сетки данного периода: во сколько раз лучшая фаза лучше
    средней.

    Именно отношение, а не сумма и не среднее. Сумма всегда выигрывает у
    быстрого темпа (больше ударов попало), среднее — у медленного (сетка
    садится только на сильные доли и пропускает слабые). Отношение
    «лучшая фаза / случайная фаза» не зависит ни от числа ударов, ни от
    громкости — темпы разных октав сравниваются честно."""
    if period_frames < 4 or len(env) < period_frames * 8:
        return 0.0
    n_hits = int(len(env) / period_frames)
    phases = np.linspace(0.0, period_frames, n_phases, endpoint=False)
    k = np.arange(n_hits)
    idx = np.rint(phases[:, None] + k[None, :] * period_frames).astype(int)
    idx = np.clip(idx, 0, len(env) - 1)
    resp = env[idx] + 0.6 * env[np.clip(idx - 1, 0, len(env) - 1)] + 0.6 * env[np.clip(idx + 1, 0, len(env) - 1)]
    scores = resp.mean(axis=1)
    m = float(np.mean(scores))
    return 0.0 if m <= 1e-9 else float(np.max(scores) / m)


def refine_tempo(y: np.ndarray, sr: int, bpm_hint: float,
                 band: tuple[float, float] = (60.0, 210.0),
                 span_pct: float = 3.0, step_bpm: float = 0.05,
                 allow_ratios: tuple[float, ...] = (1.0,)) -> dict:
    """Уточняет темп вокруг подсказки и её музыкальных кратных.

    Ищем максимум гребёнчатого отклика: сетка с правильным периодом
    попадает на удары на ВСЁМ окне, с чуть неправильным — уезжает и
    отклик падает. Именно длина окна и даёт точность: 40 секунд при
    172 BPM это ~115 долей, ошибка 0.05 BPM уводит хвост на 12 мс.
    """
    wide, lows, frame_sec = _onset_envelopes(y, sr)
    if len(wide) < 64:
        return {"bpm": float(bpm_hint), "confidence": 0.0, "ratio": 1.0, "refined": False}

    hint = float(bpm_hint) if bpm_hint and bpm_hint > 20 else 120.0
    # ВАЖНО: по умолчанию октаву не пересматриваем (allow_ratios=(1.0,)).
    # Контраст гребёнки на половинном темпе почти всегда выше — сетка
    # садится только на сильные доли, — поэтому «честное» сравнение октав
    # по звуку невозможно в принципе. Октаву решает tempo.py по полосе
    # исполнения и голосованию всей библиотеки; здесь только точная
    # доводка внутри уже выбранной октавы.
    seen: list[float] = []
    for r in allow_ratios:
        c = hint * r
        if band[0] <= c <= band[1] and all(abs(c - s) > 0.5 for s in seen):
            seen.append(c)
    if not seen:
        seen = [hint]

    def scan(lo: float, hi: float, step: float, ratio: float, acc: list) -> None:
        for bpm in np.arange(lo, hi + 1e-9, step):
            pf = (60.0 / bpm) / frame_sec
            acc.append((_comb_best(wide, pf), float(bpm), ratio))

    coarse: list = []
    for center in seen:
        scan(center * (1 - span_pct / 100.0), center * (1 + span_pct / 100.0), 0.4, center / hint, coarse)
    if not coarse:
        return {"bpm": hint, "confidence": 0.0, "ratio": 1.0, "refined": False}
    coarse.sort(reverse=True)
    fine: list = []
    for s, bpm, ratio in coarse[:3]:  # три лучших кандидата уточняем мелким шагом
        scan(bpm - 0.5, bpm + 0.5, step_bpm, ratio, fine)
    best = max(fine or coarse)

    # уверенность: насколько победитель выделился над «средним темпом»
    ref = np.mean([_comb_best(wide, (60.0 / b) / frame_sec)
                   for b in np.linspace(band[0], band[1], 24)])
    conf = 0.0 if best[0] <= 0 else float(np.clip((best[0] - ref) / best[0], 0.0, 1.0))
    return {"bpm": round(best[1], 3), "confidence": round(conf, 3),
            "ratio": round(best[2], 4), "refined": True,
            "hint": round(hint, 2)}


def refine_tempo_file(path: str, bpm_hint: float, sr: int = 22050,
                      probe_seconds: float = 45.0,
                      band: tuple[float, float] = (60.0, 210.0),
                      allow_ratios: tuple[float, ...] = (1.0,)) -> dict:
    """refine_tempo по файлу: берём кусок из тела трека (не интро — там
    часто нет барабанов вообще)."""
    import librosa

    try:
        dur = float(librosa.get_duration(path=path))
    except Exception:
        dur = probe_seconds
    start = max(0.0, dur * 0.45 - probe_seconds / 2)
    y, _ = librosa.load(path, sr=sr, mono=True, offset=start, duration=probe_seconds)
    out = refine_tempo(y, sr, bpm_hint, band=band, allow_ratios=allow_ratios)
    out["probe_start"] = round(start, 1)
    return out


def drum_map(y: np.ndarray, sr: int, bpm: float, win_bars: float = 4.0,
             threshold: float = 0.5) -> dict:
    """Где в треке РЕАЛЬНО играют барабаны — по низу, по такту.

    Это оказалось главной причиной кривого звучания. Точки входа
    выбирались по structure-анализу («за 16 тактов до дропа»), а дропы он
    находит с уверенностью 0.14-0.23, то есть почти наугад. Половина
    переходов заводила новый трек в его эмбиентное интро: фейдер резал с
    громкого бита на пустоту. Ни техника, ни точность фазы этого не
    спасают — сводить просто не с чем.

    Метрика — энергия ниже 180 Гц относительно 85-го перцентиля этой же
    энергии по треку. Проверено: у Hybrid Minds интро кончается на 85 с,
    у Dualistic на 65 с — сплошным блоком, без дребезга на границе.

    Что НЕ работает и почему: контраст гребёнки (сколько выделяется
    лучшая фаза) в интро ВЫШЕ, чем в теле трека — редкие одиночные
    события дают острый пик, а плотная партия барабанов размазывает
    отклик по всем фазам. По контрасту интро выглядит «ритмичнее» дропа.
    """
    import librosa

    bpm = float(bpm) if bpm and bpm > 20 else 172.0
    bar = 60.0 / bpm * 4
    if len(y) < sr * 4:
        return {"bar_seconds": round(bar, 4), "on": "", "drums_start": 0.0, "coverage": 0.0}

    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = S[freqs <= 180.0]
    lb = np.sqrt((low ** 2).sum(axis=0) + 1e-12)
    ref = float(np.percentile(lb, 85))
    if ref <= 1e-9:
        return {"bar_seconds": round(bar, 4), "on": "", "drums_start": 0.0, "coverage": 0.0}

    frame_sec = HOP / sr
    step = max(1, int(round(bar / frame_sec)))
    span = max(step, int(round(win_bars * bar / frame_sec)))
    flags = []
    for start in range(0, max(1, len(lb) - span), step):
        flags.append(float(np.mean(lb[start:start + span])) / ref > threshold)

    drums_start, run = 0.0, 0
    for i, v in enumerate(flags):
        run = run + 1 if v else 0
        if run >= 4:
            drums_start = max(0.0, (i - run + 1) * bar)
            break

    return {"bar_seconds": round(bar, 4),
            "on": "".join("1" if v else "0" for v in flags),   # компактно для хранения в БД
            "drums_start": round(drums_start, 2),
            "coverage": round(float(np.mean(flags)) if flags else 0.0, 3)}


def energy_map(y: np.ndarray, sr: int, bpm: float) -> dict:
    """Уровень низа ПО ТАКТАМ и события структуры: брейкдауны, ямы, дропы.

    Зачем отдельно от structure-анализа: там дропы находятся с
    уверенностью 0.14-0.26, то есть почти наугад, и точка входа по ним
    попадала мимо. А брейкдаун и дроп на самом деле видны прямым
    измерением — это ровно те места, где низ уходит и где он возвращается.
    Ту же метрику (энергия ниже 180 Гц относительно 85-го перцентиля по
    треку) уже проверили на drum_map, она даёт сплошные блоки без
    дребезга на границе.

    Разница между тремя событиями — только в длине и направлении:
      * брейкдаун — низ ушёл и не возвращается 4 такта и дольше;
      * яма — та же просадка, но короткая (1-3 такта): вдох перед дропом;
      * дроп — такт, где низ вернулся после просадки и держится 4 такта.

    Дроп мы намеренно считаем как «конец просадки», а не как «пик
    энергии». Пиков в драм-н-бейсе много, а событие, которое слышно как
    дроп, ровно одно — момент, когда после пустоты возвращается бас.
    """
    import librosa

    bpm = float(bpm) if bpm and bpm > 20 else 172.0
    bar = 60.0 / bpm * 4
    empty = {"bar_seconds": round(bar, 4), "level": [], "drops": [],
             "breakdowns": [], "pits": []}
    if len(y) < sr * 8:
        return empty

    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    lb = np.sqrt((S[freqs <= 180.0] ** 2).sum(axis=0) + 1e-12)
    ref = float(np.percentile(lb, 85))
    if ref <= 1e-9:
        return empty

    frame_sec = HOP / sr
    step = max(1, int(round(bar / frame_sec)))
    n_bars = max(1, len(lb) // step)
    level = np.array([float(np.mean(lb[i * step:(i + 1) * step])) / ref
                      for i in range(n_bars)])

    LOW, HIGH, HOLD = 0.45, 0.70, 4
    is_low = level < LOW

    # разбиваем на однородные полосы
    runs = []
    start = 0
    for i in range(1, n_bars + 1):
        if i == n_bars or is_low[i] != is_low[start]:
            runs.append((start, i, bool(is_low[start])))
            start = i

    breakdowns, pits, drops = [], [], []
    for idx, (a, b, low) in enumerate(runs):
        if not low:
            continue
        length = b - a
        if length >= HOLD:
            breakdowns.append((round(a * bar, 2), round(b * bar, 2)))
        elif a > 0:
            pits.append(round(a * bar, 2))
        # дроп — возврат низа после просадки, если он держится
        if b < n_bars and float(np.mean(level[b:b + HOLD])) > HIGH:
            drops.append(round(b * bar, 2))

    return {"bar_seconds": round(bar, 4),
            "level": [round(float(v), 3) for v in level],
            "drops": drops,
            "breakdowns": breakdowns,
            "pits": pits}


def level_at(emap: dict, seconds: float) -> float:
    """Уровень низа в этой секунде, 1.0 — как в теле трека."""
    bars = emap.get("level") or []
    bar = float(emap.get("bar_seconds") or 1.379)
    if not bars or bar <= 0:
        return 1.0
    i = int(max(0.0, seconds) // bar)
    return float(bars[min(i, len(bars) - 1)])


def next_drop(emap: dict, after_seconds: float = 0.0) -> float | None:
    """Ближайший дроп после указанной секунды."""
    for d in emap.get("drops") or []:
        if d > after_seconds + 1e-6:
            return float(d)
    return None


def in_breakdown(emap: dict, seconds: float) -> bool:
    for a, b in emap.get("breakdowns") or []:
        if a - 1e-6 <= seconds < b:
            return True
    return False


def drums_at(dmap: dict, seconds: float, need_bars: float = 0.0) -> bool:
    """Играют ли барабаны в этот момент — и держатся ли ещё need_bars тактов."""
    on = (dmap or {}).get("on") or ""
    bar = float((dmap or {}).get("bar_seconds") or 1.4)
    if not on:
        return True          # карты нет — не мешаем выбору
    i = int(round(float(seconds) / bar))
    j = i + int(round(need_bars))
    if i < 0 or i >= len(on):
        return False
    window = on[i:max(i + 1, min(len(on), j + 1))]
    return window.count("1") >= max(1, int(len(window) * 0.7))


def drums_start_of(dmap: dict) -> float:
    return float((dmap or {}).get("drums_start") or 0.0)


def chroma_map(y: np.ndarray, sr: int, bpm: float, phrase_bars: float = 8.0) -> dict:
    """Гармонический профиль трека по фразам.

    Зачем, если тональность уже есть: camelot считается по ВСЕМУ треку, а
    сводим мы конкретные 30 секунд. Измерено на реальном плане — связи
    между ними нет: у пары с совместимостью camelot 0.90 накладываемые
    куски расходятся (0.70), у пары с 0.25 — сходятся (0.71). Оценка по
    треку целиком просто не про то место, где треки встречаются.

    Барабаны из хромы убираем: иначе профиль меряет широкополосный удар,
    а не гармонию, и все треки выглядят одинаково.
    """
    import librosa

    bpm = float(bpm) if bpm and bpm > 20 else 172.0
    span = 60.0 / bpm * 4 * phrase_bars
    if len(y) < sr * 8:
        return {"phrase_seconds": round(span, 3), "frames": []}

    y_h = librosa.effects.harmonic(y, margin=3.0)
    hop = 512
    c = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=hop)
    per = max(1, int(round(span * sr / hop)))
    frames = []
    for i in range(0, c.shape[1], per):
        v = c[:, i:i + per].mean(axis=1)
        n = float(np.linalg.norm(v))
        frames.append([round(float(x / n), 3) for x in v] if n > 1e-9 else [0.0] * 12)
    return {"phrase_seconds": round(span, 3), "frames": frames}


def chroma_at(cmap: dict, seconds: float) -> np.ndarray | None:
    frames = (cmap or {}).get("frames") or []
    span = float((cmap or {}).get("phrase_seconds") or 0)
    if not frames or span <= 0:
        return None
    i = int(round(float(seconds) / span))
    if i < 0 or i >= len(frames):
        return None
    return np.array(frames[i], dtype=float)


def harmony_fit(cmap_a: dict, at_a: float, cmap_b: dict, at_b: float,
                phrases: int = 3) -> float | None:
    """Насколько сходятся гармонии двух кусков (1 = совпадают).

    Смотрим несколько фраз подряд, а не одну: за время сведения гармония
    успевает смениться, и важно, чтобы они не разошлись по дороге."""
    span_a = float((cmap_a or {}).get("phrase_seconds") or 0)
    span_b = float((cmap_b or {}).get("phrase_seconds") or 0)
    if span_a <= 0 or span_b <= 0:
        return None
    vals = []
    for k in range(max(1, phrases)):
        va = chroma_at(cmap_a, at_a + k * span_a)
        vb = chroma_at(cmap_b, at_b + k * span_b)
        if va is None or vb is None:
            continue
        vals.append(float(np.dot(va, vb)))
    return float(np.mean(vals)) if vals else None
