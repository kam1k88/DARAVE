"""
Строит каталог контролов Mixxx из дампа ControlObject'ов.

Mixxx умеет выгрузить ВСЕ свои контролы в CSV (co_dump_*.csv в папке
настроек). Это точный, версия-в-версию список того, что есть в конкретной
сборке — надёжнее документации и надёжнее ручного списка. У пользователя
там 37 989 строк.

Брать всё подряд нельзя и не нужно: большая часть — это либо状态
(read-only: *_loaded, *_indicator, num_*, playposition, beat_distance),
либо микроварианты одного и того же (*_up_small, *_set_zero,
*_minus_toggle). Здесь мы отбираем то, что диджей реально «крутит и
нажимает», и раскладываем по типам и человеческим названиям.

Результат — mixxx_catalog.json, который читает mixxx_controls.py.

Запуск:
    python build_control_catalog.py --dump co_dump_*.csv --out mixxx_catalog.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict

# --- что ОТБРАСЫВАЕМ ---------------------------------------------------
# Только чтение / служебное: выставлять их бессмысленно.
READONLY_SUFFIX = (
    "_loaded", "_type", "_status", "_indicator", "_position", "_color",
    "_enabled_status", "_possible", "_configured", "_active", "_distance",
    "_next", "_prev", "_closest", "_samplerate", "_samples",
)
READONLY_EXACT = {
    "loaded", "loaded_effect", "loaded_chain_preset", "loop_anchor",
    "playposition", "duration", "end", "end_of_track", "file_bpm", "file_key",
    "local_bpm", "track_loaded", "peak_indicator", "peak_indicator_left",
    "peak_indicator_right", "VuMeter", "VuMeterL", "VuMeterR", "jog", "scratch2",
    "beat_active", "beat_closest", "beat_next", "beat_prev", "bpm_tap_after",
    "play_indicator", "play_latched", "cue_point", "loop_start_position",
    "loop_end_position", "waveform_zoom", "track_color", "stars",
}
READONLY_PREFIX = ("num_", "beat_distance", "vinylcontrol_signal", "VuMeter", "vu_meter")
# Микроварианты — шум, который загромождает каталог, не добавляя
# возможностей (значение и так выставляется напрямую).
NOISE_SUFFIX = (
    "_up", "_down", "_up_small", "_down_small", "_set_default", "_set_one",
    "_set_zero", "_set_minus_one", "_minus_toggle", "_toggle",
    "_link_type", "_link_inverse",
)


def is_settable(key: str) -> bool:
    if key in READONLY_EXACT:
        return False
    if key.startswith(READONLY_PREFIX):
        return False
    if key.endswith(READONLY_SUFFIX):
        return False
    if key.endswith(NOISE_SUFFIX):
        return False
    return True


# --- какие группы нас интересуют и как их назвать ----------------------
GROUP_RULES = [
    (re.compile(r"^\[Channel(\d)\]$"),                          "deck",      "Дека {0}"),
    (re.compile(r"^\[EqualizerRack1_\[Channel(\d)\]_Effect1\]$"), "eq",       "EQ деки {0}"),
    (re.compile(r"^\[EqualizerRack1_\[Channel(\d)\]\]$"),        "eqrack",    "EQ-рэк деки {0}"),
    (re.compile(r"^\[QuickEffectRack1_\[Channel(\d)\]\]$"),      "quickfx",   "Фильтр деки {0}"),
    (re.compile(r"^\[QuickEffectRack1_\[Channel(\d)\]_Effect1\]$"), "quickfx_e", "Фильтр деки {0} (эффект)"),
    (re.compile(r"^\[EffectRack1_EffectUnit(\d)\]$"),            "fxunit",    "FX-юнит {0}"),
    (re.compile(r"^\[EffectRack1_EffectUnit(\d)_Effect(\d)\]$"), "fxslot",    "FX-юнит {0}, слот {1}"),
    (re.compile(r"^\[Sampler(\d+)\]$"),                          "sampler",   "Сэмплер {0}"),
    (re.compile(r"^\[Microphone(\d*)\]$"),                       "mic",       "Микрофон {0}"),
    (re.compile(r"^\[Auxiliary(\d)\]$"),                         "aux",       "Aux {0}"),
    (re.compile(r"^\[Master\]$"),                                "master",    "Мастер"),
    (re.compile(r"^\[Main\]$"),                                  "master",    "Мастер"),
    (re.compile(r"^\[Library\]$"),                               "library",   "Библиотека"),
    (re.compile(r"^\[AutoDJ\]$"),                                "autodj",    "Auto DJ"),
    (re.compile(r"^\[Recording\]$"),                             "recording", "Запись"),
    (re.compile(r"^\[PreviewDeck(\d)\]$"),                       "preview",   "Прослушка {0}"),
]

# Из огромных семейств оставляем разумный срез: hotcue 1..8 (а не 36),
# длины лупа/прыжка — музыкально осмысленные (а не 0.03125 и 512).
# Полнота тут не в том, чтобы вывалить всё подряд: 36 hotcue по 14
# вариантов каждый — это 500 строк, в которых невозможно ничего найти, а
# делают они одно и то же. Всё, что не попало в каталог, ВСЁ РАВНО
# доступно по имени через SysEx — каталог это удобный индекс, а не забор.
HOTCUE_KEEP = tuple(str(n) for n in range(1, 9))
SIZE_KEEP = ("0.5", "1", "2", "4", "8", "16", "32")
HOTCUE_ACTIONS = ("activate", "set", "clear", "goto", "gotoandplay", "gotoandstop")

# У сэмплеров и прослушки полная дечная раскладка (по 734 ключа на штуку,
# 16 сэмплеров = 11 тысяч строк) — берём рабочий минимум.
SAMPLER_KEYS = {
    "play", "cue_default", "cue_gotoandplay", "cue_set", "start_play", "start_stop",
    "stop", "eject", "volume", "pregain", "pfl", "mute", "orientation", "rate",
    "keylock", "sync_enabled", "beatsync", "quantize", "repeat", "reverse",
    "LoadSelectedTrack", "LoadSelectedTrackAndPlay", "loop_enabled", "loop_exit",
    "beatloop_1_activate", "beatloop_2_activate", "beatloop_4_activate",
    "beatloop_8_activate", "hotcue_1_activate", "hotcue_2_activate",
    "hotcue_3_activate", "hotcue_4_activate",
}


def keep_family(key: str, family: str = "") -> bool:
    if family in ("sampler", "preview"):
        return key in SAMPLER_KEYS
    m = re.match(r"^hotcue_(\d+)_(.+)$", key)
    if m:
        return m.group(1) in HOTCUE_KEEP and m.group(2) in HOTCUE_ACTIONS
    # beatloop_<N> без суффикса и beatloop_<N>_enabled — это состояние
    # текущей петли, а не команда; размеры вне SIZE_KEEP (0.03125, 512)
    # музыкального смысла не имеют. То же для loop_move_<N>.
    m = re.match(r"^(?:beatloop|beatlooproll|beatjump|loop_move)_([\d.]+)(?:_(.+))?$", key)
    if m:
        size, action = m.group(1), m.group(2)
        if size not in SIZE_KEEP:
            return False
        return action in ("activate", "forward", "backward", "toggle")
    return True


# --- тип контрола по имени --------------------------------------------
BUTTONISH = re.compile(
    r"(_activate|_activatecue|_activateloop|_clear|_set$|_goto|_gotoandplay|"
    r"_gotoandstop|_gotoandloop|_setcue|_setloop|_cueloop|_forward|_backward|"
    r"^beatsync|^bpm_tap|^beats_|^eject$|^start|^stop$|^back$|^fwd$|"
    r"^Load|^Clone|_toggle$|^next_|^prev_|^clear$|^shuffle|^skip|^fade|^add_random|"
    r"^loop_in$|^loop_out$|^loop_exit$|^reloop|^loop_halve$|^loop_double$|"
    r"^Move|^GoTo|^Scroll|^Select|^Toggle|^Choose|^Auto)"
)
TOGGLEISH = re.compile(
    r"^(play|keylock|quantize|slip_enabled|sync_enabled|pfl|mute|passthrough|"
    r"repeat|loop_enabled|enabled|vinylcontrol_enabled|vinylcontrol_cueing|"
    r"talkover|headSplit|maximize_library|show_|group_\[.*\]_enable|"
    r"filter(Low|Mid|High)Kill|star_)"
)

# Диапазоны Mixxx (min, max, нейтраль) для тех ручек, где 0..1 не годится.
RANGES = {
    "pregain": (0.0, 4.0, 1.0), "parameter1": (0.0, 4.0, 1.0),
    "parameter2": (0.0, 4.0, 1.0), "parameter3": (0.0, 4.0, 1.0),
    "rate": (-1.0, 1.0, 0.0), "pitch": (-6.0, 6.0, 0.0),
    "pitch_adjust": (-3.0, 3.0, 0.0), "crossfader": (-1.0, 1.0, 0.0),
    "balance": (-1.0, 1.0, 0.0), "headMix": (-1.0, 1.0, 0.0),
    "orientation": (0.0, 2.0, 1.0), "volume": (0.0, 1.0, 1.0),
    "gain": (0.0, 1.0, 1.0), "headGain": (0.0, 1.0, 1.0),
}
# EQ-рэк использует parameter1..3 с диапазоном 0..4, а вот у эффектов
# parameterN обычно 0..1 — различаем по семейству группы.
FX_PARAM_RANGE = (0.0, 1.0, 0.5)


def classify(family: str, key: str) -> tuple[str, float, float, float]:
    """-> (kind, min, max, neutral)"""
    if TOGGLEISH.match(key):
        return "toggle", 0.0, 1.0, 0.0
    if BUTTONISH.search(key):
        return "button", 0.0, 1.0, 0.0
    if family in ("fxslot", "quickfx_e") and re.match(r"^(parameter|button_parameter)\d+$", key):
        if key.startswith("button_"):
            return "toggle", 0.0, 1.0, 0.0
        lo, hi, nu = FX_PARAM_RANGE
        return "range", lo, hi, nu
    lo, hi, nu = RANGES.get(key, (0.0, 1.0, 0.5))
    return "range", lo, hi, nu


# Шаблоны групп: один и тот же ключ на 4 деках / 4 юнитах / 16 сэмплерах —
# это ОДНА запись каталога с подстановкой, а не 4 (или 16) копий. Иначе
# каталог раздувается вчетверо, а адресовать всё равно удобнее как
# "ключ + номер деки".
TEMPLATE = {
    "deck":      ("[Channel{n}]",                              ("n",), "дека"),
    "eq":        ("[EqualizerRack1_[Channel{n}]_Effect1]",     ("n",), "дека"),
    "eqrack":    ("[EqualizerRack1_[Channel{n}]]",             ("n",), "дека"),
    "quickfx":   ("[QuickEffectRack1_[Channel{n}]]",           ("n",), "дека"),
    "quickfx_e": ("[QuickEffectRack1_[Channel{n}]_Effect1]",   ("n",), "дека"),
    "fxunit":    ("[EffectRack1_EffectUnit{u}]",               ("u",), "FX-юнит"),
    "fxslot":    ("[EffectRack1_EffectUnit{u}_Effect{s}]",     ("u", "s"), "FX-юнит/слот"),
    "sampler":   ("[Sampler{i}]",                              ("i",), "сэмплер"),
    "mic":       ("[Microphone{i}]",                           ("i",), "микрофон"),
    "aux":       ("[Auxiliary{i}]",                            ("i",), "aux"),
    "preview":   ("[PreviewDeck{i}]",                          ("i",), "прослушка"),
    "master":    ("[Master]",                                  (), ""),
    "library":   ("[Library]",                                 (), ""),
    "autodj":    ("[AutoDJ]",                                  (), ""),
    "recording": ("[Recording]",                               (), ""),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    seen: set[tuple[str, str]] = set()
    with open(args.dump, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^(\[.*?\]),([^,]+),", line.rstrip("\n"))
            if m:
                seen.add((m.group(1), m.group(2)))

    # (family, key) -> запись; сколько конкретных групп её порождают
    templates: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    concrete = 0
    for group, key in sorted(seen):
        for pattern, family, _label in GROUP_RULES:
            if not pattern.match(group):
                continue
            if family not in TEMPLATE:
                break
            if not is_settable(key) or not keep_family(key, family):
                break
            concrete += 1

            # У EQ-рэка и быстрого фильтра ключ маршрутизации содержит НОМЕР
            # СВОЕЙ ЖЕ деки: у [EqualizerRack1_[Channel2]] существует только
            # group_[Channel2]_enable. Если оставить ключ константой, шаблон
            # породит [EqualizerRack1_[Channel3]] group_[Channel2]_enable —
            # такого контрола в Mixxx нет. Поэтому номер в ключе тоже
            # становится подстановкой. У FX-юнита иначе: юнит 1 может
            # маршрутизироваться на любую деку, там ключ действительно
            # независим и его не трогаем.
            key_out = key
            if family in ("eqrack", "quickfx", "eq", "quickfx_e"):
                key_out = re.sub(r"\[Channel\d\]", "[Channel{n}]", key)

            sig = (family, key_out)
            if sig not in templates:
                kind, lo, hi, neutral = classify(family, key)
                tmpl, params, scope = TEMPLATE[family]
                templates[sig] = {
                    "family": family, "key": key_out, "group_template": tmpl,
                    "params": list(params), "scope": scope,
                    "kind": kind, "min": lo, "max": hi, "neutral": neutral,
                    "instances": 0,
                }
            templates[sig]["instances"] += 1
            # Запоминаем РЕАЛЬНЫЙ диапазон номеров: сэмплеров 16, а
            # Auxiliary всего 4 и микрофонов 4. Без этого "сэмплер 9"
            # прошёл бы валидацию и ушёл в никуда.
            if TEMPLATE[family][1] == ("i",):
                # Нумерация в Mixxx не всегда 1..N: первый микрофон — это
                # [Microphone] БЕЗ номера, а дальше [Microphone2..4].
                # Поэтому запоминаем фактические суффиксы, а не диапазон.
                idx = re.search(r"(\d*)\]$", group)
                suffix = idx.group(1) if idx else ""
                vals = templates[sig].setdefault("index_values", [])
                if suffix not in vals:
                    vals.append(suffix)
            break

    entries = list(templates.values())
    for e in entries:
        if "index_values" in e:
            e["index_values"] = sorted(e["index_values"], key=lambda v: int(v or 1))
    payload = {
        "source": args.dump.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
        "controls_in_dump": len(seen),
        "concrete_controls_covered": concrete,
        "entries": entries,
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    fams = OrderedDict()
    for e in entries:
        fams[e["family"]] = fams.get(e["family"], 0) + 1
    print(f"контролов в дампе:        {len(seen)}", file=sys.stderr)
    print(f"покрыто конкретных:       {concrete}", file=sys.stderr)
    print(f"записей каталога:         {len(entries)}  (шаблоны с подстановкой)", file=sys.stderr)
    for fam, n in fams.items():
        inst = sum(e["instances"] for e in entries if e["family"] == fam)
        print(f"   {fam:11} {n:4} шаблонов -> {inst:5} конкретных", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
