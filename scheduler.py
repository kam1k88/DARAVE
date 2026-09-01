"""
PlanScheduler — сердце тактического слоя companion'а.

Получает MixPlan один раз, разворачивает его в плоский список
(абсолютное время выполнения, MIDI-сообщение) и исполняет локально.
После разворота плана облако из критического пути полностью исключено —
исполнение идёт по монотонным локальным часам (time.perf_counter).

Точность сна: asyncio.sleep() сам по себе даёт точность порядка 1-15мс
(зависит от ОС/планировщика), чего мало для попадания в долю такта.
Поэтому используется гибридный сон: asyncio.sleep() до последних ~3мс,
затем busy-wait на perf_counter для финальной точности.
"""
from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass

from midi_mapping import resolve_discrete, resolve_discrete_off, resolve_ramp_tick
from mixplan import EventKind, MixEvent, MixPlan

# Шаг генерации тиков для ramp-событий (чем меньше — тем плавнее, но больше
# сообщений; 20мс достаточно плавно для fx/crossfader и не флудит порт)
RAMP_TICK_SECONDS = 0.02

# Порог, с которого переходим с asyncio.sleep на busy-wait для точного финиша
BUSY_WAIT_THRESHOLD_SECONDS = 0.003


@dataclass(order=True)
class TimedMidiEvent:
    exec_time: float  # абсолютное время по perf_counter()
    message: list[int] = None  # type: ignore[assignment]
    label: str = ""

    def __post_init__(self):
        # dataclass(order=True) сравнивает по exec_time первым полем — ок,
        # но heapq сравнивает весь tuple при равенстве времени, а list
        # не сравнивается корректно между разными событиями -> используем
        # только exec_time как ключ сортировки явно в scheduler'е.
        pass


class PlanScheduler:
    def __init__(self, backend) -> None:
        self._backend = backend

    def expand_plan(self, plan: MixPlan, plan_received_at: float) -> list[TimedMidiEvent]:
        """MixPlan -> плоский список таймированных MIDI-событий (perf_counter-время)."""
        anchor_time = plan_received_at + plan.anchor_lead_seconds
        beat_seconds = plan.beat_duration_seconds()

        timed: list[TimedMidiEvent] = []
        for ev in plan.events:
            start_time = anchor_time + ev.beat_offset * beat_seconds
            if ev.kind == EventKind.DISCRETE:
                msg = resolve_discrete(ev.action, ev.deck, ev.params)
                timed.append(TimedMidiEvent(start_time, msg, f"{ev.action}@{ev.deck}"))
            elif ev.kind == EventKind.HOLD:
                assert ev.duration_beats is not None
                on_msg = resolve_discrete(ev.action, ev.deck, ev.params)
                # params нужны и на отпускании: у лупролла длина лупа зашита
                # в ИМЯ контрола (beatlooproll_2_activate), и отпустить надо
                # ровно тот, что нажали, — иначе каскад 4->2->1 отпускал бы
                # каждый раз четырёхдольный и оставлял бы остальные зажатыми.
                off_msg = resolve_discrete_off(ev.action, ev.deck, ev.params)
                timed.append(TimedMidiEvent(start_time, on_msg, f"{ev.action}@{ev.deck} hold-on"))
                # Пустое сообщение — «отпускать нечего»: brake/spinback
                # кончаются сами, а команда «выключить» вернула бы деку на
                # нормальную скорость и отменила приём.
                if off_msg:
                    timed.append(TimedMidiEvent(
                        start_time + ev.duration_beats * beat_seconds, off_msg,
                        f"{ev.action}@{ev.deck} hold-off",
                    ))
            else:
                timed.extend(self._expand_ramp(ev, start_time, beat_seconds))

        timed.sort(key=lambda e: e.exec_time)
        return timed

    def _expand_ramp(self, ev: MixEvent, start_time: float, beat_seconds: float) -> list[TimedMidiEvent]:
        assert ev.duration_beats is not None
        assert ev.value_from is not None and ev.value_to is not None
        duration = ev.duration_beats * beat_seconds
        n_ticks = max(1, int(duration / RAMP_TICK_SECONDS))

        cycles = ev.params.get("cycles", 4)  # только для curve="sine" (EQ Roller и т.п.)
        out = []
        for i in range(n_ticks + 1):
            progress = i / n_ticks
            frac = self._apply_curve(progress, ev.curve, cycles)
            value = ev.value_from + (ev.value_to - ev.value_from) * frac
            msg = resolve_ramp_tick(ev.action, ev.deck, value, ev.params)
            t = start_time + i * (duration / n_ticks)
            out.append(TimedMidiEvent(t, msg, f"{ev.action}@{ev.deck} tick{i}"))
        return out

    @staticmethod
    def _apply_curve(frac: float, curve: str, cycles: float = 4) -> float:
        if curve == "ease_in":
            return frac * frac
        if curve == "ease_out":
            return 1 - (1 - frac) * (1 - frac)
        if curve == "sine":
            # LFO-колебание value_from<->value_to, `cycles` полных периодов за
            # весь duration_beats, начинается и заканчивается в value_from
            # (нужно для EQ Roller — модуляция фильтра, а не монотонный переход).
            import math
            return 0.5 - 0.5 * math.cos(2 * math.pi * cycles * frac)
        return frac  # linear

    async def run(self, timed_events: list[TimedMidiEvent]) -> list[float]:
        """Исполняет события, возвращает список джиттеров (факт - план) в секундах."""
        heap = list(timed_events)
        heapq.heapify([e.exec_time for e in heap])  # no-op, держим порядок через sort
        heap.sort(key=lambda e: e.exec_time)

        jitters = []
        for ev in heap:
            await self._precise_wait_until(ev.exec_time)
            self._backend.send(ev.message)
            jitters.append(time.perf_counter() - ev.exec_time)
        return jitters

    @staticmethod
    async def _precise_wait_until(target: float) -> None:
        while True:
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return
            if remaining > BUSY_WAIT_THRESHOLD_SECONDS:
                await asyncio.sleep(remaining - BUSY_WAIT_THRESHOLD_SECONDS)
            else:
                # финальный отрезок — короткие sleep(0) чтобы не жрать 100% CPU,
                # но и не терять точность на длинных sleep()
                await asyncio.sleep(0)
