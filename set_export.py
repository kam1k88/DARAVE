"""
Выгрузка готового сета: плейлист для Mixxx и рендер всего микса в файл.

Две разные вещи, и обе нужны:

  * export_m3u()   — плейлист в порядке плана. Mixxx открывает M3U штатно
    (Медиатека -> Импорт плейлиста), после чего сет можно вести руками или
    отдать Auto DJ. Это и есть «нативно в Mixxx, а не только в вебе»:
    треки играет сам Mixxx со своего движка, DARAVE лишь задаёт порядок.

  * render_set()   — весь микс одним аудиофайлом, офлайн. Переиспользует
    demo_render: каждый переход рендерится тем же движком, что и кнопка
    «Демо», а между переходами играет «тело» трека. Получается файл,
    который можно послушать целиком до того, как играть вживую.

Почему нельзя просто «загрузить трек в деку по MIDI»: в Mixxx нет
контрола «загрузи файл по пути». Есть LoadSelectedTrack — он грузит то,
что ВЫДЕЛЕНО в библиотеке, а выделение по имени через MIDI не задать.
Поэтому путь к нативному воспроизведению — через плейлист.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("darave")


def set_master_bpm(strategy: dict, fallback: float = 174.0) -> float:
    """One tempo for the whole set: the median of the tracks that share the
    library centre's tempo family.

    Why not a plain median of the raw BPM values. Half-time and double-time
    readings are the SAME tempo counted at a different rate, so a raw median
    mixes 87 and 174 and lands somewhere in between: that is bug #20 in
    claude/status.md, where the master came out 120-147 for a drum'n'bass set
    and the whole thing played at 70% speed.

    So: find the library centre (tempo.library_center already folds octaves
    and applies the performance-band prior once), keep only the tracks that
    are in the same family as that centre, fold each of them onto the
    centre's octave, and take the median of those. A track at a genuinely
    different tempo does not drag the master anywhere - it is simply not in
    the family and does not vote.
    """
    import tempo

    tracks = strategy.get("tracks") or []
    bpms = []
    for t in tracks:
        try:
            b = float(t.get("bpm") or 0.0)
        except (TypeError, ValueError):
            continue
        if b > 0:
            bpms.append(b)
    if not bpms:
        return float(fallback)

    centre = tempo.library_center(bpms, fallback)
    family = [tempo.fold_to_center(b, centre) for b in bpms
              if tempo.same_family(b, centre)]
    if not family:
        # Nothing shares the centre's family - the centre itself is still the
        # best answer we have, and it is already in the performance band.
        return round(float(centre), 2)

    family.sort()
    return round(float(family[len(family) // 2]), 2)


def export_m3u(strategy: dict, out_path: str, title: str = "DARAVE set") -> dict:
    """Пишет M3U8 с треками сета в порядке плана.

    M3U8 (а не M3U) — потому что в именах у диджея кириллица, а M3U по
    стандарту в latin-1; Mixxx читает оба, но кириллица корректна только
    в UTF-8 варианте."""
    tracks = strategy.get("tracks") or []
    missing = [t["name"] for t in tracks if not t.get("path")]
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    written = 0
    # В плейлист идёт .stem.mp4, если он собран: именно его надо
    # заводить на деку, иначе у деки нет стемовых фейдеров и приёмы по
    # слоям выродятся в обычный кроссфейд. Позиции при этом те же —
    # мастер стемового файла совпадает с .mp3 сэмпл в сэмпл (измерено).
    import stem_mp4 as _sm

    stems_used = 0
    for t in tracks:
        path = t.get("path")
        if not path:
            continue
        play = _sm.playable_path(path)
        if play != path:
            stems_used += 1
        dur = int(t.get("duration_seconds") or 0)
        lines.append(f"#EXTINF:{dur},{t['name']}")
        lines.append(play)
        written += 1

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")

    return {"path": out_path, "tracks": written, "skipped": missing,
            "stem_files": stems_used}


def cue_sheet(strategy: dict) -> str:
    """Текстовая раскладка сета: во сколько какой трек и чем сводится.
    То, что диджей держит перед глазами, если ведёт руками.

    Время берём из плана (set_time_seconds), а не из длин файлов: трек
    почти никогда не играет целиком — у него есть точка входа и точка
    ухода, и раскладка должна показывать именно их."""
    rows = ["# DARAVE — раскладка сета", ""]
    tracks = strategy.get("tracks") or []
    transitions = {t["index"]: t for t in strategy.get("transitions") or []}
    layout = strategy.get("layout") or {}
    if layout.get("slot_seconds"):
        rows.append(f"Каждый трек играет около {layout['slot_seconds']:.0f}с "
                    f"({len(tracks)} треков, цель {layout.get('target_minutes')} мин)")
        if layout.get("warning"):
            rows.append(f"! {layout['warning']}")
        rows.append("")

    for i, tr in enumerate(tracks):
        clock = float(tr.get("set_time_seconds") or 0.0)
        mm, ss = divmod(int(clock), 60)
        rows.append(f"{mm:02d}:{ss:02d}  {tr['name']}")
        rows.append(f"        {tr['bpm']:.0f} BPM · {tr['camelot']} · EL{tr.get('el', '?')}"
                    f" · играет {float(tr.get('airtime_seconds') or 0):.0f}с")
        t = transitions.get(i)
        if t:
            fp = t.get("from_point") or {}
            tp = t.get("to_point") or {}
            rows.append(f"        -> {t['technique_name']} ({t['bars']:g} тактов): "
                        f"уводим {fp.get('label', '?')}, входим {tp.get('label', '?')}")
            rows.append(f"           {t.get('tempo_note', '')}")
        rows.append("")
    total = float(strategy.get("total_duration_minutes") or 0.0)
    rows.append(f"Итого: {len(tracks)} треков, {total:.0f} мин")
    return "\n".join(rows)


def _match_join_level(a, b, overlap_n: int, ramp_bars_n: int):
    """Подгоняет уровень входящего куска под уходящий на стыке.

    Кусок k заканчивается уже вошедшим треком B — иногда на его тихой
    части; кусок k+1 начинается тем же B, но на его финале, то есть громко.
    Стык между ними — скачок на 5-7 dB, слышный как «дёрнуло громкость».
    Ровно это диджей делает ручкой trim: подхватывает новый трек на уровне
    предыдущего и за несколько тактов отпускает к его собственному."""
    import numpy as np

    win = max(1024, overlap_n * 2)
    ta, tb = a[-win:], b[:win]
    ra = float(np.sqrt(np.mean(ta ** 2))) if len(ta) else 0.0
    rb = float(np.sqrt(np.mean(tb ** 2))) if len(tb) else 0.0
    if ra < 1e-5 or rb < 1e-5:
        return b
    g0 = float(np.clip(ra / rb, 0.35, 2.5))
    if abs(g0 - 1.0) < 0.05:
        return b
    ramp_n = int(min(len(b), max(overlap_n * 2, ramp_bars_n)))
    x = np.linspace(0.0, 1.0, ramp_n)
    x = x * x * (3.0 - 2.0 * x)  # smoothstep: без излома на конце рампы
    env = np.ones(len(b))
    env[:ramp_n] = g0 + (1.0 - g0) * x
    return b * (env[:, None] if b.ndim > 1 else env)


def _equal_power_join(a, b, overlap_n: int):
    """Склейка двух кусков через кроссфейд постоянной мощности.

    Раньше куски просто ставились встык (np.concatenate) — каждые ~18
    секунд был слышен обрыв: старый кусок кончался на полуслове, новый
    начинался с другого места трека. Теперь последний такт предыдущего
    куска и вводный такт следующего накладываются: оба содержат ОДИН И
    ТОТ ЖЕ трек (конец перехода A->B и начало перехода B->C), поэтому
    тембр и тональность совпадают, и стык читается как монтажная склейка,
    а не как обрыв."""
    import numpy as np

    overlap_n = int(max(0, min(overlap_n, len(a), len(b))))
    if overlap_n < 64:
        return np.concatenate([a, b])
    x = np.linspace(0.0, 1.0, overlap_n)
    ang = x * (np.pi / 2.0)
    co, si = np.cos(ang), np.sin(ang)
    if a.ndim > 1:
        co, si = co[:, None], si[:, None]
    tail = a[-overlap_n:] * co + b[:overlap_n] * si
    return np.concatenate([a[:-overlap_n], tail, b[overlap_n:]])


def _trim_trailing_silence(wav_path: str, sr: int,
                           floor_db: float = -60.0,
                           keep_seconds: float = 0.35) -> float:
    """Обрезает тишину в конце готового WAV и возвращает его длину в секундах.

    Зачем вообще. Последний трек дописывается «столько же, сколько играли
    остальные», и если он кончился раньше отведённого хвоста, в файле
    остаётся ровная тишина — иногда на минуту. В плеере это выглядит как
    сет, который завис.

    Как. Файл читается блоками С КОНЦА до первого места громче порога, и
    дальше он ПРОСТО ОБРЕЗАЕТСЯ: PCM WAV — это заголовок и сплошной кусок
    данных, поэтому достаточно укоротить файл и поправить два поля длины
    в заголовке. Переписывать целиком нельзя: часовой сет это полгигабайта,
    и копия ради выброшенной тишины стоит дороже самой тишины.

    Ошибка здесь не должна ронять готовый сет: файл уже отрендерен, и если
    что-то пошло не так — возвращаем длину как есть.
    """
    import struct

    try:
        import numpy as _np
        import soundfile as _sf

        with _sf.SoundFile(wav_path) as f:
            total = len(f)
            channels = f.channels
            if total <= 0:
                return 0.0
            thr = 10.0 ** (float(floor_db) / 20.0)
            block = max(1, int(sr))          # секунда за раз
            end = total
            pos = total
            while pos > 0:
                start = max(0, pos - block)
                f.seek(start)
                buf = f.read(pos - start, dtype="float32", always_2d=True)
                loud = _np.nonzero(_np.max(_np.abs(buf), axis=1) > thr)[0]
                if len(loud):
                    end = start + int(loud[-1]) + 1
                    break
                pos = start
            else:
                end = 0
        end = min(total, end + int(max(0.0, keep_seconds) * sr))
        if end >= total:
            return round(total / float(sr), 2)

        # --- укорачиваем файл на месте ---
        with open(wav_path, "r+b") as fh:
            head = fh.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return round(total / float(sr), 2)
            data_off = bits = None
            while True:
                hdr = fh.read(8)
                if len(hdr) < 8:
                    return round(total / float(sr), 2)
                cid, csize = struct.unpack("<4sI", hdr)
                if cid == b"fmt ":
                    fmt = fh.read(csize)
                    bits = struct.unpack("<H", fmt[14:16])[0]
                elif cid == b"data":
                    data_off = fh.tell()
                    break
                else:
                    fh.seek(csize + (csize & 1), 1)
            if data_off is None or not bits:
                return round(total / float(sr), 2)
            new_bytes = end * channels * (bits // 8)
            fh.seek(data_off - 4)
            fh.write(struct.pack("<I", new_bytes))
            fh.seek(4)
            fh.write(struct.pack("<I", data_off + new_bytes - 8))
        os.truncate(wav_path, data_off + new_bytes)
        return round(end / float(sr), 2)
    except Exception as exc:                      # noqa: BLE001 — сет уже готов
        logger.info("не смог обрезать тишину в конце: %s", exc)
        try:
            import soundfile as _sf2

            with _sf2.SoundFile(wav_path) as f:
                return round(len(f) / float(f.samplerate), 2)
        except Exception:
            return 0.0


def _trim_to_bars(chunk, bar_n: int):
    """Обрезает кусок до целого числа тактов — иначе на каждой склейке
    пульс сдвигается на огрызок такта и сет начинает спотыкаться."""
    if bar_n <= 0 or len(chunk) < bar_n * 2:
        return chunk
    return chunk[: (len(chunk) // bar_n) * bar_n]


def _blend_override(transition: dict) -> dict | None:
    """Длина сведения, выбранная планом, — в параметры техники.

    План считает её по контексту (4 такта, если входящему нечем
    прикрыться, 12 — если уходящий сам уходит в брейкдаун), но техника об
    этом не узнает, если не передать: она возьмёт своё умолчание, и весь
    расчёт останется на бумаге.
    """
    out = {}
    bars = transition.get("blend_bars") or transition.get("bars")
    try:
        bars = float(bars)
        if bars > 0:
            out["blend_bars"] = bars
    except (TypeError, ValueError):
        pass
    try:
        duck = float(transition.get("mid_duck") or 0.0)
        if duck > 0:
            out["mid_duck"] = duck
    except (TypeError, ValueError):
        pass
    return out or None


def set_mix_profile(strategy: dict, transition: dict) -> dict | None:
    """Профиль ведения фейдеров для ЭТОГО перехода.

    Порядок: правка конкретного перехода -> умолчание плана -> ничего
    (форму ведёт приём).

    Зачем понадобился плановый уровень. Профиль появлялся только у швов,
    отобранных в Clipping: там диджей ведёт фейдеры руками, и профиль
    уезжает вместе со швом. А план, собранный в ControlC, не нёс его
    вовсе — и превью с полным сетом собирались по кроссфейду приёма.
    Получалось ровно то, на что диджей и пожаловался: «при сведении
    используется фейдер для каждого трека, а сведение превью и микса до
    сих пор использует логику, будто треки сводятся единым
    кроссфейдером». Две половины программы вели фейдеры по-разному.
    """
    own = (transition or {}).get("mix_profile") \
        or ((transition or {}).get("params") or {}).get("mix_profile")
    if own:
        return own
    return (strategy or {}).get("mix_profile") or None


def _point_seconds(point) -> float | None:
    if not isinstance(point, dict):
        return None
    v = point.get("time_seconds")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0.5 else None


# Сколько тактов хвоста оставлять после сведения в превью. Четыре — это
# одна фраза: слышно, чем сведение кончилось, и не начинается прослушивание
# интро входящего трека.
PREVIEW_TAIL_BARS = 4.0


def render_set(strategy: dict, out_path: str, sr: int = 44100,
               seconds_between: float = 50.0,
               max_minutes: float = 30.0,
               join_bars: float = 1.0,
               progress=None,
               sample: int = 10,
               fmt: str = "mp3") -> dict:
    """Рендерит весь сет в один WAV.

    Каждый переход рендерится тем же движком, что и кнопка «Демо», в тех
    точках треков, которые выбраны в плане (from_point / to_point) — раньше
    рендер эти точки игнорировал и всегда брал хвост одного трека и начало
    другого, из-за чего план и то, что слышно, расходились.

    seconds_between — сколько времени отводится одному переходу. Это не
    «сколько играет трек», а окно, в которое техника должна уложиться
    целиком: если она не влезает, demo_render ужимает её в 2/4/8 раз по
    тактам, но НЕ обрывает на середине.
    """
    import numpy as np
    import soundfile as sf

    import demo_render

    tracks = strategy.get("tracks") or []
    transitions = strategy.get("transitions") or []
    if not transitions:
        raise ValueError("В плане нет переходов — нечего рендерить.")

    by_name = {t["name"]: t for t in tracks}
    master = set_master_bpm(strategy)
    pieces: list = []
    bar_ns: list[int] = []
    solos: list[float] = []
    rendered = 0
    skipped: list[str] = []
    used_techniques: dict[str, int] = {}
    budget = max_minutes * 60.0
    used = 0.0

    # 47 сведений подряд без музыки между ними — это не превью, это каша:
    # материал меняется каждые 6 секунд. Берём выборку по всему сету.
    picked = transitions
    if sample and len(transitions) > sample:
        step = len(transitions) / sample
        picked = [transitions[int(i * step)] for i in range(sample)]

    for t in picked:
        if used >= budget:
            skipped.append(f"после {rendered} переходов упёрлись в лимит {max_minutes:g} мин")
            break
        a, b = by_name.get(t["from"]), by_name.get(t["to"])
        if not a or not b or not a.get("path") or not b.get("path"):
            skipped.append(f"{t['from']} -> {t['to']}: нет пути к файлу")
            continue
        if not (os.path.exists(a["path"]) and os.path.exists(b["path"])):
            skipped.append(f"{t['from']} -> {t['to']}: файл не найден на диске")
            continue

        tmp = f"{out_path}.part{rendered}.wav"
        try:
            meta = demo_render.render_demo(
                t["technique_id"], a["path"], b["path"],
                a["bpm"], b["bpm"], tmp,
                # длину сведения выбрал план (4-12 тактов по контексту);
                # без этого техника отработает своим умолчанием
                param_overrides=_blend_override(t),
                max_seconds=min(seconds_between, max(8.0, budget - used)),
                source_at=_point_seconds(t.get("from_point")),
                target_at=_point_seconds(t.get("to_point")),
                # Профиль ввода — тот же, с которым шов слушали и
                # одобряли. Без него превью сета звучит иначе, чем клип.
                mix_profile=set_mix_profile(strategy, t),
                # Тот же мастер-темп, что и у полного сета. Раньше здесь
                # стоял None (темп ведёт уходящий), а render_full_set
                # собирал всё под общий мастер — то есть превью и готовый
                # файл звучали по-разному, и слушать превью, чтобы решить
                # про сет, было бессмысленно. В непрерывном миксе темп
                # обязан быть один: иначе он прыгал бы на каждой границе
                # тела и сведения. Цена — сдвиг по высоте: на реальной
                # библиотеке в среднем 0.15 полутона, максимум 0.61 у
                # самого далёкого от мастера трека. Порядок треков её не
                # меняет — мастер считается по библиотеке, а не по
                # соседям, — но зато убирает питч-тяжку ВНУТРИ переходов.
                master_bpm=set_master_bpm(strategy),
                # НЕ нормируем каждый кусок: это ровно то, что убило звук.
                # Подтянув каждые 20 секунд к одному RMS, мы сравняли
                # брейкдаун с дропом — размах громкости всего микса стал
                # 1.8 dB против 7-20 dB у самих треков. Музыка перестаёт
                # дышать, и весь сет звучит одинаково громко и одинаково
                # скучно, сколько его ни слушай.
                normalize_output=False,
            )
        except (NotImplementedError, ValueError, FileNotFoundError) as exc:
            skipped.append(f"{t['from']} -> {t['to']}: {exc}")
            continue

        chunk, chunk_sr = sf.read(tmp, dtype="float32")
        os.remove(tmp)
        chunk = demo_render._to_stereo(chunk)     # не схлопываем в моно
        if chunk_sr != sr and len(chunk):
            import librosa
            chunk = librosa.resample(chunk, orig_sr=chunk_sr, target_sr=sr, axis=0)

        bar_n = int(round(float(meta.get("bar_seconds") or 1.4) * sr))
        # Хвост после сведения обрезаем до нескольких тактов.
        #
        # Превью существует, чтобы слушать ШВЫ, а не интро входящих
        # треков. А хвост куска — это и есть интро: входящий заведён в
        # своё начало, и там у него 15-20 dB ниже, чем в сведении.
        # Сшитые подряд, такие хвосты читаются как провалы между
        # сведениями — диджей на это и пожаловался: «между треками
        # образуется пустое пространство». Пары тактов хватает, чтобы
        # услышать, чем сведение кончилось.
        sync = float(meta.get("sync_time_seconds") or 0.0)
        bar_sec = float(meta.get("bar_seconds") or 1.4)
        blend_bars = float(t.get("blend_bars") or t.get("bars") or 8)
        end_of_blend = sync + blend_bars * bar_sec
        tail_limit = int(round((end_of_blend + PREVIEW_TAIL_BARS * bar_sec) * sr))
        chunk = chunk[:max(bar_n * 2, min(len(chunk), tail_limit))]
        chunk = _trim_to_bars(chunk, bar_n)
        pieces.append(chunk)
        solos.append(_solo_level(chunk, sr, meta.get("sync_time_seconds")) or 0.0)
        bar_ns.append(bar_n)
        used += len(chunk) / sr
        rendered += 1
        used_techniques[t["technique_id"]] = used_techniques.get(t["technique_id"], 0) + 1
        if progress:
            progress(rendered, len(picked))

    if not pieces:
        raise ValueError("Ни один переход не удалось отрендерить: " + "; ".join(skipped[:3]))

    # Куски выравниваются по ОБЩЕМУ уровню, а не каждый по своему.
    #
    # Раньше каждый кусок подгонялся под свой соло-участок — и это ровно
    # та же ошибка «двух разных мерок», из-за которой в полном сете
    # переходы шли на 9 dB громче тел (см. claude/seam-loudness.md).
    # В превью она читается иначе: куски скачут по уровню друг
    # относительно друга. Общая мерка — медиана соло-уровней: половина
    # кусков её и так держит, а выбивающиеся приводятся к ней.
    import numpy as _np

    live = [x for x in solos if x > 0]
    common = float(_np.median(live)) if live else SET_DECK_RMS
    pieces = [_ride_to_reference(p, sr, ref_rms=common) for p in pieces]

    mix = pieces[0]
    joins: list[float] = []
    for i in range(1, len(pieces)):
        overlap = int(round(join_bars * bar_ns[i]))
        nxt = _match_join_level(mix, pieces[i], overlap, bar_ns[i] * 4)
        joins.append(round((len(mix) - overlap / 2) / sr, 2))
        mix = _equal_power_join(mix, nxt, overlap)

    # общий уровень — один раз на весь файл, чтобы динамика внутри осталась
    mix = demo_render._soft_limit(mix * SET_MAKEUP)
    peak = float(np.max(np.abs(mix))) if len(mix) else 0.0
    if peak > 0.98:
        mix = mix * (0.98 / peak)

    enc = mp3_encoder()
    want_mp3 = str(fmt).lower() == "mp3" and enc["name"] is not None
    if str(fmt).lower() == "mp3" and not want_mp3:
        skipped.append("нет ни lameenc, ни ffmpeg — сохраняю WAV")
    elif want_mp3 and not enc["full_quality"]:
        skipped.append("mp3 пишет libsndfile (~74 кбит/с) — поставьте lameenc "
                       "для 320: pip install lameenc")
    if want_mp3:
        out_path = _write_mp3(out_path, mix, sr)
    else:
        out_path = str(Path(out_path).with_suffix(".wav"))
        sf.write(out_path, mix, sr, subtype="PCM_16")

    return {
        "path": out_path,
        "format": "mp3" if want_mp3 else "wav",
        "bitrate": enc["bitrate"] if want_mp3 else 1411,
        "encoder": enc["name"] if want_mp3 else "wav",
        "sampled": len(picked),
        "transitions_rendered": rendered,
        "transitions_total": len(transitions),
        "duration_seconds": round(len(mix) / sr, 1),
        "techniques_used": used_techniques,
        "join_seconds": joins,
        "chunk_seconds": [round(len(p) / sr, 1) for p in pieces],
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Полный сет одним файлом
# ---------------------------------------------------------------------------

# Общий добор громкости всего сета. Деки приведены к -18 dBFS по RMS,
# чтобы сумма двух не клиппировала; готовый файл при этом тихий, поэтому
# в конце добавляем фиксированные +7.6 dB — одинаковые для тел и
# переходов, так что относительные уровни не меняются.
SET_MAKEUP = 2.4

# Уровень, к которому приведена ОДНА дека (demo_render.TARGET_RMS).
# Всё, что в переходе выше него, — это сумма двух дек, а не музыка.
#
# Замер на реальном сведении (ST-08, Enei -> Paramount, обе деки со
# слоями):
#
#     кусок перехода   -3.6 dBFS RMS, лимитер давит 2.4 dB, пик -0.4 dB
#     тело трека      -13.1 dBFS RMS, лимитер не работает,  пик -4.1 dB
#
# То есть на стыке скачок почти в 10 dB, и весь переход идёт через
# постоянно работающий ограничитель — это и слышно как перегруз. В демо
# этого не было слышно, потому что демо нормализуется целиком
# (normalize_output=True), а в сет кусок кладётся как есть.
#
# Приём тут ни при чём: на ST-09, где играет по одному слою с деки,
# разница тела и перехода 0.9 dB. Виновата именно СУММА двух полных дек
# в приёмах, где кроссфейдер стоит посередине и деки не приглушает.
SET_DECK_RMS = 0.125
# Насколько сильно позволено срезать. Потолок нужен: без него выравнивание
# превращается в компрессор, и тихий переход (приём разобрал деку на слои и
# играет один из них) вытянулся бы до уровня соседей, то есть перестал бы
# быть тихим.
#
# Было 9 dB — и этого не хватало. Замер на настоящем плане диджея: на
# стыке требовалось срезать 11.9 dB, кусок упирался в потолок и оставался
# на 2.8 dB громче соседей. Сумма двух полных дек — это уже 6 dB, плюс
# собственная динамика материала; 12 dB покрывает худший случай с запасом.
SET_RIDE_MAX_CUT_DB = 12.0

# Ниже этого уровня опора выравнивателя не опускается.
#
# Выравниватель приводит кусок перехода к уровню соседних тел: начало — к
# тому, чем кончилось предыдущее тело, конец — к тому, с чего начнётся
# следующее. Обычно это ровно то, что нужно. Но если переход попал МЕЖДУ
# спадом одного трека и тихим интро другого, обе опоры оказываются
# тихими разом — и выравниватель честно давит весь переход к ним, до
# упора в свой потолок.
#
# Замер на сете диджея (45 минут, фонк): три места, где уровень падал на
# 11.7, 11.7 и 13.1 dB ОДИНАКОВО во всех полосах и держался так 4-13
# секунд. Одинаково во всех полосах — это не фильтр и не брейкдаун, это
# смена усиления; а 12 dB — ровно потолок среза. Снаружи слышно как
# «фейдеры кривые, провалы в переходах».
#
# Шесть децибел — это вдвое тише обычного: достаточно, чтобы переход в
# тихое место не выпирал, и мало, чтобы он не исчезал.
SET_RIDE_REF_FLOOR_DB = 6.0


def _level_p90(m, sr: int) -> float:
    """«Как громко здесь бывает» — 90-я процентиль скользящего СКЗ.

    ОДНА функция на все измерения уровня в сете, и это главное её
    свойство. Пока тела мерились средним по треку, а переходы — 90-й
    процентилью своего соло-участка, две величины назывались «уровнем», а
    означали разное: у трека с сильной динамикой p90 дропа вдвое выше
    среднего. Переход честно подгонялся под свою мерку, тело под свою, и
    на стыке получался скачок, которого никто не закладывал.

    Замер на сете диджея (44 минуты, 20 переходов): области, где работал
    ограничитель, шли на -6.1 dBFS против -15.3 у остального — **+9.2 dB
    на каждом переходе**. Ограничитель там работал непрерывно, и это и
    слышно как «овер дБ»."""
    import numpy as np

    m = np.asarray(m, dtype="float64")
    if len(m) < sr // 8:
        return float(np.sqrt((m ** 2).mean())) if len(m) else 0.0
    step, win = 128, max(1, int(0.35 * sr))
    csum = np.concatenate([[0.0], np.cumsum(m ** 2)])
    grid = np.arange(0, len(m), step)
    lo = np.maximum(0, grid - win // 2)
    hi = np.minimum(len(m), grid + win // 2)
    env = np.sqrt((csum[hi] - csum[lo]) / np.maximum(1, hi - lo))
    if not len(env):
        return float(np.sqrt((m ** 2).mean()))
    return float(np.percentile(env, 90))


def _mono_of(y):
    import numpy as np

    return (y.mean(axis=1) if getattr(y, "ndim", 1) > 1 else y).astype("float64")


def _solo_level(y, sr: int, solo_seconds: float | None) -> float:
    """Уровень куска ТАМ, ГДЕ ИГРАЕТ ОДНА ДЕКА.

    Это и есть та громкость, с которой переход обязан состыковаться с
    телом трека: до входа второй деки в куске звучит ровно тот же
    уходящий трек, что играл до сведения.

    Брать вместо этого общую константу (уровень, к которому приведена
    дека) нельзя: у трека своя динамика, и если сведение назначено на
    дроп, дека там честно громче своего среднего. Замер показал ровно
    это — на ST-13 первые полторы секунды куска, где играет ОДИН
    уходящий трек, идут на -8.4 dBFS при среднем уровне деки -18. С
    константой их срезало бы на 9 dB, то есть в сет добавилась бы дырка
    ровно там, где мы её и чинили, только с другой стороны.
    """
    import numpy as np

    if not solo_seconds or solo_seconds <= 0.2:
        return 0.0
    n = min(len(y), int(solo_seconds * sr))
    if n < sr // 4:
        return 0.0
    m = (y[:n].mean(axis=1) if y.ndim > 1 else y[:n]).astype("float64")

    # Не среднее, а ВЕРХНЯЯ граница уровня одиночной деки.
    #
    # Среднее — плохой ориентир: скользящее СКЗ на окне в одну долю
    # гуляет вокруг него на пару децибел (бочка громче паузы между
    # бочками), и выравнивание начинало резать сильные доли ЕДИНСТВЕННОЙ
    # играющей деки. Замер: соло-участок терял 2.3 dB там, где резать
    # нечего. Девяностая процентиль тех же окон описывает «как громко эта
    # дека бывает» и не срабатывает на собственном ритме трека.
    return _level_p90(m, sr)


def _ride_to_reference(y, sr: int, ref_rms: float = SET_DECK_RMS,
                       window_sec: float = 0.35, attack_sec: float = 0.03,
                       release_sec: float = 0.8,
                       max_cut_db: float = SET_RIDE_MAX_CUT_DB):
    """Приводит кусок перехода к уровню одиночной деки — но только вниз.

    Почему не одним коэффициентом на весь кусок: в куске есть и
    совмещение (две деки), и хвост, где играет уже только входящий. Один
    коэффициент, посчитанный по среднему, сделал бы хвост на 9 dB тише
    тела — то есть дырку после каждого сведения.

    Почему огибающая несимметричная. Сначала усиление сглаживалось
    симметричным окном в полторы секунды — и срез «протекал назад», в те
    полторы секунды начала куска, где играет ещё ОДНА дека. Замер: на
    ST-08 соло-участок терял 2.8 dB ни за что. Теперь как у нормального
    компрессора: быстрая атака (30 мс — среагировать на вход второй
    деки) и долгое отпускание (0.8 с — не качать в такт), и будущее на
    прошлое не влияет вовсе.

    Только вниз и не глубже max_cut_db: это выравнивание, а не
    компрессия. Тихий переход (приём разобрал деку на слои и играет один
    из них) обязан остаться тихим — это музыка, а не перебор."""
    import numpy as np

    ref = np.asarray(ref_rms, dtype="float64")
    if len(y) < sr // 4 or not ref.size or float(ref.min()) <= 0:
        return y
    m = _mono_of(y)

    # Скользящее СКЗ через интеграл квадратов, но считаем его на
    # прореженной сетке: 128 отсчётов — это 2.9 мс, мельче любой атаки.
    step = 128
    csum = np.concatenate([[0.0], np.cumsum(m ** 2)])
    grid = np.arange(0, len(m), step)
    win = max(1, int(window_sec * sr))
    lo = np.maximum(0, grid - win // 2)
    hi = np.minimum(len(m), grid + win // 2)
    env = np.sqrt((csum[hi] - csum[lo]) / np.maximum(1, hi - lo))

    floor = 10.0 ** (-max_cut_db / 20.0)
    # Цель может быть не числом, а КРИВОЙ: у перехода два соседа, и
    # уровни у них разные. Уходящее тело кончилось на своём уровне,
    # входящее начнётся на своём — значит и вести переход надо от первого
    # ко второму, как диджей ведёт два трима. Одно число на весь кусок
    # состыковало бы его только с одной стороной.
    if ref.ndim:
        # цель задана концами — растягиваем её по сетке в ДЕЦИБЕЛАХ:
        # линейная интерполяция амплитуд дала бы кривую, которая на
        # середине идёт не по слуху, а по арифметике
        ref_db = np.interp(grid, np.linspace(0, len(m) - 1, len(ref)),
                           20.0 * np.log10(np.maximum(ref, 1e-9)))
        ref = 10.0 ** (ref_db / 20.0)
    want = np.clip(ref / np.maximum(env, 1e-9), floor, 1.0)

    # Огибающая усиления с атакой и отпусканием, по прореженной сетке.
    dt = step / sr
    a_att = float(np.exp(-dt / max(1e-4, attack_sec)))
    a_rel = float(np.exp(-dt / max(1e-4, release_sec)))
    g = np.empty_like(want)
    cur = 1.0
    for i, w in enumerate(want):
        coef = a_att if w < cur else a_rel
        cur = w + (cur - w) * coef
        g[i] = cur

    gain = np.interp(np.arange(len(m)), grid, g)
    gain = np.minimum(gain, 1.0)
    return y * (gain[:, None] if y.ndim > 1 else gain)


def _write_body(fh, path: str, start: float, end: float, rate: float, sr: int,
                ref_rms: float, fade_n: int, tail_sec: float = 4.0) -> tuple:
    """Пишет «тело» трека — кусок, который просто играет между сведениями.

    Возвращает (сколько секунд легло, уровень последних tail_sec секунд).
    Второе число — то, с чем обязан состыковаться следующий переход.
    Меряется НА ТОМ, ЧТО РЕАЛЬНО УШЛО В ФАЙЛ, после добора и ограничителя:
    вычислять ожидаемый уровень по формуле значило бы завести вторую
    правду о громкости рядом с первой, а расходились они ровно на стыке.

    Растягиваем тем же rate, что и в переходе, иначе на стыке тела и
    сведения менялся бы темп."""
    import numpy as np

    import demo_render

    dur = end - start
    if dur <= 0.2:
        return 0.0, 0.0
    import librosa

    y, _ = librosa.load(path, sr=sr, mono=False, offset=max(0.0, start), duration=dur)
    y = demo_render._to_stereo(y)          # поток сета двухканальный
    if len(y) < sr * 0.05:
        return 0.0, 0.0
    y = demo_render._stretch(y, sr, rate)
    # ТОЛЬКО опорный уровень трека. _normalize_output здесь применять
    # нельзя: он подтягивает КАЖДЫЙ кусок к одному RMS, то есть стирает
    # собственную динамику трека — брейкдаун становился бы такой же
    # громкости, что и дроп, а на границе кусков прыгал бы уровень.
    y = demo_render._match_loudness(y, ref_rms)
    y = demo_render._soft_limit(y * SET_MAKEUP)
    # микрофейд по краям: тело и переход растягивались разными проходами
    # фазового вокодера, на границе возможен щелчок
    if fade_n > 1 and len(y) > fade_n * 2:
        y[:fade_n] *= demo_render._env(np.linspace(0.0, 1.0, fade_n), y)
        y[-fade_n:] *= demo_render._env(np.linspace(1.0, 0.0, fade_n), y)
    fh.write(y.astype("float32"))
    tail = _level_p90(_mono_of(y[-int(tail_sec * sr):]), sr)
    return len(y) / sr, tail


def _write_mp3(path: str, mix, sr: int, bitrate: int = 320) -> str:
    """Пишет mp3 320. Через libsndfile этого сделать нельзя.

    soundfile.write(format="MP3") отдаёт ~74 кбит/с, а с самым низким
    compression_level упирается в 128 — своего битрейта у него не задать.
    Поэтому кодируем LAME напрямую (пакет lameenc, чистый pip, без
    системных зависимостей), с запасными вариантами на ffmpeg и, в самом
    крайнем случае, на тот же libsndfile.
    """
    import numpy as np

    out = Path(path).with_suffix(".mp3")
    data = np.asarray(mix)
    if data.ndim == 1:
        data = data[:, None]
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")

    if data.ndim < 2 or data.shape[1] < 2:
        logger.warning("mp3: на входе моно (%s) — рендер собрал одноканальный микс",
                       data.shape)

    try:
        import lameenc

        enc = lameenc.Encoder()
        enc.set_bit_rate(bitrate)
        enc.set_in_sample_rate(sr)
        enc.set_channels(pcm.shape[1])
        enc.set_quality(2)
        with open(out, "wb") as fh:
            step = sr * 30
            for i in range(0, len(pcm), step):
                fh.write(enc.encode(pcm[i:i + step].tobytes()))
            fh.write(enc.flush())
        return str(out)
    except Exception:
        # Молчать здесь нельзя: дальше идут запасные кодировщики, и самый
        # последний из них пишет ~74 кбит/с. Именно так «320» превращались
        # в глухой файл без единой строки в логе.
        logger.warning("mp3: lameenc не сработал, перехожу к запасному кодировщику",
                       exc_info=True)

    import shutil as _sh
    if _sh.which("ffmpeg"):
        import subprocess
        import soundfile as sf

        tmp = str(out) + ".tmp.wav"
        sf.write(tmp, data, sr, subtype="PCM_16")
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp,
                            "-b:a", f"{bitrate}k", str(out)], check=True)
            return str(out)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    import soundfile as sf
    logger.warning("mp3 пишет libsndfile — битрейт будет ~74-128 кбит/с, "
                   "а не 320. Поставьте lameenc: pip install lameenc")
    sf.write(str(out), data, sr, format="MP3", subtype="MPEG_LAYER_III")
    return str(out)


def _wav_to_mp3(wav_path: str, bitrate: int = 320, progress=None) -> str:
    """Перекодирует готовый WAV в mp3, читая его блоками.

    Читать 93-минутный сет целиком нельзя: это 989 МБ на диске и около
    2 ГБ во float32 в памяти, плюс столько же на копию под int16. Машина
    уходит в своп, а диджей четыре минуты смотрит на замерший индикатор,
    потому что сведения уже посчитаны, а файла ещё нет.

    Блоками по 30 секунд расход памяти — единицы мегабайт, и по дороге
    можно честно показывать процент.
    """
    out = Path(wav_path).with_suffix(".mp3")

    try:
        import lameenc
        import soundfile as sf

        with sf.SoundFile(wav_path) as src:
            enc = lameenc.Encoder()
            enc.set_bit_rate(bitrate)
            enc.set_in_sample_rate(src.samplerate)
            enc.set_channels(src.channels)
            enc.set_quality(2)
            total = max(1, len(src))
            step = src.samplerate * 30
            done = 0
            with open(out, "wb") as fh:
                while True:
                    block = src.read(step, dtype="int16", always_2d=True)
                    if not len(block):
                        break
                    fh.write(enc.encode(block.tobytes()))
                    done += len(block)
                    if progress:
                        try:
                            progress(min(1.0, done / total))
                        except Exception:
                            pass
                fh.write(enc.flush())
        return str(out)
    except Exception:
        logger.warning("mp3: потоковый lameenc не сработал, пробую запасные пути",
                       exc_info=True)
        try:
            if out.exists():
                out.unlink()
        except OSError:
            pass

    import shutil as _sh
    if _sh.which("ffmpeg"):
        import subprocess

        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
                        "-b:a", f"{bitrate}k", str(out)], check=True)
        return str(out)

    # последний вариант — libsndfile, целиком в память и без 320
    import soundfile as sf

    logger.warning("mp3 пишет libsndfile — битрейт будет ~74-128 кбит/с, а не %s. "
                   "Поставьте lameenc: pip install lameenc", bitrate)
    data, _sr = sf.read(wav_path, dtype="float32")
    sf.write(str(out), data, _sr, format="MP3", subtype="MPEG_LAYER_III")
    return str(out)


def mp3_encoder() -> dict:
    """Кто именно закодирует mp3 и на каком битрейте.

    Важно различать: lameenc и ffmpeg дают настоящие 320 кбит/с, а
    libsndfile — нет. Он игнорирует запрошенный битрейт, упирается в 128
    и по умолчанию отдаёт ~74 кбит/с. Раньше все трое считались просто
    "mp3 поддерживается", и диджей получал глухой файл, не понимая почему.
    """
    try:
        import lameenc  # noqa: F401

        return {"name": "lameenc", "bitrate": 320, "full_quality": True}
    except Exception:
        pass
    import shutil as _sh
    if _sh.which("ffmpeg"):
        return {"name": "ffmpeg", "bitrate": 320, "full_quality": True}
    try:
        import soundfile as sf

        if "MP3" in sf.available_formats():
            return {"name": "libsndfile", "bitrate": 74, "full_quality": False}
    except Exception:
        pass
    return {"name": None, "bitrate": 0, "full_quality": False}


def mp3_supported() -> bool:
    return mp3_encoder()["name"] is not None


def _wav_to_flac(wav_path: str, progress=None) -> str:
    """WAV -> FLAC, блоками, без загрузки всего сета в память.

    Зачем FLAC вообще: это единственный из форматов «без потерь», который
    и сжимает (примерно вдвое против WAV), и читается всем — Mixxx,
    плеерами, монтажом. Диджей забирает сет, чтобы слушать его на другой
    системе и резать; mp3 для этого уже проигранный этап качества, а WAV
    на полтора гигабайта неудобно возить.

    Блоками — по той же причине, по какой и сам сет пишется потоком:
    90 минут стерео 44.1/16 это около гигабайта, и держать их в памяти
    ради перекодирования незачем.
    """
    import soundfile as sf

    out = str(Path(wav_path).with_suffix(".flac"))
    block = 1 << 20                      # ~1 млн кадров, около 4 МБ на канал
    with sf.SoundFile(wav_path) as src:
        total = max(1, len(src))
        done = 0
        with sf.SoundFile(out, "w", samplerate=src.samplerate,
                          channels=src.channels, format="FLAC",
                          subtype="PCM_16") as dst:
            while True:
                chunk = src.read(block, dtype="int16")
                if not len(chunk):
                    break
                dst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(min(1.0, done / total))
    if progress:
        progress(1.0)
    return out


def _body_head_level(path: str, start: float, rate: float, sr: int,
                     ref_rms: float, probe_sec: float = 4.0) -> float:
    """Уровень ПЕРВЫХ секунд тела, которое пойдёт после перехода.

    Считается ровно тем же путём, что и само тело (`_write_body`):
    match_loudness по опорному уровню трека, добор, ограничитель. Иначе
    вторая половина стыка снова мерилась бы своей меркой.

    Читается только четыре секунды — это дешевле одной десятой секунды
    на переход и убирает второй скачок уровня, тот, что после сведения."""
    import demo_render

    try:
        import librosa

        y, _ = librosa.load(path, sr=sr, mono=False,
                            offset=max(0.0, start), duration=probe_sec)
    except Exception:                                # noqa: BLE001
        return 0.0
    y = demo_render._to_stereo(y)
    if len(y) < sr * 0.2:
        return 0.0
    y = demo_render._stretch(y, sr, rate)
    y = demo_render._match_loudness(y, ref_rms)
    y = demo_render._soft_limit(y * SET_MAKEUP)
    return _level_p90(_mono_of(y), sr)


def render_full_set(strategy: dict, out_path: str, sr: int = 44100,
                    max_minutes: float = 150.0,
                    master_bpm: float | None = None,
                    progress=None,
                    fmt: str = "wav") -> dict:
    """Собирает ВЕСЬ сет одним непрерывным файлом: тело трека — сведение —
    тело следующего — сведение, до конца плана.

    Отличие от render_set(): тот склеивает только переходы (превью на
    несколько минут), а здесь играет всё, как в реальном сете, поэтому
    результат длится столько же, сколько план.

    Пишется потоком на диск (90 минут в памяти — почти гигабайт), темп
    единый на весь сет.
    """
    import numpy as np
    import soundfile as sf

    import demo_render

    tracks = strategy.get("tracks") or []
    transitions = strategy.get("transitions") or []
    if not transitions:
        raise ValueError("В плане нет переходов — нечего собирать.")

    by_name = {t["name"]: t for t in tracks}
    master = float(master_bpm or set_master_bpm(strategy))
    bar_sec = 60.0 / master * 4
    fade_n = int(0.02 * sr)
    budget = max_minutes * 60.0

    written = 0.0
    rendered = 0
    skipped: list[str] = []
    used_techniques: dict[str, int] = {}
    out_dir = Path(out_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Промежуточный WAV на 90 минут — это ~1 ГБ. Если предыдущая сборка
    # оборвалась на перекодировании (например, backend перезапустили), он
    # остаётся лежать и тихо съедает диск. Подчищаем свой же мусор: только
    # временные WAV полного сета и только заведомо чужие (старше получаса).
    try:
        import time as _t

        for stale in out_dir.glob("*_full_*.wav"):
            if _t.time() - stale.stat().st_mtime > 1800:
                stale.unlink()
                logger.info("удалил недоделанный временный WAV: %s", stale.name)
    except OSError:
        pass

    first = by_name.get(transitions[0]["from"])
    if not first or not first.get("path") or not os.path.exists(first["path"]):
        raise ValueError(f"Не найден файл первого трека: {transitions[0]['from']}")

    # с какой секунды играет самый первый трек
    pos = _point_seconds(transitions[0].get("to_point")) or 0.0
    pos = 0.0  # первый трек играем с начала — перед ним ничего нет
    # Уровень, которым кончилось последнее записанное тело. Первый
    # переход соседа слева не имеет, поэтому стартуем от расчётного
    # уровня одиночной деки — но уже в том масштабе, в каком лежит файл.
    prev_tail = SET_DECK_RMS * SET_MAKEUP
    cur = first

    enc = mp3_encoder()
    want = str(fmt).lower()
    want_flac = want == "flac"
    want_mp3 = want == "mp3" and enc["name"] is not None
    if want == "mp3" and not want_mp3:
        skipped.append("нет ни lameenc, ни ffmpeg — сохраняю WAV")
    elif want_mp3 and not enc["full_quality"]:
        skipped.append("mp3 пишет libsndfile (~74 кбит/с) — поставьте lameenc "
                       "для 320: pip install lameenc")
    # Поток всегда в WAV (90 минут в память не берём), в mp3/FLAC
    # перекодируем в конце: ни LAME, ни FLAC-энкодер не умеют дописывать
    # в уже открытый sf.SoundFile, а обрезка хвостовой тишины работает
    # по готовому WAV.
    out_path = str(Path(out_path).with_suffix(".wav"))

    with sf.SoundFile(out_path, "w", samplerate=sr, channels=2, subtype="PCM_16") as fh:
        for t in transitions:
            if written >= budget:
                skipped.append(f"после {rendered} переходов упёрлись в лимит {max_minutes:g} мин")
                break
            a, b = by_name.get(t["from"]), by_name.get(t["to"])
            if not a or not b or not a.get("path") or not b.get("path") \
                    or not os.path.exists(a["path"]) or not os.path.exists(b["path"]):
                skipped.append(f"{t['from']} -> {t['to']}: файл не найден")
                continue

            tmp = f"{out_path}.part.wav"
            try:
                meta = demo_render.render_demo(
                    t["technique_id"], a["path"], b["path"], a["bpm"], b["bpm"], tmp,
                    param_overrides=_blend_override(t),
                    # окно с запасом на такт разгона и хвост
                    max_seconds=max(16.0, float(t.get("bars") or 8) * bar_sec + 8 * bar_sec),
                    source_at=_point_seconds(t.get("from_point")),
                    target_at=_point_seconds(t.get("to_point")),
                    mix_profile=set_mix_profile(strategy, t),
                    master_bpm=master,
                    normalize_output=False,
                )
            except (NotImplementedError, ValueError, FileNotFoundError) as exc:
                skipped.append(f"{t['from']} -> {t['to']}: {exc}")
                continue

            # тело уходящего трека — от текущей позиции до начала сведения
            src_in = meta.get("source_in_seconds")
            if src_in is not None and src_in > pos:
                sec, tail_level = _write_body(
                    fh, a["path"], pos, src_in,
                    meta.get("rate_source") or 1.0, sr,
                    demo_render._track_reference_rms(a["path"]), fade_n)
                written += sec
                if tail_level > 0:
                    prev_tail = tail_level

            chunk, chunk_sr = sf.read(tmp, dtype="float32")
            os.remove(tmp)
            chunk = demo_render._to_stereo(chunk)  # не схлопываем в моно
            if chunk_sr != sr and len(chunk):
                import librosa
                chunk = librosa.resample(chunk, orig_sr=chunk_sr, target_sr=sr, axis=0)
            # Выравниваем кусок по уровню одиночной деки ДО добора и
            # ограничителя: иначе ограничитель делает эту работу за нас,
            # но искажением, а не громкостью.
            # Выравниваем кусок по уровню ОДИНОЧНОЙ деки ДО добора и
            # ограничителя: иначе эту работу делает ограничитель, но
            # искажением, а не громкостью. Опорный уровень берём из
            # самого куска — из его начала, где вторая дека ещё молчит.
            # Уровень стыка задаётся СОСЕДЯМИ, а не самим переходом.
            #
            # Раньше кусок подгонялся под 90-ю процентиль своего же
            # соло-участка, а тела — под средний уровень трека. Две разные
            # мерки под одним словом «уровень»: у трека с сильной
            # динамикой p90 дропа вдвое выше среднего, и переход честно
            # выходил на десяток децибел громче соседей. Замер на сете
            # диджея: +9.2 dB, ограничитель работает все 6.4% времени, что
            # заняли переходы. Это и слышно как «овер дБ на переходах при
            # нормальной громкости в телах».
            #
            # Теперь цель — кривая от того, чем кончилось предыдущее тело,
            # к тому, с чего начнётся следующее. Оба конца измерены НА
            # ГОТОВОМ ЗВУКЕ, тем же `_level_p90`, поэтому состыковаться они
            # обязаны по построению, а не по совпадению формул.
            next_head = _body_head_level(
                b["path"], meta.get("target_out_seconds") or 0.0,
                meta.get("rate_target") or 1.0, sr,
                demo_render._track_reference_rms(b["path"]))
            head = prev_tail or (SET_DECK_RMS * SET_MAKEUP)
            tail = next_head or head
            # Пол под опорой: см. SET_RIDE_REF_FLOOR_DB. Тихое интро
            # следующего трека — это свойство трека, а не повод давить
            # весь переход к нему.
            floor = (SET_DECK_RMS * SET_MAKEUP) * (10.0 ** (-SET_RIDE_REF_FLOOR_DB / 20.0))
            head, tail = max(head, floor), max(tail, floor)
            chunk = chunk * SET_MAKEUP
            chunk = _ride_to_reference(chunk, sr,
                                       ref_rms=np.array([head, tail], dtype="float64"),
                                       max_cut_db=SET_RIDE_MAX_CUT_DB)
            chunk = demo_render._soft_limit(chunk)
            fh.write(chunk)
            written += len(chunk) / sr
            prev_tail = tail

            pos = meta.get("target_out_seconds") or 0.0
            cur = b
            rendered += 1
            used_techniques[t["technique_id"]] = used_techniques.get(t["technique_id"], 0) + 1
            if progress:
                progress(rendered, len(transitions), round(written, 1), "mix", 0.0)

        # Хвост последнего трека — столько же, сколько играли остальные.
        # Раньше здесь дописывался ВЕСЬ остаток файла, и сет получался на
        # несколько минут длиннее плана.
        if cur and cur.get("path") and os.path.exists(cur["path"]) and written < budget:
            slot = (strategy.get("layout") or {}).get("slot_seconds")
            rate = master / float(cur.get("bpm") or master)
            tail_out = float(slot) if slot else 150.0                 # секунд в готовом файле
            tail_out = min(tail_out, max(0.0, budget - written))
            tail_end = min(float(cur.get("duration_seconds") or (pos + tail_out)),
                           pos + tail_out * rate)
            sec, _ = _write_body(fh, cur["path"], pos, tail_end, rate, sr,
                                 demo_render._track_reference_rms(cur["path"]), fade_n)
            written += sec

    written = _trim_trailing_silence(out_path, sr)
    if want_flac:
        def _flac_progress(frac: float) -> None:
            if progress:
                progress(rendered, len(transitions), round(written, 1), "encode", frac)

        _flac_progress(0.0)
        try:
            flac_path = _wav_to_flac(out_path, progress=_flac_progress)
        except Exception as exc:                       # noqa: BLE001
            skipped.append(f"FLAC не собрался ({exc}) — оставляю WAV")
            want_flac = False
        else:
            try:
                os.remove(out_path)
            except OSError:
                skipped.append("не смог удалить временный WAV — место на диске занято им")
            out_path = flac_path
    if want_mp3:
        def _enc_progress(frac: float) -> None:
            if progress:
                progress(rendered, len(transitions), round(written, 1), "encode", frac)

        _enc_progress(0.0)
        mp3_path = _wav_to_mp3(out_path, progress=_enc_progress)
        try:
            os.remove(out_path)
        except OSError:
            skipped.append("не смог удалить временный WAV — место на диске занято им")
        out_path = mp3_path

    return {
        "path": out_path,
        "transitions_rendered": rendered,
        "transitions_total": len(transitions),
        "duration_seconds": round(written, 1),
        "master_bpm": master,
        "techniques_used": used_techniques,
        "skipped": skipped,
        "full": True,
        "format": "mp3" if want_mp3 else ("flac" if want_flac else "wav"),
        "bitrate": enc["bitrate"] if want_mp3 else 1411,
        "encoder": enc["name"] if want_mp3 else ("libsndfile/FLAC" if want_flac else "wav"),
        "lossless": not want_mp3,
    }
