"""
Таблица соответствий "действие плана" -> MIDI-сообщение.

Номера нот/CC и привязки к контролам Mixxx живут в mixxx_controls.py —
это единственный источник истины, из которого генерируется и
DARAVE-Virtual-Controller.midi.xml. Здесь только перевод в байты.

Канал = дека (A=0, B=1, C=2, D=3), мастер-команды — канал 15.
Дискретные действия -> Note On (velocity 127) / отпускание -> Note On
velocity 0. Непрерывные (ramp) -> CC 0..127, промежуточные тики
генерирует scheduler.

Значения ramp — нормализованные 0..1 (0 = минимум контрола Mixxx, 1 =
максимум); где у ручки «ноль», см. Control.neutral в mixxx_controls.py.
"""
from __future__ import annotations

from mixxx_controls import (
    BY_ID,
    FX_BY_TARGET,
    CC_NUMBER,
    DECK_CHANNEL,
    MASTER_CHANNEL,
    NOTE_NUMBER,
    nearest_loop_size,
    variant_note,
)

# --- совместимость со старым API (техники, scheduler, тесты) ---
DISCRETE_NOTE = dict(NOTE_NUMBER)
RAMP_CC = dict(CC_NUMBER)
MASTER_DISCRETE_NOTE = {
    c.id: NOTE_NUMBER[c.id]
    for c in BY_ID.values() if c.scope == "master" and c.kind in ("button", "hold")
}
LOOP_SIZES = list(BY_ID["loop_activate"].variants)
LOOP_SIZE_NOTE = {float(v): variant_note("loop_activate", v) for v in LOOP_SIZES}


def note_on(channel: int, note: int, velocity: int = 127) -> list[int]:
    return [0x90 | channel, note, velocity]


def note_off(channel: int, note: int) -> list[int]:
    """Отпускание = Note ON со скоростью 0, а НЕ статус 0x80.

    Это не стилистика, а совместимость с Mixxx: <control> в XML привязан к
    КОНКРЕТНОМУ статусу, и биндинг на 0x90 не ловит сообщения 0x80 (ключ
    поиска — status<<8|midino, это разные ключи). Раньше отпускание
    уходило как 0x80 и не совпадало ни с одним биндингом: reverse_hold
    зажимался и НИКОГДА не отпускался — дека так и оставалась в
    reverse+slip после Reverse Drop / Backspin. "Note On velocity 0 = note
    off" — штатная MIDI-конвенция, и для <button/> Mixxx трактует значение
    0 как отпускание."""
    return [0x90 | channel, note, 0]


def cc(channel: int, controller: int, value_0_127: int) -> list[int]:
    value_0_127 = max(0, min(127, value_0_127))
    return [0xB0 | channel, controller, value_0_127]


def _channel_for(control_id: str, deck: str) -> int:
    ctrl = BY_ID.get(control_id)
    if ctrl is not None and ctrl.scope == "master":
        return MASTER_CHANNEL
    return DECK_CHANNEL[deck]


def resolve_discrete(action: str, deck: str, params: dict) -> list[int]:
    ctrl = BY_ID.get(action)
    channel = _channel_for(action, deck)

    if ctrl is not None and ctrl.kind == "loop":
        beats = (params or {}).get("beats")
        if beats:
            return note_on(channel, variant_note(action, beats))
        # без явной длины ведём себя как раньше: зациклить на текущем размере
        return note_on(channel, NOTE_NUMBER.get("loop_activate_current", variant_note(action, 4)))

    if ctrl is not None and ctrl.kind == "hotcue":
        number = int((params or {}).get("hotcue", 1))
        return note_on(channel, variant_note(action, number))

    if action == "hotcue_jump":  # исторический путь
        return note_on(channel, variant_note("hotcue_jump", int(params["hotcue"])))

    if action not in NOTE_NUMBER:
        raise ValueError(f"Unknown discrete action: {action}")
    return note_on(channel, NOTE_NUMBER[action])


def resolve_discrete_off(action: str, deck: str) -> list[int]:
    """Note Off — пара к resolve_discrete() для действий с удержанием
    (reverse_hold, sync_lock, fx_enable): зажать в начале события,
    отпустить в конце."""
    channel = _channel_for(action, deck)
    if action not in NOTE_NUMBER:
        raise ValueError(f"Unknown discrete action: {action}")
    return note_off(channel, NOTE_NUMBER[action])


