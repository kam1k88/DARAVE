"""Эффекты по сторонам шва: что делают с уходящим и что с входящим.

Зачем отдельный модуль, а не параметры внутри приёмов. Приёмов 63, и у
каждого свои параметры — их 101 разных ключей. Вписать FX туда значило бы
трогать все 63 и размечать все 101. А рендер уже умеет принимать события
СВЕРХ приёма (`extra_events` в demo_render.render_demo) и уже адресует
эффекты по деке: `source` — уходящий, `target` — входящий. Значит FX
ложатся слоем поверх, приём остаётся нетронутым, и словарь эффектов живёт
в одном месте.

Правило одного эффекта на сторону — не техническое ограничение, а копия
физики пульта: у диджея две руки и обычно один FX-юнит на деку. Два
эффекта разом на одной стороне дают кашу, в которой не слышно ни трека,
ни приёма. Поэтому пресет — это ОДИН эффект, а первым в списке всегда
стоит «чисто»: состояние без единого эффекта должно выбираться одним
нажатием, а не перебором по кругу.

Жанр сужает список, а не задаёт его. Гейтер уместен в трансе и почти
всегда лишний в ликвиде, но запрещать его незачем: пресеты своего жанра
идут первыми, остальные следом и помечаются как чужие.
"""

from __future__ import annotations

# Какие эффекты рендер действительно умеет. Список не выдуман: это
# FX_SENDS + FX_INSERTS из demo_render.py. Всё, чего здесь нет
# (гейтер, хорус, шумовой райзер), рендером пока не поддержано — и
# пресетов на это не будет, потому что молча промолчать хуже.
UNITS = ("echo", "reverb", "filter", "flanger", "phaser",
         "wobble", "distortion", "bitcrush")

# Семейства, для которых пресет «родной». Пустой набор — годится всем.
_ALL: frozenset[str] = frozenset()


def _ramp(deck: str, unit: str, at: float, dur: float,
          v_from: float, v_to: float, curve: str = "linear", **params) -> dict:
    """Одно событие эффекта: где начать, сколько вести и куда приехать."""
    return {
        "deck": deck, "action": "fx",
        "beat_offset": float(at), "duration_beats": float(dur),
        "value_from": float(v_from), "value_to": float(v_to),
        "curve": curve,
        "params": {"unit": unit, **params},
    }


# Пресеты уходящего (deck "source") ---------------------------------------
#
# beat_offset считается от начала перехода в долях. Длина перехода
# приходит снаружи (bars * 4), и пресет подстраивается под неё: эффект на
# уходе должен закончиться вместе с треком, а не жить своей жизнью.

def _out_presets(beats: float) -> list[dict]:
    b = max(4.0, float(beats))
    return [
        {"id": "clean", "name": "Clean", "unit": None, "genres": _ALL,
         "why": "без эффектов — как задумано приёмом", "events": []},

        {"id": "echo_tail", "name": "Delay Tail", "unit": "echo", "genres": _ALL,
         "why": "классика ухода: последние такты уезжают в повторы",
         "events": [_ramp("source", "echo", b * 0.6, b * 0.4, 0.0, 0.75,
                          delay_beats=0.75)]},

        {"id": "filter_up", "name": "Filter Up", "unit": "filter", "genres": _ALL,
         "why": "трек истончается и уходит вверх, освобождая низ",
         "events": [_ramp("source", "filter", b * 0.25, b * 0.75, 0.0, 1.0,
                          curve="ease_in")]},

        {"id": "room", "name": "Reverb Out", "unit": "reverb",
         "genres": frozenset({"house", "techno", "trance", "ambient", "dnb"}),
         "why": "хвост растворяется в пространстве, а не обрывается",
         "events": [_ramp("source", "reverb", b * 0.5, b * 0.5, 0.0, 0.6,
                          decay_beats=6.0)]},

        {"id": "flange_out", "name": "Flanger Lift", "unit": "flanger",
         "genres": frozenset({"trance", "house", "dnb", "breaks"}),
         "why": "свистящий подъём перед сбросом",
         "events": [_ramp("source", "flanger", b * 0.4, b * 0.6, 0.0, 0.8,
                          curve="ease_in", rate_beats=4.0, depth_ms=4.0)]},

        {"id": "phase_out", "name": "Phaser", "unit": "phaser",
         "genres": frozenset({"house", "ambient", "breaks"}),
         "why": "мягкое фазовое движение, без агрессии",
         "events": [_ramp("source", "phaser", b * 0.35, b * 0.65, 0.0, 0.7,
                          rate_beats=8.0, stages=4)]},

        {"id": "grind_out", "name": "Overdrive", "unit": "distortion",
         "genres": frozenset({"techno", "dubstep", "hardcore", "hardstyle",
                              "industrial", "phonk"}),
         "why": "грязный уход — энергия не падает, а ломается",
         "events": [_ramp("source", "distortion", b * 0.55, b * 0.45, 0.0, 0.7,
                          drive=9.0)]},

        {"id": "crush_out", "name": "Bitcrusher", "unit": "bitcrush",
         "genres": frozenset({"dubstep", "hardcore", "phonk", "industrial", "trap"}),
         "why": "разрядность падает — трек буквально рассыпается",
         "events": [_ramp("source", "bitcrush", b * 0.6, b * 0.4, 0.0, 0.8,
                          bits=5.0, downsample=10)]},
    ]


