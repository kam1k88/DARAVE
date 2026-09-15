"""
genre.py — жанр и поджанр трека.

## Что здесь честно работает, а что нет

**Жанр из тегов — работает.** У 75% библиотеки в ID3 стоит жанр, просто
записан он как попало: «Драм-н-бэйс», «Drum & Bass», «Электронная;
Драм-н-бэйс», «Танцевальная», «Other». Приведение этой каши к одному
списку — задача словаря, а не догадки, и решается точно.

**Поджанр из звука своими признаками — НЕ работает, проверено.** Было
посчитано 12 акустических признаков (доля саба, рис против саба, доля
середины, крест-фактор, плотность и неровность атак, стабильность
гармонии, доля гармонической энергии, спектральная плоскость низа) на 18
треках библиотеки с известным поджанром: 5 нейрофанк, 5 ликвид, 4 джангл,
3 джамп-ап, 1 дабстеп. Классы не разделяются:

    признак              нейрофанк   ликвид   джангл   джамп-ап
    доля середины           0.280     0.189    0.227     0.252
    стабильность гармонии   0.777     0.790    0.864     0.871
    крест-фактор            9.64     10.71    10.77     11.50
    плотность атак          4.96      6.05     5.67      5.67

Единственный признак, который вообще расходится, — доля середины
(нейрофанк 0.227..0.357 против ликвида 0.148..0.230), и тот пересекается:
Keeno (ликвид) даёт 0.230, Current Value (нейрофанк) — 0.227. Остальные
перемешаны полностью. «Рис против саба» выглядел самым осмысленным
признаком и оказался мусором: у двух треков он равен 45 и 130 при медиане
около 1.5, то есть меряет он мастеринг, а не бас.

Поэтому поджанра из звука здесь НЕТ. Написать пороги, которые красиво
раскладывают эти же 18 треков, можно за десять минут — и они будут
описывать шум. Лучше пустое поле, чем поле, которому нельзя верить: по
поджанру диджей строит порядок сета, и «ликвид» на нейрофанке испортит
переход надёжнее, чем отсутствие подписи.

Что поджанр реально даёт:
  * `subgenre_from_tags` — если поджанр записан в теге или в имени файла
    («[DnB]», «Jungle», «Neurofunk»), берём оттуда: это не догадка;
  * `ARTIST_SUBGENRE` — таблица артистов, у которых поджанр однозначен.
    Тоже не догадка, а знание, и оно помечено источником `artist`;
  * `classify_audio` — точка подключения модели (CLAP zero-shot умеет
    поджанры по текстовым описаниям). Пока модели нет — возвращает None,
    и это правильный ответ, а не заглушка.

Источник у каждого значения хранится рядом со значением (`genre_source`),
чтобы в интерфейсе было видно, откуда подпись: из тега, по артисту или от
модели.
"""
from __future__ import annotations

import re

# --- канонические жанры -------------------------------------------------

GENRES = ("dnb", "techno", "trance", "house", "dubstep", "breaks",
          "hardcore", "garage", "hiphop", "ambient", "electronic", "other")

GENRE_TITLES = {
    "dnb": "драм-н-бейс", "techno": "техно", "trance": "транс",
    "house": "хаус", "dubstep": "дабстеп", "breaks": "брейкс",
    "hardcore": "хардкор", "garage": "гэридж", "hiphop": "хип-хоп",
    "ambient": "эмбиент", "electronic": "электроника", "other": "прочее",
}

