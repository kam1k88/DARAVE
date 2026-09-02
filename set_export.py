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


def _point_seconds(point) -> float | None:
    if not isinstance(point, dict):
        return None
    v = point.get("time_seconds")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0.5 else None


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
        chunk = _trim_to_bars(chunk, bar_n)
        pieces.append(chunk)
        bar_ns.append(bar_n)
        used += len(chunk) / sr
        rendered += 1
        used_techniques[t["technique_id"]] = used_techniques.get(t["technique_id"], 0) + 1
        if progress:
            progress(rendered, len(picked))

    if not pieces:
        raise ValueError("Ни один переход не удалось отрендерить: " + "; ".join(skipped[:3]))

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
# в конце добавляем фиксированные +4 dB — одинаковые для тел и переходов,
# так что относительные уровни не меняются.
SET_MAKEUP = 2.4


def _trim_trailing_silence(path: str, sr: int, floor_db: float = -55.0) -> float:
    """Убирает тишину в конце: последний трек дочитывается до конца файла,
    а там у многих релизов несколько секунд пустоты."""
    import numpy as np
    import soundfile as sf

    y, _ = sf.read(path, dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    if len(y) < sr:
        return len(y) / sr
    k = sr // 4
    prof = np.array([np.sqrt(np.mean(y[i * k:(i + 1) * k] ** 2) + 1e-12) for i in range(len(y) // k)])
    db = 20 * np.log10(np.maximum(prof, 1e-7))
    voiced = np.nonzero(db > floor_db)[0]
    if len(voiced) == 0 or voiced[-1] >= len(prof) - 2:
        return len(y) / sr
    end = min(len(y), (voiced[-1] + 2) * k)
    y = y[:end]
    fade = min(len(y) // 4, int(1.5 * sr))
    if fade > 1:
        y[-fade:] *= np.linspace(1.0, 0.0, fade)
    sf.write(path, y, sr, subtype="PCM_16")
    return len(y) / sr


def set_master_bpm(strategy: dict) -> float:
    """Темп, в котором играет весь сет.

    НЕ медиана сырых BPM. На реальной библиотеке медиана дала 147 (а на
    части плана — 120): в неё попали треки, которые детектор посчитал в
    2/3 темпа, 112 и 117 вместо 168 и 176. Весь драм-н-бейс тогда
    растягивался к 120 и играл на 70% скорости — это и слышно как «в два
    раза медленнее».

    Правильно: сначала центр семьи темпов (tempo.library_center — он
    голосует по октавам и один раз прикладывает полосу исполнения), потом
    медиана только среди тех треков, которые в эту семью попадают. Треки
    другого темпа на мастер-темп не влияют и растягиваться к нему не
    будут (см. guard в demo_render)."""
    import statistics

    import tempo as _tempo

    bpms = [float(t.get("bpm") or 0) for t in (strategy.get("tracks") or []) if t.get("bpm")]
    bpms = [b for b in bpms if b > 20]
    if not bpms:
        return 174.0
    center = _tempo.library_center(bpms) or statistics.median(bpms)
    family = [_tempo.fold_to_center(b, center) for b in bpms]
    family = [b for b in family if _tempo.same_family(center, b)]
    return round(statistics.median(family) if family else center, 2)


def _write_body(fh, path: str, start: float, end: float, rate: float, sr: int,
                ref_rms: float, fade_n: int) -> float:
    """Пишет «тело» трека — кусок, который просто играет между сведениями.

    Возвращает сколько секунд легло в файл. Растягиваем тем же rate, что и
    в переходе, иначе на стыке тела и сведения менялся бы темп."""
    import numpy as np

    import demo_render

    dur = end - start
    if dur <= 0.2:
        return 0.0
    import librosa

    y, _ = librosa.load(path, sr=sr, mono=False, offset=max(0.0, start), duration=dur)
    y = demo_render._to_stereo(y)          # поток сета двухканальный
    if len(y) < sr * 0.05:
        return 0.0
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
    return len(y) / sr


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
    cur = first

    enc = mp3_encoder()
    want_mp3 = str(fmt).lower() == "mp3" and enc["name"] is not None
    if str(fmt).lower() == "mp3" and not want_mp3:
        skipped.append("нет ни lameenc, ни ffmpeg — сохраняю WAV")
    elif want_mp3 and not enc["full_quality"]:
        skipped.append("mp3 пишет libsndfile (~74 кбит/с) — поставьте lameenc "
                       "для 320: pip install lameenc")
    # Поток всегда в WAV (90 минут в память не берём), в mp3 перекодируем
    # в конце: LAME не умеет писать в открытый sf.SoundFile.
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
                    master_bpm=master,
                    normalize_output=False,
                )
            except (NotImplementedError, ValueError, FileNotFoundError) as exc:
                skipped.append(f"{t['from']} -> {t['to']}: {exc}")
                continue

            # тело уходящего трека — от текущей позиции до начала сведения
            src_in = meta.get("source_in_seconds")
            if src_in is not None and src_in > pos:
                written += _write_body(fh, a["path"], pos, src_in,
                                       meta.get("rate_source") or 1.0, sr,
                                       demo_render._track_reference_rms(a["path"]), fade_n)

            chunk, chunk_sr = sf.read(tmp, dtype="float32")
            os.remove(tmp)
            chunk = demo_render._to_stereo(chunk)  # не схлопываем в моно
            if chunk_sr != sr and len(chunk):
                import librosa
                chunk = librosa.resample(chunk, orig_sr=chunk_sr, target_sr=sr, axis=0)
            chunk = demo_render._soft_limit(chunk * SET_MAKEUP)
            fh.write(chunk)
            written += len(chunk) / sr

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
            written += _write_body(fh, cur["path"], pos, tail_end,
                                   rate, sr, demo_render._track_reference_rms(cur["path"]), fade_n)

    written = _trim_trailing_silence(out_path, sr)
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
        "format": "mp3" if want_mp3 else "wav",
        "bitrate": enc["bitrate"] if want_mp3 else 1411,
        "encoder": enc["name"] if want_mp3 else "wav",
    }
