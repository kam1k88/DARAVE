"""
style.py — стилевые профили сведения и энергетическое планирование сета.

Две вещи, которых не хватало выбору техники.

**Стиль.** Длина перехода и сам набор приёмов зависят от поджанра, а не
от «универсального хорошего сведения». House и techno сводят долго, по
16-64 такта, и держат переход на обмене низом; драм-н-бейс — вдвое
короче, и держит его на фильтре и эффектах; big-room вообще режет в
дроп. Одна и та же «Классика» на всех троих звучит как один и тот же
приём, потому что это он и есть.

**Энергия.** Диджей ведёт не пары треков, а дугу. После трёх жёстких
подряд залу нужна передышка, и это не «более тихий трек», а приём:
войти акапеллой, уронив плотность. После долгой раскачки наоборот нужен
удар — дабл-дроп или рез на дроп. Ни то, ни другое из оценки ОДНОЙ пары
не выводится: нужно смотреть, что было до неё.

Модуль ничего не решает сам — он отдаёт множители к весам кандидатов,
а решение остаётся за mix_strategist._pick_technique.
"""
from __future__ import annotations

# --- стилевые профили -------------------------------------------------
#
# bars: (минимум, типичное, максимум) длины перехода в тактах.
# main: чем ведут переход по умолчанию, когда есть слои.
# accent: чем делают акцент — раз за сет, а не через раз.
# vocal: бывает ли в этом поджанре вокальный слой. У ликвида он есть
#   почти всегда (там мелодию ведёт голос), у нейрофанка и техно —
#   почти никогда, там мелодия инструментальная. Это не косметика:
#   от этого зависит, предлагать ли вокальные приёмы вообще.

PROFILES: dict[str, dict] = {
    "liquid":    {"bars": (16, 24, 48), "main": "ST-03", "accent": "ST-07",
                  "vocal": True,  "note": "ликвид: длинные фразы, вокал ведёт мелодию"},
    "neurofunk": {"bars": (8, 16, 32),  "main": "ST-02", "accent": "ST-08",
                  "vocal": False, "note": "нейрофанк: коротко и по барабанам"},
    "jungle":    {"bars": (8, 16, 32),  "main": "ST-02", "accent": "ST-08",
                  "vocal": False, "note": "джангл: обмен барабанами такт за тактом"},
    "dnb":       {"bars": (12, 20, 40), "main": "ST-03", "accent": "ST-08",
                  "vocal": False, "note": "драм-н-бейс: фильтр и эффекты вместо эквалайзера"},
    "house":     {"bars": (16, 32, 64), "main": "DNB-25", "accent": "HS-01",
                  "vocal": True,  "note": "хаус: долгий переход, обмен низом по фразам"},
    "techno":    {"bars": (16, 32, 64), "main": "ST-02", "accent": "HS-01",
                  "vocal": False, "note": "техно: длинный слой на слой, мелодии мало"},
    "dubstep":   {"bars": (8, 16, 32),  "main": "ST-03", "accent": "ST-08",
                  "vocal": False, "note": "дабстеп: коротко, через фильтр и FX"},
    "trance":    {"bars": (16, 32, 64), "main": "DNB-25", "accent": "ST-06",
                  "vocal": True,  "note": "транс: длинные интро и брейкдауны"},
    "bigroom":   {"bars": (4, 8, 16),   "main": "ST-08", "accent": "ST-07",
                  "vocal": True,  "note": "биг-рум: короткий рез прямо в дроп"},
}

DEFAULT_PROFILE = {"bars": (12, 20, 40), "main": "DNB-25", "accent": "ST-08",
                   "vocal": False, "note": "профиля для этого жанра нет — общий случай"}


def track_genre(track: dict) -> tuple[str, str]:
    """(жанр, поджанр) трека в нижнем регистре, '' если неизвестно.

    Сканер кладёт жанр не в корень трека, а внутрь tags: `tags["genre"]`
    это словарь с полями genre/subgenre и источником каждого. Читать
    только корень — ровно та ошибка, из-за которой профили жанров не
    работали НИ РАЗУ: у всех треков библиотеки profile_for отдавал
    общий случай, и стилевые приёмы (ликвид против нейрофанка) в подбор
    не попадали вообще."""
    g = track.get("genre")
    sub = track.get("subgenre")
    if not (g or sub):
        tags = track.get("tags")
        if isinstance(tags, dict):
            node = tags.get("genre")
            if isinstance(node, dict):
                g = node.get("genre")
                sub = node.get("subgenre")
            elif isinstance(node, str):
                g = node
    return str(g or "").lower(), str(sub or "").lower()


def profile_for(track: dict) -> dict:
    """Профиль по поджанру, а если его нет — по жанру."""
    g, sub = track_genre(track)
    for key in (sub, g):
        if key and key in PROFILES:
            return PROFILES[key]
    return DEFAULT_PROFILE


# --- энергетическая дуга ----------------------------------------------