# Сырые теги -> канон. Ключи в нижнем регистре, сравнение по вхождению.
# Русские варианты здесь не «на всякий случай»: ровно так и подписана
# библиотека диджея (Драм-н-бэйс, Танцевальная, Электронная).
TAG_MAP = (
    ("драм-н-бэйс", "dnb"), ("драм-н-бейс", "dnb"), ("драм энд бэйс", "dnb"),
    ("drum & bass", "dnb"), ("drum and bass", "dnb"), ("drum'n'bass", "dnb"),
    ("drumandbass", "dnb"), ("drum n bass", "dnb"), ("dnb", "dnb"),
    ("d&b", "dnb"), ("jungle", "dnb"), ("neurofunk", "dnb"), ("liquid", "dnb"),
    ("dubstep", "dubstep"), ("дабстеп", "dubstep"), ("brostep", "dubstep"),
    ("techno", "techno"), ("техно", "techno"),
    ("trance", "trance"), ("транс", "trance"), ("psytrance", "trance"),
    ("house", "house"), ("хаус", "house"),
    ("breakbeat", "breaks"), ("big beat", "breaks"), ("bigbeat", "breaks"),
    ("брейкбит", "breaks"), ("breaks", "breaks"),
    ("hardcore", "hardcore"), ("gabber", "hardcore"), ("хардкор", "hardcore"),
    ("garage", "garage"), ("2-step", "garage"), ("ukg", "garage"),
    ("hip hop", "hiphop"), ("hip-hop", "hiphop"), ("rap", "hiphop"),
    ("хип-хоп", "hiphop"), ("рэп", "hiphop"),
    ("ambient", "ambient"), ("эмбиент", "ambient"), ("downtempo", "ambient"),
    # Общие теги — только если ничего конкретнее не нашлось, поэтому они
    # в конце: порядок кортежа и есть приоритет.
    ("electronic", "electronic"), ("электронная", "electronic"),
    ("electronica", "electronic"), ("танцевальная", "electronic"),
    ("dance", "electronic"),
)

# --- поджанры -----------------------------------------------------------

SUBGENRES = {
    "dnb": ("neurofunk", "liquid", "jungle", "jumpup", "techstep",
            "drumfunk", "halftime", "minimal_dnb", "ragga"),
    "techno": ("peak_time", "melodic", "industrial", "minimal", "dub_techno"),
    "trance": ("uplifting", "psytrance", "progressive", "hard_trance"),
    "house": ("deep", "tech_house", "progressive", "bass_house"),
    "breaks": ("big_beat", "nu_skool", "florida"),
}

SUBGENRE_TITLES = {
    "neurofunk": "нейрофанк", "liquid": "ликвид", "jungle": "джангл",
    "jumpup": "джамп-ап", "techstep": "техстеп", "drumfunk": "драмфанк",
    "halftime": "халфтайм", "minimal_dnb": "минимал днб", "ragga": "рагга",
    "big_beat": "биг-бит", "peak_time": "пик-тайм", "melodic": "мелодик",
    "industrial": "индастриал", "minimal": "минимал", "dub_techno": "даб-техно",
    "uplifting": "аплифтинг", "psytrance": "пситранс",
    "progressive": "прогрессив", "hard_trance": "хард-транс",
    "deep": "дип", "tech_house": "тек-хаус", "bass_house": "бас-хаус",
    "nu_skool": "ню-скул", "florida": "флорида",
}

SUB_TAG_MAP = (
    ("neurofunk", "neurofunk"), ("нейрофанк", "neurofunk"), ("neuro", "neurofunk"),
    ("liquid", "liquid"), ("ликвид", "liquid"), ("liquid funk", "liquid"),
    ("jungle", "jungle"), ("джангл", "jungle"), ("джангл", "jungle"),
    ("jump up", "jumpup"), ("jump-up", "jumpup"), ("jumpup", "jumpup"),
    ("techstep", "techstep"), ("darkstep", "techstep"),
    ("drumfunk", "drumfunk"), ("halftime", "halftime"), ("half-time", "halftime"),
    ("ragga", "ragga"), ("big beat", "big_beat"), ("bigbeat", "big_beat"),
    ("psytrance", "psytrance"), ("psy-trance", "psytrance"),
    ("tech house", "tech_house"), ("deep house", "deep"),
    ("dub techno", "dub_techno"), ("minimal", "minimal"),
)

# Артисты, у которых поджанр однозначен. Это ЗНАНИЕ, а не догадка по
# звуку, поэтому источник помечается отдельно (`artist`) и такую подпись
# всегда можно перебить тегом или моделью.
ARTIST_SUBGENRE = {
    "black sun empire": "neurofunk", "current value": "neurofunk",
    "evol intent": "neurofunk", "noisia": "neurofunk", "phace": "neurofunk",
    "mefjus": "neurofunk", "the upbeats": "neurofunk", "enei": "neurofunk",
    "billain": "neurofunk", "signal": "neurofunk", "gydra": "neurofunk",
    "calibre": "liquid", "bcee": "liquid", "keeno": "liquid",
    "logistics": "liquid", "hybrid minds": "liquid", "nelver": "liquid",
    "high contrast": "liquid", "netsky": "liquid", "lsb": "liquid",
    "etherwood": "liquid", "kubiks": "liquid", "makoto": "liquid",
    "dead man's chest": "jungle", "dead mans chest": "jungle",
    "tim reaper": "jungle", "paradox": "drumfunk", "seba": "jungle",
    "omni trio": "jungle", "dj hype": "jungle",
    "chase & status": "jumpup", "hazard": "jumpup", "original sin": "jumpup",
    "dj guv": "jumpup", "macky gee": "jumpup",
    "ivy lab": "halftime", "imanu": "halftime", "sam binga": "halftime",
}


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