# Пресеты входящего (deck "target") ---------------------------------------
#
# Здесь эффекты работают наоборот: не уводят, а вводят. Поэтому почти все
# идут ОТ полного значения К нулю — новый трек выходит из-под эффекта, а
# не заезжает в него.

def _in_presets(beats: float) -> list[dict]:
    b = max(4.0, float(beats))
    return [
        {"id": "clean", "name": "Clean", "unit": None, "genres": _ALL,
         "why": "без эффектов — как задумано приёмом", "events": []},

        {"id": "filter_in", "name": "Filter In", "unit": "filter", "genres": _ALL,
         "why": "новый заходит закрытым и раскрывается к стыку",
         "events": [_ramp("target", "filter", 0.0, b * 0.7, 1.0, 0.0)]},

        {"id": "room_in", "name": "Reverb In", "unit": "reverb",
         "genres": frozenset({"house", "techno", "trance", "ambient", "dnb"}),
         "why": "новый проявляется из пространства",
         "events": [_ramp("target", "reverb", 0.0, b * 0.5, 0.7, 0.0,
                          decay_beats=5.0)]},

        {"id": "echo_in", "name": "Delay In", "unit": "echo",
         "genres": frozenset({"dnb", "dubstep", "techno", "breaks", "trap"}),
         "why": "первые доли двоятся, дальше чисто",
         "events": [_ramp("target", "echo", 0.0, b * 0.35, 0.6, 0.0,
                          delay_beats=0.5)]},

        {"id": "flange_in", "name": "Flanger In", "unit": "flanger",
         "genres": frozenset({"trance", "house", "breaks"}),
         "why": "новый выплывает из гребёнки",
         "events": [_ramp("target", "flanger", 0.0, b * 0.5, 0.8, 0.0,
                          rate_beats=4.0, depth_ms=3.0)]},

        {"id": "wobble_in", "name": "Wobble", "unit": "wobble",
         "genres": frozenset({"dubstep", "dnb", "trap", "phonk"}),
         "why": "качание по темпу — бас заходит волнами",
         "events": [_ramp("target", "wobble", 0.0, b * 0.5, 0.8, 0.0,
                          rate_beats=2.0)]},

        {"id": "crush_in", "name": "Bitcrusher In", "unit": "bitcrush",
         "genres": frozenset({"dubstep", "hardcore", "phonk", "industrial", "trap"}),
         "why": "новый собирается из грязи в чистый звук",
         "events": [_ramp("target", "bitcrush", 0.0, b * 0.4, 0.8, 0.0,
                          bits=5.0, downsample=8)]},
    ]


# --------------------------------------------------------------- ручки
#
# У каждого пресета сила и параметры были ЗАШИТЫ в код. Это правильно как
# отправная точка — пресет и должен звучать осмысленно без настройки — но
# дальше диджей хочет то же самое, что и на пульте: ручку глубины и одну-
# две своих ручки у каждого юнита.
#
# Ручки описаны ДАННЫМИ, а не формой в интерфейсе: интерфейс их рисует,
# сервер применяет, код шва запоминает — все трое читают один список.
# Добавить ручку — дописать строку здесь, и она появится везде.
#
# ВАЖНО: здесь только то, что рендер действительно читает (см. блок
# FX_INSERTS/FX_SENDS в demo_render.py). Ручка, которую никто не слушает,
# хуже отсутствующей: она обещает и врёт.

def _knob(kid, name, lo, hi, step, default, suffix="", hint=""):
    return {"id": kid, "name": name, "min": lo, "max": hi, "step": step,
            "default": default, "suffix": suffix, "hint": hint}


# Сила есть у всех: она масштабирует глубину эффекта, то есть оба конца
# хода. 0% — эффекта нет вовсе, 100% — как задумано пресетом.
_STRENGTH = _knob("strength", "сила", 0, 100, 5, 100, "%",
                  "глубина эффекта; 0 — как «чисто»")

