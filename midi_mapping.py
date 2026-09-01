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
    DECK_NUMBER,
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


# --- приёмы вертушки: то, чего в каталоге контролов нет в принципе ---
#
# brake / spinback / softStart в Mixxx — НЕ ControlObject'ы, а функции
# скрипт-движка (engine.brake(deck, activate, factor)). Их нельзя ни
# замапить в XML, ни выставить через engine.setValue: искать их в
# mixxx_catalog.json бесполезно, там их нет и не будет. Единственный путь —
# позвать функцию на стороне скрипта, для чего в SysEx-протоколе заведена
# отдельная операция "E" (engine), см. darave-controller-scripts.js.
#
# Остальные приёмы транспорта — обычные контролы, и идут через "S":
#   loop_roll    -> beatlooproll_<доли>_activate (моментарный, со слипом)
#   reverse_play -> reverseroll (censor: реверс со слипом)
#   beatjump     -> beatjump (значение = сколько долей, со знаком)
ENGINE_ACTIONS = {"brake": "brake", "spinback": "spinback", "soft_start": "softStart"}
# Маркер «сообщения нет и не должно быть» — отличается от None, которым
# _transport_message говорит «это вообще не транспортное действие».
_NO_MESSAGE: list[int] = []
# Ряд длин, которые Mixxx понимает в имени beatlooproll_<N>_activate
ROLL_SIZES = (0.03125, 0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8, 16, 32)


def _deck_group(deck: str) -> str:
    return f"[Channel{DECK_NUMBER.get(deck, 1)}]"


def _nearest_roll(beats: float) -> str:
    best = min(ROLL_SIZES, key=lambda v: abs(v - float(beats)))
    return f"{best:g}"


def _transport_message(action: str, deck: str, params: dict, on: bool) -> list[int] | None:
    """MIDI для действия, двигающего иглу. None — действие не транспортное."""
    params = params or {}
    group = _deck_group(deck)
    if action in ENGINE_ACTIONS:
        if not on:
            # Отпускать эти приёмы НЕЛЬЗЯ: по документации Mixxx выключение
            # brake/spinback/softStart «возвращает деку на нормальную
            # скорость». То есть команда «отпустить тейп-стоп» отменила бы
            # ровно то, ради чего он делался — дека поехала бы дальше.
            # Приём кончается сам, когда обороты дошли до нуля.
            return _NO_MESSAGE
        factor = float(params.get("factor", 1.0))
        rate = float(params.get("rate", -10.0))
        return sysex_engine(ENGINE_ACTIONS[action], DECK_NUMBER.get(deck, 1),
                            1, factor, rate)
    if action == "loop_roll":
        size = _nearest_roll(params.get("beats", 4))
        return sysex_set(group, f"beatlooproll_{size}_activate", 1.0 if on else 0.0)
    if action in ("reverse_play", "reverse_hold"):
        return sysex_set(group, "reverseroll", 1.0 if on else 0.0)
    if action == "beatjump":
        if not on:
            return _NO_MESSAGE
        return sysex_set(group, "beatjump", float(params.get("beats", 4)))
    return None


def resolve_discrete(action: str, deck: str, params: dict) -> list[int]:
    msg = _transport_message(action, deck, params, on=True)
    if msg is not None:
        return msg
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


def resolve_discrete_off(action: str, deck: str, params: dict | None = None) -> list[int]:
    """Note Off — пара к resolve_discrete() для действий с удержанием
    (reverse_hold, sync_lock, fx_enable): зажать в начале события,
    отпустить в конце."""
    msg = _transport_message(action, deck, params or {}, on=False)
    if msg is not None:
        # пустой список = отпускать нечего (см. _NO_MESSAGE)
        return msg
    channel = _channel_for(action, deck)
    if action not in NOTE_NUMBER:
        raise ValueError(f"Unknown discrete action: {action}")
    return note_off(channel, NOTE_NUMBER[action])