# Теги, которые ничего не говорят. «Other» стоит у 10 треков библиотеки,
# «Танцевальная» и «Электронная» — ещё у десятка, и все они на деле
# драм-н-бейс. Если такой тег считать ответом, разбор останавливается на
# нём и до имени файла с темпом дело не доходит — треть библиотеки
# оказывается «прочее». Поэтому общий тег — не ответ, а «пока ничего».
GENERIC_GENRES = {"other", "electronic"}


# Что можно сказать о жанре по ОДНОМУ ТОЛЬКО ТЕМПУ.
#
# Прежде здесь была ровно одна полоса — 160-185 как драм-н-бейс, — а всё
# остальное оставалось без жанра вовсе. Для библиотеки, где 42 трека из 66
# это драм-н-бейс, работало; но у диджея есть ещё 11 треков на 135 и
# четыре на 140, и им доставалось None. Дальше по цепочке None означал
# «возьми настройки по умолчанию», то есть драм-н-бейсовые: чужая сетка
# фраз и чужие приёмы. Он это и заметил: «под эти стили нужны другие фразы
# и другие техники сведения».
#
# Но и врать нельзя. 135 BPM — это не один жанр, это СЕМЕЙСТВО: гэридж,
# бэйслайн, спид-гэридж, брейкс. Поэтому полоса отдаёт семейство плюс
# список кандидатов, а источником честно пишется "bpm": решение по темпу
# слабее тега и слабее имени файла, и интерфейс вправе спросить.
#
# Полосы кончаются там, где темп перестаёт что-либо значить: 120-128 это
# и хаус, и техно, и поп — гадать не о чем.
BPM_FAMILIES: tuple[tuple[float, float, str, tuple[str, ...]], ...] = (
    (160.0, 185.0, "dnb",      ("dnb", "liquid", "neurofunk", "jungle")),
    (140.0, 152.0, "dubstep",  ("dubstep", "halftime", "techno")),
    (128.0, 140.0, "garage",   ("garage", "bassline", "speed garage", "breaks")),
    (85.0, 100.0,  "hiphop",   ("hiphop", "halftime dnb")),
)


def genre_from_bpm(bpm: float | None) -> dict:
    """Семейство жанров по темпу, с кандидатами и честным источником."""
    if not bpm:
        return {"genre": None, "genre_source": None, "candidates": ()}
    v = float(bpm)
    for lo, hi, fam, cands in BPM_FAMILIES:
        if lo <= v <= hi:
            return {"genre": fam, "genre_source": "bpm",
                    "raw": f"{v:.0f} BPM", "candidates": cands,
                    "certain": len(cands) == 1 or fam == "dnb"}
    return {"genre": None, "genre_source": None, "candidates": ()}


# Жанр по ПАПКЕ. Диджей раскладывает музыку руками, и папка «Phonk» —
# это его собственная разметка, самая надёжная из всех доступных: тег
# ставил кто-то чужой, имя файла пишет релиз-группа, а папку делал он.
#
# Игнорировать её было прямой ошибкой. Из-за неё фонк на 165 BPM
# подписывался как драм-н-бейс (полоса 160-185 в BPM_FAMILIES), а фонк на
# 155 и 190 не подписывался вовсе — и то и другое диджей увидел сразу:
# «новые треки в папке Phonk не определяют стиль».
FOLDER_HINTS: tuple[tuple[str, str], ...] = (
    ("phonk", "phonk"),
    ("drift", "phonk"),
    ("neurofunk", "dnb"),
    ("liquid", "dnb"),
    ("jungle", "dnb"),
    ("dnb", "dnb"),
    ("drum and bass", "dnb"),
    ("drum n bass", "dnb"),
    ("dubstep", "dubstep"),
    ("halftime", "dubstep"),
    ("garage", "garage"),
    ("ukg", "garage"),
    ("bassline", "garage"),
    ("breaks", "breaks"),
    ("techno", "techno"),
    ("trance", "trance"),
    ("house", "house"),
    ("hiphop", "hiphop"),
    ("hip-hop", "hiphop"),
    ("trap", "trap"),
)