# Доли, а не герцы и не миллисекунды. Эхо и качание обязаны попадать в
# темп, иначе это слышно сразу — потому размер задаётся долей такта.
_BEAT_CHOICES = [[0.25, "1/4"], [0.5, "1/2"], [0.75, "3/4"],
                 [1.0, "1/1"], [1.5, "1½"], [2.0, "2/1"]]

KNOBS: dict[str | None, list[dict]] = {
    None: [],
    "echo": [
        _STRENGTH,
        {**_knob("delay_beats", "доля", 0.25, 2.0, 0.25, 0.75, "",
                 "через сколько повторять; 3/4 — «точка», классика дабового эха"),
         "choices": _BEAT_CHOICES},
        _knob("feedback", "повторы", 10, 85, 5, 45, "%",
              "сколько от повтора возвращается обратно: больше — длиннее хвост"),
    ],
    "reverb": [
        _STRENGTH,
        _knob("decay_beats", "хвост", 1.0, 12.0, 0.5, 4.0, " долей",
              "сколько затухает хвост; повторов в нём не различить — этим реверб и отличается от эха"),
    ],
    "filter": [_STRENGTH],
    "flanger": [
        _STRENGTH,
        {**_knob("rate_beats", "период", 0.5, 8.0, 0.5, 4.0, "",
                 "за сколько долей один проход гребёнки"),
         "choices": [[0.5, "1/2"], [1.0, "1/1"], [2.0, "2/1"],
                     [4.0, "4/1"], [8.0, "8/1"]]},
        _knob("depth_ms", "глубина", 0.5, 10.0, 0.5, 3.5, " мс",
              "разбег задержки: больше — свист резче"),
    ],
    "phaser": [
        _STRENGTH,
        {**_knob("rate_beats", "период", 1.0, 16.0, 1.0, 8.0, "",
                 "за сколько долей один проход"),
         "choices": [[1.0, "1/1"], [2.0, "2/1"], [4.0, "4/1"],
                     [8.0, "8/1"], [16.0, "16/1"]]},
        _knob("stages", "ступени", 2, 12, 2, 4, "",
              "сколько всенаправленных звеньев: больше — плотнее гребёнка"),
    ],
    "wobble": [
        _STRENGTH,
        {**_knob("rate_beats", "доля", 0.25, 4.0, 0.25, 2.0, "",
                 "период качания"),
         "choices": _BEAT_CHOICES},
    ],
    "distortion": [
        _STRENGTH,
        _knob("drive", "драйв", 1.0, 20.0, 1.0, 8.0, "",
              "насколько загоняем в ограничение"),
    ],
    "bitcrush": [
        _STRENGTH,
        _knob("bits", "разрядность", 2.0, 12.0, 1.0, 6.0, " бит",
              "меньше бит — грязнее ступеньки"),
        _knob("downsample", "даунсэмпл", 1, 24, 1, 8, "×",
              "во сколько раз реже берём отсчёты"),
    ],
}

# Ручки, которые едут в params события как есть. strength стоит особняком:
# он масштабирует сам ход, а не параметр юнита.
_PARAM_KNOBS = {"delay_beats", "feedback", "decay_beats", "rate_beats",
                "depth_ms", "stages", "drive", "bits", "downsample"}


def knobs_for(unit: str | None) -> list[dict]:
    """Ручки юнита. Пустой список — настраивать нечего (это «чисто»)."""
    return [dict(k) for k in KNOBS.get(unit, [])]


def _with_knobs(p: dict) -> dict:
    """Пресет плюс его ручки, где default взят ИЗ САМОГО пресета.

    Иначе ползунок при открытии показывал бы не то, что сейчас звучит:
    пресет задал delay_beats=0.5, а ручка рисовала бы общее умолчание
    0.75. Расхождение между тем, что видно, и тем, что слышно, — ровно
    тот сорт ошибки, который потом ищут часами."""
    own: dict = {}
    for e in p.get("events") or []:
        own.update(e.get("params") or {})
    out = []
    for k in knobs_for(p.get("unit")):
        if k["id"] in own:
            k["default"] = float(own[k["id"]])
        elif k["id"] == "feedback":
            k["default"] = 45.0
        out.append(k)
    return {**p, "knobs": out}


def defaults_for(side: str, pid: str | None, beats: float = 64.0) -> dict:
    """Значения ручек пресета по умолчанию — одним словарём."""
    if not pid or pid == "clean":
        return {}
    found = next((p for p in presets(side, beats) if p["id"] == pid), None)
    return {} if not found else {k["id"]: k["default"] for k in found["knobs"]}