# Сколько жёстких треков подряд считаем «пора отдышаться». Три — это не
# круглое число: два подряд ещё читаются как связка, четыре уже
# утомляют. Порог вынесен сюда, чтобы его можно было двигать замером, а
# не искать по коду.
HARD_RUN = 3
CALM_RUN = 3
HARD_LEVEL = 4      # уровень энергии (1..5), с которого трек «жёсткий»
CALM_LEVEL = 2


def energy_need(recent_levels: list[int]) -> str:
    """Что сету нужно СЕЙЧАС: 'cooldown' | 'lift' | 'hold'.

    recent_levels — уровни энергии последних сыгранных треков, свежие
    первыми. Из одной пары этого не видно в принципе, поэтому решение и
    вынесено на уровень сета."""
    if not recent_levels:
        return "hold"
    head = recent_levels[:max(HARD_RUN, CALM_RUN)]
    if len(head) >= HARD_RUN and all(x >= HARD_LEVEL for x in head[:HARD_RUN]):
        return "cooldown"
    if len(head) >= CALM_RUN and all(x <= CALM_LEVEL for x in head[:CALM_RUN]):
        return "lift"
    return "hold"


# Чем отвечать на потребность дуги. Порядок — приоритет.
NEED_TECHNIQUES = {
    "cooldown": ("ST-06", "ST-05"),          # войти акапеллой; разобрать на слои
    "lift":     ("ST-07", "ST-08", "ST-04"),  # рез на дроп; дабл-дроп по слоям
    "hold":     (),
}


def has_stems(track: dict) -> bool:
    try:
        import stems as _stems

        return bool(track.get("path") and _stems.stem_paths(track["path"]))
    except Exception:
        return False


def has_vocals(track: dict) -> bool:
    """Есть ли у трека НАСТОЯЩИЙ вокальный слой.

    Сначала спрашиваем посчитанные стемы (там измерена громкость слоя), и
    только если их нет — идём от поджанра: у ликвида вокал есть почти
    всегда, у нейрофанка почти никогда. Догадка по жанру честно помечена
    как догадка и слабее измерения."""
    try:
        import stems as _stems

        if track.get("path") and _stems.stem_paths(track["path"]):
            meta = _stems.stem_meta(track["path"])
            db = meta.get("vocals_db")
            if db is not None:
                return float(db) > -60.0
            return bool(_stems.has_vocals(track["path"]))
    except Exception:
        pass
    return bool(profile_for(track).get("vocal"))


def bias(tid: str, a: dict, b: dict, need: str = "hold") -> tuple[float, str]:
    """Множитель к весу кандидата и объяснение — почему.

    Возвращает (множитель, причина). Множитель 1.0 означает «стиль об
    этой технике ничего не говорит», а не «плохо»."""
    prof = profile_for(b) if b.get("subgenre") or b.get("genre") else profile_for(a)
    mult, why = 1.0, ""

    if tid == prof.get("main"):
        mult *= 1.35
        why = f"основной ход для этого стиля ({prof['note']})"
    elif tid == prof.get("accent"):
        mult *= 1.10
        why = f"акцент, принятый в этом стиле ({prof['note']})"

    wanted = NEED_TECHNIQUES.get(need, ())
    if tid in wanted:
        # Чем раньше в списке, тем сильнее ответ на потребность дуги.
        mult *= 1.6 - 0.15 * wanted.index(tid)
        why = ("после нескольких жёстких треков подряд нужна передышка"
               if need == "cooldown" else
               "после долгой раскачки сет просит удара") + (f"; {why}" if why else "")
    return mult, why


def transition_bars(a: dict, b: dict, slot_seconds: float | None = None,
                    bpm: float = 174.0) -> int:
    """Сколько тактов держать переход по стилю входящего трека.

    Если у сета жёсткий хронометраж, длина ужимается — но не ниже
    минимума профиля: короче него приём перестаёт быть собой."""
    prof = profile_for(b)
    lo, typ, hi = prof["bars"]
    bars = typ
    if slot_seconds:
        bar_seconds = 60.0 / max(60.0, bpm) * 4
        # Переход не должен съедать больше трети слота трека.
        bars = max(lo, min(hi, int(slot_seconds / 3 / bar_seconds)))
    return int(bars)


# --- стыковка секций: техника СЛЕДУЕТ из того, что с чем стыкуется ----
#
# Мысль диджея, и она меняет постановку задачи. Раньше приём выбирался
# «для пары треков», а точки входа и ухода подбирались под приём. На деле
# наоборот: диджей смотрит, ЧТО у него стыкуется — брейкдаун с интро,
# дроп с дропом, грув с брейкдауном, — и приём получается сам. Значит
# разнообразие приёмов берётся не из перебора техник, а из разнообразия
# СТЫКОВОК. Если весь сет стыкует аутро с интро, любой алгоритм будет
# ставить один и тот же приём, и будет прав.

# Интро и аутро сознательно исключены: это «дежурная» стыковка, ради
# которой ничего планировать не надо. Интересное живёт в теле трека.
MIX_SECTION_KINDS = ("drop", "breakdown", "groove", "build")


