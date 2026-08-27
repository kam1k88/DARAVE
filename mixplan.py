"""
MixPlan — декларативный план перехода/техники, который backend (LLM/RL-агент)
присылает companion'у ОДИН раз. Дальше companion сам разворачивает план во
времени и исполняет его локально, без обращений в облако на каждый шаг.

Два типа событий:
  - discrete : одно MIDI-сообщение в конкретный момент (hotcue, sync, loop...)
  - ramp     : непрерывное изменение параметра (filter sweep, crossfade...),
               companion сам генерирует промежуточные тики с нужным шагом.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class EventKind(str, Enum):
    DISCRETE = "discrete"
    RAMP = "ramp"
    # Удержание: note_on в момент beat_offset, note_off через duration_beats —
    # для действий вроде reverse_hold (Reverse Drop/Backspin Transition).
    HOLD = "hold"


@dataclass
class MixEvent:
    # Момент начала события, в долях такта от анкера плана (anchor = beat 0)
    beat_offset: float

    # Имя действия — резолвится в midi_mapping.py в конкретные MIDI-сообщения.
    # Примеры: "hotcue_jump", "loop_activate", "sync", "eq_kill",
    #          "filter_sweep", "crossfade", "gain_ramp"
    action: str

    # На какую деку действие (A/B/C/D — маппится на MIDI-канал)
    deck: str

    kind: EventKind = EventKind.DISCRETE

    # Для ramp-событий: длительность в долях такта и границы значения (0..1)
    duration_beats: float | None = None
    value_from: float | None = None
    value_to: float | None = None
    # "sine" — колебание между value_from/value_to с частотой params["cycles"]
    # за весь duration_beats (см. scheduler.py) — для EQ Roller и похожих
    # автоматизаций-LFO, а не монотонного перехода.
    curve: Literal["linear", "ease_in", "ease_out", "sine"] = "linear"

    # Произвольные доп.параметры под конкретное действие
    # (например {"hotcue": 3} или {"target_deck": "B"})
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class MixPlan:
    plan_id: str

    # BPM, по которому считаем длительность такта -> секунды.
    # В реальной системе берётся из последнего state-тика Mixxx для anchor-деки.
    bpm: float

    # Событие t=0 в плане наступит через anchor_lead_seconds от момента приёма
    # плана companion'ом (это и есть тот самый "бюджет на доставку плана").
    anchor_lead_seconds: float

    events: list[MixEvent] = field(default_factory=list)

    def beat_duration_seconds(self) -> float:
        return 60.0 / self.bpm

    @staticmethod
    def from_dict(d: dict) -> "MixPlan":
        events = [
            MixEvent(
                beat_offset=e["beat_offset"],
                action=e["action"],
                deck=e["deck"],
                kind=EventKind(e.get("kind", "discrete")),
                duration_beats=e.get("duration_beats"),
                value_from=e.get("value_from"),
                value_to=e.get("value_to"),
                curve=e.get("curve", "linear"),
                params=e.get("params", {}),
            )
            for e in d["events"]
        ]
        return MixPlan(
            plan_id=d["plan_id"],
            bpm=d["bpm"],
            anchor_lead_seconds=d.get("anchor_lead_seconds", 1.0),
            events=events,
        )
