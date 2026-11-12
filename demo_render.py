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

from transition_controls import CONTROL_NEUTRAL
import transport as _transport
from techniques import TECHNIQUES, build_plan, _ramp as _mk_ramp

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
# Уровень возврата эффекта в микс, относительно сухого сигнала. На пульте
# ручку возврата держат заметно ниже канала: возврат идёт мимо гейн-райдинга
# и мимо «одной бочки за раз», поэтому при 1.0 эхо звучит не как эффект, а
# как вторая копия трека — ровно то, на что жаловались в ControlC. 0.45 —
# примерно -7 дБ от сухого: слышно, но микс остаётся миксом.
FX_RETURN_LEVEL = 0.45
# Ниже какой доли от собственного уровня трека считаем, что низа НЕТ.
# 0.35 — там, где уходящий ушёл в брейкдаун: замерено, в брейкдауне
# энергия ниже 180 Гц падает до 0.01..0.17 от уровня тела трека.
BASS_PRESENT_FRAC = 0.35


def _smooth_curve(x, win: int):
    """Скользящее среднее по крупной сетке — чтобы поправка низа не
    дёргалась от одного удара, а следовала за секцией."""
    import numpy as _np

    win = max(1, int(win))
    if win <= 1 or len(x) <= win:
        return _np.asarray(x, dtype=float)
    k = _np.ones(win) / win
    pad = _np.concatenate([_np.full(win, x[0]), _np.asarray(x, dtype=float),
                           _np.full(win, x[-1])])
    return _np.convolve(pad, k, mode="same")[win:win + len(x)]
EDGE_FADE_SEC = 0.010
# Во сколько раз техника растягивается или ужимается по времени, чтобы
# занять отведённое окно. Только степени двойки — 32 такта -> 16 -> 8
# остаются кратными такту, а 32 -> 19 разъехалось бы с музыкой.
#
# Раньше лесенка шла только вниз, и это была вторая причина коротких
# переходов: сколько бы тактов ни стояло на карточке шва, техника
# играла свою «родную» длину, а остаток окна просто доигрывал вошедший
# трек. Теперь при большом окне приём РАСТЯГИВАЕТСЯ — ровно этого и
# просил диджей, когда сказал «максимально более долгие переходы».
TIME_SCALES = (1.0, 0.5, 0.25, 0.125)
# Пределы растяжения, когда длина перехода ЗАДАНА. Растягивать приём (а
# не только ужимать) разрешено только в этом случае: без заданной длины
# каждое демо растягивалось до полного окна в 38 секунд, и все приёмы
# разъезжались вчетверо — диджей услышал это сразу и назвал «сводит
# просто ужасно».
#
# Восьмикратный потолок — это четырёхтактовый приём на 32 такта. Дальше
# растягивать нечего: жест перестаёт быть жестом.
MIN_TIME_SCALE = 0.125
MAX_TIME_SCALE = 8.0

