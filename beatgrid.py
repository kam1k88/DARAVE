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


# ---------------------------------------------------------------------------
# Карты трека: где играют барабаны и где ходит низ
# ---------------------------------------------------------------------------
# Обе карты режут трек на такты, и обе раньше резали ОТ НУЛЯ ФАЙЛА. Это и
# оказалось причиной систематического промаха меток. Замерено на «Sili —
# Playing In The Darkness»: все четыре дропа стояли на 0.78 с раньше
# слышимого — одинаково, до третьего знака. 0.78 с при 174 BPM это 0.57
# такта, то есть ровно фаза сетки, а не ошибка детектора: у релиза перед
# первой долей есть пауза, и «такт N от нуля файла» не совпадает с
# музыкальным тактом.
#
# Теперь у обеих карт есть якорь `bar_offset` — секунда первой линии
# сетки. Такт i покрывает [bar_offset + i*bar, bar_offset + (i+1)*bar).
# Старые карты без этого поля читаются как bar_offset = 0, поэтому
# пересчёт библиотеки не обязателен, но желателен.

LOW_HZ = 180.0        # полоса «низа»: бочка и саб
# Пороги — в долях от «полной» партии этого же трека (90-й перцентиль
# по тактам), а не в абсолютных величинах: у одного трека дроп вдвое
# громче интро, у другого вдесятеро, и общей шкалы тут нет.
DRUMS_ON = 0.25       # барабаны вообще звучат (хотя бы редкая перкуссия)
DRUMS_DENSE = 0.70    # партия плотная — это и есть «бит пошёл»
DROP_DOWN = 0.50      # ниже этого партия считается разреженной
DROP_FULL = 0.70      # столько держится после дропа
DROP_STEP = 0.70      # предыдущие 8 тактов не громче этой доли от дропа
DROP_SPAN = 8         # столько тактов дроп должен держаться
PHRASE_SNAP = 2       # на столько тактов метку можно подвинуть на фразу
DROP_LOW = 0.45       # запасной путь по миксу: ниже этого — низ ушёл
DROP_HIGH = 0.70      # запасной путь по миксу: выше этого — низ вернулся
DROP_HOLD = 4         # столько тактов смотрим после возврата
DROP_CONFIRM = 3      # столько из них должны быть выше DROP_HIGH
PHRASE_BARS = 8       # фраза; ближе этого два дропа не считаются разными


def bass_envelope(y: np.ndarray, sr: int, fmax: float = LOW_HZ,
                  wide: bool = False) -> tuple[np.ndarray, float]:
    """Огибающая энергии (низ или вся полоса), кадр HOP.

    Возвращает (огибающая, секунд на кадр)."""
    import librosa

    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    if wide:
        env = np.sqrt((S ** 2).sum(axis=0) + 1e-12)
    else:
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        env = np.sqrt((S[freqs <= fmax] ** 2).sum(axis=0) + 1e-12)
    return env, HOP / sr


def bar_offset_for(y: np.ndarray, sr: int, bpm: float) -> float:
    """Секунда первой линии тактовой сетки — фаза, а не ноль файла.

    Берём фазу гребёнки (beat_phase) и сворачиваем её в один такт: сетка
    периодична, поэтому годится любой настоящий даунбит."""
    try:
        g = beat_phase(y, sr, bpm)
    except Exception:
        return 0.0
    bar = float(g.get("bar_seconds") or 0.0)
    if bar <= 0:
        return 0.0
    return float(g.get("downbeat_offset") or 0.0) % bar