def resolve_ramp_tick(action: str, deck: str, value_0_1: float) -> list[int]:
    if action not in CC_NUMBER:
        raise ValueError(f"Unknown ramp action: {action}")
    channel = _channel_for(action, deck)
    return cc(channel, CC_NUMBER[action], round(value_0_1 * 127))


def resolve_master_discrete(action: str) -> list[int]:
    """Как resolve_discrete(), но для команд без привязки к деке —
    шлются одним note_on на MASTER_CHANNEL."""
    if action not in MASTER_DISCRETE_NOTE:
        raise ValueError(f"Unknown master action: {action}")
    return note_on(MASTER_CHANNEL, MASTER_DISCRETE_NOTE[action])


def neutral_of(action: str) -> float:
    ctrl = BY_ID.get(action)
    return ctrl.neutral if ctrl is not None else 0.5


# ---------------------------------------------------------------- SysEx
# Универсальный путь до ЛЮБОГО контрола Mixxx по имени.
#
# Зачем он нужен рядом с обычными CC/Note: в Mixxx около 38 000 контролов
# (только на одной деке 1150), а MIDI даёт 128 нот и 128 CC на канал.
# Расписать всё отдельными <control> в XML невозможно в принципе. Поэтому:
#   * быстрый путь (CC/Note) — для того, что идёт часто и критично ко
#     времени: кроссфейдер, EQ, свипы внутри MixPlan;
#   * SysEx — для всего остального, включая каждый параметр каждого
#     эффекта, выбор эффекта в слоте, сэмплеры, Auto DJ и прочее.
# Разбирает это darave-controller-scripts.js::onSysex.

def fx_message(group: str, key: str, value: float | None) -> list[int] | None:
    """FX-контрол -> прямое CC/Note. None, если этой пары нет в раскладке.

    Прямой путь, а не SysEx: им уже работают ручки EQ, значит механизм
    заведомо рабочий. FX-юниты живут на отдельных MIDI-каналах 4-7, где
    128 CC и 128 нот на юнит — хватает на 4 слота по 16 параметров."""
    b = FX_BY_TARGET.get((group, key))
    if b is None:
        return None
    if b["kind"] == "range":
        v = 0.0 if value is None else float(value)
        return [b["status"], b["number"], max(0, min(127, round(v * 127)))]
    # toggle/button: 127 = включить/нажать, 0 = выключить/отпустить
    on = 127 if (value is None or value) else 0
    return [b["status"], b["number"], on]


def fx_message_off(group: str, key: str) -> list[int] | None:
    b = FX_BY_TARGET.get((group, key))
    return None if b is None else [b["status"], b["number"], 0]


SYSEX_START, SYSEX_END = 0xF0, 0xF7
SYSEX_SEP = "|"


def _sysex(payload: str) -> list[int]:
    # В SysEx допустимы только 7-битные байты. Имена групп/ключей Mixxx —
    # ASCII, но проверяем явно: иначе кириллица в аргументе молча
    # превратилась бы в мусор на стороне Mixxx.
    try:
        raw = payload.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"SysEx принимает только ASCII, получено: {payload!r}") from exc
    if any(b > 0x7F for b in raw):
        raise ValueError(f"не 7-битный байт в SysEx: {payload!r}")
    if SYSEX_SEP not in payload:
        raise ValueError(f"в SysEx-команде нет разделителя '{SYSEX_SEP}': {payload!r}")
    return [SYSEX_START, *raw, SYSEX_END]


def sysex_set(group: str, key: str, value: float) -> list[int]:
    """engine.setValue(group, key, value) на стороне Mixxx."""
    return _sysex(f"S{SYSEX_SEP}{group}{SYSEX_SEP}{key}{SYSEX_SEP}{value:g}")


def sysex_toggle(group: str, key: str) -> list[int]:
    """Переключить 0<->1, не зная текущего значения на нашей стороне."""
    return _sysex(f"T{SYSEX_SEP}{group}{SYSEX_SEP}{key}")


def sysex_press(group: str, key: str) -> list[int]:
    """Нажатие кнопки: 1, затем 0. Часть контролов Mixxx реагирует на
    фронт и без отпускания остаётся «зажатой»."""
    return _sysex(f"P{SYSEX_SEP}{group}{SYSEX_SEP}{key}")


def decode_sysex(message: list[int]) -> str:
    """Обратная операция — для тестов и логов."""
    if not message or message[0] != SYSEX_START or message[-1] != SYSEX_END:
        raise ValueError("это не SysEx-сообщение")
    return bytes(message[1:-1]).decode("ascii")
