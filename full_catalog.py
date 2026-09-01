"""
Полный каталог контролов Mixxx: поиск, адресация, проверка значений.

Работает поверх mixxx_catalog.json (его собирает build_control_catalog.py
из дампа ControlObject'ов реального Mixxx — 36 841 контрол, из них 2 968
осмысленно управляемых, свёрнутых в 607 шаблонов).

Два уровня доступа, намеренно разные:

  * mixxx_controls.py — 84 «горячих» контрола с человеческими именами
    (eq_low, fx_mix, loop_activate). У них выделены свои Note/CC, они
    идут быстрым путём и участвуют в MixPlan, где важен тайминг.

  * этот модуль — ВСЁ остальное, адресуется по ключу Mixxx
    (parameter7, effect_selector, beatjump_32_forward, loop_scale...)
    и уходит через SysEx. Номера маплить не надо: контролов 38 тысяч, а
    MIDI даёт 128 нот и 128 CC на канал — расписать всё физически негде.

Адресация: ключ + номер того, к чему он относится.
    ("parameter7", unit=1, slot=2)   -> [EffectRack1_EffectUnit1_Effect2] parameter7
    ("beatjump_32_forward", deck="B") -> [Channel2] beatjump_32_forward
    ("volume", sampler=3)             -> [Sampler3] volume
"""
from __future__ import annotations

import json
import re
from difflib import get_close_matches
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "mixxx_catalog.json"

DECK_NUMBER = {"A": 1, "B": 2, "C": 3, "D": 4}


class ControlNotFound(ValueError):
    """Текст предназначен для показа человеку — с подсказкой, что имелось в виду."""


