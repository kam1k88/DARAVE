"""
demo_render.py — офлайн аудио-демонстрация техники сведения на РЕАЛЬНЫХ
файлах из библиотеки диджея: "Техника" во веб-UI не должна быть просто
справочником — диджей выбирает технику, система сама предлагает пару
треков из библиотеки, жмёт "Демо" и слышит готовый WAV прямо в браузере,
без Mixxx/MIDI/companion вообще (это офлайн-рендер, не управление живыми
деками — см. server.py::POST /techniques/{id}/demo).

Ключевая идея: НЕ хардкодим 21 отдельный рецепт "как это должно звучать".
Вместо этого интерпретируем ТЕ ЖЕ САМЫЕ events, что build_plan() отдаёт
для реального MixPlan (см. techniques.py) — единственный источник истины
о том, что делает техника, общий для живого MIDI-пути и офлайн-демо.
Словарь действий закрытый (11 штук, см. techniques.py):
  crossfade, volume_ramp, filter_sweep, eq_kill_low, eq_kill_mid,
  fx_meta (delay/echo send), key_shift, reverse_hold, play_toggle,
  loop_activate/loop_exit, sync.

Честные упрощения (это ПРЕВЬЮ, не точная копия живого сведения):
  - Только 2 деки source/target — событие на "third" деке (Triple Drop)
    игнорируется, рендер вырождается в подобие Double Drop.
  - key_shift применяется одним сдвигом (конечное значение окна), а не
    непрерывной рампой по семплам — так же для громкости эха.
  - loop_activate/loop_exit — грубая аппроксимация повтором короткого
    фрагмента, а не honest точка зацикливания как в Mixxx.
  - "sync" не делает НИЧЕГО кроме как метит момент, когда target впервые
    становится слышен, и что his темп подгоняется под source — это и
    есть звуковой эффект sync (в отличие от живого MIDI, где sync — это
    реальная команда деке).
Технически: цель не "бит в бит как настоящий сет", а "услышать характер
техники на СВОИХ треках" — прежде всего для показа/выбора, не для замены
реального сведения на пульте.
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np
from scipy.signal import butter, sosfilt, sosfiltfilt

from mixxx_controls import BY_ID as _CONTROLS
from techniques import TECHNIQUES, build_plan

SR = 44100
MAX_SECONDS = 38.0
# Раньше отступы были «1 секунда до / 3 секунды после» — некратные такту,
# из-за чего событие «beat 0» приходилось на произвольное место такта.
# Теперь всё в тактах: сетка таймлайна и сетка музыки — одно и то же.
LEAD_BARS = 1.0
TAIL_BARS = 2.0
BAR_BEATS = 4
# Куда приводим громкость каждой деки перед суммой. -18 dBFS по RMS
# оставляет запас: две деки в сумме на пике дают примерно -12, до клипа
# ещё далеко, и микс не приходится потом душить нормализацией.
TARGET_RMS = 0.125
OUTPUT_RMS = 0.20       # ~ -16 dBFS на готовом куске — общий уровень всего сета
MAX_MATCH_GAIN = 6.0
BASS_SPLIT_HZ = 180.0
EDGE_FADE_SEC = 0.010
# Во сколько раз можно ужать технику по времени, если она не влезает в
# отведённое окно. Только степени двойки — 32 такта -> 16 -> 8 остаются
# кратными такту, а 32 -> 19 разъехалось бы с музыкой.
TIME_SCALES = (1.0, 0.5, 0.25, 0.125)


# Рендер был моно от начала и до конца: librosa.load(mono=True), все
# фильтры на одномерных массивах, запись в один канал. Диджейский микс в
# моно звучит узко и «из телефона» — два трека сходятся в одну точку
# пространства вместо того, чтобы занимать разную ширину. Теперь буферы
# двумерные (n, каналы), анализ (сетка, громкость) по-прежнему по моно-
# сумме, а обработка — по каналам.
STEREO = True


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _as2d(y: np.ndarray) -> np.ndarray:
    """Приводит буфер к форме (сэмплы, каналы)."""
    if y.ndim == 1:
        return y[:, None]
    if y.shape[0] < y.shape[1]:      # librosa отдаёт (каналы, сэмплы)
        return y.T
    return y


def _to_stereo(y: np.ndarray) -> np.ndarray:
    """Гарантирует два канала: моно-файл дублируем, иначе поток сета не
    сойдётся по форме с двухканальными кусками."""
    y = _as2d(y)
    if y.shape[1] == 1:
        return np.repeat(y, 2, axis=1)
    return y[:, :2]


def _mono(y: np.ndarray) -> np.ndarray:
    """Моно-вид для анализа: сетку и громкость меряем по сумме каналов."""
    return y.mean(axis=1) if y.ndim > 1 else y


def _env(e: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Огибающая под форму буфера, чтобы умножалась по каналам."""
    return e[:, None] if y.ndim > 1 else e


def _seconds_per_beat(bpm: float) -> float:
    return 60.0 / max(1.0, bpm)


def _event_window(ev: dict, spb: float) -> tuple[float, float]:
    start = ev["beat_offset"] * spb
    dur = ev.get("duration_beats", 0.0) * spb
    return start, start + dur


def _curve_value(progress: float, curve: str, cycles: float = 1.0) -> float:
    progress = _clamp(progress, 0.0, 1.0)
    if curve == "ease_in":
        return progress ** 2
    if curve == "sine":
        return 0.5 - 0.5 * np.cos(2 * np.pi * cycles * progress)
    return progress  # linear (и любой нераспознанный curve — безопасный дефолт)


def _piecewise_ramp(events: list[dict], spb: float, default: float = 0.0):
    """events — ramp-события ОДНОГО (action, deck). До первого окна держим
    его value_from, между окнами держим предыдущий value_to (гейт), после
    последнего — его value_to. Возвращает fn(t) -> float."""
    windows = []
    for ev in sorted(events, key=lambda e: e["beat_offset"]):
        start, end = _event_window(ev, spb)
        windows.append((start, end, ev["value_from"], ev["value_to"], ev.get("curve", "linear"),
                         (ev.get("params") or {}).get("cycles", 1.0)))
    if not windows:
        return lambda t: default

    def fn(t: float) -> float:
        if t <= windows[0][0]:
            return windows[0][2]
        held = windows[0][2]
        for start, end, vf, vt, curve, cycles in windows:
            if start <= t <= end:
                progress = (t - start) / max(1e-6, end - start)
                return vf + (vt - vf) * _curve_value(progress, curve, cycles)
            if t < start:
                return held
            held = vt
        return held

    return fn


def _events_step_sec(events: list[dict], spb: float, default: float = 0.05) -> float:
    """Шаг сэмплирования огибающей под самое короткое событие.

    Резовые техники (Fader Chop, Bar Switch) бросают фейдер за ~10 мс.
    На сетке 50 мс такой бросок превращался в плавный переход длиной в
    три отсчёта — то есть рез звучал как маленький кроссфейд, и вся
    характерная резкость DnB-сведения пропадала."""
    durs = [e.get("duration_beats", 0.0) * spb for e in events
            if e.get("duration_beats", 0.0) and e["action"] in ("crossfade", "volume_ramp", "eq_low")]
    durs = [d for d in durs if d > 1e-4]
    if not durs:
        return default
    return float(max(0.0005, min(default, min(durs) / 4.0)))


