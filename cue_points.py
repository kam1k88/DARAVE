"""
cue_points.py — именованные точки трека: интро, билд, дроп, брейкдаун,
аутро, и места, где чисто ложится луп.

## Зачем отдельно от mix_points.py

`mix_points.py` отвечает на вопрос «где свести ЭТУ пару треков за столько
секунд» — его кандидаты зависят от хронометража сета, от техники и от
второго трека. Здесь другое: разметка ОДНОГО трека, одинаковая всегда.
Такую разметку можно посчитать один раз, показать в интерфейсе, выгрузить
в Mixxx горячими метками и брать из неё точки для любого перехода.

## Две вещи, которые здесь сделаны иначе

**1. Фразовая сетка считается от первой доли трека, а не от нуля файла.**
В `mix_points._snap_all_to_phrase` сетка фраз строится от начала файла, и
в самом коде стоит замечание, что это уводит точку на пол-фразы. Так и
есть: у релиза почти никогда нет ровного нуля — там тишина, всплеск, шум
винила, и «такт 8» от нуля файла попадает в середину музыкальной фразы.

Здесь якорь — первая доля, где реально вступают барабаны
(`drum_map.drums_start`, измеренная по низу спектра). От неё
отсчитываются такты и фразы, и все метки садятся на начало фразы по
музыке, а не по файлу. Это и есть «адаптивные CUE»: сетка подстраивается
под трек, а не трек под сетку.

**2. Метка называется ролью, а не временем.** Было «дроп на 0:37» —
время впереди, роль позади. Диджей выбирает не секунду, он выбирает
место: первый бит, конец интро, билд, дроп, брейкдаун. Поэтому имя точки
— это её роль и место в аранжировке («второй дроп», «брейкдаун после
первого дропа»), а время идёт следом как уточнение.

## Что считается лупом

Луп «ложится чисто», если он начинается на границе фразы, барабаны играют
всю его длину и уровень низа внутри не проваливается: иначе луп
периодически выключает бас, и это слышно как заикание. Длины — только
степени двойки в тактах (4/8/16), потому что фразовая кратность на другом
не сохраняется.
"""
from __future__ import annotations

PHRASE_BARS = 8
LOOP_BARS = (4, 8, 16)

# Роли точек и их порядок в списке при равном времени.
KIND_TITLE = {
    "first_beat": "первый бит",
    "intro_end": "конец интро",
    "build": "билд перед дропом",
    "drop": "дроп",
    "breakdown": "брейкдаун",
    "pit": "яма перед дропом",
    "phrase": "начало фразы",
    "outro": "аутро",
    "loop": "луп",
}

KIND_HINT = {
    "first_beat": "первая доля, где вступают барабаны — отсюда трек можно заводить резом",
    "intro_end": "интро кончилось, дальше идёт бит",
    "build": "последняя фраза перед дропом: напряжение растёт, низ вот-вот вернётся",
    "drop": "низ вернулся после просадки — то, что слышно как дроп",
    "breakdown": "барабаны ушли — новая дека входит почти без шва",
    "pit": "короткая просадка низа перед дропом, вдох на такт-другой",
    "phrase": "чистая граница фразы",
    "outro": "хвост трека, здесь он уже отыграл своё",
    "loop": "здесь луп ложится чисто: барабаны играют всю длину, низ не проваливается",
}

# В какие хоткеи Mixxx выгружать какие роли. Порядок — тот, в котором
# диджей их нажимает вживую, а не порядок по времени.
HOTCUE_LAYOUT = ("first_beat", "build", "drop", "breakdown", "drop2", "outro")