def mix_sections(track: dict, min_bars: int = 4) -> list[dict]:
    """Секции трека, пригодные для СОДЕРЖАТЕЛЬНОЙ стыковки.

    Без интро и аутро — и без огрызков короче четырёх тактов: в них
    физически не помещается ни один приём."""
    try:
        import cue_points

        secs = cue_points.sections_for_track(track).get("sections", [])
    except Exception:
        return []
    return [x for x in secs
            if x["kind"] in MIX_SECTION_KINDS and x["bars"] >= min_bars]


# Что делают, когда стыкуются такие-то секции. Ключ — «откуда → куда».
# Это не таблица вкусов: каждая строка отвечает на вопрос «что в этот
# момент звучит и чего в нём не хватает».
# Что вообще уместно на этом стыке. Порядок внутри кортежа —
# предпочтение: первый приём получает полный вес, каждый следующий
# немного меньше (см. audition.candidates).
#
# Списки специально длиннее трёх: диджей слушает переходы подряд и
# жмёт «другая техника» — если вариантов три, кнопка кончается на
# четвёртом нажатии, и подбор начинает предлагать один и тот же приём
# на соседних стыках.
PAIR_TECHNIQUES: dict[str, tuple[str, ...]] = {
    # уходящий на дропе, входящий тоже — совмещать целиком нельзя,
    # нужен рез, который держится на одном слое, либо честный дабл-дроп
    # со связкой по сайдчейну
    "drop→drop":           ("ST-07", "ST-08", "ST-12", "ST-04", "ST-14"),
    # уходящий на дропе, входящий заходит в разрядку — понижение
    "drop→breakdown":      ("ST-06", "SF-04", "ST-05", "SF-03", "DNB-25"),
    # разрядка переходит в дроп — самый сильный подъём
    "breakdown→drop":      ("ST-07", "ST-08", "SF-02", "ST-12", "DNB-20"),
    # две разрядки — место для длинного бленда и акапеллы
    "breakdown→breakdown": ("ST-06", "ST-09", "SF-03", "DNB-25", "ST-03"),
    # тело в тело — рабочая лошадь: обмен слоями и ввод по частям
    "groove→groove":       ("SE-01", "ST-10", "SF-05", "ST-03", "ST-02", "DNB-25"),
    "groove→breakdown":    ("ST-13", "ST-05", "SF-04", "DNB-25"),
    "breakdown→groove":    ("ST-09", "ST-03", "SF-05", "DNB-25"),
    # билд ведёт в дроп — классическая подача
    "build→drop":          ("ST-08", "SF-02", "ST-12", "DNB-20"),
    "groove→drop":         ("ST-02", "ST-08", "SF-05", "ST-10"),
    "drop→groove":         ("ST-03", "SE-01", "ST-13", "ST-05"),
}


# Насколько стыковка САМА ПО СЕБЕ интересна. Дроп в дроп и брейкдаун в
# дроп — события, ради которых зал и стоит; тело в тело — рабочая
# лошадь, на которой держится сет, но не то, что запоминают. Это не
# «вкусовые коэффициенты»: без них разнообразие вырождается в случайное
# перемешивание, а сет должен иметь события и паузы между ними.
PAIR_INTEREST: dict[str, float] = {
    "drop→drop": 1.25,
    "breakdown→drop": 1.20,
    "build→drop": 1.15,
    "drop→breakdown": 1.10,
    "groove→drop": 1.05,
    "drop→groove": 1.00,
    "breakdown→breakdown": 0.95,
    "breakdown→groove": 0.90,
    "groove→breakdown": 0.90,
    "groove→groove": 0.85,
}


def pair_label(exit_kind: str, entry_kind: str) -> str:
    return f"{exit_kind}→{entry_kind}"


def pair_options(a: dict, b: dict, min_bars: int = 8) -> list[dict]:
    """Все содержательные стыковки пары треков.

    Возвращает список {label, exit, entry, techniques, bars} — то есть
    не «одну правильную точку», а веер возможностей, из которого план
    сета выбирает так, чтобы соседние переходы не повторялись."""
    out = []
    ex = [x for x in mix_sections(a, min_bars) if x["can_exit"]]
    en = [x for x in mix_sections(b, min_bars) if x["can_enter"] or x["kind"] == "drop"]
    for x in ex:
        for y in en:
            label = pair_label(x["kind"], y["kind"])
            tech = PAIR_TECHNIQUES.get(label)
            if not tech:
                continue
            out.append({
                "label": label,
                "exit": {"kind": x["kind"], "at": x["start_seconds"], "bars": x["bars"]},
                "entry": {"kind": y["kind"], "at": y["start_seconds"], "bars": y["bars"]},
                "techniques": tech,
                # Длина стыковки ограничена более коротким из двух кусков.
                "bars": min(x["bars"], y["bars"]),
            })
    return out


def pair_diversity(label: str, recent_labels: list[str]) -> float:
    """Множитель за НЕповторение стыковки.

    Однообразие стыковок и есть та причина, по которой сет звучит
    одинаково, даже когда каждый переход по отдельности выбран верно."""
    if not recent_labels:
        return 1.0
    if label == recent_labels[0]:
        return 0.45
    if label in recent_labels[:2]:
        return 0.65
    if label in recent_labels[:4]:
        return 0.85
    return 1.15