def _envelope_array(value_fn, t0: float, n: int, sr: int, step_sec: float = 0.05) -> np.ndarray:
    """Дёшево сэмплит value_fn на грубой сетке и линейно интерполирует на
    все n сэмплов — вызывать value_fn n раз (миллионы раз для 25с) слишком
    медленно на чистом Python."""
    step_n = max(1, int(step_sec * sr))
    idx = np.arange(0, n, step_n)
    if idx[-1] != n - 1:
        idx = np.append(idx, n - 1)
    times = t0 + idx / sr
    vals = np.array([value_fn(float(tt)) for tt in times])
    return np.interp(np.arange(n), idx, vals)


def _toggle_segments(times: list[float]) -> list[tuple[float, float | None]]:
    """Чередующиеся discrete-события одного (action, deck) — состояние
    стартует "выключено", каждое событие переключает. Возвращает интервалы,
    где состояние "включено" (end=None значит "до конца рендера")."""
    segments = []
    state = False
    start = None
    for t in sorted(times):
        state = not state
        if state:
            start = t
        else:
            segments.append((start, t))
    if state:
        segments.append((start, None))
    return segments


def _fit_length(y: np.ndarray, n: int) -> np.ndarray:
    if len(y) >= n:
        return y[:n]
    pad = [(0, n - len(y))] + [(0, 0)] * (y.ndim - 1)
    return np.pad(y, pad)


def _sample_idx(t: float, timeline_start: float, sr: int) -> int:
    return int(round((t - timeline_start) * sr))


def _apply_highpass_segment(buf: np.ndarray, s: int, e: int, sr: int, cutoff_hz: float = 200.0) -> None:
    if e - s < 32:
        return
    nyq = sr / 2
    sos = butter(2, _clamp(cutoff_hz, 20, nyq - 50) / nyq, btype="high", output="sos")
    buf[s:e] = sosfiltfilt(sos, buf[s:e], axis=0)


def _apply_bandstop_segment(buf: np.ndarray, s: int, e: int, sr: int, lo_hz: float = 300.0, hi_hz: float = 3000.0) -> None:
    if e - s < 32:
        return
    nyq = sr / 2
    lo = _clamp(lo_hz, 20, nyq - 100)
    hi = _clamp(hi_hz, lo + 50, nyq - 20)
    sos = butter(2, [lo / nyq, hi / nyq], btype="bandstop", output="sos")
    buf[s:e] = sosfiltfilt(sos, buf[s:e], axis=0)


def _time_varying_lowpass(signal: np.ndarray, sr: int, value_fn, t0: float,
                           lo_hz: float = 1000.0, hi_hz: float = 18000.0,
                           block: int = 4096, hop: int = 2048) -> np.ndarray:
    """value 0 -> почти без фильтра (hi_hz), value 1 -> сильно приглушено
    (lo_hz) — классический "закрывающийся" фильтр перед резом/переходом.
    Overlap-add с окном Ханна, чтобы не было щелчков на границах блоков."""
    n = len(signal)
    if n == 0:
        return signal
    out = np.zeros_like(signal)
    norm = np.zeros(n)
    nyq = sr / 2
    # проект фильтра кэшируем по округлённой частоте: свип плавный, а
    # butter() на каждый блок это была тысяча вызовов на один переход
    cache: dict[int, np.ndarray] = {}
    pos = 0
    while pos < n:
        end = min(pos + block, n)
        seg = signal[pos:end]
        w = np.hanning(len(seg)) if len(seg) > 1 else np.ones(len(seg))
        t_center = t0 + (pos + len(seg) / 2) / sr
        v = _clamp(value_fn(t_center), 0.0, 1.0)
        cutoff = hi_hz - v * (hi_hz - lo_hz)
        cutoff = _clamp(cutoff, 50, nyq - 100)
        key = int(cutoff / 20)
        sos = cache.get(key)
        if sos is None:
            sos = butter(2, (key * 20 + 10) / nyq, btype="low", output="sos")
            cache[key] = sos
        filtered = sosfilt(sos, seg, axis=0)
        out[pos:end] += filtered * _env(w, seg)
        norm[pos:end] += w
        pos += hop
    norm[norm == 0] = 1.0
    return out / _env(norm, out)


def _apply_echo(signal: np.ndarray, sr: int, mix_env: np.ndarray,
                 delay_sec: float = 0.375, feedback: float = 0.4, repeats: int = 6) -> np.ndarray:
    """Затухающее эхо через несколько сдвинутых копий (векторизовано —
    быстрее и стабильнее, чем IIR-гребёнка на весь буфер). mix_env — уже
    посчитанная по времени огибающая громкости эха (0..1), той же длины,
    что signal; добавляется ПОВЕРХ сухого сигнала (эффект-посыл)."""
    n = len(signal)
    delay_n = max(1, int(delay_sec * sr))
    wet = np.zeros((n + delay_n * repeats,) + signal.shape[1:])
    amp = 1.0
    pos = 0
    for _ in range(repeats):
        amp *= feedback
        pos += delay_n
        if pos >= len(wet) or amp < 0.02:
            break
        seg_len = min(n, len(wet) - pos)
        wet[pos:pos + seg_len] += signal[:seg_len] * amp
    wet = wet[:n]
    return signal + wet * _env(mix_env, signal)




# ---------------------------------------------------------------------------
# Громкость, кроссфейд, низ — то, из-за чего сведение «звучит некрасиво»
# даже когда такты сошлись.
# ---------------------------------------------------------------------------

def _rms(y: np.ndarray, floor: float = 1e-6) -> float:
    """RMS по звучащей части буфера. Считать по всему подряд нельзя: у
    интро полтакта тишины, и трек получил бы завышенный гейн."""
    if len(y) == 0:
        return floor
    a = np.abs(_mono(y))
    thr = np.percentile(a, 60)
    loud = a[a > thr]
    if len(loud) < 16:
        loud = a
    return max(float(np.sqrt(np.mean(loud ** 2))), floor)


@lru_cache(maxsize=256)
def _refined_bpm(path: str, bpm_hint: float) -> float:
    """Истинный темп трека, а не то, что записано в библиотеке.

    В базе после сканирования у 22 треков стоит РОВНО 172.30 — это
    значение с сетки-приора librosa, а не измеренный темп. На самом деле
    там 171.00, 173.00, 174.03. Разница в 1.7 BPM — это 1%: за 30 секунд
    сведения вторая дека уезжает на 300 мс, почти на целую долю. Пока темп
    брался из базы, свести такты было нельзя в принципе, сколько бы точно
    мы ни ловили фазу.

    Проверено на библиотеке: контраст сетки на окне 90 с при 172.3 равен
    1.14 (то есть сетки нет вообще), при уточнённом темпе — 2.5..7.1."""
    import beatgrid

    try:
        r = beatgrid.refine_tempo_file(path, bpm_hint)
    except Exception:
        return float(bpm_hint)
    # низкая уверенность — не спорим с библиотекой
    if not r.get("refined") or r.get("confidence", 0) < 0.25:
        return float(bpm_hint)
    return float(r["bpm"])


@lru_cache(maxsize=256)
def _first_beat_time(path: str, bpm_hint: float, probe_seconds: float = 240.0) -> float:
    """Момент, с которого во ВХОДЯЩЕМ треке реально идут барабаны.

    Меряем по низу (beatgrid.drum_map), а не по онсетам: в эмбиентном
    интро контраст ритмической сетки ВЫШЕ, чем в теле трека — редкие
    события дают острый пик, плотная партия барабанов размазывает отклик.
    По онсетам интро выглядит ритмичнее дропа, и вход выбирался в тишину."""
    import librosa

    import beatgrid

    try:
        y, sr_p = librosa.load(path, sr=11025, mono=True, duration=probe_seconds)
    except Exception:
        return 0.0
    if len(y) < sr_p * 4:
        return 0.0
    try:
        return float(beatgrid.drum_map(y, sr_p, bpm_hint).get("drums_start") or 0.0)
    except Exception:
        return 0.0


