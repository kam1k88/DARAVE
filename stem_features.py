"""
stem_features.py — признаки ОТДЕЛЬНО ПО КАЖДОМУ СЛОЮ.

## Зачем это вообще

До сих пор у трека было ОДНО число яркости — спектральный центроид
всего микса (track_analysis.brightness). Замер по библиотеке диджея
(145 треков) показал, чего она стоит: по темповым семействам яркость
разошлась всего на 37,4..46,9 из 100. Это не признак, это шум с
наклоном. И понятно почему: центроид микса — средневзвешенное по
барабанам, басу, гармонии и голосу сразу. Яркие хэты вытягивают его
вверх, саб — вниз, и два совершенно разных трека получают одинаковое
число.

Смешивать их в одно число нельзя ещё и потому, что КОНФЛИКТУЮТ НА
ПЕРЕХОДЕ ОНИ ПО ОТДЕЛЬНОСТИ. Два баса сразу — это грязь, и лечится она
обменом баса. Две яркие гармонии — это каша в верхах, и лечится она
совсем другим: фильтром, резом, вводом по частям. Барабаны наоборот:
чем МЕНЬШЕ они похожи, тем чище звучит наложение. Один признак на весь
микс не отличает ни один из этих случаев от другого.

Отсюда модуль. Считается он по уже посчитанным слоям — то есть только у
тех треков, у кого есть стемы, — и ложится в тот же meta.json рядом со
слоями. Нет слоёв — нет признаков, и это честное «не знаю», а не ноль.

## Что именно считается

    drums_bright   яркость БАРАБАНОВ: хэты и рэйды вверху или бочка и томы внизу
    drums_density  насколько густо барабаны идут (атак в секунду)
    bass_density   непрерывность баса: риз-лайн фонка ~0,9, хаус с паузами ~0,5
    bass_sub       доля энергии баса ниже 120 Гц (саб против мид-баса)
    harm_bright    яркость ГАРМОНИИ: лиды и пэды наверху или подклад внизу
    harm_density   гармония непрерывна (пэд) или отрывиста (стабы)
    vocal_share    доля трека с голосом — берётся из карты вокала, если она есть
    share_*        доля энергии слоя в сумме слоёв

Все числа 0..1, чтобы их можно было вычитать друг из друга без оглядки
на единицы.

## Чего здесь НЕТ

Здесь нет обученной модели. Это измерения и пороги, выведенные из
замеров, и относиться к ним надо как к измерениям: они говорят «бас
идёт непрерывно», а не «эти треки подойдут друг другу». Вывод из них
делают style.layer_technique_bias и audition.layer_weight, и делают
мягкими множителями, а не запретами.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import stems

# Версия набора признаков. Меняется вместе с формулами: записанное
# прежней версией считается НЕпосчитанным и пересчитывается. Без этого
# в библиотеке жили бы вперемешку числа по двум разным шкалам, и разница
# между треками означала бы версию расчёта, а не разницу треков.
VERSION = 1

SR = 22050          # выше не нужно: всё, что меряем, живёт ниже 11 кГц
N_FFT = 4096        # 5,4 Гц на корзину — хватает, чтобы отделить саб от мид-баса
HOP = 2048          # ~93 мс: короче бессмысленно, мы меряем не атаки, а плотность

PARTS = ("drums", "bass", "other", "vocals")

SUB_HZ = 120.0      # граница «саб / мид-бас»: ниже — то, что слышно телом
DENSITY_DB = 12.0   # окно тише пика слоя на столько — уже пауза, а не звук


# ------------------------------------------------------------ чтение

def features(track_path: str) -> dict | None:
    """Готовые признаки слоёв или None, если их нет.

    None и «все нули» — разные вещи: первое значит «слоёв нет или
    признаки не посчитаны», второе — «посчитали, слой пустой». Путать их
    нельзя, иначе трек с тишиной в басу будет вечно пересчитываться, а
    трек без стемов — участвовать в подборе с выдуманными числами."""
    m = stems.stem_meta(track_path) or {}
    f = m.get("layers")
    if not isinstance(f, dict):
        return None
    if int(f.get("version") or 0) != VERSION:
        return None
    return f


def have(track: dict) -> dict | None:
    """То же, но от словаря трека из библиотеки."""
    p = (track or {}).get("path")
    return features(p) if p else None


def coverage(paths) -> dict:
    """Сколько треков библиотеки уже размечено по слоям."""
    paths = [p for p in (paths or []) if p]
    with_stems = [p for p in paths if stems.has_stems(p)]
    done = [p for p in with_stems if features(p) is not None]
    return {"tracks": len(paths), "with_stems": len(with_stems),
            "with_layers": len(done), "todo": len(with_stems) - len(done)}


# ------------------------------------------------------------- расчёт

def _load(path, sr: int = SR):
    """Моно-сигнал слоя. Через ffmpeg, а не через librosa/soundfile.

    Две причины, и обе практические. Первая: librosa тянет за собой
    numba и resampy, а здесь от него нужны ровно БПФ и среднее — то есть
    numpy. Вторая — та же, по которой через ffmpeg читает и сам stems.py
    (см. _ffmpeg_decode): задержка кодировщика снимается по заголовку, и
    слои не разъезжаются между собой."""
    import numpy as np

    buf = stems._ffmpeg_decode(str(path), sr=sr, channels=1)
    if buf is None:
        raise RuntimeError(f"ffmpeg не прочитал {os.path.basename(str(path))}")
    return np.asarray(buf, dtype=float).reshape(-1)


def _frames(np, y, n: int):
    """Нарезка на неперекрывающиеся окна длиной n."""
    k = len(y) // n
    if k < 1:
        return None
    return y[:k * n].reshape(k, n)


def _spec(np, y):
    """Модуль спектра по окнам и частоты корзин.

    Окно Ханна и БПФ на N_FFT с шагом HOP — ровно то, что делает
    librosa.stft, только без самой librosa."""
    if len(y) < N_FFT:
        return None, None
    k = 1 + (len(y) - N_FFT) // HOP
    if k < 2:
        return None, None
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(k)[:, None]
    win = np.hanning(N_FFT)
    S = np.abs(np.fft.rfft(y[idx] * win, axis=1))
    freqs = np.fft.rfftfreq(N_FFT, d=1.0 / SR)
    return S, freqs


def _rms_db(np, y):
    """Громкость по окнам, в дБ. Одна строка, но переиспользуется дважды."""
    fr = _frames(np, y, HOP)
    if fr is None:
        return np.array([-120.0])
    return 20.0 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-12)


def _density(np, db) -> float:
    """Доля времени, когда слой РЕАЛЬНО звучит.

    Порог считается от самого слоя (95-й процентиль минус DENSITY_DB), а
    не абсолютный: тихо сведённый бас — это всё ещё непрерывный бас, и
    абсолютный порог назвал бы его паузой.

    Процентиль, а не максимум: один щелчок на весь трек поднял бы
    максимум на 20 дБ и объявил бы паузой весь остальной трек."""
    if db.size < 4:
        return 0.0
    top = float(np.percentile(db, 95))
    if top < -60.0:
        return 0.0                       # слой пустой — плотности нет
    return float((db >= top - DENSITY_DB).mean())


def _bright(np, S, freqs) -> float:
    """Яркость слоя: центроид, взвешенный ЭНЕРГИЕЙ окон.

    Без взвешивания тишина между ударами (где центроид скачет по шуму)
    весит столько же, сколько сам удар, и яркость барабанов у трека с
    паузами оказывается выше, чем у сплошной долбёжки."""
    if S is None:
        return 0.0
    e = S.sum(axis=1)
    if float(e.sum()) <= 0:
        return 0.0
    cen = (S * freqs[None, :]).sum(axis=1) / np.maximum(e, 1e-12)
    v = float(np.average(cen, weights=e))
    # Нормируем на sr/4, как track_analysis.brightness: числа должны
    # лежать на одной шкале со старой яркостью микса, иначе их нельзя
    # сравнивать между собой и нельзя объяснить диджею.
    return float(np.clip(v / (SR / 4.0), 0.0, 1.0))


def _sub_share(np, S, freqs) -> float:
    """Доля энергии ниже SUB_HZ. Отвечает на вопрос «где сидит бас».

    Два баса в одной полосе — грязь; саб под мид-бас — уже музыка. Это
    и есть та величина, которую яркость всего микса стирала полностью."""
    if S is None:
        return 0.0
    P = S ** 2
    total = float(P.sum())
    if total <= 0:
        return 0.0
    return float(np.clip(float(P[:, freqs < SUB_HZ].sum()) / total, 0.0, 1.0))


def _onset_rate(np, S) -> float:
    """Атак в секунду, приведённые к 0..1 делением на 8.

    Восемь — не с потолка: это шестнадцатые на 120 BPM, то есть верх
    того, что ухо ещё читает как рисунок, а не как дробь.

    Сама огибающая — спектральный поток: сумма ТОЛЬКО прироста энергии
    по корзинам. Спад в неё не входит сознательно, иначе конец ноты
    считался бы атакой наравне с началом."""
    if S is None or S.shape[0] < 4:
        return 0.0
    flux = np.maximum(0.0, np.diff(S, axis=0)).sum(axis=1)
    if not flux.size or float(flux.max()) <= 0:
        return 0.0
    thr = float(np.percentile(flux, 70))
    hit = flux > max(thr, float(flux.mean()))
    # Считаем НАЧАЛА участков, а не отдельные окна: одна атака длиннее
    # окна дала бы два-три срабатывания подряд и завысила бы плотность
    # втрое у всего, что играет с реверберацией.
    peaks = int(np.count_nonzero(hit[1:] & ~hit[:-1])) + int(bool(hit[0]))
    dur = (S.shape[0] * HOP) / float(SR)
    if dur <= 0:
        return 0.0
    return float(np.clip(peaks / dur / 8.0, 0.0, 1.0))


def compute(track_path: str, save: bool = True) -> dict | None:
    """Посчитать признаки слоёв одного трека.

    None — считать нечем: полного комплекта слоёв нет. Неполный комплект
    не берём сознательно: доля энергии слоя считается от СУММЫ слоёв, и
    без одного из них все доли поедут."""
    import numpy as np

    paths = stems.stem_paths(track_path)
    if not paths or not all(paths.get(p) for p in PARTS):
        return None

    buf: dict[str, object] = {}
    for part in PARTS:
        try:
            buf[part] = _load(paths[part])
        except Exception:                            # noqa: BLE001
            return None

    n = min(len(b) for b in buf.values())
    if n < SR:                                       # меньше секунды — нечего мерить
        return None
    buf = {k: v[:n] for k, v in buf.items()}

    energy = {k: float((v ** 2).sum()) for k, v in buf.items()}
    total = sum(energy.values()) or 1.0

    S_dr, f_dr = _spec(np, buf["drums"])
    S_ba, f_ba = _spec(np, buf["bass"])
    S_ot, f_ot = _spec(np, buf["other"])

    out = {
        "version": VERSION,
        "drums_bright": round(_bright(np, S_dr, f_dr), 3),
        "drums_density": round(_onset_rate(np, S_dr), 3),
        "bass_density": round(_density(np, _rms_db(np, buf["bass"])), 3),
        "bass_sub": round(_sub_share(np, S_ba, f_ba), 3),
        "harm_bright": round(_bright(np, S_ot, f_ot), 3),
        "harm_density": round(_density(np, _rms_db(np, buf["other"])), 3),
    }
    for part in PARTS:
        out["share_" + part] = round(energy[part] / total, 3)

    # Долю вокала не пересчитываем: её уже посчитала карта вокала, и
    # посчитала ЧЕСТНЕЕ — там проверяется не громкость слоя, а спектр и
    # длительность кусков, то есть утечка баса в вокальный слой голосом
    # не считается. Второе число об одном и том же расходилось бы с
    # первым, и оба перестали бы что-то значить.
    m = stems.stem_meta(track_path) or {}
    if m.get("vocal_track") is not None:
        out["vocal_share"] = round(float(m.get("vocal_share") or 0.0), 3)
        out["vocal_track"] = bool(m.get("vocal_track"))

    if save:
        _save(track_path, out)
    return out


def _save(track_path: str, feat: dict) -> None:
    """Дописать признаки в meta.json рядом со слоями.

    Именно рядом со слоями, а не в базу библиотеки: пересчитали стемы —
    признаки обязаны уехать вместе с ними. В базе они пережили бы
    пересчёт и описывали бы слои, которых больше нет."""
    # Куда именно — решает stems.write_meta: папка слоёв, если она есть,
    # иначе файл-спутник рядом с .stem.mp4. Раньше здесь был только
    # первый путь, и у 54 треков с одним mp4 признаки молча пропадали.
    stems.write_meta(track_path, {"layers": feat})


def library(paths, rebuild: bool = False, log=print, progress=None) -> dict:
    """Проход по библиотеке. Считает только тем, у кого есть слои."""
    paths = [p for p in (paths or []) if p]
    done = skipped = nostems = failed = 0
    for i, p in enumerate(paths, 1):
        name = os.path.basename(str(p))[:50]
        try:
            if not stems.has_stems(p):
                nostems += 1
                continue
            if not rebuild and features(p) is not None:
                skipped += 1
                continue
            r = compute(p)
            if r is None:
                nostems += 1
                continue
            done += 1
            log(f"[{i}/{len(paths)}] {name}: бас {r['bass_density']:.2f}"
                f"/саб {r['bass_sub']:.2f}, гармония {r['harm_bright']:.2f}"
                f"/{r['harm_density']:.2f}, барабаны {r['drums_bright']:.2f}")
        except Exception as exc:                     # noqa: BLE001
            failed += 1
            log(f"[{i}/{len(paths)}] {name}: сбой — {exc}")
        if progress is not None:
            progress(i, len(paths), done, failed, str(p))
    return {"checked": done, "skipped": skipped, "no_stems": nostems,
            "failed": failed, "total": len(paths)}


# --------------------------------------------------- сравнение пары

def gap(fa: dict | None, fb: dict | None) -> dict | None:
    """Чем два трека СПОРЯТ друг с другом по слоям.

    Возвращает None, если хотя бы у одного признаков нет: догадываться
    тут нельзя — половина пары без чисел означает, что вывод будет
    сделан по одному треку, а выглядеть будет как вывод по двум.

    Все величины 0..1, «больше — хуже» для конфликтов и «больше —
    лучше» для drum_fit."""
    if not fa or not fb:
        return None

    def g(f, k, d=0.0):
        try:
            return float(f.get(k, d))
        except (TypeError, ValueError):
            return d

    # БАС. Конфликт тем сильнее, чем оба непрерывнее И чем ближе они
    # сидят по частоте. Саб под мид-басом не спорит — это рабочая
    # раскладка, её и надо отличать от «два риза в одной полосе».
    same_band = 1.0 - min(1.0, abs(g(fa, "bass_sub") - g(fb, "bass_sub")) * 2.0)
    bass = min(g(fa, "bass_density"), g(fb, "bass_density")) * same_band

    # ГАРМОНИЯ. Тот же принцип, но по яркости: два пэда в одной полосе
    # верха дают кашу, пэд под отрывистыми стабами — нет.
    same_top = 1.0 - min(1.0, abs(g(fa, "harm_bright") - g(fb, "harm_bright")) * 4.0)
    harm = min(g(fa, "harm_density"), g(fb, "harm_density")) * same_top

    # БАРАБАНЫ — наоборот: расхождение в яркости это ПЛЮС. Тёмная бочка
    # под чужими хэтами читается как один грув, две одинаково яркие
    # партии — как смаз.
    fit = min(1.0, abs(g(fa, "drums_bright") - g(fb, "drums_bright")) * 2.0)

    voc = min(g(fa, "vocal_share"), g(fb, "vocal_share"))

    return {"bass": round(bass, 3), "harm": round(harm, 3),
            "drum_fit": round(fit, 3), "vocal": round(voc, 3)}


# Пороги ВЫВЕДЕНЫ ИЗ ЗАМЕРА, а не выбраны на глаз. Замер: 93 трека с
# готовыми слоями из библиотеки диджея, все 4278 пар между ними.
#
#   bass     >= 0.55  срабатывает на 15% пар
#   harm     >= 0.55  на 22% пар
#   drum_fit >= 0.55  на 26% пар, а «одинаковые» (<= 0.15) — на 24%
#
# Это и есть то, ради чего пороги калибруются числом. Первая версия
# считала drum_fit с множителем 5.0 и порогом 0.45 — и срабатывала на
# 71% ПАР. Надбавка, которую получают две трети пар, не отличает ничего:
# она просто поднимает общий уровень весов и не меняет порядок. Признак
# полезен ровно настолько, насколько он делит библиотеку.
BASS_CLASH = 0.55      # выше — «два непрерывных баса в одной полосе»
HARM_CLASH = 0.55      # выше — «две плотные гармонии в одной полосе»
DRUM_APART = 0.55      # выше — барабаны дополняют друг друга
DRUM_SAME = 0.15       # ниже — барабаны почти одинаковые


def note(g: dict | None) -> str:
    """Одна строка для карточки стыка. Пусто — говорить нечего."""
    if not g:
        return ""
    said = []
    if g["bass"] >= BASS_CLASH:
        said.append("басы в одной полосе и оба непрерывны — низ надо разводить")
    if g["harm"] >= HARM_CLASH:
        said.append("гармонии одинаково плотные и яркие — верх слипнется")
    if not said and g["drum_fit"] >= DRUM_APART:
        said.append("барабаны дополняют друг друга — наложение будет читаться")
    return "; ".join(said)
