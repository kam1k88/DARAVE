"""
Каталог ВСЕХ контролов Mixxx, которыми умеет управлять DARAVE.

Единственный источник истины. Из него выводятся:
  * midi_mapping.py      — какие Note/CC слать
  * make_mixxx_mapping.py — DARAVE-Virtual-Controller.midi.xml
  * agent.py             — список контролов для ИИ-чата (enum в схеме инструмента)
  * midi_console.py      — меню ручного пульта

Раньше таблица нот жила в midi_mapping.py, а привязки — отдельно в XML, и
они разъезжались: половины контролов (ручки EQ, маршрутизация FX, длина
лупа) не было ни в одной из них, отчего «агент крутит только кроссфейдер».
Теперь добавление контрола — одна строка здесь.

ПРО ДИАПАЗОНЫ. В XML <control> с <normal/> линейно растягивает MIDI 0..127
на min..max контрола Mixxx. Поэтому наружу мы всегда отдаём НОРМАЛИЗОВАННОЕ
значение 0..1 (0 = минимум контрола, 1 = максимум), а CC = round(value*127).
Где «ноль» у конкретной ручки — в поле neutral, тоже нормализованном:
    ручки EQ и gain: Mixxx 0..4, штатный уровень 1.0  -> neutral 0.25
    фейдер громкости: 0..1, поднят                    -> neutral 1.0
    питч-фейдер rate: -1..1, без изменения            -> neutral 0.5
Это честнее прежней схемы со скрытым множителем: доступен весь ход ручки,
включая boost выше единицы.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DECKS = ("A", "B", "C", "D")
DECK_CHANNEL = {"A": 0, "B": 1, "C": 2, "D": 3}
DECK_NUMBER = {"A": 1, "B": 2, "C": 3, "D": 4}
MASTER_CHANNEL = 15

# Тип контрола:
#   "range"  — крутилка/фейдер, CC, значение 0..1
#   "button" — кнопка, Note On (нажал-отпустил)
#   "hold"   — удержание: Note On в начале, Note On velocity 0 в конце
#   "loop"   — кнопка с параметром «сколько долей» (params={"beats": N})
#   "hotcue" — кнопка с параметром «номер» (params={"hotcue": N})


@dataclass(frozen=True)
class Control:
    id: str                      # как называет его агент/техника
    label: str                   # человекочитаемо (пульт, подсказки агенту)
    group: str                   # раздел для группировки в UI/подсказках
    kind: str                    # range | button | hold | loop | hotcue
    mixxx_group: str             # шаблон группы Mixxx, {ch}/{u}/{n}
    mixxx_key: str               # шаблон ключа Mixxx
    scope: str = "deck"          # deck | master
    neutral: float = 0.5         # нормализованное «нейтральное» положение (range)
    where: str = ""              # где искать глазами в интерфейсе Mixxx
    midi: int | None = None      # фиксированный номер Note/CC (None -> авто)
    variants: tuple = ()         # для loop/hotcue — набор значений параметра


def _deck(*a, **kw) -> Control:
    return Control(*a, **kw)


# ------------------------------------------------------------------ ДЕКА
# Шаблоны: {ch} = [Channel1], {u} = номер EffectUnit, {n} = номер деки.

DECK_CONTROLS: list[Control] = [
    # --- микшер: ручки ---
    Control("eq_low",  "EQ низ",      "EQ", "range", "[EqualizerRack1_{ch}_Effect1]", "parameter1",
            neutral=0.25, where="ручка LOW в микшере деки", midi=0x11),
    Control("eq_mid",  "EQ середина", "EQ", "range", "[EqualizerRack1_{ch}_Effect1]", "parameter2",
            neutral=0.25, where="ручка MID", midi=0x12),
    Control("eq_high", "EQ верх",     "EQ", "range", "[EqualizerRack1_{ch}_Effect1]", "parameter3",
            neutral=0.25, where="ручка HIGH", midi=0x13),
    # Kill-кнопки живут НЕ на [ChannelN]. Контролов filterLowKill/MidKill/
    # HighKill в Mixxx 2.4 не существует вовсе (проверено по дампу всех
    # 36 841 контрола реальной сборки) — они переехали на EQ-рэк, где
    # являются button_parameter1..3 того же эффекта, что и сами ручки.
    # Пока они были привязаны к несуществующим именам, нажатия уходили в
    # пустоту: Mixxx молча игнорирует неизвестный контрол.
    Control("eq_kill_low",  "Kill низа",      "EQ", "button",
            "[EqualizerRack1_{ch}_Effect1]", "button_parameter1",
            where="кнопка под ручкой LOW", midi=0x19),
    Control("eq_kill_mid",  "Kill середины",  "EQ", "button",
            "[EqualizerRack1_{ch}_Effect1]", "button_parameter2",
            where="кнопка под ручкой MID", midi=0x1A),
    Control("eq_kill_high", "Kill верха",     "EQ", "button",
            "[EqualizerRack1_{ch}_Effect1]", "button_parameter3",
            where="кнопка под ручкой HIGH", midi=0x1B),
    Control("filter_sweep", "Фильтр (QuickEffect)", "Микшер", "range",
            "[QuickEffectRack1_{ch}]", "super1", neutral=0.5,
            where="ручка FILTER/FX деки", midi=0x0A),
    Control("gain_ramp", "Gain (трим канала)", "Микшер", "range", "{ch}", "pregain",
            neutral=0.25, where="ручка GAIN сверху канала", midi=0x0C),
    Control("volume_ramp", "Фейдер канала", "Микшер", "range", "{ch}", "volume",
            neutral=1.0, where="вертикальный фейдер деки", midi=0x0D),
    Control("pfl", "Наушники (CUE канала)", "Микшер", "button", "{ch}", "pfl",
            where="кнопка с наушниками на канале"),
    Control("mute", "Заглушить канал", "Микшер", "button", "{ch}", "mute",
            where="кнопка MUTE канала"),

    # --- темп и тональность ---
    Control("rate_ramp", "Питч-фейдер (темп)", "Темп", "range", "{ch}", "rate",
            neutral=0.5, where="вертикальный питч-фейдер / значение BPM", midi=0x14),
    Control("key_shift", "Тональность (не темп)", "Темп", "range", "{ch}", "pitch_adjust",
            neutral=0.5, where="индикатор KEY", midi=0x10),
    Control("keylock", "Keylock (держать тональность)", "Темп", "button", "{ch}", "keylock",
            where="кнопка с замком возле питч-фейдера"),
    Control("sync", "Sync (разовое выравнивание)", "Темп", "button", "{ch}", "beatsync",
            where="кнопка SYNC мигает", midi=0x16),
    Control("sync_lock", "Sync-lock (держать синхрон)", "Темп", "hold", "{ch}", "sync_enabled",
            where="SYNC горит, пока удерживается", midi=0x21),
    Control("bpm_tap", "Отбить темп вручную", "Темп", "button", "{ch}", "bpm_tap",
            where="значение BPM меняется"),
    Control("beats_faster", "Бит-сетка: быстрее", "Темп", "button", "{ch}", "beats_adjust_faster",
            where="сетка на волне сжимается"),
    Control("beats_slower", "Бит-сетка: медленнее", "Темп", "button", "{ch}", "beats_adjust_slower",
            where="сетка на волне растягивается"),
    Control("beats_translate", "Бит-сетка: привязать к текущей позиции", "Темп", "button",
            "{ch}", "beats_translate_curpos", where="сетка сдвигается под курсор"),

    # --- транспорт ---
    Control("play_toggle", "Play/Pause (переключает)", "Транспорт", "button", "{ch}", "play",
            where="кнопка PLAY", midi=0x14 + 0),  # 0x14 — исторический номер ноты
    Control("play_from_cue", "Играть с cue-точки", "Транспорт", "button", "{ch}", "cue_gotoandplay",
            where="воспроизведение с метки CUE", midi=0x22),
    Control("cue_set", "Cue", "Транспорт", "button", "{ch}", "cue_default",
            where="кнопка CUE", midi=0x15),
    Control("reverse_hold", "Реверс (удержание)", "Транспорт", "hold", "{ch}", "reverseroll",
            where="трек играет назад", midi=0x1E),
    Control("slip", "Slip-режим", "Транспорт", "button", "{ch}", "slip_enabled",
            where="кнопка SLIP"),
    Control("quantize", "Квантование к доле", "Транспорт", "button", "{ch}", "quantize",
            where="кнопка Q на деке"),
    Control("eject", "Выгрузить трек", "Транспорт", "button", "{ch}", "eject",
            where="дека пустеет"),
    Control("load_selected", "Загрузить выбранный в библиотеке трек", "Транспорт", "button",
            "{ch}", "LoadSelectedTrack", where="в деку загружается трек из библиотеки"),

    # --- лупы ---
    Control("loop_activate", "Луп заданной длины", "Луп", "loop", "{ch}", "beatloop_{beats}_activate",
            where="секция LOOP, подсветка петли", variants=(0.5, 1, 2, 4, 8, 16)),
    Control("loop_exit", "Выйти из лупа", "Луп", "button", "{ch}", "loop_exit",
            where="петля гаснет", midi=0x18),
    Control("reloop_toggle", "Вернуться в луп", "Луп", "button", "{ch}", "reloop_toggle",
            where="петля загорается снова", midi=0x1C),
    Control("loop_halve", "Луп вдвое короче", "Луп", "button", "{ch}", "loop_halve",
            where="длина петли делится пополам"),
    Control("loop_double", "Луп вдвое длиннее", "Луп", "button", "{ch}", "loop_double",
            where="длина петли удваивается"),
    Control("loop_in", "Поставить начало лупа", "Луп", "button", "{ch}", "loop_in",
            where="маркер IN на волне"),
    Control("loop_out", "Поставить конец лупа", "Луп", "button", "{ch}", "loop_out",
            where="маркер OUT на волне"),
    Control("beatjump_forward", "Прыжок вперёд", "Луп", "button", "{ch}", "beatjump_forward",
            where="курсор прыгает вперёд"),
    Control("beatjump_backward", "Прыжок назад", "Луп", "button", "{ch}", "beatjump_backward",
            where="курсор прыгает назад"),

    # --- hotcue ---
    Control("hotcue_jump", "Hotcue", "Hotcue", "hotcue", "{ch}", "hotcue_{hotcue}_activate",
            where="кнопки hotcue", variants=(1, 2, 3, 4, 5, 6, 7, 8)),
    Control("hotcue_clear", "Стереть hotcue", "Hotcue", "hotcue", "{ch}", "hotcue_{hotcue}_clear",
            where="кнопка hotcue гаснет", variants=(1, 2, 3, 4, 5, 6, 7, 8)),

    # --- FX-юнит этой деки (EffectUnit N закреплён за декой N) ---
    Control("fx_enable", "Подключить FX-юнит к деке (удержание)", "FX", "hold",
            "[EffectRack1_EffectUnit{u}]", "group_{ch}_enable",
            where="кнопка деки в блоке Effect Unit", midi=0x1F),
    Control("fx_mix", "FX dry/wet (MIX)", "FX", "range", "[EffectRack1_EffectUnit{u}]", "mix",
            neutral=0.0, where="ручка MIX в Effect Unit", midi=0x0F),
    Control("fx_super", "FX суперручка юнита", "FX", "range",
            "[EffectRack1_EffectUnit{u}]", "super1", neutral=0.5,
            where="большая ручка справа в Effect Unit"),
    Control("fx_meta", "FX META (эффект 1)", "FX", "range",
            "[EffectRack1_EffectUnit{u}_Effect1]", "meta", neutral=0.0,
            where="большая ручка первого эффекта", midi=0x0E),
]

# Слоты эффектов внутри юнита: у каждого своя вкл/выкл, META и параметры.
# В интерфейсе это Фильтр / Эхо / BQ EQ|ISO и их крутилки (Время, Обратная
# связь, Ping Pong, Отправить, Низкие/Средние/Высокие частоты и т.п.).
FX_EFFECT_SLOTS = (1, 2, 3)
FX_PARAMS = (1, 2, 3, 4, 5, 6)
FX_BUTTON_PARAMS = (1, 2, 3)


def _fx_slot_controls() -> list[Control]:
    out: list[Control] = []
    for slot in FX_EFFECT_SLOTS:
        g = "[EffectRack1_EffectUnit{u}_Effect%d]" % slot
        out.append(Control(
            f"fx{slot}_enabled", f"Эффект {slot}: вкл/выкл", "FX", "button", g, "enabled",
            where=f"кнопка питания у {slot}-го эффекта",
        ))
        out.append(Control(
            f"fx{slot}_meta", f"Эффект {slot}: META", "FX", "range", g, "meta",
            neutral=0.0, where=f"большая ручка {slot}-го эффекта",
        ))
        for p in FX_PARAMS:
            out.append(Control(
                f"fx{slot}_param{p}", f"Эффект {slot}: параметр {p}", "FX", "range",
                g, f"parameter{p}", neutral=0.5,
                where=f"{p}-я малая ручка {slot}-го эффекта",
            ))
        for p in FX_BUTTON_PARAMS:
            out.append(Control(
                f"fx{slot}_button{p}", f"Эффект {slot}: кнопка {p}", "FX", "button",
                g, f"button_parameter{p}",
                where=f"{p}-я малая кнопка {slot}-го эффекта",
            ))
    return out


DECK_CONTROLS.extend(_fx_slot_controls())


# ---------------------------------------------------------------- МАСТЕР
MASTER_CONTROLS: list[Control] = [
    Control("crossfade", "Кроссфейдер", "Мастер", "range", "[Master]", "crossfader",
            scope="master", neutral=0.5, where="кроссфейдер внизу", midi=0x0B),
    Control("main_gain", "Громкость мастера", "Мастер", "range", "[Master]", "gain",
            scope="master", neutral=0.5, where="ручка MAIN"),
    Control("head_gain", "Громкость наушников", "Мастер", "range", "[Master]", "headGain",
            scope="master", neutral=0.5, where="ручка HEAD"),
    Control("head_mix", "Баланс наушников CUE/MAIN", "Мастер", "range", "[Master]", "headMix",
            scope="master", neutral=0.5, where="ручка MIX возле наушников"),
    Control("head_split", "Раздельные наушники (SPLIT)", "Мастер", "button", "[Master]", "headSplit",
            scope="master", where="кнопка SPLIT"),
    Control("recording_toggle", "Запись микса", "Мастер", "button", "[Recording]", "toggle_recording",
            scope="master", where="индикатор REC", midi=0x1D),
    Control("library_up", "Библиотека: выше", "Библиотека", "button", "[Library]", "MoveUp",
            scope="master", where="выделение в списке треков"),
    Control("library_down", "Библиотека: ниже", "Библиотека", "button", "[Library]", "MoveDown",
            scope="master", where="выделение в списке треков"),
]

ALL_CONTROLS: list[Control] = DECK_CONTROLS + MASTER_CONTROLS
BY_ID: dict[str, Control] = {c.id: c for c in ALL_CONTROLS}

RANGE_KINDS = {"range"}
NOTE_KINDS = {"button", "hold", "loop", "hotcue"}


# ------------------------------------------------- распределение номеров
# Фиксированные номера (midi=...) сохраняют совместимость с уже
# сгенерированным XML; остальным номера выдаются детерминированно —
# по порядку объявления, в первую свободную дырку.

def _allocate(controls: list[Control], kinds: set[str], lo: int, hi: int) -> dict[str, int]:
    taken = {c.midi for c in controls if c.kind in kinds and c.midi is not None}
    assigned = {c.id: c.midi for c in controls if c.kind in kinds and c.midi is not None}
    nxt = lo
    for c in controls:
        if c.kind not in kinds or c.id in assigned:
            continue
        while nxt in taken:
            nxt += 1
        if nxt > hi:
            raise RuntimeError(f"кончились MIDI-номера для {c.id} (диапазон {lo:#x}..{hi:#x})")
        assigned[c.id] = nxt
        taken.add(nxt)
        nxt += 1
    return assigned


# Ноты вариативных контролов (loop/hotcue) занимают по номеру на вариант,
# поэтому им отдельный, заранее известный блок — иначе автораздача
# «съела» бы номера непредсказуемо.
# Блоки не должны пересекаться — внизу файла это проверяется утверждением,
# чтобы «тихая» коллизия (две разные кнопки на одной ноте) не уехала в XML.
HOTCUE_NOTE_BASE = 0x28    # hotcue N = 0x28 + N -> 0x29..0x30
LOOP_NOTE_BASE = 0x38      # 0x38..0x3D — 6 длин лупа
HOTCUE_CLEAR_BASE = 0x50   # 0x51..0x58

VARIANT_BASE = {
    "loop_activate": LOOP_NOTE_BASE,
    "hotcue_jump": HOTCUE_NOTE_BASE,
    "hotcue_clear": HOTCUE_CLEAR_BASE,
}

_variant_reserved: set[int] = set()
for _cid, _base in VARIANT_BASE.items():
    _ctrl = BY_ID[_cid]
    if _ctrl.id == "loop_activate":
        _variant_reserved.update(range(_base, _base + len(_ctrl.variants)))
    else:
        _variant_reserved.update(_base + int(v) for v in _ctrl.variants)

_simple_notes = [c for c in ALL_CONTROLS if c.kind in ("button", "hold")]
_note_taken_pre = {c.midi for c in _simple_notes if c.midi is not None} | _variant_reserved


def _allocate_notes() -> dict[str, int]:
    assigned = {c.id: c.midi for c in _simple_notes if c.midi is not None}
    taken = set(_note_taken_pre)
    nxt = 0x3E  # после блока лупов
    for c in _simple_notes:
        if c.id in assigned:
            continue
        while nxt in taken or (0x50 <= nxt <= 0x58):
            nxt += 1
        if nxt > 0x7F:
            raise RuntimeError(f"кончились ноты для {c.id}")
        assigned[c.id] = nxt
        taken.add(nxt)
        nxt += 1
    return assigned


NOTE_NUMBER = _allocate_notes()
CC_NUMBER = _allocate([c for c in ALL_CONTROLS if c.kind == "range"], RANGE_KINDS, 0x15, 0x7F)


def variant_note(control_id: str, value: float | int) -> int:
    """Нота для конкретного варианта loop/hotcue."""
    ctrl = BY_ID[control_id]
    base = VARIANT_BASE[control_id]
    if control_id == "loop_activate":
        return base + list(ctrl.variants).index(nearest_loop_size(value))
    return base + int(value)


def nearest_loop_size(beats: float) -> float:
    """Mixxx умеет зацикливать только на фиксированных длинах, поэтому
    округляем к ближайшей — в log2-пространстве, а не линейно: 3 доли
    музыкально ближе к 4, чем к 2 (линейно это ничья)."""
    import math
    sizes = list(BY_ID["loop_activate"].variants)
    beats = max(1e-6, float(beats))
    return min(sizes, key=lambda s: abs(math.log2(s) - math.log2(beats)))


def resolve_group_key(ctrl: Control, deck: str, variant: float | int | None = None) -> tuple[str, str]:
    """Шаблон -> реальные (group, key) Mixxx для конкретной деки."""
    ch = f"[Channel{DECK_NUMBER[deck]}]"
    u = DECK_NUMBER[deck]
    # Подставляем ВСЕ плейсхолдеры за один проход: у вариативных контролов
    # ключ вида "beatloop_{beats}_activate" содержит и {beats}, поэтому
    # раздельное .format() падало бы с KeyError на первом же проходе.
    fmt = {"ch": ch, "u": u, "n": u}
    if variant is not None:
        if ctrl.id == "loop_activate":
            fmt["beats"] = f"{nearest_loop_size(variant):g}"
        else:
            fmt["hotcue"] = int(variant)
    return ctrl.mixxx_group.format(**fmt), ctrl.mixxx_key.format(**fmt)


def describe_for_agent() -> str:
    """Компактная таблица для системного промпта ИИ-чата."""
    lines = []
    group = None
    for c in ALL_CONTROLS:
        if c.group != group:
            group = c.group
            lines.append(f"  --- {group} ---")
        if c.kind == "range":
            extra = f"0..1, нейтраль {c.neutral:g}"
        elif c.kind == "loop":
            extra = "value = длина в долях: " + "/".join(f"{v:g}" for v in c.variants)
        elif c.kind == "hotcue":
            extra = "value = номер 1..8"
        elif c.kind == "hold":
            extra = "value = сколько секунд удерживать"
        else:
            extra = "кнопка"
        scope = "мастер" if c.scope == "master" else "дека"
        lines.append(f"  {c.id:20} {c.label:38} [{scope}, {extra}]")
    return "\n".join(lines)


# --------------------------------------------------- проверка целостности
# Выполняется при импорте: две разные кнопки на одной ноте — это тихий баг,
# который проявился бы только «крутил одно, сработало другое».

def _selfcheck() -> None:
    slots: dict[tuple[str, int], list[str]] = {}
    for c in ALL_CONTROLS:
        if c.kind == "range":
            slots.setdefault(("cc", CC_NUMBER[c.id]), []).append(c.id)
        elif c.kind in ("button", "hold"):
            slots.setdefault(("note", NOTE_NUMBER[c.id]), []).append(c.id)
        else:
            for v in c.variants:
                slots.setdefault(("note", variant_note(c.id, v)), []).append(f"{c.id}[{v:g}]")
    clashes = {k: v for k, v in slots.items() if len(v) > 1}
    if clashes:
        detail = "; ".join(f"{k[0]} {k[1]:#04x}: {', '.join(v)}" for k, v in clashes.items())
        raise RuntimeError(f"Коллизия MIDI-номеров в каталоге контролов: {detail}")
    if len(BY_ID) != len(ALL_CONTROLS):
        raise RuntimeError("Повторяющиеся id контролов в каталоге")


_selfcheck()


# ====================================================================
# FX: полная раскладка на ОТДЕЛЬНЫХ MIDI-каналах
# ====================================================================
# Почему не через SysEx: SysEx-мост зависит от того, как конкретная сборка
# Mixxx маршрутизирует системные сообщения в скрипт, и проверить это в
# песочнице нечем. Прямые CC/Note — тот же механизм, которым уже работают
# ручки EQ, то есть заведомо рабочий. FX — главное, ради чего всё
# затевалось, поэтому он не должен зависеть от непроверяемого пути.
#
# Места хватает: каналы 0-3 заняты деками, 15 — мастером, а 4-7 свободны и
# отдаются под FX-юниты 1-4. На канал приходится 128 CC и 128 нот — этого
# с запасом хватает на 4 слота по 16 параметров и 16 кнопок.
#
# Раскладка внутри канала юнита (детерминированная, без «дырок»):
#   CC   (s-1)*16 + (p-1)   параметр p слота s          -> 0..63
#   CC   64 + (s-1)         META слота s                -> 64..67
#   CC   68                 MIX юнита (dry/wet)
#   CC   69                 SUPER юнита
#   CC   70 + (s-1)         выбор эффекта в слоте s     -> 70..73
#   нота (s-1)*16 + (p-1)   кнопка-параметр p слота s   -> 0..63
#   нота 64 + (s-1)         вкл/выкл слота s            -> 64..67
#   нота 68 + (s-1)         следующий эффект в слоте s  -> 68..71
#   нота 72 + (s-1)         предыдущий эффект в слоте s -> 72..75
#   нота 76                 вкл/выкл всего юнита
#   нота 80 + (d-1)         подключить юнит к деке d    -> 80..83

FX_UNIT_CHANNEL = {1: 4, 2: 5, 3: 6, 4: 7}
FX_SLOTS = (1, 2, 3, 4)
FX_PARAM_COUNT = 16

FX_CC_META_BASE = 64
FX_CC_MIX = 68
FX_CC_SUPER = 69
FX_CC_SELECTOR_BASE = 70

FX_NOTE_ENABLED_BASE = 64
FX_NOTE_NEXT_BASE = 68
FX_NOTE_PREV_BASE = 72
FX_NOTE_UNIT_ENABLED = 76
FX_NOTE_ROUTE_BASE = 80


def fx_bindings() -> list[dict]:
    """Все FX-привязки: (unit, канал, статус, номер, группа Mixxx, ключ, тип).

    Один источник и для XML-генератора, и для резолва в midi_mapping —
    разъехаться не могут."""
    out: list[dict] = []
    for unit, channel in FX_UNIT_CHANNEL.items():
        ug = f"[EffectRack1_EffectUnit{unit}]"

        out.append({"unit": unit, "channel": channel, "status": 0xB0 | channel,
                    "number": FX_CC_MIX, "group": ug, "key": "mix",
                    "kind": "range", "label": f"FX{unit}: dry/wet"})
        out.append({"unit": unit, "channel": channel, "status": 0xB0 | channel,
                    "number": FX_CC_SUPER, "group": ug, "key": "super1",
                    "kind": "range", "label": f"FX{unit}: суперручка"})
        out.append({"unit": unit, "channel": channel, "status": 0x90 | channel,
                    "number": FX_NOTE_UNIT_ENABLED, "group": ug, "key": "enabled",
                    "kind": "toggle", "label": f"FX{unit}: вкл/выкл юнита"})
        for deck_no in (1, 2, 3, 4):
            out.append({"unit": unit, "channel": channel, "status": 0x90 | channel,
                        "number": FX_NOTE_ROUTE_BASE + deck_no - 1, "group": ug,
                        "key": f"group_[Channel{deck_no}]_enable", "kind": "toggle",
                        "label": f"FX{unit}: подключить к деке {deck_no}"})

        for slot in FX_SLOTS:
            sg = f"[EffectRack1_EffectUnit{unit}_Effect{slot}]"
            for p in range(1, FX_PARAM_COUNT + 1):
                out.append({"unit": unit, "channel": channel, "status": 0xB0 | channel,
                            "number": (slot - 1) * 16 + (p - 1), "group": sg,
                            "key": f"parameter{p}", "kind": "range",
                            "label": f"FX{unit}.{slot}: параметр {p}"})
                out.append({"unit": unit, "channel": channel, "status": 0x90 | channel,
                            "number": (slot - 1) * 16 + (p - 1), "group": sg,
                            "key": f"button_parameter{p}", "kind": "toggle",
                            "label": f"FX{unit}.{slot}: кнопка {p}"})
            out.append({"unit": unit, "channel": channel, "status": 0xB0 | channel,
                        "number": FX_CC_META_BASE + slot - 1, "group": sg, "key": "meta",
                        "kind": "range", "label": f"FX{unit}.{slot}: META"})
            out.append({"unit": unit, "channel": channel, "status": 0xB0 | channel,
                        "number": FX_CC_SELECTOR_BASE + slot - 1, "group": sg,
                        "key": "effect_selector", "kind": "range",
                        "label": f"FX{unit}.{slot}: выбор эффекта"})
            out.append({"unit": unit, "channel": channel, "status": 0x90 | channel,
                        "number": FX_NOTE_ENABLED_BASE + slot - 1, "group": sg,
                        "key": "enabled", "kind": "toggle",
                        "label": f"FX{unit}.{slot}: вкл/выкл слота"})
            out.append({"unit": unit, "channel": channel, "status": 0x90 | channel,
                        "number": FX_NOTE_NEXT_BASE + slot - 1, "group": sg,
                        "key": "next_effect", "kind": "button",
                        "label": f"FX{unit}.{slot}: следующий эффект"})
            out.append({"unit": unit, "channel": channel, "status": 0x90 | channel,
                        "number": FX_NOTE_PREV_BASE + slot - 1, "group": sg,
                        "key": "prev_effect", "kind": "button",
                        "label": f"FX{unit}.{slot}: предыдущий эффект"})
    return out


# (group, key) -> (status, number) для быстрого резолва
FX_BY_TARGET = {(b["group"], b["key"]): b for b in fx_bindings()}


def _fx_selfcheck() -> None:
    seen: dict[tuple[int, int], str] = {}
    for b in fx_bindings():
        sig = (b["status"], b["number"])
        if sig in seen:
            raise RuntimeError(
                f"Коллизия FX-раскладки на 0x{b['status']:02X}/{b['number']}: "
                f"{seen[sig]} и {b['group']} {b['key']}"
            )
        seen[sig] = f"{b['group']} {b['key']}"
        if not 0 <= b["number"] <= 127:
            raise RuntimeError(f"номер вне 0..127: {b}")


_fx_selfcheck()