@lru_cache(maxsize=128)
def _track_bar_phase(path: str, bpm: float, probe_seconds: float = 75.0) -> tuple | None:
    """Фаза ТАКТА трека — одно число на весь файл: (секунда «раза», такт).

    Сетка долей и фаза такта — разные вещи, и вторая ломается тише.
    `beat_phase` определяет период и фазу доли уверенно, а вот КАКАЯ из
    четырёх долей «раз», выбирает голосованием по низу в том окне, что ему
    дали. Окно у точки сведения часто попадает на брейкдаун, где бочки нет,
    а снейр на 3-й доле громче всех — и голос уходит на соседнюю долю.

    Слышно это так: темп совпадает, доли совпадают, а бочка входящего
    трека садится на снейр уходящего. Диджей говорит «зашёл на долю
    раньше». Замер по трём реальным переходам: 1.97, 0.03 и 1.01 доли
    расхождения рисунков — то есть в двух случаях из трёх такты
    разъезжались.

    Фаза такта постоянна на весь трек (темп мы уже уточнили), поэтому
    считаем её ОДИН раз по самому плотному по барабанам куску, где голос
    надёжен, и дальше пользуемся ей везде."""
    import librosa

    import beatgrid

    bar = 60.0 / max(1.0, bpm) * BAR_BEATS
    try:
        dur = float(librosa.get_duration(path=path))
    except Exception:
        return None

    # ищем, где барабаны точно играют: там голосование за «раз» осмысленно
    start = _first_beat_time(path, bpm)
    if start <= 0.0 or start > dur - 20.0:
        start = max(0.0, dur * 0.45)
    start = min(start, max(0.0, dur - probe_seconds))

    try:
        y, sr_p = librosa.load(path, sr=22050, mono=True,
                               offset=start, duration=probe_seconds)
    except Exception:
        return None
    if len(y) < sr_p * 8:
        return None
    try:
        grid = beatgrid.beat_phase(y, sr_p, bpm)
    except Exception:
        return None
    if float(grid.get("confidence") or 0.0) < 0.25:
        return None
    return (round((start + float(grid["downbeat_offset"])) % bar, 4), round(bar, 6))


def _snap_to_track_bar(path: str, bpm: float, want_seconds: float,
                       fallback: float) -> float:
    """Подтягивает точку к сетке ТАКТОВ этого трека (не окна)."""
    phase = _track_bar_phase(path, bpm)
    if not phase:
        return fallback
    t0, bar = phase
    k = round((want_seconds - t0) / bar)
    return max(0.0, t0 + k * bar)


@lru_cache(maxsize=256)
def _track_reference_rms(path: str, probe_seconds: float = 40.0) -> float:
    """Собственный уровень ТРЕКА (не куска), измеренный по середине файла.

    Мерить по используемому фрагменту нельзя: у входящего трека это интро,
    и оно тихое ПО ЗАМЫСЛУ. Выравняв по нему, мы вытянули бы эмбиентное
    интро до уровня дропа — трек входил бы неестественно громко, а его
    собственная динамика («тихо -> дроп») исчезала бы. Поэтому опорный
    уровень берём один на трек, из его тела: тогда компенсируется разница
    мастерингов, а музыкальная динамика остаётся."""
    import librosa

    try:
        dur = float(librosa.get_duration(path=path))
    except Exception:
        dur = probe_seconds
    start = max(0.0, dur * 0.4 - probe_seconds / 2)
    try:
        y, _ = librosa.load(path, sr=11025, mono=True, offset=start, duration=probe_seconds)
    except Exception:
        return TARGET_RMS
    return _rms(y)


def _match_loudness(y: np.ndarray, ref_rms: float, target: float = TARGET_RMS) -> np.ndarray:
    """Приводит деку к общему уровню. Без этого мастер 2003 года и мастер
    2021 года в одном переходе различаются на 8-10 dB: один трек «выпрыгивает»
    на входе, второй проваливается — на слух это и есть «нет плавности»."""
    if len(y) == 0:
        return y
    gain = target / max(ref_rms, 1e-6)
    return y * float(np.clip(gain, 1.0 / MAX_MATCH_GAIN, MAX_MATCH_GAIN))


def _soft_limit(y: np.ndarray, ceiling: float = 0.95, knee: float = 0.65) -> np.ndarray:
    """Мягкий потолок вместо деления всего куска на пик.

    Раньше один случайный пик заставлял приглушить ВЕСЬ кусок — в склейке
    сета это читалось как «то громче, то тише». Здесь линейная часть до
    knee остаётся нетронутой, а выше плавно поджимается, поэтому средний
    уровень кусков совпадает."""
    a = np.abs(y)
    over = a > knee
    if not np.any(over):
        return y
    span = max(1e-6, ceiling - knee)
    comp = knee + span * np.tanh((a[over] - knee) / span)
    out = y.copy()
    out[over] = np.sign(y[over]) * comp
    return out


def _normalize_output(mix: np.ndarray, target: float = OUTPUT_RMS, peak_ceiling: float = 0.95) -> np.ndarray:
    """Доводит готовый кусок до рабочего уровня. Раньше выход только
    ограничивался по пику — если пик не доходил до потолка, кусок так и
    оставался тихим, и в склейке сета громкость гуляла от перехода к
    переходу."""
    if len(mix) == 0:
        return mix
    cur = _rms(mix)
    if cur > 1e-6:
        mix = mix * float(np.clip(target / cur, 0.25, 4.0))
    mix = _soft_limit(mix, ceiling=peak_ceiling)
    peak = float(np.max(np.abs(mix)))
    if peak > peak_ceiling:
        mix = mix * (peak_ceiling / peak)
    return mix