# Папки, которые ничего не говорят о жанре: по ним подписывать нельзя.
FOLDER_IGNORE = frozenset({
    "music", "музыка", "tracks", "треки", "new", "новое", "downloads",
    "загрузки", "mp3", "flac", "wav", "audio", "library", "медиатека",
    "++", "misc", "разное", "temp", "tmp", "sets", "demos",
})


def genre_from_folder(path: str | None) -> dict:
    """Жанр по имени папки. Ближняя папка сильнее дальней.

    Ближняя — потому что «Music/DnB/Liquid» означает ликвид, а не просто
    драм-н-бейс, и «Music/Phonk/2024» — всё ещё фонк."""
    parts = [x for x in str(path or "").replace("\\", "/").split("/") if x][:-1]
    for part in reversed(parts):
        low = part.strip().lower()
        if not low or low in FOLDER_IGNORE:
            continue
        for needle, canon in FOLDER_HINTS:
            if needle in low:
                return {"genre": canon, "genre_source": "folder", "raw": part}
    return {"genre": None, "genre_source": None, "raw": None}


def genre_from_tags(tag_genre: str | None, file_name: str | None = None,
                    bpm: float | None = None) -> dict:
    """Канонический жанр. Возвращает {'genre', 'genre_source', 'raw'}.

    Приоритет: конкретный тег -> имя файла -> темп -> общий тег. Темп
    намеренно осторожен: 174 — это почти наверняка драм-н-бейс, а 128
    может быть чем угодно от хауса до транса, поэтому по темпу
    подписывается только драм-н-бейсовая полоса."""
    raw = _norm(tag_genre)
    fallback = None
    for needle, canon in TAG_MAP:
        if needle in raw:
            hit = {"genre": canon, "genre_source": "tag", "raw": tag_genre}
            if canon not in GENERIC_GENRES:
                return hit
            fallback = fallback or hit

    # Папка — разметка самого диджея, и она сильнее имени файла и темпа.
    by_folder = genre_from_folder(file_name)
    if by_folder.get("genre"):
        return by_folder

    name = _norm(file_name)
    for needle, canon in TAG_MAP:
        if needle in name:
            hit = {"genre": canon, "genre_source": "filename", "raw": file_name}
            if canon not in GENERIC_GENRES:
                return hit
            fallback = fallback or hit

    by_bpm = genre_from_bpm(bpm)
    if by_bpm.get("genre"):
        # Общий тег («Dance», «Other») слабее темпа: он не врёт, но и не
        # говорит ничего, а темп сужает хотя бы до семейства.
        return by_bpm
    if fallback:
        return fallback
    if raw:
        return {"genre": "other", "genre_source": "tag", "raw": tag_genre}
    return {"genre": None, "genre_source": None, "raw": None}


def subgenre_from_tags(tag_genre: str | None, file_name: str | None = None,
                       artist: str | None = None) -> dict:
    """Поджанр — только оттуда, где он записан явно, либо по артисту."""
    for source, text in (("tag", _norm(tag_genre)), ("filename", _norm(file_name))):
        for needle, canon in SUB_TAG_MAP:
            if needle in text:
                return {"subgenre": canon, "subgenre_source": source}

    a = _norm(artist)
    if a:
        for known, canon in ARTIST_SUBGENRE.items():
            # Совпадение по началу имени: «Black Sun Empire, Concord Dawn»
            # и «BCee, Charlotte Haining» — это те же артисты.
            if a.startswith(known) or f", {known}" in a or f" & {known}" in a:
                return {"subgenre": canon, "subgenre_source": "artist"}
    return {"subgenre": None, "subgenre_source": None}