# Профиль ввода «наложение». Диджей описал его дословно: новый трек
# заходит с урезанным низом, доходит до 85% громкости, только потом
# меняется бас (или слои), и лишь в самом конце уходящий уводится
# фейдером. Кроссфейдер при этом почти всё время стоит на месте — то
# есть это НЕ кроссфейд, а наложение, и звучит оно принципиально иначе:
# оба трека какое-то время играют вместе полноценно.
OVERLAY_IN_LEN = 0.40   # за какую долю перехода входящий доходит до полного
OVERLAY_OUT_LEN = 0.25  # сколько длится увод уходящего


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
    последнего — его value_to. Возвращает fn(t) -> float.

    value_from может отсутствовать или быть None. Это не ошибка данных, а
    осмысленный случай: «поехали оттуда, где сейчас стоим». Раньше здесь
    стояло ev["value_from"], и такое событие роняло весь рендер с
    KeyError: 'value_from' — 500 на "послушать этот переход" и на демо
    ST-06. Теперь недостающее начало берётся из value_to предыдущего окна
    (а для первого окна — из default), то есть ровно из того значения, на
    котором ручка и стоит к этому моменту.

    Событие без value_to пропускается: ramp, которому некуда ехать, — это
    уже не «продолжи с текущего», а сломанные данные, и молча подставлять
    туда число значит спрятать поломку.
    """
    windows = []
    prev_to = None
    for ev in sorted(events, key=lambda e: e["beat_offset"]):
        vt = ev.get("value_to")
        if vt is None:
            print(f"[demo_render] ramp без value_to пропущен: "
                  f"{ev.get('action')}@{ev.get('deck')} beat={ev.get('beat_offset')}")
            continue
        vf = ev.get("value_from")
        if vf is None:
            vf = prev_to if prev_to is not None else default
        start, end = _event_window(ev, spb)
        windows.append((start, end, float(vf), float(vt), ev.get("curve", "linear"),
                         (ev.get("params") or {}).get("cycles", 1.0)))
        prev_to = float(vt)
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


# ---------------------------------------------------------------------------
# Эффекты.
#
# Делятся на два класса, и это деление не косметическое, а определяет, где
# эффект стоит в тракте:
#
#   ВСТАВКА (insert) — фленджер, фейзер, вобл, дисторшн, биткрашер,
#   фильтр. Обрабатывают сам сигнал деки и живут ДО фейдера: увёл
#   фейдер — эффекта тоже не слышно, как и трека. Так они и включены на
#   пульте.
#
#   ПОСЫЛ (send) — эхо и реверб. У них есть ХВОСТ, который обязан пережить
#   увод трека: ровно ради этого их и включают в конце фразы. Поэтому они
#   считаются как отдельная шина возврата и складываются в микс ПОСЛЕ
#   фейдера (см. FX_RETURN_LEVEL).
#
# Разбираться, «как правильно», проще всего по этому признаку: если у
# эффекта есть хвост — это посыл, если нет — вставка.
#
# ВАЖНО про посыл: линия возврата стоит после фейдера, но КОРМИТСЯ посыл
# тоже после фейдера. Это не одно и то же, и путать эти две вещи —
# ровно тот баг, из-за которого эхо звучало как ещё одна дорожка поверх
# готового клипа и загоняло пик в потолок: дека уведена в ноль, а в эхо
# по-прежнему уходил её сигнал на полной громкости. На пульте так не
# бывает — увёл фейдер, и в эффект больше ничего не поступает. Хвост при
# этом всё равно доигрывает: он уже лежит в линии задержки. Поэтому
# возвраты считаются НЕ здесь, а откладываются в очередь (fx_send_queue)
# и обсчитываются в конце, когда огибающие фейдеров уже известны.

FX_SENDS = ("echo", "reverb")
FX_INSERTS = ("flanger", "phaser", "wobble", "distortion", "bitcrush", "filter")
FX_ALL = FX_SENDS + FX_INSERTS


def _lfo(n: int, sr: int, rate_hz: float, shape: str = "sine") -> np.ndarray:
    """Управляющий генератор, 0..1. Частота задаётся в Гц, а техника
    считает её из ДОЛЕЙ — чтобы качание всегда попадало в темп."""
    t = np.arange(n, dtype=np.float64) / sr
    ph = 2 * np.pi * max(0.01, rate_hz) * t
    if shape == "tri":
        return np.abs(((ph / np.pi) % 2.0) - 1.0)
    if shape == "saw":
        return ((ph / (2 * np.pi)) % 1.0)
    return 0.5 - 0.5 * np.cos(ph)


def _mix_dry_wet(dry: np.ndarray, wet: np.ndarray, env: np.ndarray) -> np.ndarray:
    g = _env(np.clip(env, 0.0, 1.0), dry)
    return dry * (1.0 - g) + wet * g


def _fx_flanger(x: np.ndarray, sr: int, env: np.ndarray, rate_hz: float,
                depth_ms: float = 3.5, base_ms: float = 1.0) -> np.ndarray:
    """Фленджер: короткая ПЛАВАЮЩАЯ задержка, сложенная с сухим сигналом.

    Обратной связи здесь нет: рекурсия по отсчётам в numpy стоит дороже
    всего остального рендера вместе взятого. Вместо неё второй отвод на
    удвоенной задержке с меньшим весом — гребёнка получается плотнее, и
    на слух это ровно тот же «взлетающий самолёт»."""
    n = len(x)
    d = (base_ms + depth_ms * _lfo(n, sr, rate_hz)) * sr / 1000.0
    idx = np.arange(n, dtype=np.float64)
    grid = np.arange(n, dtype=np.float64)
    wet = np.zeros_like(x)
    for ch in range(x.shape[1]):
        wet[:, ch] = (np.interp(idx - d, grid, x[:, ch], left=0.0)
                      + 0.45 * np.interp(idx - 2 * d, grid, x[:, ch], left=0.0))
    return _mix_dry_wet(x, 0.65 * (x + wet), env)


def _fx_phaser(x: np.ndarray, sr: int, env: np.ndarray, rate_hz: float,
               stages: int = 4, block: int = 512) -> np.ndarray:
    """Фейзер: каскад всепропускающих звеньев с плавающей частотой.

    Коэффициент звена зависит от времени, а рекурсивный фильтр с
    меняющимся коэффициентом на каждом отсчёте в numpy не считается.
    Поэтому идём блоками по 512 отсчётов (12 мс — быстрее, чем ухо
    различает движение ручки) и внутри блока коэффициент постоянный."""
    from scipy.signal import lfilter

    n = len(x)
    lfo = _lfo(n, sr, rate_hz)
    wet = np.empty_like(x)
    zi = [np.zeros((1,) + (x.shape[1],)) for _ in range(stages)]
    for start in range(0, n, block):
        end = min(n, start + block)
        f = 200.0 * (10.0 ** (1.6 * float(lfo[start])))     # 200 Гц .. 8 кГц
        a = (np.tan(np.pi * f / sr) - 1.0) / (np.tan(np.pi * f / sr) + 1.0)
        seg = x[start:end]
        for k in range(stages):
            seg, zi[k] = lfilter([a, 1.0], [1.0, a], seg, axis=0, zi=zi[k])
        wet[start:end] = seg
    return _mix_dry_wet(x, 0.5 * (x + wet), env)


def _fx_wobble(x: np.ndarray, sr: int, env: np.ndarray, rate_hz: float,
               lo_hz: float = 180.0, hi_hz: float = 5000.0) -> np.ndarray:
    """Вобл: фильтр низких частот, которым качает генератор в темп.

    То, на чём стоит половина бас-музыки. Отличается от свипа только тем,
    что ручка не едет в одну сторону, а качается — поэтому переиспользуем
    тот же плавающий фильтр, подав ему LFO вместо рампы."""
    n = len(x)
    lfo = _lfo(n, sr, rate_hz, "tri")

    def value_fn(t: float) -> float:
        i = int(_clamp(t * sr, 0, n - 1))
        return 1.0 - float(lfo[i])          # 1.0 = фильтр закрыт

    wet = _time_varying_lowpass(x, sr, value_fn, 0.0, lo_hz=lo_hz, hi_hz=hi_hz)
    return _mix_dry_wet(x, wet, env)


def _fx_distortion(x: np.ndarray, env: np.ndarray, drive: float = 8.0) -> np.ndarray:
    """Перегруз мягким ограничением. Компенсация громкости обязательна:
    tanh поднимает средний уровень, и без неё «эффект» слышится просто как
    «стало громче»."""
    wet = np.tanh(x * drive) / np.tanh(drive)
    ref = float(np.sqrt(np.mean(x ** 2))) or 1e-6
    cur = float(np.sqrt(np.mean(wet ** 2))) or 1e-6
    return _mix_dry_wet(x, wet * (ref / cur), env)


def _fx_bitcrush(x: np.ndarray, sr: int, env: np.ndarray, bits: float = 6.0,
                 downsample: int = 8) -> np.ndarray:
    """Биткрашер: грубее сетка по уровню и по времени. Две независимые
    вещи, и слышны они по-разному — квантование даёт песок, прореживание
    даёт металлический призвук."""
    q = 2.0 ** max(1.0, bits)
    wet = np.round(x * q) / q
    k = max(1, int(downsample))
    if k > 1:
        idx = (np.arange(len(wet)) // k) * k
        wet = wet[idx]
    return _mix_dry_wet(x, wet, env)


def _reverb_return(x: np.ndarray, sr: int, send_env: np.ndarray,
                   decay_sec: float = 1.8) -> np.ndarray:
    """Возврат реверберации. Как и эхо — ПОСЫЛ: ручка решает, что попадёт
    в комнату, а хвост живёт дальше сам и переживает увод трека.

    Импульсная характеристика — затухающий шум с ранними отражениями.
    Это не модель конкретного зала, но именно так звучит реверб на
    диджейском пульте: хвост, а не помещение."""
    from scipy.signal import fftconvolve

    # Потолок длины хвоста. Был 3 секунды — и это делало ручку «хвост»
    # ложью: замерено, что при decay_sec 3, 6 и 10 хвост до -26 дБ
    # держится 3.03, 3.23 и 3.24 секунды, то есть ручка выше трёх секунд
    # не меняла НИЧЕГО. Двенадцать долей — а столько ручка и позволяет —
    # это 4.1 секунды на 174 BPM и 5.6 на 128. Потолок поднят так, чтобы
    # весь ход ручки был слышен; считается это офлайн, лишняя секунда
    # импульса стоит миллисекунды.
    ir_n = int(min(decay_sec, 8.0) * sr)
    t = np.arange(ir_n) / sr
    rng = np.random.default_rng(7)          # фиксируем: рендер должен быть воспроизводим
    ir = rng.standard_normal(ir_n) * np.exp(-t / max(0.1, decay_sec / 3.0))
    ir[: int(0.005 * sr)] = 0.0             # предзадержка: без неё «мыло» на атаках
    ir /= np.sqrt(np.sum(ir ** 2)) or 1.0
    feed = x * _env(send_env, x)
    wet = np.zeros_like(x)
    for ch in range(x.shape[1]):
        wet[:, ch] = fftconvolve(feed[:, ch], ir)[:len(x)]
    return wet


def _echo_return(signal: np.ndarray, sr: int, send_env: np.ndarray,
                 delay_sec: float = 0.375, feedback: float = 0.45,
                 repeats: int = 8) -> np.ndarray:
    """ВОЗВРАТ эффекта: то, что дилей отдаёт обратно в микс.

    Здесь была ошибка, которую диджей услышал сразу: «эхо берёт первую
    половину фразы, а не последнюю». Причина в том, что огибающая посыла
    умножалась на ВЫХОД дилея, а не на его ВХОД. У настоящего посыла
    ручка решает, что ПОПАДЁТ в линию задержки; попавшее живёт дальше
    само и звенит уже после того, как ручку закрыли. При гейтировании
    выхода всё наоборот: закрыл посыл — хвост оборвался, и слышно ровно
    ту часть фразы, во время которой ручка была открыта.

    Вторая половина той же ошибки — где этот возврат складывается.
    Раньше эхо подмешивалось внутрь буфера деки, а дальше дека шла через
    кроссфейдер. То есть «эхо-хвост», ради которого приём и делается,
    выключался тем самым движением фейдера, которое он должен пережить.
    Теперь функция отдаёт ТОЛЬКО мокрый сигнал, и он складывается в микс
    ПОСЛЕ фейдера — как возврат эффекта на настоящем пульте."""
    n = len(signal)
    delay_n = max(1, int(delay_sec * sr))
    feed = signal * _env(send_env, signal)
    wet = np.zeros_like(signal)
    amp, pos = 1.0, 0
    for _ in range(repeats):
        amp *= feedback
        pos += delay_n
        if pos >= n or amp < 0.02:
            break
        wet[pos:] += feed[:n - pos] * amp
    return wet




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


# Ниже этого уровня в хвосте куска — не музыка, а конец материала.
# −60 dBFS: тише любого затухания, которое диджей ещё слышит, но выше
# численного нуля, куда уходит рендер, вышедший за конец файла.
TAIL_SILENCE_DB = -60.0
# Короче этого хвост не режем: полсекунды тишины — это пауза внутри
# музыки, а не обрыв материала.
TAIL_SILENCE_MIN_SEC = 0.5


def _trim_dead_tail(mix: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """Отрезает хвост, в котором материала уже нет.

    Зачем. Окно рендера задаётся планом, а не файлами: «сведи здесь и дай
    ещё сорок секунд». Если входящий трек заведён под конец собственного
    файла, эти сорок секунд взять неоткуда — и рендер честно писал
    ЦИФРОВУЮ ТИШИНУ. Замерено на треке диджея: −240 dBFS на протяжении
    девяти секунд. В превью сета такие хвосты сшиваются встык, и между
    сведениями возникает дыра — ровно то, на что он и пожаловался:
    «между треками образуется пустое пространство».

    Чинить это в планировщике мало: он не может знать, сколько материала
    попросит рендер. Поэтому правило простое и последнее в цепи — кусок
    не может быть длиннее, чем звук, из которого он сделан.

    Возвращает (обрезанный кусок, сколько секунд убрали)."""
    if not len(mix):
        return mix, 0.0
    m = np.abs(mix.mean(axis=1) if mix.ndim > 1 else mix)
    thr = 10.0 ** (TAIL_SILENCE_DB / 20.0)
    # Ищем последний отсчёт, который ещё звучит. Грубой сеткой: точность
    # до блока в 10 мс здесь избыточна, а цикл по миллиону отсчётов — нет.
    step = max(1, sr // 100)
    k = len(m) // step
    if k < 2:
        return mix, 0.0
    blocks = m[:k * step].reshape(k, step).max(axis=1)
    alive = np.nonzero(blocks > thr)[0]
    if not len(alive):
        return mix, 0.0
    end = min(len(mix), (int(alive[-1]) + 1) * step)
    cut = (len(mix) - end) / sr
    if cut < TAIL_SILENCE_MIN_SEC:
        return mix, 0.0
    return mix[:end], cut


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


def _handover_position(vol_env, src_env, margin: float = 0.10,
                       width: float = 0.05) -> "np.ndarray":
    """ОДИН момент, когда низ и барабаны переезжают к входящему.

    При кроссфейде такого вопроса нет: момент — середина хода
    кроссфейдера. При РУЧНОМ ведении кроссфейдера не существует вовсе,
    и это ломало сразу два места:

      * низ считался от cf_env, переопределённого как «сколько
        уходящего ушло». Уходящий по профилю стоит на полном до
        последних out_len — значит почти весь переход эта величина
        равна нулю, и низ входящего не открывался;
      * ворота барабанов («одна ударная установка за раз») считались от
        СЫРЫХ событий crossfade. А `mix_profile.apply` их выбрасывает —
        именно в этом его смысл. Событий нет, огибающая падает на
        умолчание 0.0, ворота входящего не открываются НИ РАЗУ.

    Второе и есть то, что диджей описал:

        остальные ломают входящий трек, где он звучит будто только в
        слое others, без басса драмсов и вокала

    Слой others — это ровно то, что остаётся, когда у входящего закрыты
    барабаны и бас. Замер по всем 62 приёмам каталога: под профилем шли
    20 приёмов, у которых не было ни crossfade, ни eq_low на обеих
    деках, — у всех двадцати барабаны входящего молчали весь переход.
    Уцелели те, где низом управляет сама техника (eq_low: DNB-01,
    DNB-25), резы, которым профиль не навязывается (DNB-02), и приёмы,
    которые ведут слои сами. Диджей назвал рабочими DNB-01, DNB-02 и
    DNB-25 — те же три.

    Где ставить обмен. Не «когда входящий дошёл до полного»: при
    in_len до 0.95 это конец перехода, и низ приезжал бы позже, чем
    уходящий начал уводиться, — провал ровно там, где его слышно.
    Правило простое и одно на всё: **обмен за фразу до того, как
    уходящий тронется**. Тогда старый до последнего идёт со своим
    низом, новый входит уже со своим, и между ними нет дыры.

    Возвращает положение 0..1: 0 — низ и барабаны у уходящего, 1 — у
    входящего."""
    v = np.asarray(vol_env, dtype=float)
    s = np.asarray(src_env, dtype=float)
    m = int(len(v))
    if m == 0:
        return np.zeros(0)

    def _first(cond, default: int) -> int:
        idx = np.flatnonzero(cond)
        return int(idx[0]) if idx.size else default

    leave = _first(s < 0.99, m - 1)          # уходящий тронулся
    half = _first(v >= 0.5, 0)               # входящему есть что отдать
    swap = min(max(half, leave - int(margin * m)), leave)
    w = max(1, int(width * m))
    return np.clip((np.arange(m) - (swap - w / 2.0)) / w, 0.0, 1.0)


def _manual_handover(events, spb, timeline_start, n, sr):
    """Положение обмена для ручного ведения, или None, если ведёт кроссфейд.

    Считается ОДИН раз и отдаётся обоим потребителям — воротам барабанов
    и передаче низа. Раньше они считались порознь и из разных источников,
    хотя в комментарии рядом было написано верное: «барабаны и бас диджей
    всегда переключает вместе»."""
    if any(e["action"] == "crossfade" for e in events):
        return None
    tgt = [e for e in events if e["action"] == "volume_ramp" and e["deck"] == "target"]
    src = [e for e in events if e["action"] == "volume_ramp" and e["deck"] == "source"]
    if not tgt or not src:
        return None
    tv = _envelope_array(_piecewise_ramp(tgt, spb, default=1.0), timeline_start, n, sr)
    sv = _envelope_array(_piecewise_ramp(src, spb, default=1.0), timeline_start, n, sr)
    return _handover_position(tv, sv)


def _bass_handover(cf_env: np.ndarray, lo: float = 0.46, hi: float = 0.54) -> tuple[np.ndarray, np.ndarray]:
    """«Одна бочка за раз» — правило, которое диджей выполняет руками на
    каждом сведении. Два полноспектральных драм-н-бейса, сложенные без
    разведения низа, дают кашу: сабы складываются по случайной фазе,
    получается гудение и потеря панча.

    Ключевое слово — ОДНА. Окно было 0.38..0.62, то есть четверть пути
    кроссфейдера, и ровно в середине обе деки получали по 0.5. Но это не
    «одна бочка», а ДВЕ ПОЛОВИНКИ разных бочек: сигналы разные, по фазе
    не складываются, и вместо баса получается ничто. Замерено на демо,
    которые диджей прислал: с 1.9 до 13.9 секунды энергия ниже 180 Гц
    держится 0.03..0.17 от уровня трека — двенадцать секунд без низа
    подряд. Он назвал это «сводит просто ужасно», и был прав.

    Теперь окно 0.46..0.54 — переключение, а не смешивание: неоднозначная
    зона длится доли секунды вместо трети перехода.

    Возвращает (гейн низа уходящего, гейн низа входящего) как функцию
    положения кроссфейда."""
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
               under_db: float = -9.0, max_boost: float = 6.0,
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

    # ОДНО число на весь переход, а не значение на каждые полсекунды.
    #
    # Здесь была слышная поломка, и диджей описал её точно: «входящий трек
    # начинает со 100%, потом затихает до нужного уровня, и потом плавно
    # поднимается». Причина арифметическая. Гейн считался как отношение
    # want/rb в каждом окне; в первых окнах входящей деки ещё нет звука
    # (она молчит до своего входа), rb там около нуля, отношение улетает в
    # потолок — замерено: 16.0, 11.2, 8.7, 5.3, 1.7 и дальше 1.0. То есть
    # первые полторы секунды новый трек шёл с шестнадцатикратным
    # усилением, а как только у него появлялся собственный материал,
    # усиление схлопывалось. Отсюда и «громко — тихо — поднимается».
    #
    # Диджей ручку trim крутит ОДИН раз на переход, а не каждые полсекунды.
    # Считаем так же: берём окна, где входящая дека реально звучит, и по
    # ним одну медиану. Тишина в расчёт не идёт вовсе — усиливать нечего.
    want = ra * (10.0 ** (under_db / 20.0))
    audible = rb > max(1e-6, float(np.percentile(rb, 90)) * 0.12)
    if not np.any(audible):
        return np.ones(n)
    ratio = float(np.median(want[audible] / np.maximum(rb[audible], 1e-6)))
    boost = np.full(m, float(np.clip(ratio, 1.0, max_boost)))
    # Пока дека молчит, гейн не нужен вовсе: он всё равно ни на что не
    # действует, но при первом же звуке дал бы ступеньку.
    boost[~audible] = 1.0
    # мягкий вход гейна на границе тишина/звук
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


def technique_scale(raw_end: float, budget: float, bars_given: bool) -> float:
    """Во сколько раз растянуть или ужать приём под окно перехода.

    Вынесено из `render_demo` не ради красоты: пока расчёт жил внутри,
    проверка вынужденно повторяла ту же арифметику у себя — и молча
    соглашалась с любой правкой рендера. Теперь и рендер, и кнопка
    «такты», и проверка спрашивают ОДНУ функцию.

    `bars_given` — длина задана явно (со шва, с карточки). Тогда масштаб
    считается ТОЧНО: обещали 32 такта — звучит 32. Раньше и этот случай
    шёл по лесенке степеней двойки, где бралась первая ступень, которая
    «влезает» в бюджет; влезает — не значит совпадает. Замер на 138 BPM:
    из 180 сочетаний «приём × 16/32/64 такта» в заказанную длину
    попадали 10, медианный промах 44%. На карточке 32 такта — звучало
    18, и переход начинался посреди фразы. Диджей: «не сходятся такты и
    доли теперь, ни в одной технике».

    Без заданной длины остаётся лесенка: выбрать круглую ступень
    разумнее, чем тянуть приём под случайное окно."""
    raw_end = max(float(raw_end), 1e-6)
    if bars_given:
        return _clamp(float(budget) / raw_end, MIN_TIME_SCALE, MAX_TIME_SCALE)
    scale = TIME_SCALES[-1]
    for s in TIME_SCALES:
        if raw_end * s <= budget:
            return s
    return scale


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
        # длина лупа — тоже время, и она обязана ужиматься вместе с
        # техникой: иначе у сжатой вдвое техники четырёхдольный луп
        # съедает весь приём. Остальные params (скорость спинбэка, кривая
        # выбега, слип) — не время, их не трогаем.
        pr = e2.get("params")
        if pr and "beats" in pr:
            pr = dict(pr)
            pr["beats"] = pr["beats"] * scale
            e2["params"] = pr
        out.append(e2)
    return out


def _fader_shape(x: float, smooth: float) -> float:
    """Форма хода фейдера: 0 — рывком в начале, 1 — долгий разгон.

    Одна ручка вместо кривой: `smooth` меняет показатель степени от 0.4
    (фейдер сразу набирает и потом доводит) до 2.5 (долго почти ничего,
    потом быстро). 0.5 посередине — почти линейно, как и ведут руками."""
    g = 0.4 * (2.5 / 0.4) ** _clamp(float(smooth), 0.0, 1.0)
    return float(_clamp(x, 0.0, 1.0) ** g)


def _ramp_curve(t0: float, span: float, action: str, deck: str,
                v0: float, v1: float, smooth: float, steps: int = 8) -> list[dict]:
    """Ход фейдера заданной формы — ломаной из нескольких отрезков.

    `_ramp` умеет только прямую между двумя значениями, а форма — это как
    раз то, что отличает «завёл и повёл» от «выкрутил и ждёшь»."""
    out = []
    span = max(0.5, float(span))
    for i in range(steps):
        a, b = i / steps, (i + 1) / steps
        out.append(_mk_ramp(t0 + span * a, action, deck, span / steps,
                            v0 + (v1 - v0) * _fader_shape(a, smooth),
                            v0 + (v1 - v0) * _fader_shape(b, smooth)))
    return out


def _apply_entry_profile(events: list[dict], profile: dict | None) -> list[dict]:
    """Профиль ввода. Сам расчёт живёт в `mix_profile` — там же, откуда
    его берут живое исполнение и сборка сета.

    Раньше он был только здесь, и это и была причина расхождения: демо
    одиночного шва звучало с ручными фейдерами, а тот же шов в пуле, в
    сете и на деках — без них."""
    import mix_profile

    return mix_profile.apply(events, profile)


def _loop_layer(buf: "np.ndarray | None", start_sample: int, loop_n: int,
                until_sample: int) -> "np.ndarray | None":
    """Зациклить кусок слоя, начиная с start_sample, до until_sample.

    Нужно для вокала уходящего трека. При длинном наложении фраза
    старого трека обрывается на полуслове — на пульте это лечат лупом:
    голос допевает свою фразу столько, сколько нужно, и уходит на
    границе, а не посреди строки."""
    if buf is None or loop_n <= 0:
        return buf
    n = len(buf)
    s = max(0, min(int(start_sample), n))
    e = max(s, min(int(until_sample), n))
    if e - s <= loop_n or s + loop_n > n:
        return buf
    src = buf[s:s + loop_n]
    out = buf.copy()
    # Короткий кросс на стыке повтора: луп на границе такта почти
    # никогда не бесшовен по фазе, и без склейки слышен щелчок.
    fade = max(1, min(loop_n // 16, int(0.008 * 44100)))
    ramp_in = np.linspace(0.0, 1.0, fade)
    pos = s + loop_n
    while pos < e:
        take = min(loop_n, e - pos)
        chunk = src[:take].copy()
        if take > fade:
            chunk[:fade] = chunk[:fade] * _env(ramp_in, chunk[:fade])
            prev = out[pos - fade:pos]
            out[pos - fade:pos] = prev * _env(1.0 - ramp_in, prev)
        out[pos:pos + take] = out[pos:pos + take] * 0.0 + chunk
        pos += take
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
    if paths and part in paths:
        import librosa

        try:
            y, _ = librosa.load(paths[part], sr=sr, mono=not STEREO,
                                offset=max(0.0, offset), duration=max(0.1, duration))
        except Exception:
            return None
    else:
        # Кэша слоёв нет - читаем прямо из .stem.mp4. Он лежит рядом с
        # треком, в нём те же четыре слоя, и второй копии на диске не
        # нужно. Позиция при перемотке точна (проверено: сдвиг 0 отсчётов
        # на всех позициях), см. stem_mp4.decode_layer.
        sp = _stems.stem_mp4_for(track_path)
        if sp is None:
            return None
        try:
            import stem_mp4 as _sm

            y = _sm.decode_layer(sp, part, sr=sr, offset=max(0.0, offset),
                                 duration=max(0.1, duration), mono=not STEREO)
        except Exception:
            return None
        if y is None:
            return None
    y = _as2d(y)
    if len(y) < sr // 10:
        return None
    return _stretch(y, sr, rate)


STEM_PARTS = ("drums", "bass", "other", "vocals")


def _load_stems(track_path: str, sr: int, offset: float, duration: float,
                rate: float) -> dict | None:
    """Все четыре слоя трека одним куском, выровненные как основной буфер.

    Либо все четыре, либо ничего: техника, которая держит бас одного трека
    и барабаны другого, на трёх слоях из четырёх не соберётся, а
    «соберётся как получится» — это тихая потеря куска музыки, которую
    потом не найти на слух."""
    out = {}
    for part in STEM_PARTS:
        buf = _load_stem_slice(track_path, part, sr, offset, duration, rate)
        if buf is None:
            return None
        out[part] = buf
    return out


def _combo(stems: dict | None, name: str) -> np.ndarray | None:
    """Сумма слоёв по имени комбинации (см. stems.COMBOS) или один слой."""
    if not stems:
        return None
    if name in stems:
        return stems[name]
    try:
        import stems as _stems
        parts = _stems.COMBOS.get(name)
    except Exception:
        parts = None
    if not parts:
        return None
    acc = None
    for part in parts:
        if part not in stems:
            return None
        acc = stems[part].copy() if acc is None else acc + stems[part]
    return acc


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


# Ручки, которые юниты действительно читают (см. пункты 5b и очередь
# посылов ниже). Список один на всю программу: fx_presets описывает те же
# ключи как ручки интерфейса, а здесь их накладывают на события.
FX_PARAM_KEYS = ("delay_beats", "feedback", "decay_beats", "rate_beats",
                 "depth_ms", "stages", "drive", "bits", "downsample")


def _tune_own_fx(events: list[dict], tune: dict | None) -> list[dict]:
    """Ручки диджея поверх эффекта, который ведёт САМ приём.

    Двадцать два приёма трогают FX самостоятельно, и до сих пор это
    означало «как задумано, и никак иначе». Пятнадцать из них к тому же
    юнит не называют (fx_meta/fx_mix), а рендер подставлял им эхо — то
    есть «уход в реверб» на DNB-25 был невозможен физически.

    Здесь диджей получает над ними те же три вещи, что и над своим
    эффектом: чем играть (только там, где приём юнит не назвал — иначе
    это был бы уже не тот приём), насколько глубоко и с какими
    параметрами юнита.

    Накладывается ПОСЛЕ профиля и ДО расчёта: события приёма к этому
    моменту уже в долях готового окна."""
    if not tune:
        return events
    out = []
    for e in events:
        act = e.get("action")
        opts = tune.get(e.get("deck")) or {}
        if act not in ("fx", "stem_fx", "fx_meta", "fx_mix") or not opts:
            out.append(e)
            continue
        ev = {**e, "params": dict(e.get("params") or {})}
        unit = opts.get("unit")
        # Юнит подменяем только у «безымянной ручки». У FX-01 фленджер —
        # это и есть приём, и заменить его вобблом значит играть другой.
        if unit and act in ("fx_meta", "fx_mix"):
            ev["params"]["unit"] = str(unit)
        try:
            k = max(0.0, min(1.0, float(opts.get("strength", 100.0)) / 100.0))
        except (TypeError, ValueError):
            k = 1.0
        for field in ("value_from", "value_to"):
            if ev.get(field) is not None:
                ev[field] = float(ev[field]) * k
        for key, val in opts.items():
            if key not in FX_PARAM_KEYS:
                continue
            try:
                ev["params"][key] = float(val)
            except (TypeError, ValueError):
                continue
        out.append(ev)
    return out


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
    bars: float | None = None,
    mix_profile: dict | None = None,
    vocal_loop: float | None = None,
    extra_events: list[dict] | None = None,
    fx_tune: dict | None = None,
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
        # Раньше здесь стоял глухой отказ: «требует живых стемов». Это было
        # верно, пока стемов не было вовсе. Теперь они считаются офлайн и
        # лежат в кэше, поэтому вопрос не «поддержано ли», а «посчитаны ли
        # они для ЭТИХ двух треков» — и ответ должен называть трек, из-за
        # которого не вышло, а не отсылать к README.
        try:
            import stems as _stems_check
            missing = [os.path.basename(t) for t in (track_a_path, track_b_path)
                       if not _stems_check.has_stems(t)]
        except Exception as exc:
            raise NotImplementedError(f"«{technique.name}» работает по слоям, "
                                      f"но модуль стемов недоступен: {exc}") from exc
        if missing:
            raise NotImplementedError(
                f"«{technique.name}» работает по слоям, а стемы не посчитаны для: "
                + ", ".join(missing)
                + ". Посчитать: python stems.py --dir <папка с музыкой>")
        # Голос — отдельный разговор: у быстрого разделения (HPSS) слоя
        # вокала нет вовсе, он пустой по построению. Техника с акапеллой
        # на таких стемах выдаст тишину там, где должен петь голос, и это
        # будет выглядеть поломкой рендера. Лучше сказать прямо.
        # Признак берём из описания техники, а не угадываем по событиям:
        # обмен барабанами тоже ДВИГАЕТ вокальный слой, но услышать его не
        # рассчитывает — на пустом слое приём звучит ровно так же.
        if getattr(technique, "needs_vocals", False):
            no_voice = [os.path.basename(t) for t in (track_a_path, track_b_path)
                        if not _stems_check.has_vocals(t)]
            if no_voice:
                raise NotImplementedError(
                    f"«{technique.name}» работает с голосом, а у этих треков стемы "
                    f"посчитаны быстрым способом (HPSS), где вокального слоя нет: "
                    + ", ".join(no_voice)
                    + ". Пересчитать моделью: python stems.py --dir <папка> --backend roformer")
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

    _overlay_mode = str((mix_profile or {}).get("entry") or "technique") != "technique"
    plan = build_plan(technique_id, "demo", "source", "target", timeline_bpm, param_overrides)
    spb = _seconds_per_beat(timeline_bpm)
    bar_sec = spb * BAR_BEATS
    lead = LEAD_BARS * bar_sec
    tail = TAIL_BARS * bar_sec

    # Длина перехода приходит со шва: на карточке написано «32 такта», и
    # звучать должно ровно столько. Раньше сюда не передавалось ничего, и
    # демо всегда шло 38 секунд независимо от того, что показано.
    if bars and float(bars) > 0:
        max_seconds = lead + tail + float(bars) * bar_sec

    # --- масштаб техники ---
    #
    # Когда длина ЗАДАНА («32 такта» на карточке), масштаб считается
    # точно: приём обязан занять ровно столько, сколько обещано.
    #
    # Раньше и этот случай шёл по лесенке степеней двойки
    # (4, 2, 1, 1/2, 1/4, 1/8): бралась первая ступень, которая «влезает»
    # в бюджет. Влезает — не значит совпадает. Замер по каталогу на
    # 138 BPM: из 180 сочетаний «приём × 16/32/64 такта» в заказанную
    # длину попадали 10, медианный промах 44%. То есть на карточке
    # написано 32 такта, а звучало 18 — и переход начинался и кончался
    # посреди фразы. Диджей услышал это сразу:
    #
    #     не сходятся такты и доли теперь, ни в одной технике
    #
    # Лесенка остаётся там, где длину НИКТО не задал: выбирать круглую
    # ступень разумнее, чем тянуть приём под случайное окно.
    raw_end = max([4.0 * spb] + [_event_window(e, spb)[1] for e in plan["events"]])
    budget = max(2.0 * bar_sec, float(max_seconds) - lead - tail)
    scale = technique_scale(raw_end, budget, bool(bars and float(bars) > 0))
    events = _scale_events(plan["events"], scale)
    # Профиль ввода применяется ПОСЛЕ масштабирования: он размечает доли
    # уже готового окна перехода, а не «родной» длины приёма.
    events = _apply_entry_profile(events, mix_profile)
    # Ручные добавки к приёму: луп слоя, эхо на слое, реверс — то, что
    # диджей вешает поверх готового перехода в тонкой настройке. Идут
    # ПОСЛЕ масштабирования: их смещения заданы в тактах готового окна,
    # а не в «родной» длине приёма, иначе они уехали бы вместе с ним.
    if extra_events:
        events = sorted(list(events) + [dict(e) for e in extra_events],
                        key=lambda e: float(e.get("beat_offset") or 0.0))
    # Ручки поверх СОБСТВЕННОГО эффекта приёма — после того, как события
    # собраны целиком: добавки диджея тоже могут нести fx.
    events = _tune_own_fx(events, fx_tune)

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

    # --- сколько материала приём проматывает НАЗАД ---
    # Бэкспин на 9 крат за одну долю уводит иглу на 3.6 доли назад,
    # ревайнд на 6 крат за такт — почти на десять. Если столько материала
    # ДО точки сведения в буфер не загружено, позиция уходит в минус и
    # рендер честно отдаёт ноль — приём превращается в дырку тишины ровно
    # там, где должен быть самый слышный его кусок. Позицию можно
    # посчитать заранее: она зависит только от событий, не от звука.
    src_pos_preview = _transport.build_position(events, "source", n, sr, spb, timeline_start)
    src_deficit = max(0.0, -float(np.min(src_pos_preview["pos"])))
    # добираем целыми тактами: _load_aligned всё равно садится на даунбит,
    # а сдвиг на целое число тактов сетку не ломает
    pre_bars = float(np.ceil(src_deficit / sr / bar_sec)) if src_deficit > 0 else 0.0
    pre_seconds = pre_bars * bar_sec
    pre_n = int(round(pre_seconds * sr))

    # --- source: где план велел уводить трек, но ровно с даунбита ---
    try:
        src_full_dur = float(librosa.get_duration(path=track_a_path))
    except Exception:
        src_full_dur = total_duration
    # сколько СЫРЫХ секунд трека нужно, чтобы после растяжения получилось
    # total_duration секунд в темпе таймлайна
    src_need_raw = (total_duration + pre_seconds) * rate_a
    if source_at is not None and source_at > 0:
        # точка из плана — это момент, где начинается СВЕДЕНИЕ, а таймлайн
        # стартует на такт раньше (lead) плюс запас под промотку назад
        src_want = max(0.0, float(source_at) - (lead + pre_seconds) * rate_a)
    else:
        src_want = max(0.0, src_full_dur - src_need_raw)
    src_want = min(src_want, max(0.0, src_full_dur - src_need_raw))
    source_full, src_grid = _load_aligned(track_a_path, sr, bpm_a, src_want, src_need_raw, from_start=False,
                                          duration_hint=src_full_dur)
    source_full = _stretch(source_full, sr, rate_a)
    source_full = _fit_length(source_full, pre_n + n)
    # То, что реально звучит на таймлайне, — это буфер БЕЗ запаса; запас
    # существует только для того, чтобы приёму было куда отматывать.
    source = source_full[pre_n:]
    src_stems_full = _load_stems(track_a_path, sr, src_grid.get("file_offset") or 0.0,
                                 src_need_raw, rate_a)
    if src_stems_full is not None:
        src_stems_full = {k: _fit_length(v, pre_n + n) for k, v in src_stems_full.items()}
    src_stems = None if src_stems_full is None else {k: v[pre_n:] for k, v in src_stems_full.items()}
    # секунда исходника, которой соответствует НАЧАЛО таймлайна
    src_origin = None if src_grid.get("file_offset") is None else \
        round(src_grid["file_offset"] + pre_seconds * rate_a, 3)

    # --- луп вокала уходящего --------------------------------------------
    # Прямая просьба диджея: «если на уходящем вокал — обязательно надо
    # его лупать». Смысл ровно тот, что на пульте: при длинном наложении
    # фраза старого трека обрывается на полуслове, и это слышно как
    # обрыв, а не как приём. Лупим ВОКАЛЬНЫЙ СЛОЙ, а не всю деку:
    # барабаны и бас должны продолжать своё, иначе получится не
    # наложение, а заикание всего трека.
    vocal_loop_bars = 0.0
    if src_stems and vocal_loop and float(vocal_loop) > 0:
        vocal_loop_bars = float(vocal_loop)
        loop_n = int(round(vocal_loop_bars * bar_sec * sr))
        start_i = _clamp(_sample_idx(0.0, timeline_start, sr), 0, n)
        src_stems = dict(src_stems)
        src_stems["vocals"] = _loop_layer(src_stems.get("vocals"), start_i, loop_n, n)

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
    if not _rel["compatible"]:
        # Чужой темп у ВХОДЯЩЕЙ деки не тянем к мастеру — по той же причине,
        # по которой не тянем у уходящей. 174 в сете на 140 — это не
        # «недотянутый» трек, это другой темп: растянуть его на 24% значит
        # проиграть драм-н-бейс на скорости хауса. Питч-фейдер такого не
        # делает (у CDJ предел ±16%), и живой диджей в этом месте не тянет
        # темп, а рвёт: эхо-хвост, тейп-стоп, спинбэк — новый трек входит
        # со своей скоростью. Ровно эти техники здесь и выбирает
        # mix_strategist (см. ветку mismatch в technique_candidates).
        rate = 1.0
    else:
        _effective_b = _rel["effective_bpm"]
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
    _src_ref = _track_reference_rms(track_a_path)
    source_full = _match_loudness(source_full, _src_ref)
    source = source_full[pre_n:]
    target_raw = _match_loudness(target_raw, _track_reference_rms(track_b_path))

    tgt_stems_raw = _load_stems(track_b_path, sr, tgt_grid.get("file_offset") or 0.0,
                                need_raw, rate)

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

    tgt_stems = (None if tgt_stems_raw is None
                 else {k: _place(v) for k, v in tgt_stems_raw.items()})

    stems_ready = src_stems is not None and tgt_stems is not None

    decks = {"source": source, "target": target}

    # Очередь посылов на эффекты. Сами возвраты считаются в самом конце,
    # когда известны огибающие фейдеров: посыл КОРМИТСЯ пост-фейдерным
    # сигналом (см. комментарий у FX_SENDS). Каждая запись — «с какого
    # сигнала снимаем, на какой деке, каким юнитом и с какой глубиной».
    fx_send_queue: list[dict] = []

    # signal=None означает «весь сигнал деки», и он берётся в момент
    # расчёта, а не постановки в очередь: после этого блока деку ещё
    # обрабатывают свипы и фильтры, и посыл обязан снимать их тоже.
    def _queue_send(deck_name: str, unit: str, signal, env, **kw) -> None:
        fx_send_queue.append({"deck": deck_name, "unit": unit,
                              "signal": signal, "env": env, "params": kw})

    # --- сколько низа РЕАЛЬНО даёт уходящий -------------------------------
    #
    # Правило «одна бочка за раз» держится на молчаливом допущении, что у
    # уходящего эта бочка есть. Уводят же чаще всего в брейкдаун — там
    # низа нет по построению. Тогда закрытый низ входящего означает, что
    # низа нет НИ У КОГО: замерено на присланном демо, с 2-й по 13-ю
    # секунду энергия ниже 180 Гц держится 0.01 от уровня трека.
    # Двенадцать секунд без баса — это и есть «сводит просто ужасно».
    #
    # bass_relief — обратная величина: 1 там, где уходящий низа не даёт.
    # На неё поднимается низ входящего, чем бы он ни был закрыт: если
    # второй бочки нет, первая не спорит ни с чем и может звучать сразу.
    # Меряем ДОЛЮ низа в энергии уходящего, а не его абсолютный уровень:
    # доля не зависит ни от мастеринга трека, ни от длины окна. Замерено
    # на реальном шве: в теле трека доля 0.95, в брейкдауне 0.21.
    _src_mono = _mono(decks["source"])
    _src_low, _ = _split_low(_src_mono, sr)
    _low_win = max(1, int(0.25 * sr))
    _low_k = max(1, min(len(_src_low), len(_src_mono)) // _low_win)
    _cut = _low_k * _low_win
    _low_rms = np.sqrt((_src_low[:_cut].reshape(_low_k, _low_win) ** 2).mean(axis=1) + 1e-12)
    _all_rms = np.sqrt((_src_mono[:_cut].reshape(_low_k, _low_win) ** 2).mean(axis=1) + 1e-12)
    _share = _low_rms / np.maximum(_all_rms, 1e-9)
    _present = _smoothstep(_share / BASS_PRESENT_FRAC)
    _relief = _smooth_curve(1.0 - _present, max(1, int(round(bar_sec / 0.25))))
    bass_relief = np.clip(np.interp(np.arange(n),
                                    np.linspace(0, n - 1, len(_relief)), _relief),
                          0.0, 1.0)

    # --- 1. транспорт: где в эти секунды находится игла ---
    # Бэкспин, тейп-стоп, реверс, лупы и роллы — это не обработка звука, а
    # движение ПОЗИЦИИ воспроизведения. Раньше здесь кусок буфера просто
    # переворачивался задом наперёд: реверс на полной скорости, без
    # падения темпа и высоты, то есть без того единственного, по чему
    # вертушка и узнаётся на слух. Теперь позицию считает transport.py, а
    # звук читается по ней с band-limit под скорость чтения.
    transport_info: dict[str, dict] = {}
    # Уходящая дека читается из буфера С ЗАПАСОМ (source_full), поэтому её
    # позиция сдвинута на pre_n: отсчёт pre_n этого буфера — это начало
    # таймлайна. Входящей запас не нужен: до своего входа она молчит, и
    # отматывать ей некуда и незачем.
    src_tr = src_pos_preview
    if src_tr["moved"]:
        decks["source"] = _transport.read(source_full, src_tr["pos"] + pre_n, src_tr["gain"], sr)
        transport_info["source"] = src_tr
        if src_stems_full is not None:
            src_stems = {k: _transport.read(v, src_tr["pos"] + pre_n, src_tr["gain"], sr)
                         for k, v in src_stems_full.items()}
    tgt_tr = _transport.build_position(events, "target", n, sr, spb, timeline_start)
    if tgt_tr["moved"]:
        decks["target"] = _transport.read(decks["target"], tgt_tr["pos"], tgt_tr["gain"], sr)
        transport_info["target"] = tgt_tr
        if tgt_stems is not None:
            tgt_stems = {k: _transport.read(v, tgt_tr["pos"], tgt_tr["gain"], sr)
                         for k, v in tgt_stems.items()}

    # Момент обмена низом и барабанами. Считается здесь, потому что
    # первым его спрашивает сборка из слоёв (ниже), а вторым — передача
    # низа в финальном миксе. Величина одна на обоих.
    manual_hand = _manual_handover(events, spb, timeline_start, n, sr)

    # --- 1c. дека собирается ИЗ СЛОЁВ, и делается это здесь ---
    #
    # Порядок не случайный. Раньше сборка по стемам стояла в самом конце,
    # ПОСЛЕ фильтров, эха и EQ — и просто затирала их результат: свип
    # фильтра, посчитанный на полном миксе, до выхода не доживал. На живом
    # пульте всё наоборот: стем-фейдеры сидят ДО канала, а эквалайзер,
    # фильтр и посыл на эффект стоят после и работают уже с тем, что из
    # стемов собралось. Теперь так же и здесь.
    #
    # Управление бывает двух видов:
    #   * техника явно двигает слои (действие stem_gain) — тогда слушаем её;
    #   * техника про слои ничего не знает — тогда включается правило
    #     «одна ударная установка за раз»: барабаны звучат только у одной
    #     деки, потому что две плотные партии складывать нечем. Момент
    #     обмена берём тот же, что у баса: барабаны и бас диджей всегда
    #     переключает вместе.
    stem_swap = None
    stem_events = [e for e in events if e["action"] == "stem_gain"]
    stem_bufs = {"source": src_stems, "target": tgt_stems}
    if stems_ready:
        explicit = {e["deck"] for e in stem_events}
        auto_gate = {}
        if explicit != {"source", "target"}:
            eq_src = [e for e in events if e["action"] == "eq_low" and e["deck"] == "source"]
            eq_tgt = [e for e in events if e["action"] == "eq_low" and e["deck"] == "target"]
            neutral = CONTROL_NEUTRAL["eq_low"] or 1.0
            if eq_src and eq_tgt:
                ga = _envelope_array(_piecewise_ramp(eq_src, spb, default=neutral),
                                     timeline_start, n, sr,
                                     step_sec=_events_step_sec(eq_src, spb)) / neutral
                gb = _envelope_array(_piecewise_ramp(eq_tgt, spb, default=0.0),
                                     timeline_start, n, sr,
                                     step_sec=_events_step_sec(eq_tgt, spb)) / neutral
            elif manual_hand is not None:
                # Ручное ведение: кроссфейда нет, и брать его события
                # бессмысленно — профиль их выбросил. Момент тот же, что
                # у низа (см. _handover_position): барабаны и бас
                # переключаются вместе, одним движением.
                gb = _smoothstep(manual_hand)
                ga = 1.0 - gb
            else:
                cf_raw_ev = [e for e in events if e["action"] == "crossfade"]
                cf_raw = _envelope_array(_piecewise_ramp(cf_raw_ev, spb, default=0.0),
                                         timeline_start, n, sr,
                                         step_sec=_events_step_sec(cf_raw_ev, spb))
                # Та же ошибка, что была у баса: окно 0.40..0.60 давало
                # ровно в середине по 0.5 каждой деке, то есть ДВЕ
                # ПОЛОВИНКИ разных ударных вместо одной установки. В
                # присланных демо верх (тарелки, хэты) держался 0.02..0.10
                # почти весь переход — барабанов не было слышно ни у кого.
                gb = _smoothstep((cf_raw - 0.46) / 0.08)
                ga = 1.0 - gb
            ga = np.clip(ga, 0.0, 1.0)
            gb = np.clip(gb, 0.0, 1.0)
            # страховка: сумма гейтов не больше единицы — две установки
            # вместе не зазвучат, даже если техника задала перехлёст
            over = np.maximum(ga + gb, 1.0)
            auto_gate = {"source": ga / over, "target": gb / over}
            stem_swap = (float(np.argmax(gb > 0.5)) / sr if np.any(gb > 0.5) else None)

        # Обработка ОТДЕЛЬНОГО слоя. Всё, что здесь есть, невозможно
        # сделать на канале целиком, и ради этого слои и разделяют:
        #   * stem_eq   — срез полосы у одного слоя. Нужен, когда две
        #     бочки складываются: они гасят друг друга по фазе и вместе
        #     звучат ТИШЕ и пустее, чем порознь. Лечится тем, что у
        #     одной срезают низ и оставляют щелчок;
        #   * stem_phase — инверсия фазы слоя, второе лекарство от того
        #     же: иногда достаточно перевернуть, а не резать;
        #   * stem_pitch — сдвиг одного слоя на полутона. Мелодия,
        #     поднятая на октаву, перестаёт спорить с чужим басом и
        #     превращается в атмосферный верх;
        #   * stem_fx   — посыл на эффект С ОДНОГО СЛОЯ. Вокал уходит в
        #     эхо, а барабаны продолжают бить сухими — на полном миксе
        #     так не получится в принципе.
        stem_fx_events = [e for e in events if e["action"] == "stem_fx"]

        def _layer_events(action, deck_name, part):
            return [e for e in events
                    if e["action"] == action and e["deck"] == deck_name
                    and (e.get("params") or {}).get("stem") == part]

        def _stem_eq_apply(layer, evs):
            """Полосовые ручки на одном слое. gain 1.0 — полоса на месте."""
            for band, (kind, f1, f2) in (("low", ("low", 200.0, None)),
                                         ("mid", ("band", 300.0, 3000.0)),
                                         ("high", ("high", 3000.0, None))):
                band_ev = [e for e in evs if (e.get("params") or {}).get("band") == band]
                if not band_ev:
                    continue
                env = _envelope_array(_piecewise_ramp(band_ev, spb, default=1.0),
                                      timeline_start, n, sr,
                                      step_sec=_events_step_sec(band_ev, spb))
                if np.allclose(env, 1.0):
                    continue
                if kind == "low":
                    sos = butter(2, min(f1 / (sr / 2), 0.99), btype="lowpass", output="sos")
                elif kind == "high":
                    sos = butter(2, min(f1 / (sr / 2), 0.99), btype="highpass", output="sos")
                else:
                    sos = butter(2, [min(f1 / (sr / 2), 0.98), min(f2 / (sr / 2), 0.99)],
                                 btype="bandpass", output="sos")
                bandbuf = sosfiltfilt(sos, layer, axis=0)
                layer = layer - bandbuf + bandbuf * _env(np.clip(env, 0.0, 4.0), layer)
            return layer

        drum_layers: dict[str, np.ndarray] = {}

        for deck_name in ("source", "target"):
            parts = stem_bufs[deck_name]
            mine = [e for e in stem_events if e["deck"] == deck_name]
            acc = None
            for part, buf in parts.items():
                if mine:
                    ev = [e for e in mine if (e.get("params") or {}).get("stem") == part]
                    gain = (_envelope_array(_piecewise_ramp(ev, spb, default=1.0),
                                            timeline_start, n, sr,
                                            step_sec=_events_step_sec(ev, spb))
                            if ev else np.ones(n))
                elif part == "drums" and deck_name in auto_gate:
                    gain = auto_gate[deck_name]
                else:
                    gain = None
                # Низ входящего закрывают, чтобы он не спорил с низом
                # уходящего. Спорить не с чем, когда уходящий в
                # брейкдауне, — тогда бас входящего это не второй бас, а
                # единственный, и держать его в нуле незачем (bass_relief
                # выше; там же замеры, из-за которых это появилось).
                if deck_name == "target" and part == "bass":
                    gain = bass_relief if gain is None else np.maximum(
                        np.clip(gain, 0.0, 1.0), bass_relief)
                layer = buf if gain is None else buf * _env(np.clip(gain, 0.0, 1.0), buf)

                # Луп слоя — раньше всего остального: дальше по цепочке
                # идут фейдер, EQ и эффекты, и они должны работать уже с
                # зацикленным материалом, а не наоборот.
                for e in _layer_events("stem_loop", deck_name, part):
                    beats = float((e.get("params") or {}).get("beats") or 0.0)
                    if beats <= 0:
                        continue
                    start, end = _event_window(e, spb)
                    layer = _loop_layer(
                        layer,
                        _clamp(_sample_idx(start, timeline_start, sr), 0, n),
                        int(round(beats * spb * sr)),
                        _clamp(_sample_idx(end, timeline_start, sr), 0, n))

                # фаза — до всего остального: она меняет знак, а не тембр
                for e in _layer_events("stem_phase", deck_name, part):
                    if float(e.get("value_to", 1.0)) > 0.5:
                        st = _clamp(_sample_idx(e["beat_offset"] * spb, timeline_start, sr), 0, n)
                        layer = layer.copy()
                        layer[st:] = -layer[st:]

                eq_ev = _layer_events("stem_eq", deck_name, part)
                if eq_ev:
                    layer = _stem_eq_apply(layer.copy(), eq_ev)

                for e in _layer_events("stem_pitch", deck_name, part):
                    semis = float(e.get("value_to", 0.0))
                    if abs(semis) < 0.05:
                        continue
                    start, end = _event_window(e, spb)
                    a = _clamp(_sample_idx(start, timeline_start, sr), 0, n)
                    b = _clamp(_sample_idx(end, timeline_start, sr), 0, n)
                    if b - a < sr * 0.3:
                        b = n
                    try:
                        seg = layer[a:b]
                        if seg.ndim > 1:
                            sh = np.stack([librosa.effects.pitch_shift(
                                np.ascontiguousarray(seg[:, c]), sr=sr, n_steps=semis)
                                for c in range(seg.shape[1])], axis=1)
                        else:
                            sh = librosa.effects.pitch_shift(
                                np.ascontiguousarray(seg), sr=sr, n_steps=semis)
                        layer = layer.copy()
                        layer[a:b] = _fit_length(sh, b - a)
                    except Exception:
                        pass

                for e_unit in ("echo", "reverb"):
                    evs = [e for e in stem_fx_events
                           if e["deck"] == deck_name
                           and (e.get("params") or {}).get("stem") == part
                           and ((e.get("params") or {}).get("unit") or "echo") == e_unit]
                    if not evs:
                        continue
                    env = _envelope_array(_piecewise_ramp(evs, spb, default=0.0),
                                          timeline_start, n, sr,
                                          step_sec=_events_step_sec(evs, spb))
                    if np.max(env) <= 0.02:
                        continue

                    def _pp(key, default):
                        for e in evs:
                            v = (e.get("params") or {}).get(key)
                            if v is not None:
                                try:
                                    return float(v)
                                except (TypeError, ValueError):
                                    pass
                        return default

                    # Слой кладём в очередь как есть: домножать его на
                    # фейдер здесь нельзя — эхо нелинейно по времени, и
                    # хвост должен родиться из того, что звучало ДО увода.
                    if e_unit == "echo":
                        _queue_send(deck_name, "echo", layer, env,
                                    delay_sec=_pp("delay_beats", 0.75) * spb,
                                    feedback=min(0.9, max(0.05,
                                                          _pp("feedback", 45.0) / 100.0)))
                    else:
                        _queue_send(deck_name, "reverb", layer, env,
                                    decay_sec=_pp("decay_beats", 4.0) * spb)

                if part == "drums":
                    drum_layers[deck_name] = layer
                acc = layer.copy() if acc is None else acc + layer
            if acc is not None:
                decks[deck_name] = acc

        # --- сайдчейн: чужая бочка «продавливает» эту деку ---
        #
        # Ради чего. В дабл-дропе барабаны берут у одного трека, а
        # мелодию и бас у другого — и они звучат как две отдельные
        # записи, потому что ничто их не связывает ритмически. В студии
        # это лечат компрессором с боковой цепью: мелодия приседает на
        # каждый удар бочки. Тот же приём здесь: огибающая считается по
        # слою барабанов ДРУГОЙ деки, поэтому «насос» попадает точно в
        # чужую бочку, а не в сетку.
        for e in [x for x in events if x["action"] == "sidechain"]:
            deck_name = e["deck"]
            src_deck = (e.get("params") or {}).get("from_deck") or (
                "target" if deck_name == "source" else "source")
            drums = drum_layers.get(src_deck)
            buf = decks.get(deck_name)
            if drums is None or buf is None:
                continue
            depth = _clamp(float(e.get("value_to", 0.6)), 0.0, 1.0)
            start, end = _event_window(e, spb)
            a = _clamp(_sample_idx(start, timeline_start, sr), 0, n)
            b = n if end <= start else _clamp(_sample_idx(end, timeline_start, sr), 0, n)
            if b <= a:
                continue
            # быстрый детектор по низу: бочка, а не весь барабанный слой
            sos = butter(2, min(140.0 / (sr / 2), 0.99), btype="lowpass", output="sos")
            low = np.abs(sosfilt(sos, _mono(drums)))
            # атака мгновенная, отпускание ~120 мс — как у обычного
            # компрессора с боковой цепью
            rel = int(0.12 * sr)
            follow = np.copy(low)
            if rel > 1:
                k = np.exp(-1.0 / rel)
                for i in range(1, len(follow)):
                    if follow[i] < follow[i - 1] * k:
                        follow[i] = follow[i - 1] * k
            peak = float(np.max(follow[a:b])) if b > a else 0.0
            if peak <= 1e-6:
                continue
            duck = 1.0 - depth * np.clip(follow / peak, 0.0, 1.0)
            gate = np.ones(n)
            gate[a:b] = duck[a:b]
            decks[deck_name] = buf * _env(gate, buf)

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
            if buf is None:
                continue
            if not ev:
                # Низ входящего может быть закрыт не техникой, а правилом
                # «одна бочка за раз» ниже — там своя поправка.
                continue
            # Значения нормализованы 0..1, где «штатный уровень» ручки — это
            # её нейтральное значение (0.25). Переводим в
            # множитель громкости полосы: neutral -> 1.0, ноль -> 0.0.
            neutral = CONTROL_NEUTRAL[action] or 1.0
            gain_fn = _piecewise_ramp(ev, spb, default=neutral)
            gain_env = _envelope_array(gain_fn, timeline_start, n, sr,
                                       step_sec=_events_step_sec(ev, spb)) / neutral
            if action == "eq_low" and deck == "target":
                # см. bass_relief выше: закрывать низ входящего имеет
                # смысл только против низа уходящего. Нет его — открываем.
                gain_env = np.maximum(gain_env, bass_relief)
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

    # --- 4. лупы: см. пункт 1, их считает transport.py ---
    # Раньше здесь кусок копировался поверх себя же с микрофейдом. Это
    # давало похожий звук, но не давало ни слипа (loop roll, censor: после
    # отпускания трек продолжается там, где шёл бы), ни возможности
    # сочетать луп с торможением. Теперь и то и другое — одно движение
    # иглы, и считается в одном месте.

    # --- 5. fx_meta/fx_mix — «ручка FX» приёма ---
    #
    # Эти события юнит НЕ называют. На живом пульте так и правильно:
    # диджей заранее выбрал юнит рукой, а приём говорит только «открой
    # ручку вот на столько». А здесь на это место жёстко подставлялось
    # эхо — пятнадцать приёмов (DNB-05, DNB-07, DNB-25, HS-01, ST-01,
    # ST-08, TT-07 и прочие) не могли уйти ни во что другое, и нигде об
    # этом не говорилось.
    #
    # Теперь юнит приезжает в params (его ставит панель эффектов), а эхо
    # осталось умолчанием — тем самым, что звучало раньше. Само событие
    # превращается в обычное "fx" и дальше идёт общим путём (пункт 5b):
    # и вставки, и посылы, и ручки юнита работают для него так же, как
    # для эффекта, выбранного руками. Двух разных дорог для одного и
    # того же больше нет.
    fx_from_knob: list[dict] = []
    for deck in ("source", "target"):
        # fx_mix (dry/wet юнита) и fx_meta (параметр эффекта) в реальном
        # Mixxx крутятся вместе — берём их одной огибающей, как и раньше.
        fx_events = [
            e for e in events
            if e["action"] in ("fx_meta", "fx_mix") and e["deck"] == deck
        ]
        if not fx_events:
            continue
        unit = "echo"
        for e in fx_events:
            u = (e.get("params") or {}).get("unit")
            if u:
                unit = str(u)
                break
        for e in fx_events:
            fx_from_knob.append({**e, "action": "fx",
                                 "params": {**(e.get("params") or {}), "unit": unit}})

    # --- 5b. остальные эффекты: action "fx" с именем в params ---
    # Порядок вставок фиксирован и не случаен: сначала то, что меняет
    # спектр (вобл, фильтр), потом гребёнки (фленджер, фейзер), последним
    # то, что ломает форму волны (перегруз, биткрашер). Обратный порядок
    # даёт кашу: гребёнка поверх перегруза подчёркивает не тембр трека, а
    # мусор, который перегруз только что насыпал.
    _INSERT_ORDER = ("wobble", "filter", "flanger", "phaser", "distortion", "bitcrush")
    for deck in ("source", "target"):
        by_unit: dict[str, list[dict]] = {}
        for e in list(events) + fx_from_knob:
            if e["action"] != "fx" or e["deck"] != deck:
                continue
            unit = str((e.get("params") or {}).get("unit") or "echo")
            by_unit.setdefault(unit, []).append(e)
        if not by_unit:
            continue

        def _p(evs, key, default):
            for e in evs:
                v = (e.get("params") or {}).get(key)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return default

        def _env_of(evs):
            fn = _piecewise_ramp(evs, spb, default=0.0)
            return _envelope_array(fn, timeline_start, n, sr,
                                   step_sec=_events_step_sec(evs, spb))

        for unit in _INSERT_ORDER:
            evs = by_unit.get(unit)
            if not evs:
                continue
            env = _env_of(evs)
            if np.max(env) <= 0.02:
                continue
            # частота качания задаётся В ДОЛЯХ, а не в герцах: иначе
            # эффект не попадает в темп, и это слышно сразу
            rate_hz = 1.0 / max(0.05, _p(evs, "rate_beats", 1.0) * spb)
            buf = decks[deck]
            if unit == "wobble":
                decks[deck] = _fx_wobble(buf, sr, env, rate_hz)
            elif unit == "filter":
                decks[deck] = _mix_dry_wet(
                    buf, _time_varying_lowpass(buf, sr, _piecewise_ramp(evs, spb, 0.0),
                                               timeline_start), np.ones(n))
            elif unit == "flanger":
                decks[deck] = _fx_flanger(buf, sr, env, rate_hz,
                                          depth_ms=_p(evs, "depth_ms", 3.5))
            elif unit == "phaser":
                decks[deck] = _fx_phaser(buf, sr, env, rate_hz,
                                         stages=int(_p(evs, "stages", 4)))
            elif unit == "distortion":
                decks[deck] = _fx_distortion(buf, env, drive=_p(evs, "drive", 8.0))
            elif unit == "bitcrush":
                decks[deck] = _fx_bitcrush(buf, sr, env, bits=_p(evs, "bits", 6.0),
                                           downsample=int(_p(evs, "downsample", 8)))

        for unit in FX_SENDS:
            evs = by_unit.get(unit)
            if not evs:
                continue
            env = _env_of(evs)
            if np.max(env) <= 0.02:
                continue
            if unit == "echo":
                # feedback приходит ручкой в процентах — здесь он доля.
                _queue_send(deck, "echo", None, env,
                            delay_sec=_p(evs, "delay_beats", 0.75) * spb,
                            feedback=min(0.9, max(0.05,
                                                  _p(evs, "feedback", 45.0) / 100.0)))
            else:
                _queue_send(deck, "reverb", None, env,
                            decay_sec=_p(evs, "decay_beats", 4.0) * spb)

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

    # Фейдер канала УХОДЯЩЕЙ деки. Появился вместе с ручным ведением:
    # там уходящий уводится своим фейдером, а не кроссфейдером, потому
    # что кривая кроссфейдера (хоть равной мощности, хоть аддитивная)
    # рассчитана на совместное ведение двух дек одним движком — она
    # поднимает одну ровно настолько, насколько опускает другую. При
    # независимых фейдерах компенсировать нечего, входящий уже на
    # полном, и эта компенсация слышна как грязь.
    src_vol_events = [e for e in events if e["action"] == "volume_ramp" and e["deck"] == "source"]
    src_vol_env = None
    if src_vol_events:
        src_fn = _piecewise_ramp(src_vol_events, spb, default=1.0)
        src_vol_env = _envelope_array(src_fn, timeline_start, n, sr)
        # Передача низа и ведение гейна считаются от «сколько уходящего
        # осталось», и раньше это брали из кроссфейда. Кроссфейда больше
        # нет — берём ту же величину из его фейдера.
        cf_env = np.clip(1.0 - src_vol_env, 0.0, 1.0)

    g_out, g_in = _equal_power(cf_env)
    if src_vol_env is not None:
        # Прямая амплитуда фейдера, без кривой равной мощности поверх.
        g_out = src_vol_env

    # Наложение: входящий приходит СВОИМ фейдером канала (volume_ramp), а
    # кроссфейдер только уводит уходящего в конце. Поэтому кривая ввода
    # берётся из громкости, а не из кроссфейда — иначе вся первая
    # половина перехода была бы неслышной: кроссфейдер там стоит на нуле
    # намеренно.
    if _overlay_mode:
        # Ручное ведение: уровень входящего задаёт ЕГО фейдер канала
        # (volume_ramp), а кроссфейдер только уводит уходящего. Если брать
        # g_in из кроссфейда, вся площадка, где оба играют полноценно,
        # оказалась бы неслышной — кроссфейдер там намеренно стоит.
        g_in = np.ones_like(g_in)

    # «Одна бочка за раз». Применяем только тем декам, у которых техника
    # НЕ управляет низом сама — иначе мы бы спорили с eq_low из техники.
    decks_with_eq_low = {e["deck"] for e in events if e["action"] in ("eq_low", "eq_kill_low")}

    # Передача низа — из ТОЙ ЖЕ величины, что и ворота барабанов выше.
    # У кроссфейда это узкое окно вокруг середины его хода; у ручного
    # ведения положение уже нормировано в 0..1 самой функцией, и второй
    # раз его сужать нечем и незачем.
    bass_out, bass_in = (_bass_handover(manual_hand, lo=0.0, hi=1.0)
                         if manual_hand is not None else _bass_handover(cf_env))
    bass_in = np.maximum(bass_in, bass_relief)
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
    # (сборка деки из слоёв — выше, пункт 1c: она обязана идти ДО
    # фильтров и эха, иначе затирает их)

    # Диджей ведёт гейн входящей деки рукой, пока она идёт под уходящей.
    ride = _ride_gain(_mono(decks["source"]), _mono(decks["target"]), cf_env, sr)
    if _debug is not None:
        _debug["ride"] = ride.copy()
    mix = (decks["source"] * _env(g_out, decks["source"])
           + decks["target"] * _env(vol_env * g_in * ride, decks["target"]))

    # --- посылы на эффекты: считаем здесь, когда фейдеры уже известны ---
    # Два разных «после фейдера», и путать их нельзя:
    #   * посыл КОРМИТСЯ пост-фейдерным сигналом — увели деку, и в эффект
    #     больше ничего не поступает;
    #   * возврат ВЛИВАЕТСЯ в микс мимо канала — поэтому хвост, уже лежащий
    #     в линии задержки, доигрывает после того, как деки не стало.
    # Раньше посыл снимался с сухой деки на полной громкости, и возврат
    # ложился поверх готового микса второй дорожкой: в ControlC это и
    # слышалось как «добавил FX — дБ зашкаливает».
    _fader = {"source": g_out, "target": vol_env * g_in * ride}
    for _req in fx_send_queue:
        _sig = _req["signal"]
        if _sig is None:
            _sig = decks[_req["deck"]]
        _fed = _sig * _env(_fader[_req["deck"]], _sig)
        _prm = _req["params"]
        # Ручки юнита передаём ТОЛЬКО те, что пришли: у _echo_return и
        # _reverb_return свои умолчания, и затирать их «умолчанием
        # вызывающего» означало бы держать одно и то же число в двух
        # местах.
        _pass = {k: v for k, v in _prm.items() if v is not None}
        if _req["unit"] == "echo":
            _wet = _echo_return(_fed, sr, _req["env"],
                                **{k: _pass[k] for k in ("delay_sec", "feedback")
                                   if k in _pass})
        else:
            _wet = _reverb_return(_fed, sr, _req["env"],
                                  **{k: _pass[k] for k in ("decay_sec",)
                                     if k in _pass})
        mix = mix + _wet * FX_RETURN_LEVEL
    # В полном сете кусок — часть большого файла, и подтягивать его уровень
    # отдельно НЕЛЬЗЯ: тела треков вокруг него нормируются по своему треку,
    # и переход стал бы громче или тише окружения. Там уровнем управляет
    # только _match_loudness по опорному уровню трека.
    if normalize_output:
        mix = _normalize_output(mix)
    else:
        mix = _soft_limit(mix)
    mix = _edge_fade(mix, sr)

    # Сколько материала КАЖДОГО файла кусок реально съел. При обычном
    # ходе это его длительность, но приём вертушки этот счёт меняет:
    # тейп-стоп проигрывает меньше секунд трека, чем длится сам, а луп и
    # вовсе топчется на месте. Без поправки render_full_set продолжил бы
    # тело трека не с того места — тот же класс бага, что и повтор доли на
    # шве (см. claude/mix-timing.md).
    src_used_seconds = total_duration
    tgt_used_seconds = timeline_end - sync_time
    if "source" in transport_info:
        src_used_seconds = transport_info["source"]["advance"] / sr
    if "target" in transport_info:
        tgt_used_seconds = max(0.0, (transport_info["target"]["advance"] - start_sample) / sr)

    # Хвост без материала отрезаем ПОСЛЕДНИМ действием, и вместе с ним
    # правим все длительности: иначе render_full_set продолжил бы тело
    # следующего трека с той секунды, до которой кусок якобы доиграл, —
    # и вместо дырки получилась бы дырка плюс повтор.
    mix, dead_tail = _trim_dead_tail(mix, sr)
    if dead_tail > 0:
        total_duration = max(0.0, total_duration - dead_tail)
        tgt_used_seconds = max(0.0, tgt_used_seconds - dead_tail)
        if "source" not in transport_info:
            src_used_seconds = max(0.0, src_used_seconds - dead_tail)

    # Демо пишем в mp3, а не в wav. Отбор швов — это десятки прослушиваний
    # за вечер, и каждый wav на 25 секунд стерео это 4-5 МБ против 600 КБ
    # у mp3 320. Для «послушать и решить» разницы на слух нет, а разница в
    # мусоре на диске — восьмикратная. Расширение выходного файла решает:
    # .mp3 -> LAME, что угодно другое -> прежний wav.
    if str(out_path).lower().endswith(".mp3"):
        import set_export

        set_export._write_mp3(str(out_path), mix.astype(np.float32), sr, 320)
    else:
        import soundfile as sf

        sf.write(out_path, mix.astype(np.float32), sr, subtype="PCM_16")

    return {
        "duration_seconds": round(total_duration, 2),
        # Сколько секунд пустого хвоста пришлось отрезать. Ноль — норма;
        # заметное число значит, что план просит у файла больше, чем в
        # нём есть, и точку входа стоит подвинуть.
        "dead_tail_seconds": round(dead_tail, 2),
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
        "source_offset_seconds": src_origin,
        "target_offset_seconds": tgt_grid.get("file_offset"),
        # где в ИСХОДНЫХ файлах начинается и заканчивается использованный
        # кусок — по этим числам render_full_set сшивает тела треков с
        # переходами без дырок и без повторов
        "source_in_seconds": src_origin,
        "source_out_seconds": (None if src_origin is None
                               else round(src_origin + src_used_seconds * rate_a, 3)),
        "target_in_seconds": tgt_grid.get("file_offset"),
        "target_out_seconds": (None if tgt_grid.get("file_offset") is None
                               else round(tgt_grid["file_offset"] + tgt_used_seconds * rate, 3)),
        "master_bpm": round(timeline_bpm, 2),
        "rate_source": round(rate_a, 4),
        "grid_confidence": round(min(float(src_grid.get("confidence", 0.0)),
                                     float(tgt_grid.get("confidence", 0.0))), 3),
        "crossfade_complete": bool(len(cf_env) and cf_env[-1] >= 0.99),
        "entry_profile": (str((mix_profile or {}).get("entry") or "technique")),
        "vocal_loop_bars": round(vocal_loop_bars, 2) or None,
        # Длина, которая РЕАЛЬНО прозвучала, а не заказанная. Приём
        # короче окна упирается в потолок растяжения (MAX_TIME_SCALE), и
        # тогда «64 такта» на карточке были бы неправдой: играет восемь.
        # Карточка обязана показывать то, что слышно.
        "bars": (round(raw_end * scale / bar_sec, 1) if bars else None),
        "bars_asked": (round(float(bars), 1) if bars else None),
        "stems_used": bool(stems_ready),
        "drum_swap_seconds": None if stem_swap is None else round(stem_swap, 2),
    }