def _bar_levels(env: np.ndarray, frame_sec: float, bar: float,
                offset: float = 0.0, ref: float | None = None
                ) -> tuple[np.ndarray, float]:
    """Уровень огибающей по тактам от якоря, в долях опорного уровня."""
    if ref is None:
        ref = float(np.percentile(env, 85))
    step = max(1, int(round(bar / frame_sec)))
    i0 = max(0, int(round(offset / frame_sec)))
    n = max(1, (len(env) - i0) // step)
    lv = np.array([float(np.mean(env[i0 + i * step: i0 + (i + 1) * step]))
                   for i in range(n)])
    return lv / max(ref, 1e-9), float(ref)


def drums_signal(track_path: str, sr: int) -> "np.ndarray | None":
    """Слой ударных из .stem.mp4 рядом с треком, если он есть.

    Зачем: «где играют барабаны» и «где дроп» раньше мерились по полосе
    <180 Гц МИКСА, то есть по БАСУ. Замерено на «Ci-energy —
    Chronophage»: с 0:05 до 1:53 бас держит ровно 0.8 не прерываясь —
    там и интро, и первый дроп, и брейкдаун подряд, и по басу они
    неразличимы. По слою ударных та же зона читается сразу: 0:04-0:48
    лёгкая перкуссия (0.33), 0:50-1:34 полные барабаны (0.57),
    1:35-1:53 барабанов нет вовсе (0.00). Это разные секции, и слышны
    они именно так."""
    try:
        import stems as _stems
        import stem_mp4 as _mp4

        path = _stems.stem_mp4_for(track_path)
        if not path:
            return None
        y = _mp4.decode_layer(str(path), "drums", sr=int(sr), mono=True)
        return y if y is not None and len(y) > sr else None
    except Exception:
        return None


def _drum_levels(drums: np.ndarray, sr: int, bar: float, offset: float
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    """Уровень слоя ударных по тактам, нормированный на «полные барабаны».

    Нормируем на 90-й перцентиль ПО ТАКТАМ, а не на перцентиль кадров:
    у слоя ударных кадры — это отдельные удары, и перцентиль кадров
    меряет громкость одного удара, которая от трека к треку гуляет
    втрое. Перцентиль по тактам — это уровень плотной партии, то есть
    ровно то, с чем мы сравниваем.

    Полоса — бочка (<180 Гц) внутри уже отделённых ударных: контраст
    интро/дроп по ней 11-кратный против 4-кратного по всей полосе.

    Возвращаем и средний уровень такта, и ПИКОВЫЙ. Средний — это
    плотность партии, пиковый — громкость самого удара, и они говорят
    разное. Такт, в котором один удар полной громкости и больше ничего,
    — это первая доля дропа: у «Sili» на 0:44.5 ровно такой такт, а
    плотная партия начинается тактом позже."""
    env, frame_sec = bass_envelope(drums, sr)
    lv, _ = _bar_levels(env, frame_sec, bar, offset, ref=1.0)
    step = max(1, int(round(bar / frame_sec)))
    i0 = max(0, int(round(offset / frame_sec)))
    pk = np.array([float(np.max(env[i0 + i * step: i0 + (i + 1) * step]))
                   for i in range(len(lv))])
    full = float(np.percentile(lv, 90)) or float(np.max(lv)) or 1.0
    full_pk = float(np.percentile(pk, 90)) or float(np.max(pk)) or 1.0
    return lv / max(full, 1e-9), pk / max(full_pk, 1e-9), env, frame_sec, full


# Плотность ударов по тактам — то, из чего различается half-time.
#
# `on` и `dense` говорят про УРОВЕНЬ: играют барабаны или нет, плотно
# или редко. Half-time — не про уровень: партия там такая же громкая,
# просто ударов вдвое меньше при той же сетке. По уровню это не видно
# вовсе, поэтому нужна отдельная величина — сколько АТАК в такте.
#
# Цифра на такт (0..9), чтобы карта оставалась строкой и не раздувала
# БД: точность до одного удара здесь и не нужна, решение принимается по
# отношению к медиане трека.
HALF_TIME_RATIO = 0.6      # «вдвое реже» с запасом на неровную партию
HALF_TIME_MIN_BARS = 8     # короче фразы это сбивка, а не секция


def _hits_per_bar(env, frame_sec: float, bar: float, offset: float,
                  n_bars: int) -> list[int]:
    """Сколько атак в каждом такте. Атака — локальный максимум подъёма
    огибающей выше доли от её же разброса, а не превышение абсолютного
    порога: громкость партии по треку гуляет, а количество ударов — нет."""
    if env is None or len(env) < 4 or frame_sec <= 0:
        return []
    d = np.diff(np.asarray(env, dtype=float), prepend=float(env[0]))
    d = np.maximum(d, 0.0)
    # Половина от 75-го перцентиля, а не сам перцентиль: на ровной
    # партии почти все подъёмы одинаковы, и строгое «больше перцентиля»
    # не выполняется НИ РАЗУ — первая версия этой функции возвращала
    # нули на идеальном тестовом сигнале.
    ref = 0.5 * float(np.percentile(d[d > 0], 75)) if np.any(d > 0) else 0.0
    if ref <= 1e-9:
        return []
    peak = (d >= ref) & (d >= np.roll(d, 1)) & (d > np.roll(d, -1))
    idx = np.flatnonzero(peak)
    out = [0] * max(0, int(n_bars))
    for i in idx:
        b = int((i * frame_sec - offset) / bar)
        if 0 <= b < len(out):
            out[b] = min(9, out[b] + 1)
    return out


def drum_map(y: np.ndarray, sr: int, bpm: float, threshold: float = 0.5,
             offset: float = 0.0, drums: "np.ndarray | None" = None) -> dict:
    """Где в треке РЕАЛЬНО играют барабаны — по такту.

    Это оказалось главной причиной кривого звучания. Точки входа
    выбирались по structure-анализу («за 16 тактов до дропа»), а дропы он
    находит с уверенностью 0.14-0.23, то есть почти наугад. Половина
    переходов заводила новый трек в его эмбиентное интро: фейдер резал с
    громкого бита на пустоту.

    Метрика: если есть слой ударных — его собственная бочка,
    нормированная на уровень плотной партии; иначе полоса ниже 180 Гц
    микса, как раньше (см. drums_signal — разница принципиальная).

    Что НЕ работает и почему: контраст гребёнки (сколько выделяется
    лучшая фаза) в интро ВЫШЕ, чем в теле трека — редкие одиночные
    события дают острый пик, а плотная партия барабанов размазывает
    отклик по всем фазам. По контрасту интро выглядит «ритмичнее» дропа.
    """
    bpm = float(bpm) if bpm and bpm > 20 else 172.0
    bar = 60.0 / bpm * 4
    empty = {"bar_seconds": round(bar, 4), "bar_offset": 0.0, "on": "",
             "drums_start": 0.0, "drums_end": 0.0, "coverage": 0.0,
             "source": "none"}
    use_stem = drums is not None and len(drums) >= sr * 4
    if not use_stem and (y is None or len(y) < sr * 4):
        return empty
    offset = max(0.0, float(offset or 0.0)) % bar

    if use_stem:
        level, peak, env, frame_sec, ref = _drum_levels(drums, sr, bar, offset)
        thr = DRUMS_ON
        dense_thr = DRUMS_DENSE
    else:
        env, frame_sec = bass_envelope(y, sr)
        ref = float(np.percentile(env, 85))
        if ref <= 1e-9:
            return empty
        level, _ = _bar_levels(env, frame_sec, bar, offset, ref)
        peak = None
        thr = threshold
        dense_thr = threshold

    flags = _despike(level > thr)
    n_bars = len(flags)

    # Сплошные участки: одиночный такт — это не «барабаны пошли».
    runs = []
    i = 0
    while i < n_bars:
        j = i
        while j < n_bars and flags[j] == flags[i]:
            j += 1
        if flags[i] and j - i >= 4:
            runs.append((i, j))
        i = j

    # «Первый бит» — это не первый удар, а место, где партия становится
    # ПЛОТНОЙ. У «Chronophage» бочка звучит с 0:04, но по два удара в
    # такт под басовую подложку — заводить туда деку не во что; полный
    # брейк начинается на 0:50, и слышно первым битом именно его.
    dense = _despike(level >= dense_thr)
    dense_runs = [(a, b) for a, b, v in _runs_of(dense) if v and b - a >= 4]

    first_hit = drums_start = drums_end = 0.0
    phase = 0
    if dense_runs:
        phase = _lead_hit(dense_runs[0][0], level, peak) % PHRASE_BARS
    if runs:
        first_hit = offset + _phrase_snap(runs[0][0], phase) * bar
        drums_end = offset + _phrase_snap(runs[-1][1], phase) * bar
    if dense_runs:
        drums_start = offset + _lead_hit(dense_runs[0][0], level, peak) * bar
        drums_end = max(drums_end,
                        offset + _phrase_snap(dense_runs[-1][1], phase) * bar)
    else:
        drums_start = first_hit

    return {"bar_seconds": round(bar, 4),
            "bar_offset": round(offset, 4),
            # компактно для хранения в БД
            "on": "".join("1" if v else "0" for v in flags),
            "dense": "".join("1" if v else "0" for v in dense),
            # плотность атак по тактам — из неё различается half-time
            "hits": "".join(str(v) for v in
                            _hits_per_bar(env, frame_sec, bar, offset, n_bars)),
            "drums_start": round(drums_start, 2),
            "first_hit": round(first_hit, 2),
            "drums_end": round(drums_end, 2),
            "coverage": round(float(np.mean(flags)) if n_bars else 0.0, 3),
            "source": "stem" if use_stem else "mix"}


def _despike(mask: np.ndarray) -> np.ndarray:
    """Медиана по трём тактам: одиночный такт не меняет секцию.

    Без этого один такт с перкуссией посреди брейкдауна резал его на
    три куска — у «Sili» брейкдаун 2:14-2:57 разваливался на 2:14-2:35,
    2:36-2:47 и 2:49-2:57, и каждый огрызок претендовал на свой дроп."""
    m = np.asarray(mask, dtype=bool)
    if len(m) < 3:
        return m
    out = m.copy()
    out[1:-1] = (m[:-2].astype(int) + m[1:-1].astype(int)
                 + m[2:].astype(int)) >= 2
    return out


def _runs_of(mask: np.ndarray) -> list[tuple[int, int, bool]]:
    """Однородные полосы булева массива: (начало, конец, значение)."""
    out: list[tuple[int, int, bool]] = []
    n = len(mask)
    start = 0
    for i in range(1, n + 1):
        if i == n or bool(mask[i]) != bool(mask[start]):
            out.append((start, i, bool(mask[start])))
            start = i
    return out


def _lead_hit(b: int, level: np.ndarray, peak: "np.ndarray | None") -> int:
    """Такт-затакт: один удар полной громкости и пустота после него.

    Дроп слышен с этого удара, а не с начала плотной партии. Отличить
    его от разреженного интро можно только по паре «пик/плотность»: у
    «Sili» такт 0:44.5 пустой (0.16) с ударом в полную громкость (1.01)
    — это первая доля дропа; у «Chronophage» такт перед дропом тоже
    бьёт в полную громкость (0.97), но плотность там 0.53 — это не
    затакт, а сам рисунок интро, и метку туда двигать нельзя."""
    if peak is None or b <= 0:
        return b
    if level[b - 1] < DRUMS_ON and peak[b - 1] >= 0.8:
        return b - 1
    return b


def _phrase_phase(level: np.ndarray, peak: "np.ndarray | None") -> int:
    """Остаток от деления на 8, на котором стоят фразы этого трека.

    Танцевальная музыка строится фразами по 8 тактов, и все события
    сидят на одной и той же фразовой сетке. Якорь — первый дроп: он
    определяется однозначно (первый удар после пустого интро), а всё
    остальное к нему подтягивается. Проверено на двух треках: у
    «Chronophage» оба дропа сами встали на такты 36 и 108, то есть
    ровно 4 mod 8; у «Sili» первый дроп на такте 32, а второй детектор
    видел на 129 — 129 не на фразе, и на слух он на 128."""
    dense = _despike(level >= DRUMS_DENSE)
    runs = [(a, b) for a, b, v in _runs_of(dense) if v and b - a >= 4]
    if not runs:
        return 0
    return _lead_hit(runs[0][0], level, peak) % PHRASE_BARS


def _phrase_snap(i: int, phase: int, tol: int = PHRASE_SNAP) -> int:
    """Подтянуть такт к ближайшей границе фразы, если она рядом."""
    k = round((i - phase) / PHRASE_BARS)
    j = int(phase + k * PHRASE_BARS)
    return j if j >= 0 and abs(j - i) <= tol else int(i)


def _cluster(cands: list[int], span: int = PHRASE_BARS) -> list[int]:
    """Кандидаты внутри одной фразы — одно событие; дроп это его начало.

    Без этого дроп с ямой внутри давал два события подряд, и в список
    попадали оба, причём поздний выглядел не хуже раннего — отсюда
    «метка на 4 такта позже»."""
    kept: list[int] = []
    for b in sorted(set(cands)):
        if kept and b - kept[-1] < span:
            continue
        kept.append(b)
    return kept


def _drum_events(level: np.ndarray, peak: "np.ndarray | None" = None
                 ) -> tuple[list[int], list[tuple[int, int]], list[int]]:
    """Дропы, брейкдауны и ямы по слою ударных.

    Дроп — это не «громко», а СТУПЕНЬКА: партия становится в полтора
    раза плотнее, чем была четыре такта назад, выходит на уровень
    плотной партии и держится восемь тактов. Так первый дроп отличается
    от возврата лёгкого лупа после брейкдауна: у «Chronophage» на 1:54
    барабаны возвращаются, но на 0.6 от полного уровня — это куплет, а
    дроп там только на 2:29."""
    n = len(level)
    thin = _despike(level < DROP_DOWN)
    breakdowns, pits = [], []
    for a, b, low in _runs_of(thin):
        if not low:
            continue
        if b - a >= DROP_HOLD:
            breakdowns.append((a, b))
        elif a > 0:
            pits.append((a, b))   # яма в самом начале файла — это тишина
    cand = []
    for b in range(1, n):
        span = level[b:b + DROP_SPAN]
        if len(span) < 2:
            continue
        held = float(np.median(span))
        # держится на полном уровне и сам такт уже громкий
        if held < DROP_FULL or level[b] < DROP_FULL * 0.8:
            continue
        # и это именно СТУПЕНЬКА: до неё было заметно тише.
        # Сравниваем с медианой восьми тактов, а не четырёх и не со
        # средним: перед дропом почти всегда яма в 1-2 такта, и среднее
        # она утаскивает вниз ровно настолько, чтобы «дропом» оказался
        # любой возврат после любой ямы — так у «Sili» появлялись лишние
        # дропы на 1:08 и 1:52 посреди сплошной партии.
        prev = level[max(0, b - DROP_SPAN):b]
        base = float(np.median(prev)) if len(prev) else 0.0
        if base > DROP_STEP * held:
            continue
        cand.append(_lead_hit(b, level, peak))
    phase = _phrase_phase(level, peak)
    cand = [_phrase_snap(b, phase) for b in _cluster(cand)]
    breakdowns = [(_phrase_snap(a, phase), _phrase_snap(b, phase))
                  for a, b in breakdowns]
    return _cluster(cand), breakdowns, [a for a, _ in pits]


def _bass_events(level: np.ndarray) -> tuple[list[int], list[tuple[int, int]], list[int]]:
    """Тот же разбор по низу микса — когда слоя ударных нет.

    Подтверждение дропа было `mean(level[b:b+4]) > 0.70`: одного
    провального такта внутри четвёрки хватало, чтобы среднее не
    дотянуло, и настоящий дроп пропускался. Теперь «3 такта из 4»."""
    n = len(level)
    breakdowns, pits, cand = [], [], []
    for a, b, low in _runs_of(level < DROP_LOW):
        if not low:
            continue
        if b - a >= DROP_HOLD:
            breakdowns.append((a, b))
        elif a > 0:
            pits.append(a)
        if b < n:
            after = level[b:b + DROP_HOLD]
            if int(np.sum(after >= DROP_HIGH)) >= min(DROP_CONFIRM, len(after)):
                cand.append(b)
    return _cluster(cand), breakdowns, pits


# Яркость по тактам — то, из чего различается RISER.
#
# Riser — нарастающий шум или тон в конце билда. Уровень НИЗА при этом
# может и падать (барабаны уже сняли), поэтому ни `level`, ни `hits`
# его не видят: он живёт наверху спектра и опознаётся не громкостью, а
# РОСТОМ доли высоких частот такт за тактом.
#
# Считаем здесь, вместе с остальными картами, чтобы не гонять анализ
# второй раз. Разметка событий (riser/fill/turnaround) появится отдельно
# и будет читать эту величину; треки, посчитанные старой версией, её не
# имеют — и это видно по отсутствию ключа, а не по нулям.


def _bright_per_bar(y: "np.ndarray", sr: int, bar: float, offset: float,
                    n_bars: int) -> list[float]:
    """Доля энергии выше 4 кГц в каждом такте, 0..1.

    Доля, а не абсолютный уровень: библиотека разная по мастерингу, и
    порог по громкости пришлось бы подбирать под каждый трек."""
    if y is None or len(y) < sr or n_bars <= 0:
        return []
    from scipy.signal import butter, sosfiltfilt

    try:
        sos = butter(2, min(4000.0 / (sr / 2), 0.99), btype="highpass", output="sos")
        hi = sosfiltfilt(sos, y)
    except Exception:                                # noqa: BLE001
        return []
    step = max(1, int(bar * sr))
    out: list[float] = []
    for i in range(int(n_bars)):
        s = int(offset * sr) + i * step
        e = min(len(y), s + step)
        if s >= len(y) or e <= s:
            out.append(0.0)
            continue
        all_e = float(np.mean(y[s:e] ** 2)) + 1e-12
        hi_e = float(np.mean(hi[s:e] ** 2))
        out.append(round(min(1.0, hi_e / all_e), 3))
    return out


def energy_map(y: np.ndarray, sr: int, bpm: float, offset: float = 0.0,
               drums: "np.ndarray | None" = None) -> dict:
    """Уровень низа ПО ТАКТАМ и события структуры: брейкдауны, ямы, дропы.

    Зачем отдельно от structure-анализа: там дропы находятся с
    уверенностью 0.14-0.26, то есть почти наугад. А брейкдаун и дроп на
    самом деле видны прямым измерением.

    `level` — по-прежнему низ МИКСА: это «сколько сейчас баса», и именно
    его спрашивают техники. А вот СОБЫТИЯ (дроп, брейкдаун, яма) с
    появлением стемов считаются по слою ударных: по басу интро, дроп и
    брейкдаун сплошь и рядом неразличимы (см. drums_signal). Без стема
    остаётся прежний путь по низу микса.

    Три вещи, из-за которых метки стояли не там:

    1. Дроп искался по возврату БАСА, а слышен он как возврат
       БАРАБАНОВ. Теперь при наличии стема — по барабанам.
    2. Кандидаты не схлопывались, и поздний выигрывал у раннего
       (см. _cluster).
    3. Сетка тактов шла от нуля файла (см. комментарий к bar_offset).
    """
    bpm = float(bpm) if bpm and bpm > 20 else 172.0
    bar = 60.0 / bpm * 4
    empty = {"bar_seconds": round(bar, 4), "bar_offset": 0.0, "level": [],
             "drops": [], "breakdowns": [], "pits": [], "source": "none"}
    if y is None or len(y) < sr * 8:
        return empty

    env, frame_sec = bass_envelope(y, sr)
    ref = float(np.percentile(env, 85))
    if ref <= 1e-9:
        return empty
    offset = max(0.0, float(offset or 0.0)) % bar
    level, _ = _bar_levels(env, frame_sec, bar, offset, ref)

    use_stem = drums is not None and len(drums) >= sr * 4
    if use_stem:
        dlevel, dpeak, _denv, _dframe, _dref = _drum_levels(drums, sr, bar, offset)
        cand, breaks, pits = _drum_events(dlevel, dpeak)
    else:
        cand, breaks, pits = _bass_events(level)

    def sec(i: int) -> float:
        return offset + i * bar

    # Доводка «до кадра» тут была и оказалась вредной. Сетка тактов уже
    # привязана к музыке (bar_offset), поэтому номер такта — это и есть
    # ответ; поиск подъёма внутри соседнего такта только уводил метку на
    # такт назад, потому что в конце билда бочка уже почти полной
    # громкости. Замерено: дроп «Chronophage» стоял на 0:48 вместо 0:50.
    drops = [round(sec(b), 2) for b in cand]

    return {"bar_seconds": round(bar, 4),
            "bar_offset": round(offset, 4),
            "level": [round(float(v), 3) for v in level],
            # Яркость по тактам — для будущей разметки riser'ов. Ключа
            # нет у треков, посчитанных прежней версией: «не мерили» и
            # «померили и ноль» — разные вещи.
            "bright": _bright_per_bar(y, sr, bar, offset, len(level)),
            "drops": drops,
            "breakdowns": [(round(sec(a), 2), round(sec(b), 2)) for a, b in breaks],
            "pits": [round(sec(a), 2) for a in pits],
            "source": "stem" if use_stem else "mix"}


def bar_index(emap: dict, seconds: float) -> int:
    """Индекс такта в карте по абсолютной секунде — с учётом якоря."""
    bar = float((emap or {}).get("bar_seconds") or 0.0)
    off = float((emap or {}).get("bar_offset") or 0.0)
    if bar <= 0:
        return 0
    return int((float(seconds) - off) // bar)


def level_at(emap: dict, seconds: float) -> float:
    """Уровень низа в этой секунде, 1.0 — как в теле трека."""
    bars = emap.get("level") or []
    if not bars:
        return 1.0
    i = bar_index(emap, seconds)
    return float(bars[min(max(i, 0), len(bars) - 1)])


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
    if not on:
        return True          # карты нет — не мешаем выбору
    i = bar_index(dmap, seconds)
    j = i + int(round(need_bars))
    if i < 0 or i >= len(on):
        return False
    window = on[i:max(i + 1, min(len(on), j + 1))]
    return window.count("1") >= max(1, int(len(window) * 0.7))


def drums_start_of(dmap: dict) -> float:
    return float((dmap or {}).get("drums_start") or 0.0)


def drums_end_of(dmap: dict) -> float:
    return float((dmap or {}).get("drums_end") or 0.0)


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

    # Разделение гармоники и ударных делается ПРЯМО ПО CQT, а не звуком.
    #
    # Раньше здесь стояло librosa.effects.harmonic(y) — то есть STFT,
    # маска, обратный STFT, и только потом CQT. Замерено на реальном
    # треке: 43 секунды из ~63 секунд анализа одного трека уходило сюда,
    # то есть сканирование библиотеки на две трети состояло из этой
    # строки. Обратный STFT здесь вдобавок бессмысленный: звук нам не
    # нужен, нужен спектр, а мы его выбрасываем и считаем заново.
    #
    # Маска HPSS работает на любой спектрограмме, поэтому CQT считается
    # один раз, разделение идёт по ней же, и istft не нужен вовсе.
    # Проверено тем, ради чего карта существует: матрица похожести фраз
    # (по ней выбираются точки сведения) совпадает с прежней на 0.948
    # при разбросе самих значений 0.179 — то есть решения не меняются.
    # 43 с -> 2.4 с.
    hop = 2048
    bins_per_octave = 36
    cqt = np.abs(librosa.cqt(y, sr=sr, hop_length=hop,
                             bins_per_octave=bins_per_octave,
                             n_bins=7 * bins_per_octave))
    harm, _perc = librosa.decompose.hpss(cqt, margin=3.0)
    c = librosa.feature.chroma_cqt(C=harm, sr=sr, hop_length=hop,
                                   bins_per_octave=bins_per_octave)
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