def _load() -> dict:
    if not CATALOG_PATH.exists():
        return {"entries": [], "controls_in_dump": 0, "concrete_controls_covered": 0}
    with open(CATALOG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_DATA = _load()
ENTRIES: list[dict] = _DATA.get("entries", [])

# Один ключ может встречаться в нескольких семействах (volume есть и у
# деки, и у сэмплера, и у микрофона) — храним список.
BY_KEY: dict[str, list[dict]] = {}
for _e in ENTRIES:
    BY_KEY.setdefault(_e["key"], []).append(_e)

FAMILY_ORDER = ("deck", "eq", "quickfx", "stem", "fxunit", "fxslot", "master",
                "library", "autodj", "recording", "sampler", "mic", "aux", "preview")


def stats() -> dict:
    return {
        "in_dump": _DATA.get("controls_in_dump", 0),
        "covered": _DATA.get("concrete_controls_covered", 0),
        "templates": len(ENTRIES),
    }


# Какие адресные аргументы требует семейство — по ним и выбираем, если
# ключ встречается в нескольких. "parameter7" есть и у EQ-рэка (там 16
# слотов параметров заводятся всегда, хоть и не используются), и у слота
# эффекта; переданные unit+slot однозначно говорят, что имелся в виду FX.
FAMILY_BY_PARAMS = {
    frozenset(("u", "s")): ("fxslot",),
    frozenset(("u",)): ("fxunit",),
    frozenset(("n",)): ("deck", "eq", "eqrack", "quickfx", "quickfx_e"),
    # Слой деки адресуется ДВУМЯ числами: дека и номер слоя. Ни одно
    # другое семейство такой пары не требует, поэтому выбор однозначен.
    frozenset(("n", "i")): ("stem",),
    frozenset(("i",)): ("sampler", "mic", "aux", "preview"),
    frozenset(): ("master", "library", "autodj", "recording"),
}


def _pick(key: str, family: str | None, given: frozenset | None = None) -> dict:
    variants = BY_KEY.get(key)
    if not variants:
        near = get_close_matches(key, BY_KEY.keys(), n=5, cutoff=0.6)
        hint = f" Похожие: {', '.join(near)}." if near else ""
        raise ControlNotFound(f"В Mixxx нет контрола '{key}'.{hint}")
    if family:
        for v in variants:
            if v["family"] == family:
                return v
        have = ", ".join(sorted({v["family"] for v in variants}))
        raise ControlNotFound(f"'{key}' не бывает у '{family}'. Бывает у: {have}.")
    if len(variants) == 1:
        return variants[0]

    # Сначала — семейства, подходящие под переданный адрес.
    if given is not None:
        preferred = FAMILY_BY_PARAMS.get(given, ())
        for fam in preferred:
            for v in variants:
                if v["family"] == fam:
                    return v

    # Иначе по общему приоритету (дека важнее сэмплера).
    for fam in FAMILY_ORDER:
        for v in variants:
            if v["family"] == fam:
                return v
    return variants[0]


def resolve(key: str, *, deck: str | None = None, unit: int | None = None,
            slot: int | None = None, index: int | None = None,
            family: str | None = None) -> dict:
    """Ключ + адрес -> конкретные (group, key) Mixxx и метаданные значения."""
    given = frozenset(
        name for name, val in (("n", deck), ("u", unit), ("s", slot), ("i", index))
        if val is not None
    )
    entry = _pick(key, family, given)
    params = entry["params"]
    fmt: dict[str, int] = {}

    if "n" in params:
        if deck is None:
            others = sorted({v["family"] for v in BY_KEY[key]} - {entry["family"]})
            alt = f" (а ещё бывает у: {', '.join(others)} — тогда нужен свой адрес)" if others else ""
            raise ControlNotFound(f"'{key}' относится к деке — укажите деку A/B/C/D{alt}.")
        d = str(deck).upper()
        if d not in DECK_NUMBER:
            raise ControlNotFound(f"Дека '{deck}' не существует, есть A/B/C/D.")
        fmt["n"] = DECK_NUMBER[d]
    if "u" in params:
        if unit is None:
            raise ControlNotFound(f"'{key}' относится к FX-юниту — укажите unit 1..4.")
        if not 1 <= int(unit) <= 4:
            raise ControlNotFound(f"FX-юнит бывает 1..4, а не {unit}.")
        fmt["u"] = int(unit)
    if "s" in params:
        if slot is None:
            raise ControlNotFound(f"'{key}' относится к слоту эффекта — укажите slot 1..4.")
        if not 1 <= int(slot) <= 4:
            raise ControlNotFound(f"Слот эффекта бывает 1..4, а не {slot}.")
        fmt["s"] = int(slot)
    if "i" in params:
        if index is None:
            raise ControlNotFound(f"'{key}' относится к {entry['scope']} — укажите номер.")
        # Нумерация берётся из реального Mixxx: у микрофонов первый —
        # [Microphone] без цифры, поэтому храним суффиксы, а не диапазон.
        vals = entry.get("index_values") or [str(i) for i in range(1, 17)]
        n = int(index)
        if not 1 <= n <= len(vals):
            raise ControlNotFound(
                f"{entry['scope'].capitalize()} бывает 1..{len(vals)}, а не {index}."
            )
        fmt["i"] = vals[n - 1]

    return {
        "group": entry["group_template"].format(**fmt),
        # Ключ тоже может быть шаблоном — см. group_[Channel{n}]_enable у
        # EQ-рэка, где номер деки зашит в имя самого ключа.
        "key": entry["key"].format(**fmt) if "{" in entry["key"] else entry["key"],
        "kind": entry["kind"],
        "min": entry["min"],
        "max": entry["max"],
        "neutral": entry["neutral"],
        "family": entry["family"],
        "scope": entry["scope"],
    }


# Ключи Mixxx английские, а диджей и агент спрашивают по-русски. Без
# этой таблицы поиск "эхо" или "громкость" возвращал бы пустоту.
SYNONYMS = {
    "эхо": "echo", "эффект": "effect", "параметр": "parameter",
    "громкость": "volume", "усиление": "pregain", "гейн": "pregain",
    "фильтр": "filter", "низ": "parameter1", "бас": "parameter1",
    "середина": "parameter2", "верх": "parameter3",
    "луп": "loop", "петля": "loop", "прыжок": "beatjump",
    "темп": "rate", "питч": "rate", "тональность": "key",
    "реверс": "reverse", "запись": "recording", "сэмплер": "sampler",
    "микрофон": "microphone", "наушники": "head", "кроссфейдер": "crossfader",
    "синхрон": "sync", "квантование": "quantize", "метка": "hotcue",
    "старт": "play", "пуск": "play", "стоп": "stop", "выгрузить": "eject",
    "библиотека": "library", "маршрут": "group_", "дека": "channel",
    "выбор": "selector", "выбрать": "selector", "сетка": "beats",
    "битсетка": "beats", "квантайз": "quantize", "слот": "effect",
    "юнит": "effect", "вкл": "enabled", "включить": "enabled",
    "смещение": "translate", "удвоить": "double", "половина": "halve",
    "винил": "vinylcontrol", "интро": "intro", "аутро": "outro",
}

# Имена параметров эффектов ("Время", "Обратная связь") живут в манифесте
# самого эффекта, а не в именах контролов — контрол называется просто
# parameter2. Значит по слову "обратная связь" ключ найти неоткуда: он
# зависит от того, какой эффект сейчас загружен в слот. Эта табличка —
# подсказка для самых ходовых встроенных эффектов Mixxx, чтобы агент не
# гадал. Порядок сверен с интерфейсом Mixxx 2.4.
EFFECT_PARAMS = {
    "Filter":      ["ФНЧ (LPF)", "Q", "ФВЧ (HPF)"],
    "Echo":        ["Время", "Обратная связь", "Ping Pong", "Отправить"],
    "BQ EQ/ISO":   ["Низкие", "Средние", "Высокие"],
    "Reverb":      ["Затухание", "Полоса", "Затухание отправки", "Отправить"],
    "Flanger":     ["Скорость", "Глубина", "Задержка", "Смешение", "Регенерация"],
    "Phaser":      ["Скорость", "Глубина", "Обратная связь", "Диапазон", "Этапы"],
    "Bitcrusher":  ["Глубина бит", "Частота дискретизации"],
    "Distortion":  ["Порог", "Уровень"],
    "Autopan":     ["Период", "Ширина", "Плавность"],
    "Tremolo":     ["Глубина", "Период", "Скважность", "Плавность"],
}


def describe_effect_params() -> str:
    return "\n".join(
        f"  {name:12} " + ", ".join(f"{i+1}={p}" for i, p in enumerate(params))
        for name, params in EFFECT_PARAMS.items()
    )


def search(query: str, limit: int = 25) -> list[dict]:
    """Поиск по каталогу — то, чем ИИ-чат находит нужный контрол, не держа
    в промпте все 607 записей."""
    q = query.strip().lower()
    if not q:
        return []
    words = [SYNONYMS.get(w, w) for w in re.split(r"[\s,]+", q) if w]
    q = SYNONYMS.get(q, q)

    def collect(match_all: bool) -> list:
        found = []
        for e in ENTRIES:
            hay = f"{e['key']} {e['family']} {e['scope']}".lower()
            hit = all(w in hay for w in words) if match_all else any(w in hay for w in words)
            if hit:
                exact = 0 if e["key"].lower() == q else 1
                starts = 0 if e["key"].lower().startswith(q) else 1
                found.append((exact, starts, len(e["key"]), e))
        return found

    # Сначала строго (все слова), потом мягко (любое) — иначе запрос
    # «выбор эффекта» не находит ничего только потому, что слова «выбор»
    # нет в английских ключах.
    scored = collect(True) or collect(False)
    scored.sort(key=lambda r: r[:3])

    if not scored:  # ничего не нашли — предложим похожее по написанию
        near = get_close_matches(q, BY_KEY.keys(), n=limit, cutoff=0.5)
        return [{"key": k, "family": BY_KEY[k][0]["family"],
                 "kind": BY_KEY[k][0]["kind"], "scope": BY_KEY[k][0]["scope"],
                 "needs": BY_KEY[k][0]["params"]} for k in near]

    return [{"key": e["key"], "family": e["family"], "kind": e["kind"],
             "scope": e["scope"], "needs": e["params"],
             "min": e["min"], "max": e["max"], "neutral": e["neutral"]}
            for *_, e in scored[:limit]]


def describe_families() -> str:
    """Короткая карта каталога для системного промпта агента."""
    fams: dict[str, int] = {}
    for e in ENTRIES:
        fams[e["family"]] = fams.get(e["family"], 0) + 1
    human = {
        "deck": "дека (play/cue/луп/hotcue/питч/сетка/винил)",
        "eq": "ручки EQ деки", "eqrack": "EQ-рэк деки",
        "quickfx": "быстрый фильтр деки", "quickfx_e": "эффект быстрого фильтра",
        "fxunit": "FX-юнит: mix, super1, маршрутизация на деки, пресеты цепочки",
        "fxslot": "слот эффекта: enabled, meta, parameter1..16, button_parameter1..16, выбор эффекта",
        "sampler": "сэмплеры 1..16", "mic": "микрофоны", "aux": "линейные входы",
        "master": "мастер/наушники", "library": "библиотека",
        "autodj": "Auto DJ", "recording": "запись", "preview": "дека прослушки",
    }
    lines = []
    for fam in FAMILY_ORDER:
        if fam in fams:
            lines.append(f"  {fam:10} {fams[fam]:4} — {human.get(fam, fam)}")
    return "\n".join(lines)
