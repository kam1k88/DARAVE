"""transport.py — позиция воспроизведения деки как функция времени.

Зачем отдельный модуль. Всё, что диджей делает РУКОЙ по пластинке —
бэкспин, тейп-стоп, реверс, луп, ролл — это не эффект поверх звука, это
изменение того, ГДЕ находится игла. Раньше demo_render изображал такие
приёмы обработкой готового буфера: `reverse_hold` переворачивал кусок
задом наперёд (то есть реверс на ПОЛНОЙ скорости — так вертушка не умеет
в принципе), а луп повторял кусок копипастом. Ни падения скорости, ни
падения высоты — то есть ровно того, по чему бэкспин и тейп-стоп узнаются
на слух, — там не было.

Здесь всё считается честно: строится p(t) — позиция иглы в исходном
буфере, в отсчётах. Дальше звук читается по этой позиции с интерполяцией.
Замедлилась позиция — вместе с ней уехала вниз ВЫСОТА, как у настоящей
вертушки, потому что это одно и то же явление, а не два.

Физика торможения. Платтер под трением: dv/dt = -k. Чистая линейка
звучит механически, поэтому берём v(u) = (1-u)^p, p≈1.6 — сначала падает
быстро, потом доезжает. Именно так слышен и Technics со включённым
брейком, и тейп-стоп-плагин. Спинбэк — то же самое, только стартовая
скорость отрицательная и большая по модулю (рука кидает пластинку назад
в 8-10 крат), а трение возвращает её к нулю; «бэкспин укладывается в одну
долю» — это про длительность, а не про пройденный путь, назад при этом
проматывается несколько долей.

Слип (slip / censor / loop roll). Приём кончился — игла возвращается
туда, где была бы, если бы приём не выполнялся. Поэтому позиция после
такого события считается по СВОБОДНОМУ ходу, а не по фактическому: это
разница между реверс-роллом (трек не сбился) и просто реверсом (трек уехал
назад).

Алиасинг. Чтение на скорости 8x — это децимация в 8 раз, и без
band-limit получается не «пластинка назад», а звон обратных частот.
Поэтому чтение идёт из копии буфера, отфильтрованной под нужную октаву
скорости (кэш на вызов, обычно 1-2 лишних фильтра на короткий кусок).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

# Действия, которые двигают ИГЛУ. Всё остальное (EQ, фильтры, фейдер) —
# обработка звука и живёт в demo_render.
TRANSPORT_ACTIONS = frozenset({
    "brake", "spinback", "soft_start", "reverse_play", "reverse_hold",
    "loop_activate", "loop_exit", "loop_roll", "beatjump",
})
# «reverse_hold» — историческое имя: в mixxx_controls оно привязано к
# `reverseroll`, а это censor, то есть реверс СО СЛИПОМ (после отпускания
# трек продолжается там, где шёл бы). Поэтому это синоним reverse_play,
# а не отдельный приём.

BRAKE_P = 1.6        # показатель кривой выбега (торможение)
SPIN_P = 1.5         # показатель кривой спинбэка
START_P = 0.55       # разгон мотора: быстро берёт, потом доезжает
SPINBACK_RATE = -9.0  # во сколько крат рука кидает пластинку назад
STOP_EPS = 1e-3


def _down(u: np.ndarray, p: float) -> np.ndarray:
    """1 -> 0 по кривой выбега."""
    return np.clip(1.0 - u, 0.0, 1.0) ** p


def _param(ev: dict, key: str, default: float) -> float:
    v = (ev.get("params") or {}).get(key)
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def build_position(events: list[dict], deck: str, n: int, sr: int, spb: float,
                   timeline_start: float) -> dict:
    """p(t) для одной деки.

    Возвращает {pos, gain, advance, stopped_at, moved}:
      pos     — позиция в исходном буфере (отсчёты), длина n;
      gain    — 0 там, где дека стоит (остановленная пластинка — тишина);
      advance — сколько отсчётов исходника дека фактически прошла
                (нужно, чтобы сшивка полного сета знала, где кончился кусок);
      moved   — были ли вообще транспортные события (если нет, вызывающий
                код может не тратить время на пересчёт звука).
    """
    pos = np.zeros(n, dtype=np.float64)
    gain = np.ones(n, dtype=np.float64)

    evs = [e for e in events
           if e.get("action") in TRANSPORT_ACTIONS and e.get("deck") == deck]
    if not evs or n <= 0:
        pos[:] = np.arange(n, dtype=np.float64)
        return {"pos": pos, "gain": gain, "advance": float(n), "moved": False}

    def idx(beat: float) -> int:
        return int(round((beat * spb - timeline_start) * sr))

    evs.sort(key=lambda e: (e["beat_offset"], 0 if e["action"] != "loop_exit" else 1))

    cursor = 0          # где мы на таймлайне (отсчёт)
    p_cur = 0.0         # где игла в буфере (отсчёт исходника)
    stopped = False

    def run_normal(upto: int) -> None:
        nonlocal cursor, p_cur
        upto = min(max(upto, cursor), n)
        if upto <= cursor:
            return
        k = upto - cursor
        if stopped:
            pos[cursor:upto] = p_cur
            gain[cursor:upto] = 0.0
        else:
            # позиция ОТСЧЁТА, а не его конца: первый отсчёт сегмента читается
            # ровно оттуда, где игла стоит сейчас. Сдвиг на один отсчёт здесь
            # был бы неслышен сам по себе, но пересэмплировал бы весь буфер
            # даже там, где транспорт ничего не делает.
            pos[cursor:upto] = p_cur + np.arange(k, dtype=np.float64)
            p_cur += k
        cursor = upto

    def run_rate(length: int, rate: np.ndarray) -> None:
        """Проиграть length отсчётов с заданной поштучной скоростью."""
        nonlocal cursor, p_cur
        length = min(length, n - cursor)
        if length <= 0:
            return
        r = rate[:length]
        seg = p_cur + np.cumsum(r) - r          # позиция В НАЧАЛЕ каждого отсчёта
        pos[cursor:cursor + length] = seg
        p_cur = float(p_cur + np.sum(r))
        cursor += length

    def beats_to_n(beats: float) -> int:
        return max(1, int(round(beats * spb * sr)))

    i = 0
    while i < len(evs):
        e = evs[i]
        i += 1
        action = e["action"]
        start_i = min(max(idx(e["beat_offset"]), 0), n)
        run_normal(start_i)
        if cursor >= n:
            break

        if action == "beatjump":
            p_cur += _param(e, "beats", 4.0) * spb * sr
            continue

        if action == "loop_exit":
            # одиночный выход без активации — ничего не делает
            continue

        dur_beats = float(e.get("duration_beats") or 0.0)

        if action == "brake":
            L = beats_to_n(dur_beats or 2.0)
            u = np.arange(L, dtype=np.float64) / L
            run_rate(L, _down(u, _param(e, "curve", BRAKE_P)))
            stopped = True
            continue

        if action == "spinback":
            L = beats_to_n(dur_beats or 1.0)
            u = np.arange(L, dtype=np.float64) / L
            v0 = _param(e, "rate", SPINBACK_RATE)
            run_rate(L, v0 * _down(u, _param(e, "curve", SPIN_P)))
            stopped = True
            continue

        if action == "soft_start":
            L = beats_to_n(dur_beats or 4.0)
            u = np.arange(L, dtype=np.float64) / L
            stopped = False
            run_rate(L, np.clip(u, 0.0, 1.0) ** _param(e, "curve", START_P))
            continue

        if action in ("reverse_play", "reverse_hold"):
            L = beats_to_n(dur_beats or 4.0)
            free_from = p_cur
            run_rate(L, np.full(L, -abs(_param(e, "rate", 1.0))))
            if _param(e, "slip", 1.0) >= 0.5:
                # слип: игла возвращается туда, где была бы без приёма
                p_cur = free_from + L
            continue

        if action in ("loop_roll", "loop_activate"):
            loop_n = beats_to_n(_param(e, "beats", 4.0))
            if action == "loop_roll":
                L = beats_to_n(dur_beats or _param(e, "beats", 4.0))
                slip = _param(e, "slip", 1.0) >= 0.5
            else:
                # длину держим до loop_exit; без него — до конца куска
                exit_i = n
                for j in range(i, len(evs)):
                    if evs[j]["action"] == "loop_exit":
                        exit_i = min(max(idx(evs[j]["beat_offset"]), cursor), n)
                        i = j + 1
                        break
                L = exit_i - cursor
                slip = False
            L = min(L, n - cursor)
            if L <= 0:
                continue
            free_from = p_cur
            k = np.arange(1, L + 1, dtype=np.float64)
            pos[cursor:cursor + L] = free_from + np.mod(k - 1, loop_n)
            cursor += L
            # слип — как будто лупа не было; без слипа — продолжаем оттуда,
            # где луп застал иглу (так и ведёт себя Mixxx на loop_exit)
            p_cur = free_from + L if slip else float(pos[cursor - 1])
            continue

    run_normal(n)
    # Событие в списке ещё не значит, что игла куда-то поехала: голый
    # loop_exit без активации, нулевая длительность и т.п. Если позиция
    # совпала с обычным ходом — говорим «не двигались», и demo_render не
    # станет зря пересэмплировать весь буфер (это отличие в -30 дБ на
    # ровном месте).
    moved = bool(np.max(np.abs(pos - np.arange(n, dtype=np.float64))) > 1e-6
                 or np.min(gain) < 1.0)
    return {"pos": pos, "gain": gain, "advance": float(p_cur), "moved": moved}


def _lowpassed(buf: np.ndarray, sr: int, factor: float, cache: dict) -> np.ndarray:
    """Копия буфера, ограниченная по полосе под чтение на скорости factor."""
    key = round(factor)
    if key <= 1:
        return buf
    if key in cache:
        return cache[key]
    cutoff = 0.42 * sr / key
    sos = butter(4, min(cutoff / (sr / 2), 0.99), btype="lowpass", output="sos")
    out = sosfiltfilt(sos, buf, axis=0)
    cache[key] = out
    return out


def read(buf: np.ndarray, pos: np.ndarray, gain: np.ndarray, sr: int) -> np.ndarray:
    """Прочитать буфер по позиции иглы.

    Быстрое чтение (|rate| > 1) — это децимация, поэтому берём копию,
    ограниченную по полосе под нужную октаву скорости: иначе спинбэк
    звучит не пластинкой назад, а звоном обратных частот.
    """
    n = len(pos)
    if buf.ndim == 1:
        buf = buf[:, None]
    src_len = len(buf)
    out = np.zeros((n, buf.shape[1]), dtype=buf.dtype)
    if src_len < 2 or n == 0:
        return out

    rate = np.abs(np.gradient(pos))
    # октавы скорости: 1 (без фильтра), 2, 4, 8, 16
    bucket = np.ones(n)
    for f in (2, 4, 8, 16):
        bucket[rate > f * 0.8] = f

    clamped = np.clip(pos, 0.0, src_len - 1.000001)
    inside = (pos >= -0.5) & (pos <= src_len - 1)
    grid = np.arange(src_len, dtype=np.float64)
    cache: dict[int, np.ndarray] = {}

    for f in np.unique(bucket):
        sel = bucket == f
        if not np.any(sel):
            continue
        src = _lowpassed(buf, sr, float(f), cache)
        p = clamped[sel]
        for c in range(buf.shape[1]):
            out[sel, c] = np.interp(p, grid, src[:, c])

    g = gain * inside
    return out * g[:, None]