def _equal_power(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Кроссфейд с постоянной суммарной МОЩНОСТЬЮ.

    Линейный кроссфейд (то, что было) на некоррелированных сигналах даёт
    ровно в середине провал: 0.5^2 + 0.5^2 = 0.5, то есть -3 dB. Слышно
    это как «звук просел и вернулся» на каждом переходе. cos/sin держат
    cos^2 + sin^2 = 1 на всём пути."""
    x = np.clip(x, 0.0, 1.0)
    ang = x * (np.pi / 2.0)
    return np.cos(ang), np.sin(ang)


def _split_low(y: np.ndarray, sr: int, cutoff: float = BASS_SPLIT_HZ) -> tuple[np.ndarray, np.ndarray]:
    sos = butter(3, min(cutoff / (sr / 2), 0.99), btype="low", output="sos")
    low = sosfiltfilt(sos, y, axis=0)
    return low, y - low


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _bass_handover(cf_env: np.ndarray, lo: float = 0.38, hi: float = 0.62) -> tuple[np.ndarray, np.ndarray]:
    """«Одна бочка за раз» — правило, которое диджей выполняет руками на
    каждом сведении. Два полноспектральных драм-н-бейса, сложенные без
    разведения низа, дают кашу: сабы складываются по случайной фазе,
    получается гудение и потеря панча. Возвращает (гейн низа уходящего,
    гейн низа входящего) как функцию положения кроссфейда."""
    t = _smoothstep((cf_env - lo) / max(1e-6, hi - lo))
    return 1.0 - t, t


def _short_rms(y: np.ndarray, sr: int, win: float = 0.5) -> np.ndarray:
    """Кратковременная громкость — ОДНО значение на окно, без растяжки на
    все сэмплы. Гейн всё равно меняется медленно, считать его на каждом
    сэмпле незачем."""
    n = max(1, int(win * sr))
    m = max(1, len(y) // n)
    seg = y[:m * n].reshape(m, n)
    return np.sqrt((seg ** 2).mean(axis=1) + 1e-12)


def _ride_gain(src: np.ndarray, tgt: np.ndarray, cf_env: np.ndarray, sr: int,
               under_db: float = -9.0, max_boost: float = 16.0,
               win: float = 0.5, smooth_seconds: float = 2.0) -> np.ndarray:
    """Гейн входящей деки, пока она идёт ПОД уходящей.

    Это то, что диджей делает ручкой trim и без чего классическое сведение
    не работает вообще. Интро нового трека тихое по замыслу — в мастере
    оно на 20-30 dB ниже тела. Если играть его «как есть», под полновесным
    старым треком его просто не слышно: измерено -27 dB относительно A,
    то есть слушатель не понимает, что вообще что-то заводится.

    Считаем на КРУПНОЙ сетке (одно значение на полсекунды) и растягиваем
    один раз в конце. Раньше вся арифметика шла по сэмплам, а сглаживание
    делалось np.convolve с ядром в две секунды — то есть O(n*m) на двух
    миллионах отсчётов: профиль показал 27.8 секунды из 30 на один
    переход. На крупной сетке это сотня значений вместо двух миллионов.
    """
    n = len(tgt)
    if n == 0:
        return np.ones(0)
    ra = _short_rms(src, sr, win)
    rb = _short_rms(tgt, sr, win)
    m = min(len(ra), len(rb))
    if m == 0:
        return np.ones(n)
    ra, rb = ra[:m], rb[:m]

    want = ra * (10.0 ** (under_db / 20.0))
    boost = np.clip(want / np.maximum(rb, 1e-6), 1.0, max_boost)

    # скользящее среднее через кумулятивную сумму — O(m), а не O(m*k)
    k = max(1, int(round(smooth_seconds / win)))
    if k > 1 and m > 1:
        c = np.cumsum(np.insert(boost, 0, 0.0))
        half = k // 2
        idx = np.arange(m)
        lo = np.clip(idx - half, 0, m)
        hi = np.clip(idx + k - half, 0, m)
        boost = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)

    # кроссфейд усредняем по тем же окнам и отпускаем гейн по мере ухода
    step = max(1, int(win * sr))
    cf_coarse = np.array([cf_env[min(i * step, len(cf_env) - 1)] for i in range(m)])
    release = _smoothstep((cf_coarse - 0.35) / 0.35)
    coarse = boost * (1.0 - release) + release

    return np.interp(np.arange(n), np.linspace(0, n - 1, m), coarse)


def _edge_fade(y: np.ndarray, sr: int, seconds: float = EDGE_FADE_SEC) -> np.ndarray:
    """Микрофейд по краям буфера — иначе на стыке кусков щелчок."""
    k = min(len(y) // 2, int(seconds * sr))
    if k > 1:
        y[:k] *= _env(np.linspace(0.0, 1.0, k), y)
        y[-k:] *= _env(np.linspace(1.0, 0.0, k), y)
    return y


# До какого сдвига высоты можно просто менять скорость. Полтона — это
# заведомо больше, чем нужно: семья темпов допускает 4%, то есть 0.68
# полутона, и на слух этого нет.
MAX_SPEED_SEMITONES = 1.0


def _stretch(y: np.ndarray, sr: int, rate: float) -> np.ndarray:
    """Подгонка темпа СМЕНОЙ СКОРОСТИ, как питч-фейдером на деке.

    Раньше здесь стоял librosa.effects.time_stretch — фазовый вокодер,
    который меняет темп, не трогая высоту. Звучит академически правильно,
    а на драм-н-бейсе разрушает главное: измерено, что он режет резкость
    атак ровно вдвое (0.68 -> 0.34 по нарастанию огибающей на ударах),
    при том что растяжение всего 1.75%. Барабаны становятся ватными, и
    это не видно ни в одной спектральной или временной метрике, которыми
    я мерил всё остальное — а ухом слышно сразу.

    Живой диджей время не растягивает. Он двигает питч-фейдер, то есть
    меняет СКОРОСТЬ: высота уезжает вместе с темпом. При подгонке 171 к
    174 это +0.30 полутона — неразличимо, — а транзиенты остаются целыми
    (0.68 -> 0.80, то есть даже чуть резче за счёт сжатия во времени).

    Фазовый вокодер оставлен только на случай, когда сдвиг высоты стал бы
    слышен; при нашем допуске семьи темпов такого не бывает.
    """
    if abs(rate - 1.0) < 0.002 or len(y) < 1024:
        return y
    import librosa

    semitones = abs(12.0 * np.log2(max(rate, 1e-6)))
    if semitones <= MAX_SPEED_SEMITONES:
        try:
            # играть быстрее в rate раз = прочитать как будто частота была выше
            return librosa.resample(y, orig_sr=int(round(sr * rate)), target_sr=sr,
                                    res_type="soxr_hq", axis=0)
        except Exception:
            pass
    try:
        if y.ndim > 1:
            return np.stack([librosa.effects.time_stretch(y[:, c], rate=float(rate))
                             for c in range(y.shape[1])], axis=1)
        return librosa.effects.time_stretch(y, rate=float(rate))
    except Exception:
        return y


def _scale_events(events: list[dict], scale: float) -> list[dict]:
    """Ужимает технику по времени, сохраняя её форму. Нужно, потому что
    Long Blend расписан на 96 долей (33 с при 172 BPM), а в превью сета на
    один переход выделено меньше — раньше таймлайн просто обрезался
    посередине, кроссфейд не доходил до конца, и кусок обрывался на
    ещё звучащем старом треке. Это и была главная причина «рвано»."""
    if abs(scale - 1.0) < 1e-9:
        return events
    out = []
    for e in events:
        e2 = dict(e)
        e2["beat_offset"] = e["beat_offset"] * scale
        if "duration_beats" in e2:
            e2["duration_beats"] = e2["duration_beats"] * scale
        out.append(e2)
    return out


def _load_stem_slice(track_path: str, part: str, sr: int, offset: float, duration: float,
                     rate: float) -> np.ndarray | None:
    """Кусок стема ровно с той же секунды и той же длины, что и основной
    буфер деки, растянутый тем же коэффициентом. Стемы покадрово совпадают
    с исходником, поэтому сетку заново искать не нужно — берём смещение,
    уже найденное по полному миксу (у него лучше отношение сигнал/шум)."""
    try:
        import stems as _stems
    except Exception:
        return None
    paths = _stems.stem_paths(track_path)
    if not paths or part not in paths:
        return None
    import librosa

    try:
        y, _ = librosa.load(paths[part], sr=sr, mono=not STEREO,
                            offset=max(0.0, offset), duration=max(0.1, duration))
    except Exception:
        return None
    y = _as2d(y)
    if len(y) < sr // 10:
        return None
    return _stretch(y, sr, rate)


def _load_aligned(path: str, sr: int, bpm: float, want_seconds: float,
                  need_seconds: float, from_start: bool, duration_hint: float | None = None) -> tuple[np.ndarray, dict]:
    """Грузит need_seconds аудио, начиная строго с ДАУНБИТА рядом с
    want_seconds.

    Раньше source брался как «последние N секунд файла», а target как
    «первые N секунд» — обе точки попадали в случайное место такта. Даже
    при одинаковом BPM бочки двух дек оказывались смещены на произвольную
    долю: темп совпадал, а такты нет. Здесь мы сначала грузим окно с
    запасом, находим в нём фазу сетки (beatgrid.py), и только потом
    отрезаем ровно с «раз»."""
    import librosa

    import beatgrid

    bar = 60.0 / max(1.0, bpm) * BAR_BEATS
    pad = bar * 4  # запас, чтобы было куда двигать точку до ближайшего «раз»
    win_start = max(0.0, want_seconds - pad)
    win_len = need_seconds + pad * 2

    y, _ = librosa.load(path, sr=sr, mono=not STEREO, offset=win_start, duration=win_len)
    y = _as2d(y)
    if len(y) < sr // 2:
        return y, {"bar_seconds": bar, "confidence": 0.0}

    ymono = _mono(y)          # сетку ищем по моно-сумме: так надёжнее
    if from_start and win_start <= 0.01:
        grid = beatgrid.first_musical_downbeat(ymono, sr, bpm)
        cut = grid["downbeat_offset"]
    else:
        grid = beatgrid.beat_phase(ymono, sr, bpm)
        cut = beatgrid.snap_to_downbeat(want_seconds - win_start, grid, mode="nearest")
    # Локальное окно надёжно даёт период и фазу ДОЛИ, но не то, какая из
    # четырёх долей «раз». Если у трека есть общая фаза такта — она главнее:
    # именно из-за расхождения тактов входящий трек садился бочкой на снейр.
    snapped = _snap_to_track_bar(path, bpm, win_start + cut, win_start + cut)
    if abs(snapped - (win_start + cut)) > 1e-3 and snapped >= win_start:
        new_cut = snapped - win_start
        # подтяжка вперёд не должна выехать за конец загруженного окна
        while new_cut + need_seconds > win_len and new_cut >= bar:
            new_cut -= bar
        if new_cut >= 0:
            cut = new_cut

    s = int(round(cut * sr))
    seg = y[s:s + int(round(need_seconds * sr))]
    grid = dict(grid)
    grid["file_offset"] = round(win_start + cut, 3)
    return seg, grid


def pick_render_source() -> None:  # pragma: no cover — заглушка для будущего расширения (третья дека и т.п.)
    return None


def render_demo(
    technique_id: str,
    track_a_path: str,
    track_b_path: str,
    bpm_a: float,
    bpm_b: float,
    out_path: str,
    param_overrides: dict | None = None,
    max_seconds: float = MAX_SECONDS,
    sr: int = SR,
    source_at: float | None = None,
    target_at: float | None = None,
    master_bpm: float | None = None,
    normalize_output: bool = True,
    _debug: dict | None = None,
) -> dict:
    """Рендерит демо и пишет WAV (PCM16) в out_path.

    source_at / target_at — секунды в файлах A и B, где план велел сводить
    (from_point / to_point из mix_strategist). Раньше рендер их игнорировал
    и всегда брал хвост A и голову B, поэтому «уводим на брейкдауне» в
    плане и то, что слышно в демо, были про разные места трека.

    Возвращает метаданные {duration_seconds, sync_time_seconds,
    events_count, ...}. Бросает ValueError/NotImplementedError на
    некорректный technique_id/стемо-зависимую технику — вызывающий код
    (server.py) превращает их в 400."""
    if technique_id not in TECHNIQUES:
        raise ValueError(f"Unknown technique: {technique_id}")
    technique = TECHNIQUES[technique_id]
    if technique.requires_stems:
        raise NotImplementedError(f"«{technique.name}» требует живых стемов — демо недоступно.")
    if not os.path.exists(track_a_path):
        raise FileNotFoundError(f"Файл не найден: {track_a_path}")
    if not os.path.exists(track_b_path):
        raise FileNotFoundError(f"Файл не найден: {track_b_path}")

    bpm_a_hint = float(bpm_a) if bpm_a else 128.0
    bpm_b_hint = float(bpm_b) if bpm_b else bpm_a_hint
    # темп из библиотеки — только подсказка октавы; точное значение меряем
    bpm_a = _refined_bpm(track_a_path, bpm_a_hint)
    bpm_b = _refined_bpm(track_b_path, bpm_b_hint)

    # master_bpm — темп всего сета. В живом сете дека подстраивается под ту,
    # что уже играет, поэтому весь сет идёт в ОДНОМ темпе; для склейки
    # полного микса это обязательно, иначе на каждом стыке трека и перехода
    # темп прыгал бы. Без master_bpm (одиночное демо) ведущим остаётся A.
    timeline_bpm = float(master_bpm) if master_bpm else bpm_a
    # Растягивать к мастер-темпу можно ТОЛЬКО трек той же темповой семьи.
    # Иначе один неверно определённый трек (112 вместо 168) утаскивает
    # мастер вниз, а все остальные играют на 70% скорости. Чужой темп
    # честнее оставить как есть: он всё равно не сведётся, но хотя бы не
    # будет звучать замедленным.
    import tempo as _tempo0
    _rel_a = _tempo0.relate(timeline_bpm, bpm_a)
    if master_bpm and not _rel_a["compatible"]:
        timeline_bpm = bpm_a
        rate_a = 1.0
    else:
        eff_a = _rel_a["effective_bpm"] if master_bpm else bpm_a
        rate_a = _clamp(timeline_bpm / eff_a, 0.5, 2.0) if eff_a > 1 else 1.0

    plan = build_plan(technique_id, "demo", "source", "target", timeline_bpm, param_overrides)
    spb = _seconds_per_beat(timeline_bpm)
    bar_sec = spb * BAR_BEATS
    lead = LEAD_BARS * bar_sec
    tail = TAIL_BARS * bar_sec

    # --- подбираем масштаб техники так, чтобы её ФОРМА влезла целиком ---
    raw_end = max([4.0 * spb] + [_event_window(e, spb)[1] for e in plan["events"]])
    budget = max(2.0 * bar_sec, float(max_seconds) - lead - tail)
    scale = TIME_SCALES[-1]
    for s in TIME_SCALES:
        if raw_end * s <= budget:
            scale = s
            break
    events = _scale_events(plan["events"], scale)

    windows = [_event_window(e, spb) for e in events]
    t_max = max([2.0 * bar_sec] + [e for _, e in windows])
    timeline_start = -lead
    timeline_end = t_max + tail

    # Короткие техники (Echo Cut — 4 доли, Delay Out — 10) укладывались в
    # 5-7 секунд, и превью сета превращалось в череду огрызков: чем короче
    # кусок, тем чаще склейка и тем рванее звучит целое. Дотягиваем кусок
    # до общей длины, продолжая играть уже вошедший трек — ровно так это
    # и происходит в живом сете: сведение кончилось, новый трек играет.
    min_duration = max(4.0 * bar_sec, float(max_seconds) * 0.6)
    if timeline_end - timeline_start < min_duration:
        extra = min_duration - (timeline_end - timeline_start)
        timeline_end += np.ceil(extra / bar_sec) * bar_sec

    total_duration = timeline_end - timeline_start
    n = int(total_duration * sr)

    import librosa

    # --- source: где план велел уводить трек, но ровно с даунбита ---
    try:
        src_full_dur = float(librosa.get_duration(path=track_a_path))
    except Exception:
        src_full_dur = total_duration
    # сколько СЫРЫХ секунд трека нужно, чтобы после растяжения получилось
    # total_duration секунд в темпе таймлайна
    src_need_raw = total_duration * rate_a
    if source_at is not None and source_at > 0:
        # точка из плана — это момент, где начинается СВЕДЕНИЕ, а таймлайн
        # стартует на такт раньше (lead)
        src_want = max(0.0, float(source_at) - lead * rate_a)
    else:
        src_want = max(0.0, src_full_dur - src_need_raw)
    src_want = min(src_want, max(0.0, src_full_dur - src_need_raw))
    source, src_grid = _load_aligned(track_a_path, sr, bpm_a, src_want, src_need_raw, from_start=False,
                                     duration_hint=src_full_dur)
    source = _stretch(source, sr, rate_a)
    source = _fit_length(source, n)
    src_drums = _load_stem_slice(track_a_path, "drums", sr,
                                 src_grid.get("file_offset") or 0.0, src_need_raw, rate_a)
    src_music = _load_stem_slice(track_a_path, "no_drums", sr,
                                 src_grid.get("file_offset") or 0.0, src_need_raw, rate_a)

    # --- когда target впервые становится слышен (квантуем в такт) ---
    sync_events = [e for e in events if e["action"] == "sync" and e["deck"] == "target"]
    target_play_toggles = [
        e for e in events
        if e["action"] in ("play_toggle", "play_from_cue") and e["deck"] == "target"
    ]
    if sync_events:
        sync_time = min(e["beat_offset"] for e in sync_events) * spb
    elif target_play_toggles:
        sync_time = min(e["beat_offset"] for e in target_play_toggles) * spb
    else:
        sync_time = 0.0
    sync_time = round(sync_time / bar_sec) * bar_sec
    sync_time = _clamp(sync_time, timeline_start, timeline_end - bar_sec)

    # --- target: интро (или точка из плана), приведённое к темпу source ---
    import tempo as _tempo
    _rel = _tempo.relate(timeline_bpm, bpm_b)
    _effective_b = _rel["effective_bpm"] if _rel["compatible"] else bpm_b
    rate = _clamp(timeline_bpm / _effective_b, 0.5, 2.0) if _effective_b > 1 else 1.0

    target_audible_dur = timeline_end - sync_time
    # если будем растягивать — нужно взять исходника больше/меньше в rate раз
    need_raw = target_audible_dur * rate + 2.0 * bar_sec
    if target_at is not None and target_at > 0:
        tgt_want = float(target_at)
    else:
        tgt_want = _first_beat_time(track_b_path, bpm_b)
    target_raw, tgt_grid = _load_aligned(track_b_path, sr, bpm_b, tgt_want, need_raw,
                                         from_start=(tgt_want <= 0.01))
    ch = target_raw.shape[1] if target_raw.ndim > 1 else 1

    if abs(rate - 1.0) > 0.005 and len(target_raw) > sr * 0.5:
        target_raw = _stretch(target_raw, sr, rate)
        # Здесь раньше заново искался «раз» и буфер обрезался до него. Это
        # было неверно дважды, и обе ошибки слышны.
        #
        # Во-первых, искать нечего: и ресемпл, и фазовый вокодер переводят
        # отсчёт 0 в отсчёт 0, а буфер уже начат с даунбита (_load_aligned).
        # Детектор же выбирает «раз» голосованием по четырём долям и
        # регулярно голосует за вторую — тогда с начала срезалась ЦЕЛАЯ
        # доля, и новый трек заходил на долю мимо. Ровно то, что слышно как
        # «сводит на 1-2 доли раньше или позже».
        #
        # Во-вторых, срезанные секунды не прибавлялись к file_offset. Для
        # render_full_set это значит, что кусок кончился РАНЬШЕ, чем на
        # самом деле, и тело входящего трека начиналось с уже сыгранного —
        # тот самый повтор доли сразу после сведения. Измерено на паре
        # Evol Intent -> Joanna Syze: расхождение +0.347 с, ровно одна доля.
        #
        # Поэтому: поправку оставляем только микроскопическую (вокодер
        # может сдвинуть на длину кадра, это доли миллисекунд) и
        # обязательно учитываем её в file_offset.
        try:
            import beatgrid as _bg
            g2 = _bg.beat_phase(_mono(target_raw), sr, timeline_bpm)
            beat_sec = bar_sec / BAR_BEATS
            drift = float(g2.get("beat_offset") or 0.0) % beat_sec
            if drift > beat_sec * 0.5:
                drift -= beat_sec          # ближайшая доля, а не следующая
            cut = int(round(drift * sr))
            if 0 < cut < int(beat_sec * sr * 0.25) and cut < len(target_raw) - sr:
                target_raw = target_raw[cut:]
                tgt_grid = dict(tgt_grid)
                tgt_grid["file_offset"] = round(
                    (tgt_grid.get("file_offset") or 0.0) + cut / sr * rate, 3)
        except Exception:
            pass

    # --- уравниваем громкость дек ДО суммирования ---
    source = _match_loudness(source, _track_reference_rms(track_a_path))
    target_raw = _match_loudness(target_raw, _track_reference_rms(track_b_path))

    tgt_drums_raw = _load_stem_slice(track_b_path, "drums", sr,
                                     tgt_grid.get("file_offset") or 0.0, need_raw, rate)
    tgt_music_raw = _load_stem_slice(track_b_path, "no_drums", sr,
                                     tgt_grid.get("file_offset") or 0.0, need_raw, rate)

    target = np.zeros((n, ch))
    start_sample = int(_clamp(_sample_idx(sync_time, timeline_start, sr), 0, n))
    target_seg = _fit_length(target_raw, n - start_sample)
    target[start_sample:start_sample + len(target_seg)] = target_seg

    def _place(buf):
        if buf is None:
            return None
        out = np.zeros((n, ch))
        seg = _fit_length(buf, n - start_sample)
        out[start_sample:start_sample + len(seg)] = seg
        return out

    tgt_drums = _place(tgt_drums_raw)
    tgt_music = _place(tgt_music_raw)
    src_drums = _fit_length(src_drums, n) if src_drums is not None else None
    src_music = _fit_length(src_music, n) if src_music is not None else None

    stems_ready = all(x is not None for x in (src_drums, src_music, tgt_drums, tgt_music))

    decks = {"source": source, "target": target}

    # --- 1. reverse_hold — переворачиваем кусок буфера (эффект вертушки) ---
    for e in events:
        if e["action"] == "reverse_hold":
            s = max(0, _sample_idx(e["beat_offset"] * spb, timeline_start, sr))
            dur_n = int(e.get("duration_beats", 0.0) * spb * sr)
            end = min(n, s + dur_n)
            buf = decks.get(e["deck"])
            if buf is not None and end - s > 8:
                buf[s:end] = buf[s:end][::-1]

    # --- 1b. ручки EQ (eq_low/eq_mid/eq_high) — непрерывные, не toggle ---
    # 1.0 = штатный уровень, 0.0 = полоса убрана. Приближаем полкой:
    # выделяем полосу фильтром и подмешиваем её с нужным весом обратно.
    eq_specs = {
        "eq_low": ("low", 200.0, None),
        "eq_mid": ("band", 300.0, 3000.0),
        "eq_high": ("high", 3000.0, None),
    }
    for action, (kind, f1, f2) in eq_specs.items():
        for deck, buf in decks.items():
            ev = [e for e in events if e["action"] == action and e["deck"] == deck]
            if not ev or buf is None:
                continue
            # Значения нормализованы 0..1, где «штатный уровень» ручки — это
            # её neutral (у EQ 0.25, т.к. диапазон Mixxx 0..4). Переводим в
            # множитель громкости полосы: neutral -> 1.0, ноль -> 0.0.
            neutral = _CONTROLS[action].neutral or 1.0
            gain_fn = _piecewise_ramp(ev, spb, default=neutral)
            gain_env = _envelope_array(gain_fn, timeline_start, n, sr,
                                       step_sec=_events_step_sec(ev, spb)) / neutral
            if np.allclose(gain_env, 1.0):
                continue
            if kind == "low":
                sos = butter(2, min(f1 / (sr / 2), 0.99), btype="lowpass", output="sos")
            elif kind == "high":
                sos = butter(2, min(f1 / (sr / 2), 0.99), btype="highpass", output="sos")
            else:
                sos = butter(2, [min(f1 / (sr / 2), 0.98), min(f2 / (sr / 2), 0.99)],
                             btype="bandpass", output="sos")
            band = sosfiltfilt(sos, buf, axis=0)
            # buf = (buf - band) + band*gain: остальные полосы не трогаем
            buf -= band
            buf += band * _env(gain_env, buf)

    # --- 2. eq_kill_low / eq_kill_mid — toggle-фильтры на сегментах ---
    kill_specs = {"eq_kill_low": ("high", 200.0, None), "eq_kill_mid": ("band", 300.0, 3000.0)}
    for action, (kind, a1, a2) in kill_specs.items():
        by_deck: dict[str, list[float]] = {}
        for e in events:
            if e["action"] == action:
                by_deck.setdefault(e["deck"], []).append(e["beat_offset"] * spb)
        for deck, times in by_deck.items():
            buf = decks.get(deck)
            if buf is None:
                continue
            for s_t, e_t in _toggle_segments(times):
                s = _clamp(_sample_idx(s_t, timeline_start, sr), 0, n)
                e_i = n if e_t is None else _clamp(_sample_idx(e_t, timeline_start, sr), 0, n)
                if kind == "high":
                    _apply_highpass_segment(buf, s, e_i, sr, cutoff_hz=a1)
                else:
                    _apply_bandstop_segment(buf, s, e_i, sr, lo_hz=a1, hi_hz=a2)

    # --- 3. key_shift — питч-шифт окна одним конечным значением ---
    for e in events:
        if e["action"] == "key_shift":
            buf = decks.get(e["deck"])
            if buf is None:
                continue
            start, end = _event_window(e, spb)
            s = _clamp(_sample_idx(start, timeline_start, sr), 0, n)
            e_i = _clamp(_sample_idx(end, timeline_start, sr), 0, n)
            if e_i - s > sr * 0.3:
                semitones = (e["value_to"] - 0.5) * 12.0
                if abs(semitones) > 0.05:
                    try:
                        seg = buf[s:e_i]
                        if seg.ndim > 1:
                            shifted = np.stack([librosa.effects.pitch_shift(seg[:, c], sr=sr, n_steps=semitones)
                                                for c in range(seg.shape[1])], axis=1)
                        else:
                            shifted = librosa.effects.pitch_shift(seg, sr=sr, n_steps=semitones)
                        buf[s:e_i] = _fit_length(shifted, e_i - s)
                    except Exception:
                        pass

    # --- 4. loop_activate/loop_exit — грубая аппроксимация повтором ---
    la_by_deck: dict[str, list[tuple[float, float, float]]] = {}
    activates = {}
    for e in sorted(events, key=lambda e: e["beat_offset"]):
        if e["action"] == "loop_activate":
            # длину лупа задаёт сама техника: 4-тактовый луп, повторённый
            # один раз, — это то, что делает диджей; однотактовый, повторённый
            # четыре раза, — это статтер, совсем другой приём
            beats = float((e.get("params") or {}).get("beats") or 4.0)
            activates[e["deck"]] = (e["beat_offset"] * spb, beats * spb)
        elif e["action"] == "loop_exit" and e["deck"] in activates:
            start, unit = activates.pop(e["deck"])
            la_by_deck.setdefault(e["deck"], []).append((start, e["beat_offset"] * spb, unit))
    for deck, pairs in la_by_deck.items():
        buf = decks.get(deck)
        if buf is None:
            continue
        for act_t, exit_t, want_unit in pairs:
            span = exit_t - act_t
            if span <= 0:
                continue
            unit = min(span, want_unit if want_unit > 0 else spb * 4)
            s = _clamp(_sample_idx(act_t, timeline_start, sr), 0, n)
            e_i = _clamp(_sample_idx(exit_t, timeline_start, sr), 0, n)
            unit_n = max(1, int(unit * sr))
            if e_i - s < unit_n * 2:
                continue
            src_unit = buf[s:s + unit_n].copy()
            # микрофейд на стыке повторов: без него на каждом обороте щелчок
            xf = min(unit_n // 8, int(0.006 * sr))
            pos = s
            while pos < e_i:
                seg_len = min(unit_n, e_i - pos)
                chunk = src_unit[:seg_len].copy()
                if xf > 1 and pos > s and seg_len > xf:
                    ramp = np.linspace(0.0, 1.0, xf)
                    chunk[:xf] = chunk[:xf] * ramp + buf[pos:pos + xf] * (1.0 - ramp)
                buf[pos:pos + seg_len] = chunk
                pos += unit_n

    # --- 5. fx_meta — эхо/дилей-посыл, добавляется поверх сухого сигнала ---
    for deck in ("source", "target"):
        # fx_mix (dry/wet юнита) и fx_meta (параметр эффекта) в реальном
        # Mixxx крутятся вместе — для демо берём максимум как «глубину эха».
        fx_events = [
            e for e in events
            if e["action"] in ("fx_meta", "fx_mix") and e["deck"] == deck
        ]
        if not fx_events:
            continue
        fn = _piecewise_ramp(fx_events, spb, default=0.0)
        mix_env = _envelope_array(fn, timeline_start, n, sr)
        if np.max(mix_env) > 0.02:
            decks[deck] = _apply_echo(decks[deck], sr, mix_env)

    # --- 6. filter_sweep — плавающий ФНЧ по огибающей ---
    for deck in ("source", "target"):
        fs_events = [e for e in events if e["action"] == "filter_sweep" and e["deck"] == deck]
        if not fs_events:
            continue
        fn = _piecewise_ramp(fs_events, spb, default=0.0)
        decks[deck] = _time_varying_lowpass(decks[deck], sr, fn, timeline_start)

    # --- 7. play_toggle на source — быстрый мьют (щелчок сглажен 15мс) ---
    for e in events:
        if e["action"] == "play_toggle" and e["deck"] == "source":
            s = _clamp(_sample_idx(e["beat_offset"] * spb, timeline_start, sr), 0, n)
            fade_n = min(n - s, int(0.015 * sr))
            if fade_n > 0:
                decks["source"][s:s + fade_n] *= _env(np.linspace(1.0, 0.0, fade_n), decks["source"])
            decks["source"][s + fade_n:] = 0.0


    # --- 8. финальный микс ---
    crossfade_events = [e for e in events if e["action"] == "crossfade"]
    cf_fn = _piecewise_ramp(crossfade_events, spb, default=0.0)
    cf_step = _events_step_sec(crossfade_events, spb)
    cf_env = _envelope_array(cf_fn, timeline_start, n, sr, step_sec=cf_step)

    # Страховка: кроссфейд ОБЯЗАН дойти до конца внутри куска. Если техника
    # не довела его (например, Double Drop штатно останавливается на 0.5),
    # доводим сами за последний такт — иначе кусок обрывается на ещё
    # звучащем старом треке, и в склейке сета это слышно как обрыв.
    if len(cf_env) and cf_env[-1] < 0.995:
        # у резовых техник дотягивать целый такт нельзя — получится
        # плавный хвост там, где должен быть последний рез
        quick = cf_step < 0.02
        k = min(n, int((0.25 if quick else bar_sec) * sr))
        ramp = np.linspace(float(cf_env[-k]), 1.0, k)
        cf_env[-k:] = np.maximum(cf_env[-k:], ramp)

    vol_events = [e for e in events if e["action"] == "volume_ramp" and e["deck"] == "target"]
    vol_fn = _piecewise_ramp(vol_events, spb, default=1.0) if vol_events else (lambda t: 1.0)
    vol_env = _envelope_array(vol_fn, timeline_start, n, sr) if vol_events else np.ones(n)

    g_out, g_in = _equal_power(cf_env)

    # «Одна бочка за раз». Применяем только тем декам, у которых техника
    # НЕ управляет низом сама — иначе мы бы спорили с eq_low из техники.
    decks_with_eq_low = {e["deck"] for e in events if e["action"] in ("eq_low", "eq_kill_low")}
    bass_out, bass_in = _bass_handover(cf_env)
    if "source" not in decks_with_eq_low:
        low, rest = _split_low(decks["source"], sr)
        decks["source"] = rest + low * _env(bass_out, low)
    if "target" not in decks_with_eq_low:
        low, rest = _split_low(decks["target"], sr)
        decks["target"] = rest + low * _env(bass_in, low)

    if _debug is not None:
        _debug.update({"source": decks["source"].copy(), "target": decks["target"].copy(),
                       "cf_env": cf_env.copy(), "sr": sr, "spb": spb, "bar_sec": bar_sec,
                       "timeline_start": timeline_start, "sync_time": sync_time,
                       "start_sample": start_sample, "timeline_bpm": timeline_bpm})
    # --- сведение по стемам: барабаны одного трека поверх музыки другого ---
    # Это и есть то, ради чего стемы считались. Пока играют оба трека,
    # барабаны звучат ТОЛЬКО у одного из них: складывать две плотные
    # ударные партии нечем — их просто нет одновременно. Именно так
    # работает Traktor Stems, и именно поэтому эквалайзером тот же
    # результат получить нельзя: обеим дорожкам нужны одни полосы.
    #
    # Момент обмена берём тот же, что у баса: техника уже говорит, где
    # низ уходит у старого и приходит у новому. Барабаны и бас у диджея
    # всегда переключаются вместе, разводить их незачем.
    stem_swap = None
    if stems_ready:
        eq_src = [e for e in events if e["action"] == "eq_low" and e["deck"] == "source"]
        eq_tgt = [e for e in events if e["action"] == "eq_low" and e["deck"] == "target"]
        neutral = _CONTROLS["eq_low"].neutral or 1.0
        if eq_src and eq_tgt:
            gate_a = _envelope_array(_piecewise_ramp(eq_src, spb, default=neutral),
                                     timeline_start, n, sr,
                                     step_sec=_events_step_sec(eq_src, spb)) / neutral
            gate_b = _envelope_array(_piecewise_ramp(eq_tgt, spb, default=0.0),
                                     timeline_start, n, sr,
                                     step_sec=_events_step_sec(eq_tgt, spb)) / neutral
        else:
            # техника низом не управляет — ведём барабаны по кроссфейдеру
            gate_b = _smoothstep((cf_env - 0.40) / 0.20)
            gate_a = 1.0 - gate_b
        gate_a = np.clip(gate_a, 0.0, 1.0)
        gate_b = np.clip(gate_b, 0.0, 1.0)
        # страховка: сумма гейтов не больше единицы — две установки вместе
        # не зазвучат даже если техника задала перехлёст
        over = np.maximum(gate_a + gate_b, 1.0)
        gate_a, gate_b = gate_a / over, gate_b / over

        decks["source"] = src_music + src_drums * _env(gate_a, src_drums)
        decks["target"] = tgt_music + tgt_drums * _env(gate_b, tgt_drums)
        stem_swap = float(np.argmax(gate_b > 0.5)) / sr if np.any(gate_b > 0.5) else None

    # Диджей ведёт гейн входящей деки рукой, пока она идёт под уходящей.
    ride = _ride_gain(_mono(decks["source"]), _mono(decks["target"]), cf_env, sr)
    if _debug is not None:
        _debug["ride"] = ride.copy()
    mix = (decks["source"] * _env(g_out, decks["source"])
           + decks["target"] * _env(vol_env * g_in * ride, decks["target"]))
    # В полном сете кусок — часть большого файла, и подтягивать его уровень
    # отдельно НЕЛЬЗЯ: тела треков вокруг него нормируются по своему треку,
    # и переход стал бы громче или тише окружения. Там уровнем управляет
    # только _match_loudness по опорному уровню трека.
    if normalize_output:
        mix = _normalize_output(mix)
    else:
        mix = _soft_limit(mix)
    mix = _edge_fade(mix, sr)

    import soundfile as sf

    sf.write(out_path, mix.astype(np.float32), sr, subtype="PCM_16")

    return {
        "duration_seconds": round(total_duration, 2),
        "sync_time_seconds": round(sync_time - timeline_start, 2),
        "events_count": len(events),
        "time_scale": scale,
        "bpm_source": round(bpm_a, 2),
        "bpm_target": round(bpm_b, 2),
        "bpm_source_hint": round(bpm_a_hint, 2),
        "bpm_target_hint": round(bpm_b_hint, 2),
        "stretch_rate": round(rate, 4),
        "bar_seconds": round(bar_sec, 3),
        "beats_per_bar": BAR_BEATS,
        "source_offset_seconds": src_grid.get("file_offset"),
        "target_offset_seconds": tgt_grid.get("file_offset"),
        # где в ИСХОДНЫХ файлах начинается и заканчивается использованный
        # кусок — по этим числам render_full_set сшивает тела треков с
        # переходами без дырок и без повторов
        "source_in_seconds": src_grid.get("file_offset"),
        "source_out_seconds": (None if src_grid.get("file_offset") is None
                               else round(src_grid["file_offset"] + total_duration * rate_a, 3)),
        "target_in_seconds": tgt_grid.get("file_offset"),
        "target_out_seconds": (None if tgt_grid.get("file_offset") is None
                               else round(tgt_grid["file_offset"] + (timeline_end - sync_time) * rate, 3)),
        "master_bpm": round(timeline_bpm, 2),
        "rate_source": round(rate_a, 4),
        "grid_confidence": round(min(float(src_grid.get("confidence", 0.0)),
                                     float(tgt_grid.get("confidence", 0.0))), 3),
        "crossfade_complete": bool(len(cf_env) and cf_env[-1] >= 0.99),
        "stems_used": bool(stems_ready),
        "drum_swap_seconds": None if stem_swap is None else round(stem_swap, 2),
    }