def _fmt(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


# Порядковые в трёх падежах. Не украшательство: подпись читают на бегу
# посреди сета, и «билд перед второй дропом» спотыкает глаз ровно там,
# где он должен скользить.
_ORDINALS = {
    1: ("первый", "первым", "первого"),
    2: ("второй", "вторым", "второго"),
    3: ("третий", "третьим", "третьего"),
    4: ("четвёртый", "четвёртым", "четвёртого"),
    5: ("пятый", "пятым", "пятого"),
    6: ("шестой", "шестым", "шестого"),
    7: ("седьмой", "седьмым", "седьмого"),
    8: ("восьмой", "восьмым", "восьмого"),
}
_CASE = {"nom": 0, "ins": 1, "gen": 2}


def _ordinal(n: int, case: str = "nom") -> str:
    forms = _ORDINALS.get(n)
    if not forms:
        return {"nom": f"{n}-й", "ins": f"{n}-м", "gen": f"{n}-го"}[case]
    return forms[_CASE[case]]


def _bars_word(n: int) -> str:
    """«1 такт», «4 такта», «16 тактов»."""
    if 11 <= n % 100 <= 14:
        return "тактов"
    return {1: "такт", 2: "такта", 3: "такта", 4: "такта"}.get(n % 10, "тактов")


class PhraseGrid:
    """Сетка тактов и фраз, привязанная к первой доле трека."""

    def __init__(self, bpm: float, anchor_seconds: float = 0.0,
                 duration: float = 0.0):
        self.bpm = float(bpm) if bpm and bpm > 20 else 172.0
        self.bar = 60.0 / self.bpm * 4
        self.phrase = self.bar * PHRASE_BARS
        self.anchor = max(0.0, float(anchor_seconds))
        self.duration = float(duration or 0.0)

    def bar_index(self, t: float) -> int:
        return int(round((float(t) - self.anchor) / self.bar))

    def phrase_index(self, t: float) -> int:
        return int((float(t) - self.anchor) // self.phrase) + 1

    def snap(self, t: float, mode: str = "nearest") -> float:
        """Ближайшее начало фразы. mode: nearest | prev | next."""
        import math

        k = (float(t) - self.anchor) / self.phrase
        if mode == "prev":
            k = math.floor(k + 1e-6)
        elif mode == "next":
            k = math.ceil(k - 1e-6)
        else:
            k = round(k)
        out = self.anchor + k * self.phrase
        if self.duration:
            out = min(out, max(0.0, self.duration - self.bar))
        return max(0.0, out)


def _grid(track: dict, bpm: float | None = None) -> PhraseGrid:
    structure = track.get("structure") or {}
    dmap = structure.get("drum_map") or {}
    bpm = float(bpm or track.get("bpm") or 172.0)
    # Якорь — первая доля с барабанами. Если карты барабанов нет,
    # честно падаем на ноль файла и помечаем это (см. cues_for_track).
    anchor = float(dmap.get("drums_start") or 0.0)
    return PhraseGrid(bpm, anchor, float(track.get("duration_seconds") or 0.0))


def _levels(track: dict) -> tuple[list[float], float]:
    emap = (track.get("structure") or {}).get("energy_map") or {}
    return [float(x) for x in (emap.get("level") or [])], float(emap.get("bar_seconds") or 0.0)


def _loop_is_clean(track: dict, grid: PhraseGrid, start: float, bars: int) -> bool:
    """Луп ложится чисто: барабаны играют всю длину, низ не проваливается."""
    dmap = (track.get("structure") or {}).get("drum_map") or {}
    if dmap:
        try:
            import beatgrid

            if not beatgrid.drums_at(dmap, start, bars):
                return False
        except Exception:
            pass
    level, bar_sec = _levels(track)
    if not level or bar_sec <= 0:
        return True
    i0 = int(round(start / bar_sec))
    window = level[i0:i0 + bars]
    if len(window) < bars:
        return False
    # Провал ниже 0.45 от опорного уровня — это и есть «низ ушёл»
    # (тот же порог, по которому energy_map находит брейкдауны).
    return min(window) >= 0.45


def cues_for_track(track: dict, bpm: float | None = None,
                   max_points: int = 24) -> dict:
    """Именованная разметка трека.

    Возвращает {'anchor_seconds', 'bar_seconds', 'phrase_seconds',
    'anchored', 'cues': [...]}. `anchored` = False означает, что карты
    барабанов нет и сетка построена от нуля файла — тогда номера тактов
    условны, и интерфейс обязан это показать, а не делать вид."""
    structure = track.get("structure") or {}
    emap = structure.get("energy_map") or {}
    dmap = structure.get("drum_map") or {}
    grid = _grid(track, bpm)
    duration = grid.duration

    drops = sorted(float(x) for x in (emap.get("drops") or []))
    breaks = [(float(a), float(b)) for a, b in (emap.get("breakdowns") or [])]
    pits = sorted(float(x) for x in (emap.get("pits") or []))
    drums_from = float(dmap.get("drums_start") or 0.0)

    cues: list[dict] = []

    def add(kind: str, t: float, name: str, hint: str | None = None,
            snap: str = "nearest", exact: bool = False, **extra) -> None:
        t = float(t)
        if not exact:
            t = grid.snap(t, snap)
        if t < 0 or (duration and t > duration - grid.bar):
            return
        cues.append({
            "kind": kind,
            "name": name,
            "time_seconds": round(t, 2),
            "bar": grid.bar_index(t),
            "phrase": grid.phrase_index(t),
            # Подпись целиком: роль впереди, время следом. Раньше было
            # наоборот («дроп на 0:37»), и выбирать приходилось по
            # секундам, хотя выбирают всегда по месту.
            "label": f"{name} · {_fmt(t)}",
            "hint": hint or KIND_HINT.get(kind, ""),
            **extra,
        })

    # --- первый бит -----------------------------------------------------
    if drums_from > 0.5:
        add("first_beat", drums_from,
            f"первый бит (интро {_fmt(drums_from)})", exact=True)
        add("intro_end", drums_from, "конец интро", exact=True)
    elif drums_from >= 0:
        add("first_beat", drums_from, "первый бит", exact=True)

    # --- дропы ----------------------------------------------------------
    # Дроп на первой же доле трека — это не дроп, а тот же «первый бит»:
    # карта энергии честно видит там возврат низа после интро, но метка
    # «дроп» в двух секундах от «первого бита» только засоряет список.
    for i, t in enumerate(drops, 1):
        if abs(t - drums_from) < grid.phrase * 0.5:
            continue
        add("drop", t, "дроп" if len(drops) == 1 else f"{_ordinal(i)} дроп",
            exact=True)

    # --- билд перед каждым дропом ---------------------------------------
    # Билд — это последняя фраза ДО дропа. Отсчитываем от самого дропа
    # целым числом фраз, а не от сетки файла: попасть надо ровно на фразу
    # относительно дропа, иначе метка встанет посреди нарастания.
    for i, t in enumerate(drops, 1):
        start = t - grid.phrase
        if start > drums_from + grid.bar:
            add("build", start,
                f"билд перед {_ordinal(i, 'ins')} дропом" if len(drops) > 1
                else "билд перед дропом",
                exact=True)

    # --- брейкдауны -----------------------------------------------------
    for a, b in breaks:
        if a < 1.0:
            continue  # стартовое интро — это не брейкдаун
        after = sum(1 for d in drops if d < a)
        name = "брейкдаун"
        if after:
            name += f" после {_ordinal(after, 'gen')} дропа"
        add("breakdown", a, name, snap="nearest",
            length_bars=int(round((b - a) / grid.bar)) if grid.bar else 0)

    # --- ямы ------------------------------------------------------------
    for t in pits:
        add("pit", t, "яма перед дропом")

    # --- аутро ----------------------------------------------------------
    if duration > grid.phrase * 3:
        # Конец «тела»: последний такт с барабанами, а если карты нет —
        # четыре фразы от конца.
        tail = None
        bars = dmap.get("bars")
        bar_sec = float(dmap.get("bar_seconds") or grid.bar)
        if isinstance(bars, str) and bars:
            last = bars.rfind("1")
            if last >= 0:
                tail = (last + 1) * bar_sec
        if tail is None:
            tail = duration - grid.phrase * 4
        add("outro", tail, "аутро — барабаны кончились", snap="prev")

    # --- лупы -----------------------------------------------------------
    # Не «все места, где можно зациклить» — их сотни, — а те, что реально
    # нужны: последняя фраза перед каждым дропом (её лупят, чтобы дождаться
    # второй деки) и последняя фраза тела (её лупят на выводе).
    loop_anchors = [t - grid.phrase for t in drops]
    if duration:
        loop_anchors.append(duration - grid.phrase * 3)
    seen = set()
    for start in loop_anchors:
        start = grid.snap(start, "prev")
        if start <= drums_from or round(start, 1) in seen:
            continue
        seen.add(round(start, 1))
        fits = [b for b in LOOP_BARS if _loop_is_clean(track, grid, start, b)]
        if not fits:
            continue
        best = max(fits)
        add("loop", start,
            f"луп {best} {_bars_word(best)}"
            + (f" (или {min(fits)})" if len(fits) > 1 else ""),
            exact=True, loop_bars=fits, best_loop_bars=best)

    # --- порядок и чистка ------------------------------------------------
    cues.sort(key=lambda c: (c["time_seconds"], c["kind"] != "first_beat"))
    out: list[dict] = []
    for c in cues:
        # Две метки в одном месте — оставляем более конкретную.
        if out and abs(c["time_seconds"] - out[-1]["time_seconds"]) < grid.bar * 0.5 \
                and c["kind"] == out[-1]["kind"]:
            continue
        out.append(c)

    return {
        "anchor_seconds": round(grid.anchor, 2),
        "bar_seconds": round(grid.bar, 4),
        "phrase_seconds": round(grid.phrase, 3),
        "bpm": round(grid.bpm, 2),
        # Без карты барабанов сетка строится от нуля файла: номера тактов
        # тогда условны, и врать про них нельзя.
        "anchored": bool(dmap),
        "cues": out[:max_points],
    }


def hotcues_for_track(track: dict, bpm: float | None = None,
                      slots: int = 8) -> list[dict]:
    """Что выгрузить в горячие метки Mixxx: не всё подряд, а по одной
    метке на роль, в порядке, в котором их нажимают.

    Восемь хоткеев — это восемь кнопок под пальцами, и забить их
    двадцатью брейкдаунами значит не найти нужный."""
    data = cues_for_track(track, bpm)
    cues = data["cues"]
    picked: list[dict] = []

    def take(pred, limit=1):
        n = 0
        for c in cues:
            if n >= limit:
                break
            if pred(c) and c not in picked:
                picked.append(c)
                n += 1

    take(lambda c: c["kind"] == "first_beat")
    take(lambda c: c["kind"] == "build", limit=2)
    take(lambda c: c["kind"] == "drop", limit=2)
    take(lambda c: c["kind"] == "breakdown", limit=2)
    take(lambda c: c["kind"] == "loop", limit=1)
    take(lambda c: c["kind"] == "outro")

    picked.sort(key=lambda c: c["time_seconds"])
    out = []
    for i, c in enumerate(picked[:slots], 1):
        out.append({**c, "hotcue": i})
    return out


def describe(data: dict) -> str:
    """Однострочник для лога и чата."""
    kinds: dict[str, int] = {}
    for c in data.get("cues", []):
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    parts = [f"{KIND_TITLE.get(k, k)}: {n}" for k, n in kinds.items()]
    tail = "" if data.get("anchored") else " (сетка от нуля файла — карты барабанов нет)"
    return ", ".join(parts) + tail