def artist_title_from_name(file_name: str) -> tuple[str | None, str | None]:
    """«Black Sun Empire - Arrakis.mp3» -> ('Black Sun Empire', 'Arrakis').

    Принимает и путь целиком: вызывающих мест несколько, и заставлять
    каждое из них отрезать папку — это ровно тот случай, когда одно
    забудет. Замерено: «/x/1. Bad Patterns - PERFECT STORM.mp3» давал
    исполнителя «/x/1. Bad Patterns» — с папкой и номером дорожки.
    """
    stem = str(file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", stem, flags=re.I)
    # Номер дорожки и метка жанра в начале — в любом порядке и по
    # нескольку раз: «[DnB] 03 - ...» встречается не реже, чем «03. [DnB] ...».
    for _ in range(3):
        before = stem
        stem = re.sub(r"^\s*\d{1,3}\s*[\.\-–)_]\s*", "", stem)   # «02. », «10 - »
        stem = re.sub(r"^\s*\[[^\]]*\]\s*", "", stem)             # «[DnB] »
        stem = re.sub(r"^\s*\([^)]*\)\s*", "", stem)               # «(2021) »
        if stem == before:
            break
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return None, stem.strip() or None


def classify_audio(track_path: str) -> dict | None:
    """Поджанр по звуку — точка подключения модели.

    Своими акустическими признаками это не решается (см. докстринг
    модуля: замер на 18 размеченных треках библиотеки). Рабочий путь —
    zero-shot модель, понимающая текстовые описания: CLAP сравнивает
    звук с фразами «neurofunk drum and bass», «liquid drum and bass»,
    «jungle breakbeat» и отдаёт похожесть на каждую. Ставится как
    `pip install laion-clap`, считается на GPU за секунды на трек.

    Пока модели нет — возвращаем None. Это не заглушка «сделаем потом»:
    None означает «поджанр неизвестен», и интерфейс покажет прочерк
    вместо выдуманной подписи."""
    try:
        import laion_clap  # noqa: F401
    except ImportError:
        return None

    import numpy as np

    prompts = {}
    for genre, subs in SUBGENRES.items():
        for sub in subs:
            title = SUBGENRE_TITLES.get(sub, sub).replace("_", " ")
            prompts[(genre, sub)] = f"{title} {genre} electronic dance music"

    model = laion_clap.CLAP_Module(enable_fusion=False)
    model.load_ckpt()
    audio_emb = model.get_audio_embedding_from_filelist([track_path], use_tensor=False)
    text_emb = model.get_text_embedding(list(prompts.values()), use_tensor=False)
    scores = (audio_emb @ text_emb.T)[0]
    order = np.argsort(scores)[::-1]
    keys = list(prompts.keys())
    best = keys[int(order[0])]
    top = [{"genre": keys[int(i)][0], "subgenre": keys[int(i)][1],
            "score": round(float(scores[int(i)]), 4)} for i in order[:5]]
    return {"genre": best[0], "subgenre": best[1], "subgenre_source": "clap",
            "genre_source": "clap", "candidates": top}


def describe(record: dict) -> str:
    """Подпись для интерфейса: «драм-н-бейс · нейрофанк (по артисту)»."""
    g = record.get("genre")
    sub = record.get("subgenre")
    if not g:
        return "жанр не определён"
    out = GENRE_TITLES.get(g, g)
    if sub:
        out += " · " + SUBGENRE_TITLES.get(sub, sub)
        src = {"tag": "из тега", "filename": "из имени файла",
               "artist": "по артисту", "clap": "модель"}.get(
            record.get("subgenre_source"), "")
        if src:
            out += f" ({src})"
    return out


def classify(track_path: str, tag_genre: str | None = None,
             tag_artist: str | None = None, bpm: float | None = None,
             use_model: bool = False) -> dict:
    """Полная подпись трека. Ничего не выдумывает: если источника нет —
    поле остаётся пустым."""
    import os

    name = os.path.basename(track_path or "")
    artist, _title = artist_title_from_name(name)
    artist = tag_artist or artist

    out = genre_from_tags(tag_genre, name, bpm)
    out.update(subgenre_from_tags(tag_genre, name, artist))
    out["artist"] = artist

    # Поджанр знает и жанр: если артист опознан как нейрофанк, то это
    # драм-н-бейс, каким бы общим ни был тег. Обратное неверно — жанр
    # поджанра не задаёт, — поэтому правило работает в одну сторону.
    if out.get("subgenre") and out.get("genre") in (None, "other", "electronic"):
        for g, subs in SUBGENRES.items():
            if out["subgenre"] in subs:
                out["genre"] = g
                out["genre_source"] = out.get("subgenre_source")
                break

    if use_model and not out.get("subgenre"):
        model = classify_audio(track_path)
        if model:
            out.update({k: v for k, v in model.items() if k != "candidates"})
            out["candidates"] = model.get("candidates")
    return out
