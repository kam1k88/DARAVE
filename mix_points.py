"""
Точки сведения: варианты «откуда сводить» с объяснением, чем они хороши.

Раньше точка выбиралась одна и молча — диджей не мог ни увидеть
альтернативы, ни понять, почему выбрана именно эта. Здесь для каждого
трека собираются ВСЕ кандидаты (дропы, брейкдауны, границы фраз, отступ
от конца), каждому даётся оценка и человеческая подсказка, и вернуть их
можно списком — чтобы в UI выбрать другую.

Оценка не «умная», а прямая диджейская: место тем лучше, чем оно
музыкально осмысленнее (брейкдаун и граница фразы — чистые швы, дроп —
эффектный, но требует точности), чем ближе к концу трека (не обрывать
трек на середине) и чем надёжнее его нашёл анализ.
"""
from __future__ import annotations

import math as _math


def _fmt(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


# Базовая пригодность типа точки как «шва» при сведении.
KIND_SCORE = {
    "breakdown": 1.00,   # барабаны ушли — самый чистый вход для новой деки
    "pit": 0.95,         # яма: низ проваливается на такт-другой перед дропом
    "phrase": 0.85,      # граница фразы — всегда музыкально корректна
    "outro": 0.75,       # хвост трека
    "drop": 0.55,        # эффектно, но попасть надо точно
    "tail": 0.40,        # просто отступ от конца, если структуры не нашли
    "timed": 0.70,       # ровно там, где велит хронометраж сета
}

KIND_HINT = {
    "breakdown": "барабаны ушли — новая дека входит в пустоту, шва почти не слышно",
    "pit": "яма перед дропом: низ проваливается на такт-другой — сменить трек тут почти не слышно",
    "phrase": "чистая граница фразы — безопасный вариант по умолчанию",
    "outro": "хвост трека: уходящая дека уже отыграла своё",
    "drop": "на дроп — эффектно, но попасть надо ровно, иначе слышно",
    "tail": "структуру найти не удалось, просто отступ от конца трека",
    "timed": "точка по хронометражу сета, подтянутая к границе такта",
}


def candidates_for_outgoing(track: dict, bpm: float, needed_seconds: float,
                            limit: int = 6, want_seconds: float | None = None,
                            entry_seconds: float = 0.0) -> list[dict]:
    """Откуда уводить УХОДЯЩИЙ трек.

    want_seconds — момент, к которому трек должен уйти по хронометражу
    сета. Это главное изменение модели: длительность сета больше не
    отбрасывает треки, а задаёт, сколько времени играет каждый. Если
    диджей просит 50 треков за 90 минут, каждому отведено чуть меньше
    двух минут, и уводить надо примерно там, а не «как можно позже».
    Поэтому оценка = музыкальное качество шва × близость к нужной секунде.

    entry_seconds — с какой секунды этот трек начал играть: точка ухода
    раньше входа бессмысленна.
    """
    structure = track.get("structure") or {}
    duration = float(track.get("duration_seconds") or 0.0)
    bar_seconds = (60.0 / bpm * 4) if bpm else 1.4
    latest = max(0.0, duration - needed_seconds)
    earliest = max(0.0, float(entry_seconds) + 8 * bar_seconds)  # хотя бы 8 тактов трек должен отыграть

    out: list[dict] = []

    # Брейкдауны и ямы берём из карты энергии: она меряет, где у трека
    # реально ушёл низ, а не гадает по structure-анализу. Именно сюда
    # диджей и уводит трек — в его собственную паузу, а не в середину
    # плотной партии.
    emap = _energy_map(track)
    energy_breaks = [float(a) for a, _b in (emap.get("breakdowns") or [])]
    for t in energy_breaks:
        if earliest <= t <= latest:
            out.append(_point("breakdown", t, bar_seconds, duration, latest,
                              "брейкдаун — низ уходит сам"))
    for t in [float(x) for x in (emap.get("pits") or [])]:
        if earliest <= t <= latest:
            out.append(_point("pit", t, bar_seconds, duration, latest,
                              "яма перед дропом"))

    if not energy_breaks:
        # карты энергии ещё нет (библиотека не пересчитана) — работаем по старому
        for bd in structure.get("breakdowns") or []:
            t = float(bd.get("start_seconds", 0.0))
            if earliest <= t <= latest:
                out.append(_point("breakdown", t, bar_seconds, duration, latest,
                                  "брейкдаун"))

    for t in structure.get("phrase_boundaries") or []:
        t = float(t)
        if earliest <= t <= latest and t > 0.5:
            out.append(_point("phrase", t, bar_seconds, duration, latest,
                              "граница фразы"))

    energy_drops = [float(x) for x in (emap.get("drops") or [])]
    for t in (energy_drops or [float(d.get("time_seconds", 0.0))
                               for d in (structure.get("drops") or [])]):
        if earliest <= t <= latest:
            out.append(_point("drop", t, bar_seconds, duration, latest,
                              "дроп"))

    if want_seconds is not None:
        # Ровно та секунда, которую просит хронометраж, — на случай, когда
        # рядом нет ни одного найденного шва. Подтягиваем к сетке тактов,
        # чтобы даже «слепой» рез попал в такт.
        t = min(max(float(want_seconds), earliest), latest if latest > earliest else float(want_seconds))
        t = round(t / bar_seconds) * bar_seconds
        if t > 0.5:
            out.append(_point("timed", t, bar_seconds, duration, latest,
                              "по хронометражу сета"))

    # «Хвост» — это конец трека, а не место по хронометражу. Пока он
    # добавлялся всегда, он обходил окно поиска и утаскивал уход на
    # минуты позже нужного: сет из 15 минут выходил на 21. Оставляем его
    # только как запасной вариант, когда времени не задано.
    if duration and latest > earliest and want_seconds is None:
        out.append(_point("tail", latest, bar_seconds, duration, latest,
                          f"хвост трека, за {needed_seconds:.0f}с до конца"))

    if not out:
        out.append(_point("tail", max(earliest, 0.0), bar_seconds, duration or 1.0, 0.0,
                          "по хронометражу (структуру найти не удалось)"))

    _snap_all_to_phrase(out, bar_seconds, duration)

    if want_seconds is not None:
        # Сначала ОКНО, потом качество. Иначе получается качели: либо
        # хронометраж (и тогда уводим с произвольной границы фразы), либо
        # музыка (и тогда сет из 25 минут выходит на 14 — измерено).
        # Диджей делает ровно это: смотрит, есть ли рядом с нужной минутой
        # брейкдаун или яма, и уводит туда; если нет — уводит по времени.
        win = SEARCH_BARS * bar_seconds
        near = [p for p in out
                if abs(p["time_seconds"] - float(want_seconds)) <= win
                or p["kind"] == "timed"]
        if any(p["kind"] != "timed" for p in near):
            out = near
        elif near:
            out = near

    if want_seconds is not None:
        # Внутри окна близость уже не решает — решает качество шва.
        for p in out:
            miss = abs(p["time_seconds"] - float(want_seconds))
            p["fit"] = round(1.0 / (1.0 + (miss / 45.0) ** 2), 3)
            p["miss_seconds"] = round(miss, 1)
            p["score"] = round(p["score"] * p["fit"], 4)
            if miss > 20:
                p["hint"] += f"; на {miss / 60:.1f} мин {'позже' if p['time_seconds'] > want_seconds else 'раньше'} хронометража"

    out.sort(key=lambda p: p["score"], reverse=True)
    _dedupe(out)
    return out[:limit]


PHRASE_BARS = 8

# Насколько далеко от нужной по хронометражу секунды имеет смысл искать
# музыкальный шов. 16 тактов — это две фразы, ±22 секунды при 174 BPM:
# достаточно, чтобы дотянуться до ближайшего брейкдауна, и мало, чтобы
# развалить длительность сета.
SEARCH_BARS = 16.0


def _phrase_snap(t: float, bar_seconds: float, phrase_bars: float = PHRASE_BARS,
                 mode: str = "nearest") -> float:
    """Двигает точку на границу ФРАЗЫ, а не такта.

    Это оказалось причиной того, что технически правильно сведённые треки
    всё равно звучат неправильно. Такты и доли сходились (расхождение
    ±6 мс), а фразы — нет: измерено, что в 7 переходах из 16 уходящий трек
    стоял на 8-м такте своей фразы (то есть на разрешении), а входящий на
    4-м (в середине разгона). Один был разведён на 3.7 такта из 8.

    Ухо это слышит как «некрасиво», хотя бит идеально в бит: сбивки,
    восьмитактовые разгоны и дропы двух треков приходят в разные места и
    спорят друг с другом. Диджей никогда так не сводит — он заводит трек
    ровно с начала фразы.
    """
    span = bar_seconds * phrase_bars
    if span <= 0:
        return t
    k = t / span
    if mode == "next":
        k = _math.ceil(k - 1e-6)
    elif mode == "prev":
        k = _math.floor(k + 1e-6)
    else:
        k = round(k)
    return max(0.0, k * span)


def _snap_all_to_phrase(points: list[dict], bar_seconds: float, duration: float,
                        mode: str = "nearest") -> None:
    """Ставит все кандидаты на сетку фраз и убирает получившиеся дубли."""
    for p in points:
        if p.get("exact"):
            # Точка уже привязана к событию самого трека (например,
            # отсчитана от дропа целым числом тактов). Сетка фраз здесь
            # считается от НУЛЯ файла и увела бы её на пол-фразы — ровно
            # то расхождение, которое мы и чиним.
            continue
        t = _phrase_snap(float(p["time_seconds"]), bar_seconds, mode=mode)
        if duration > 0:
            t = min(t, max(0.0, duration - bar_seconds))
        p["time_seconds"] = round(t, 2)
        p["bar_index"] = round(t / bar_seconds) if bar_seconds else 0
        # Подпись содержит время, а мы его только что сдвинули: без
        # пересборки в списке стояло бы «брейкдаун · 3:12» на секунде
        # 3:07, и диджей выбирал бы одно, а получал другое.
        if p.get("name"):
            p["label"] = f"{p['name']} · {_fmt(p['time_seconds'])}"
    _dedupe(points, min_gap_seconds=bar_seconds * PHRASE_BARS * 0.5)


def _drum_map(track: dict) -> dict:
    return ((track.get("structure") or {}).get("drum_map")) or {}


def _energy_map(track: dict) -> dict:
    """Карта энергии низа: брейкдауны, ямы, дропы — измеренные, а не
    добытые structure-анализом. У тех уверенность 0.14-0.26."""
    return ((track.get("structure") or {}).get("energy_map")) or {}


def _drums_ok(dmap: dict, t: float, need_bars: float) -> bool:
    if not dmap:
        return True
    import beatgrid

    return beatgrid.drums_at(dmap, t, need_bars)


def candidates_for_incoming(track: dict, bpm: float, limit: int = 6,
                            slot_seconds: float | None = None,
                            need_bars: float = 8.0,
                            want_drums: bool = True,
                            transition_seconds: float = 0.0) -> list[dict]:
    """Откуда запускать ВХОДЯЩИЙ трек.

    Главное здесь — не «пораньше», а «чтобы дроп нового трека пришёл туда,
    куда надо». Раньше вариант «с начала трека» имел максимальный вес
    всегда, и в каждом переходе плана стояло именно оно. Для трека, у
    которого 60-90 секунд эмбиентного интро, это значит: сведение
    закончилось, а новый трек ещё минуту не играет — сет проваливается.
    Поэтому «с начала» получает высокий вес только если интро короткое,
    а иначе вперёд выходит заход за 16 тактов до дропа."""
    structure = track.get("structure") or {}
    duration = float(track.get("duration_seconds") or 0.0)
    bar_seconds = (60.0 / bpm * 4) if bpm else 1.4
    breakdowns = structure.get("breakdowns") or []

    # Дроп входящего — из карты энергии. В structure-анализе дропы
    # находились с уверенностью 0.14-0.26, и у половины треков первым
    # «дропом» числилась нулевая секунда: заход считался от неё, и трек
    # входил в собственную тишину. Карта энергии меряет ровно то событие,
    # которое слышно как дроп, — возврат низа после просадки.
    emap = _energy_map(track)
    energy_drops = [float(x) for x in (emap.get("drops") or [])]
    # один список секунд на оба источника, чтобы ниже не разбираться,
    # откуда он взялся
    drop_times = energy_drops or [float(d.get("time_seconds", 0.0))
                                  for d in (structure.get("drops") or [])]
    first_drop = drop_times[0] if drop_times else None

    # Где у трека реально играют барабаны. Это главный фильтр: входить
    # туда, где у входящего трека тишина, нельзя ни одной техникой —
    # фейдер режет с бита на пустоту, и слышно это как «сломалось».
    dmap = _drum_map(track)
    drums_from = float(dmap.get("drums_start") or 0.0)

    # Куда заводить трек — зависит от того, ЧЕМ сводим, и это ровно то,
    # что диджей делает руками:
    #   бленд — новый трек заводится своим ИНТРО поверх ещё играющего
    #           старого, и барабаны нового приходят как раз тогда, когда
    #           старый уходит. Интро для этого и написано разреженным.
    #   рез   — фейдер бросают туда, где у нового УЖЕ есть бит, иначе
    #           режем в пустоту.
    # Один и тот же фильтр на оба случая ломает бленд: заводить его сразу
    # с барабанов — значит столкнуть две плотные партии лоб в лоб.
    intro_entry = None
    if not want_drums and drums_from > 4.0:
        intro_entry = max(0.0, drums_from - max(transition_seconds, 8 * bar_seconds))

    # «Тело» трека начинается там, где кончился стартовый брейкдаун
    # (анализ помечает интро без барабанов именно так).
    body_start = None
    if breakdowns:
        b0 = breakdowns[0]
        if float(b0.get("start_seconds", 99)) < 5.0:
            body_start = float(b0.get("end_seconds", 0.0))

    intro_bars = round((first_drop or body_start or 0.0) / bar_seconds)
    long_intro = intro_bars > 12
    # Если треку отведено мало времени, длинное интро съест весь его слот:
    # заходить надо ближе к делу.
    short_slot = bool(slot_seconds and slot_seconds < 100.0)

    out: list[dict] = []
    phrases = [float(x) for x in (structure.get("phrase_boundaries") or [])]

    out.append({
        "kind": "start", "time_seconds": 0.0, "bar_index": 0,
        "name": "с начала трека", "label": "с начала трека · 0:00",
        "hint": ("интро успевает развернуться" if not long_intro
                 else f"осторожно: до бита {intro_bars} тактов почти пустого интро"),
        "score": (0.95 if not long_intro else 0.45) * (0.5 if short_slot else 1.0),
    })

    # Главный вариант входа: завести трек так, чтобы его ДРОП пришёлся
    # ровно на конец сведения. Тогда старый трек дозвучивает и уходит, а
    # на его месте раскрывается новый — это и слышно как «красиво свёл».
    # Раньше вход считался «за 16 тактов до дропа» независимо от того,
    # сколько длится само сведение, и дроп приходил то в середину
    # наложения, то через полминуты после него.
    if energy_drops and transition_seconds > 0:
        # отступ — ЦЕЛОЕ число тактов, чтобы точка легла на ту же сетку,
        # что и сам дроп, и осталась на границе фразы трека
        back_bars = max(2.0, round(transition_seconds / bar_seconds))
        for drop_t in energy_drops[:3]:
            t = drop_t - back_bars * bar_seconds
            if t <= 1.0:
                continue
            if not _drums_ok(dmap, t, need_bars) and t < drums_from:
                # заходить туда, где у трека ещё тишина, можно только
                # блендом: у него на то и рассчитано интро
                if want_drums:
                    continue
            out.append({
                "kind": "drop_sync", "time_seconds": round(t, 1),
                "bar_index": round(t / bar_seconds),
                "name": "дроп нового ровно на смене", "label": f"дроп нового ровно на смене · {_fmt(t)}",
                "hint": f"дроп нового придёт ровно в конец сведения, на {_fmt(drop_t)}",
                "score": 1.05 if drop_t == energy_drops[0] else 0.95,
                "exact": True,
            })

    # Заход так, чтобы дроп нового трека пришёл через N тактов после входа.
    if first_drop is not None:
        ladder = ((4, 1.0), (8, 0.92), (16, 0.8)) if short_slot else ((16, 1.0), (32, 0.9), (8, 0.8))
        for bars_before, score in ladder:
            t = first_drop - bars_before * bar_seconds
            if t <= 1.0:
                continue
            out.append({
                "kind": "pre_drop", "time_seconds": round(t, 1),
                "bar_index": round(t / bar_seconds),
                "name": f"за {bars_before} тактов до дропа", "label": f"за {bars_before} тактов до дропа · {_fmt(t)}",
                "hint": f"вход, после которого дроп нового трека приходит ровно через {bars_before} тактов "
                        f"— так его слышно как событие, а не как «что-то началось»",
                "score": score,
            })

    if intro_entry is not None:
        out.append({
            "kind": "intro_lead", "time_seconds": round(intro_entry, 1),
            "bar_index": round(intro_entry / bar_seconds),
            "name": "интро под старый трек", "label": f"интро под старый трек · {_fmt(intro_entry)}",
            "hint": (f"новый заходит своим интро поверх ещё играющего старого, "
                     f"а его барабаны приходят на {_fmt(drums_from)} — ровно когда старый уходит"),
            "score": 1.10,
        })
    if drums_from > 2.0:
        out.append({
            "kind": "body", "time_seconds": round(drums_from, 1),
            "bar_index": round(drums_from / bar_seconds),
            "name": "первый бит", "label": f"первый бит · {_fmt(drums_from)}",
            "hint": "измерено по самому треку: раньше этого места бита нет",
            "score": 1.05 if want_drums else 0.6,
        })
    elif body_start and body_start > 2.0:
        out.append({
            "kind": "body", "time_seconds": round(body_start, 1),
            "bar_index": round(body_start / bar_seconds),
            "name": "конец интро", "label": f"конец интро · {_fmt(body_start)}",
            "hint": "пропустить интро без бита и войти сразу в ритм",
            "score": 0.85,
        })

    for t in phrases[:4]:
        if t > 0.5:
            out.append({
                "kind": "phrase", "time_seconds": round(t, 1),
                "bar_index": round(t / bar_seconds),
                "name": "начало фразы", "label": f"начало фразы · {_fmt(t)}",
                "hint": "пропустить интро и войти сразу в тело трека",
                "score": 0.7,
            })

    for t in drop_times[:2]:
        out.append({
            "kind": "drop", "time_seconds": round(t, 1),
            "bar_index": round(t / bar_seconds),
            "name": "сразу с дропа", "label": f"сразу с дропа · {_fmt(t)}",
            "hint": "жёстко и сразу в лоб — годится для быстрого реза, не для бленда",
            "score": 0.85 if short_slot else 0.5,
        })

    # не предлагаем точки, после которых от трека почти ничего не осталось
    if duration > 30:
        out = [p for p in out if p["time_seconds"] < duration - 60.0 or p["time_seconds"] == 0.0]

    # ГЛАВНЫЙ ФИЛЬТР (только для реза): в точке входа у трека должны
    # играть барабаны и держаться хотя бы всё сведение. Для бленда наоборот
    # — интро без бита это ровно то, что нужно.
    if dmap and want_drums:
        kept = [p for p in out if _drums_ok(dmap, p["time_seconds"], need_bars)]
        dropped = len(out) - len(kept)
        out = kept
        if not out:
            out = [{
                "kind": "body", "time_seconds": round(drums_from, 1),
                "bar_index": round(drums_from / bar_seconds),
                "name": "первый бит", "label": f"первый бит · {_fmt(drums_from)}",
                "hint": "единственное место, где у трека есть бит на всю длину сведения",
                "score": 1.0,
            }]
        elif dropped:
            for p in out:
                p.setdefault("_dropped", dropped)

    # Все точки входа — строго на границе фразы входящего трека. Уходящий
    # мы и так уводим с границы фразы, поэтому после этого обе фразы идут
    # в ногу и продолжают идти всю длину сведения.
    _snap_all_to_phrase(out, bar_seconds, duration)

    for p in out:
        if first_drop is not None:
            bars_to_drop = max(0, round((first_drop - p["time_seconds"]) / bar_seconds))
            p["bars_to_drop"] = bars_to_drop
            p["drop_seconds"] = round(first_drop, 1)
            p["hint"] += f"; дроп через {bars_to_drop} тактов"

    out.sort(key=lambda p: p["score"], reverse=True)
    return out[:limit]


def _point(kind: str, t: float, bar_seconds: float, duration: float,
           latest: float, name: str) -> dict:
    """name — это РОЛЬ точки, без времени: «брейкдаун после второго дропа».

    Время подставляется здесь и всегда одинаково, следом за ролью. Раньше
    подпись собиралась в каждом месте по-своему («дроп на 0:37», «за 30с
    до конца (5:12)»), и выбирать приходилось по секундам, хотя диджей
    выбирает место: первый бит, конец интро, билд, дроп."""
    # Чем ближе к предельно поздней допустимой точке, тем лучше: уводить
    # трек с середины — значит не дать ему доиграть.
    lateness = (t / latest) if latest > 0 else 0.0
    score = KIND_SCORE.get(kind, 0.5) * (0.55 + 0.45 * min(1.0, lateness))
    return {
        "kind": kind,
        "time_seconds": round(t, 1),
        "bar_index": round(t / bar_seconds) if bar_seconds else 0,
        "name": name,
        "label": f"{name} · {_fmt(t)}",
        "hint": KIND_HINT.get(kind, ""),
        "score": round(score, 3),
    }


def _dedupe(points: list[dict], min_gap_seconds: float = 4.0) -> None:
    """Убирает точки, стоящие почти в одном месте — в списке выбора они
    только мешают."""
    kept: list[dict] = []
    # По убыванию оценки: иначе точку, рассчитанную под дроп, вытесняет
    # случайный сосед, оказавшийся в списке раньше.
    for p in sorted(points, key=lambda x: -float(x.get("score") or 0)):
        if all(abs(p["time_seconds"] - k["time_seconds"]) >= min_gap_seconds for k in kept):
            kept.append(p)
    kept.sort(key=lambda x: x["time_seconds"])
    points[:] = kept