# Действия из словаря техник, у которых в Mixxx нет собственного контрола.
#
# "fx" — это dry/wet юнита, назначенного на деку: в Mixxx нет контрола
# «включить фленджер», есть слот эффекта, и ЧТО в него загружено, решает
# сам диджей (или скрипт перебором effect_selector — имени эффекта в
# ControlObject'ах не существует). Поэтому живьём мы честно крутим микс
# того эффекта, который в слоте уже стоит, а какой именно приём получится
# — определяется набором в юните. В офлайн-рендере эффект известен по
# имени и считается точно (см. demo_render, FX_SENDS/FX_INSERTS).
RAMP_ALIASES = {"fx": "fx_mix"}

# --- живые стемы -------------------------------------------------------
# Порядок обязан совпадать с порядком дорожек в .stem.mp4 (stem_mp4.py,
# STEM_ORDER) и с нумерацией групп в Mixxx: дорожка 1 файла -> Stem1.
# Разъедутся — и «убрать барабаны» уберёт вокал.
STEM_INDEX = {"drums": 1, "bass": 2, "other": 3, "vocals": 4}


def stem_group(deck: str, stem: str) -> str:
    """[Channel1_Stem1] и так далее — так Mixxx называет каналы слоёв
    (src/engine/channels/enginedeck.cpp, getGroupForStem)."""
    idx = STEM_INDEX.get(stem)
    if idx is None:
        raise ValueError(f"Неизвестный слой: {stem}")
    return f"[Channel{DECK_NUMBER[deck]}_Stem{idx}]"


def stem_volume_message(deck: str, stem: str, value_0_1: float) -> list[int]:
    """Громкость одного слоя живьём.

    Идёт SysEx'ом, а не CC: контролов слоёв в XML-биндингах нет и быть не
    может — Mixxx создаёт их по четыре на КАЖДУЮ деку, это ещё 32 адреса,
    и половина сборок без __STEM__ их не имеет вовсе. SysEx достаёт любой
    контрол по имени и молча ничего не делает, если его нет."""
    return sysex_set(stem_group(deck, stem), "volume",
                     max(0.0, min(1.0, float(value_0_1))))


def stem_mute_message(deck: str, stem: str, on: bool) -> list[int]:
    return sysex_set(stem_group(deck, stem), "mute", 1.0 if on else 0.0)


def resolve_ramp_tick(action: str, deck: str, value_0_1: float,
                      params: dict | None = None) -> list[int]:
    action = RAMP_ALIASES.get(action, action)
    if action == "stem_gain":
        stem = (params or {}).get("stem")
        if not stem:
            raise ValueError("stem_gain без params['stem']")
        return stem_volume_message(deck, stem, value_0_1)
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


def sysex_engine(func: str, deck_number: int, activate: int,
                 factor: float = 1.0, rate: float = -10.0) -> list[int]:
    """engine.brake / engine.spinback / engine.softStart на стороне Mixxx.

    Это не setValue: у этих приёмов нет контрола, они реализованы функциями
    скрипт-движка. Скрипт разбирает "E|<функция>|<дека>|<вкл>|<factor>|<rate>"
    и зовёт нужную. factor управляет скоростью выбега (1 — как у вертушки,
    больше — резче), rate у spinback — стартовая скорость назад."""
    if func not in ("brake", "spinback", "softStart"):
        raise ValueError(f"нет такой функции движка: {func}")
    return _sysex(f"E{SYSEX_SEP}{func}{SYSEX_SEP}{int(deck_number)}{SYSEX_SEP}"
                  f"{int(bool(activate))}{SYSEX_SEP}{factor:g}{SYSEX_SEP}{rate:g}")


def decode_sysex(message: list[int]) -> str:
    """Обратная операция — для тестов и логов."""
    if not message or message[0] != SYSEX_START or message[-1] != SYSEX_END:
        raise ValueError("это не SysEx-сообщение")
    return bytes(message[1:-1]).decode("ascii")