def _apply_knobs(events: list[dict], opts: dict | None) -> list[dict]:
    """Наложить ручки на события пресета.

    Сила масштабирует ОБА конца хода, а не только дальний: у пресетов
    ухода ход идёт 0 → полное, у пресетов входа полное → 0, и правило
    «умножить оба» одинаково верно для обоих. Отдельный случай strength=0
    — событий просто не остаётся, и это честно равно «чисто»."""
    opts = opts or {}
    try:
        strength = float(opts.get("strength", 100.0)) / 100.0
    except (TypeError, ValueError):
        strength = 1.0
    strength = max(0.0, min(1.0, strength))
    if strength <= 0.0:
        return []
    out = []
    for e in events:
        ev = {**e, "params": dict(e.get("params") or {})}
        ev["value_from"] = float(ev.get("value_from", 0.0)) * strength
        ev["value_to"] = float(ev.get("value_to", 0.0)) * strength
        for key, val in opts.items():
            if key not in _PARAM_KNOBS:
                continue
            try:
                ev["params"][key] = float(val)
            except (TypeError, ValueError):
                continue
        out.append(ev)
    return out


def presets(side: str, beats: float, genre: str | None = None) -> list[dict]:
    """Пресеты одной стороны: свои для жанра первыми, чужие следом.

    «Чисто» всегда первым и никогда не помечается чужим: состояние без
    эффектов не может быть неуместным."""
    raw = _out_presets(beats) if side == "out" else _in_presets(beats)
    g = (genre or "").strip().lower()
    out = []
    for p in raw:
        native = (not p["genres"]) or (g in p["genres"])
        out.append({**p, "native": bool(native)})
    out = [_with_knobs(p) for p in out]
    head = [p for p in out if p["id"] == "clean"]
    rest = [p for p in out if p["id"] != "clean"]
    rest.sort(key=lambda p: (0 if p["native"] else 1))
    return head + rest


# ------------------------------------------- эффект, который ведёт приём
#
# Из 62 приёмов каталога 22 трогают FX сами, и это ДВА разных случая:
#
#   * юнит назван (FX-01 — фленджер, FX-04 — реверб, SF-01 — эхо…):
#     семь приёмов. Менять юнит нельзя — это и есть приём;
#   * юнит НЕ назван (fx_meta/fx_mix у DNB-05, DNB-07, DNB-25, HS-01,
#     ST-01, ST-08, TT-07 и других): пятнадцать приёмов. На пульте так и
#     правильно — диджей выбрал юнит рукой заранее. Рендер же подставлял
#     им эхо жёстко, и об этом нигде не говорилось.
#
# Панель эффектов обязана различать эти случаи, иначе она предлагает
# ВТОРОЙ эффект поверх первого — на деке юнит один, и спорить им нечем.

_FX_ACTIONS = ("fx", "stem_fx", "fx_meta", "fx_mix")


def technique_fx(events: list[dict] | None) -> dict:
    """Что приём делает с эффектами, по декам.

    Отдаёт {deck: {"units": [...], "free": bool}}:
      * units — юниты, названные самим приёмом;
      * free  — есть «безымянная ручка», то есть юнит выбирает диджей.

    Считается ПО СОБЫТИЯМ, а не по списку идентификаторов: список
    разошёлся бы с каталогом в первый же день."""
    out: dict[str, dict] = {}
    for e in events or []:
        if e.get("action") not in _FX_ACTIONS:
            continue
        deck = e.get("deck")
        if not deck:
            continue
        slot = out.setdefault(deck, {"units": [], "free": False})
        unit = (e.get("params") or {}).get("unit")
        if unit:
            if str(unit) not in slot["units"]:
                slot["units"].append(str(unit))
        else:
            slot["free"] = True
    return out


def unit_options() -> list[dict]:
    """Юниты для «безымянной ручки»: чем именно играть этот приём."""
    names = {"echo": "эхо", "reverb": "реверб", "filter": "фильтр",
             "flanger": "фленджер", "phaser": "фейзер", "wobble": "вобл",
             "distortion": "дисторшн", "bitcrush": "биткрашер"}
    return [{"id": u, "name": names.get(u, u), "knobs": knobs_for(u)}
            for u in UNITS]


def events_for(out_id: str | None, in_id: str | None, beats: float,
               genre_out: str | None = None,
               genre_in: str | None = None,
               out_opts: dict | None = None,
               in_opts: dict | None = None) -> list[dict]:
    """События для пары выбранных пресетов.

    Больше одного эффекта на сторону здесь просто неоткуда взяться: на
    входе по одному идентификатору на сторону. Это и есть то самое
    ограничение — оно в форме данных, а не в проверке, которую можно
    забыть."""
    ev: list[dict] = []
    for side, pid, g, opts in (("out", out_id, genre_out, out_opts),
                               ("in", in_id, genre_in, in_opts)):
        if not pid or pid == "clean":
            continue
        found = next((p for p in presets(side, beats, g) if p["id"] == pid), None)
        if found:
            ev.extend(_apply_knobs(found["events"], opts))
    return ev
