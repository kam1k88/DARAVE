"""
mix_strategist.py — планировщик всего сета: берёт проанализированную
библиотеку (track_analysis.py) и строит очередность треков + список
переходов с выбранной техникой (techniques.py) под каждый, тайминг которых
по возможности привязан к реальным дропам/брейкдаунам трека.

Это НЕ ML-модель — эвристический rule-engine (гармоническая совместимость
по кругу Камелота + целевая энергетическая дуга + приоритетные правила
выбора техники), полностью объяснимый: у каждого перехода в выводе есть
`rule` — почему выбрана именно эта техника, и `alternatives` — что ещё
подходило. См. server.py::POST /api/rooms/{id}/strategy и static/chat.html
("Стратегия").

Уровни энергии (EL 1..5) — грубая перекладка непрерывной energy (0..1) из
track_analysis.py в 5 бакетов, только для целевой дуги сета и наглядности
в UI, не самостоятельная метрика.
"""
from __future__ import annotations

import mix_points
import tempo
from techniques import TECHNIQUES, Technique, build_plan


# --- гармоническая совместимость (круг Камелота) ---

def _camelot_parts(code: str) -> tuple[int, str]:
    return int(code[:-1]), code[-1]


def camelot_compatibility(a: str, b: str) -> float:
    """1.0 — идеально (тот же код), 0.9 — соседний номер той же буквы,
    0.85 — тот же номер, другая буква (relative major/minor), убывает дальше,
    0.2 — "конфликт" (для Echo Cut/Key Jump — они именно под это и нужны)."""
    if a == b:
        return 1.0
    na, la = _camelot_parts(a)
    nb, lb = _camelot_parts(b)
    if la == lb and (abs(na - nb) == 1 or abs(na - nb) == 11):
        return 0.9
    if na == nb and la != lb:
        return 0.85
    if la == lb and (abs(na - nb) == 2 or abs(na - nb) == 10):
        return 0.6
    diff = min(abs(na - nb), 12 - abs(na - nb))
    return max(0.15, 0.6 - diff * 0.07)


def bpm_type_mismatch(bpm_a: float, bpm_b: float, threshold_pct: float = 6.0) -> bool:
    """Правда ли темпы несовместимы.

    Половинный и двойной счёт — ОДИН темп, а не разные: 70 и 140 это один
    и тот же трек, просто долю считают вдвое реже, и играть их вместе
    можно без изменения скорости. Поэтому решение принимает
    tempo.relate(), которое ищет множитель из степеней двойки. Настоящая
    несовместимость — когда ни при каком таком множителе темпы не сходятся
    (105 против 172: отношение 1.64, ни одна степень двойки не подходит)."""
    if bpm_a <= 0 or bpm_b <= 0:
        return False
    return not tempo.same_family(bpm_a, bpm_b, threshold_pct)


def tempo_note(bpm_a: float, bpm_b: float) -> str:
    """Человекочитаемо о соотношении темпов — для объяснений в UI."""
    rel = tempo.relate(bpm_a, bpm_b)
    if not rel["compatible"]:
        return f"темпы не сводятся ({bpm_a:.0f} и {bpm_b:.0f}, расхождение {rel['error_pct']:.0f}%)"
    if rel["needs_recount"]:
        return f"{bpm_b:.0f} играет {rel['label']} к {bpm_a:.0f} — скорость менять не нужно"
    err = rel["error_pct"]
    if err < 0.6:
        return f"темпы совпадают ({bpm_a:.1f} и {bpm_b:.1f})"
    if err < 2.0:
        return f"{bpm_a:.1f} и {bpm_b:.1f} — разница {err:.1f}%, питч подтянет незаметно"
    return f"{bpm_a:.1f} и {bpm_b:.1f} — разница {err:.1f}%, тянуть питчем придётся ощутимо"


def energy_level(energy: float) -> int:
    """0..1 -> 1..5 (EL1..EL5) по АБСОЛЮТНОЙ шкале, порог каждые 0.2.

    Годится, чтобы охарактеризовать один трек сам по себе. Для построения
    энергодуги набора используйте library_energy_levels() — см. почему там."""
    return max(1, min(5, int(energy * 5) + 1))


# Минимальный разброс энергии, при котором есть смысл раскладывать
# библиотеку по уровням относительно неё самой. Ниже — библиотека
# действительно однородна, и разброс не выдумываем.
MIN_ENERGY_SPREAD = 0.08


def library_energy_levels(tracks: list[dict]) -> dict[str, int]:
    """EL1..EL5 ОТНОСИТЕЛЬНО этой библиотеки: раскладываем треки по
    квинтилям их собственной энергии.

    Зачем не абсолютная шкала: реальная DnB-библиотека занимает узкую
    полосу энергии — у диджея это оказалось 0.405..0.791, и абсолютные
    пороги (каждые 0.2) уложили все 48 треков всего в два уровня, EL3 и
    EL4. Энергодуга из двух значений бессмысленна: планировщику нечем
    строить подъём, а выбор техник по росту/спаду энергии почти всегда
    видит "ровно". При этом диджею для набора важна не абсолютная
    энергичность, а какие треки горячее ДРУГИХ ЕГО треков — то есть ранг
    внутри библиотеки.

    Возвращает {имя трека: EL}. Если библиотека мала или энергия почти
    одинаковая — честно отдаём абсолютные уровни, а не растягиваем шум."""
    usable = [t for t in tracks if t.get("energy") is not None]
    if len(usable) < 5:
        return {t["name"]: energy_level(t["energy"]) for t in usable}

    energies = sorted(t["energy"] for t in usable)
    if energies[-1] - energies[0] < MIN_ENERGY_SPREAD:
        return {t["name"]: energy_level(t["energy"]) for t in usable}

    # Границы квинтилей по самой библиотеке.
    n = len(energies)
    cuts = [energies[min(n - 1, int(n * q / 5))] for q in (1, 2, 3, 4)]

    def level_of(e: float) -> int:
        el = 1
        for cut in cuts:
            if e >= cut:
                el += 1
        return min(5, el)

    return {t["name"]: level_of(t["energy"]) for t in usable}


# --- энергетическая дуга сета ---

def _target_arc(n: int, shape: str = "rising") -> list[int]:
    if n <= 0:
        return []
    if shape == "wave":
        import math
        return [max(1, min(5, round(3 + 2 * math.sin(i / max(1, n - 1) * math.pi * 1.5)))) for i in range(n)]
    if shape == "peak_middle":
        mid = n / 2
        return [max(1, min(5, round(1 + 4 * (1 - abs(i - mid) / max(1, mid))))) for i in range(n)]
    # "rising" (по умолчанию) — плавный рост от EL1/2 к EL4/5 с небольшой
    # передышкой в последней трети (как на референсном скриншоте).
    arc = []
    for i in range(n):
        frac = i / max(1, n - 1)
        if frac < 0.15:
            arc.append(1 if frac < 0.08 else 2)
        elif frac < 0.75:
            arc.append(3 if frac < 0.45 else 4)
        else:
            arc.append(4 if (i % 5 == 0) else 5)
    return arc


# --- выбор порядка треков ---

VARIANT_COUNT = 5


def order_tracks(tracks: list[dict], arc_shape: str = "rising",
                 el_map: dict[str, int] | None = None,
                 variant: int = 0) -> list[dict]:
    """Жадный отбор: на каждом шаге берём из оставшихся трек, максимизирующий
    0.6*гармоническая_совместимость_с_предыдущим + 0.4*близость_к_целевому_EL.

    el_map — уровни энергии относительно библиотеки (library_energy_levels).
    Без него считаем по абсолютной шкале, но тогда у типичной DnB-библиотеки
    почти все треки окажутся на одном-двух уровнях и дуга выродится."""
    if not tracks:
        return []
    el_map = el_map if el_map is not None else library_energy_levels(tracks)

    def el(tr: dict) -> int:
        return el_map.get(tr["name"], energy_level(tr["energy"]))

    remaining = list(tracks)
    target = _target_arc(len(tracks), arc_shape)

    # Стартовый трек — ближе всего к первому целевому EL. Для варианта N
    # берём N-й по этому же признаку: дальше жадный отбор идёт от другой
    # точки и разворачивает совсем другой сет. Это честная вариативность
    # (каждый вариант так же обоснован), а не перемешивание наугад.
    remaining.sort(key=lambda tr: (abs(el(tr) - target[0]), tr["name"]))
    start_index = min(max(0, int(variant)), len(remaining) - 1)
    ordered = [remaining.pop(start_index)]

    while remaining:
        prev = ordered[-1]
        step = len(ordered)
        el_target = target[step] if step < len(target) else target[-1]

        def score(tr):
            harmonic = camelot_compatibility(prev["camelot"], tr["camelot"])
            el_fit = 1.0 - abs(el(tr) - el_target) / 4.0
            # Совместимость темпа — не косметика: если следующий трек не в
            # темп, единственная доступная техника это резкий рез, и весь
            # сет превращается в череду Quick Cut. Раньше порядок строился
            # только по тональности и энергии, и 118 BPM регулярно
            # оказывался посреди 172-х.
            # Не «в семье или нет», а НАСКОЛЬКО близко. Раньше здесь было
            # двоичное «совместим», и 170 рядом с 174 считалось таким же
            # хорошим соседом, как 174 рядом с 174. А это 2.4% — питч
            # уезжает на 0.41 полутона, и переход теряет проценты. Диджей
            # это слышит: «когда 100%, звучит хорошо».
            rel = tempo.relate(prev["bpm"], tr["bpm"])
            if not rel["compatible"]:
                in_tempo = 0.0
            else:
                # 0% -> 1.0, 2% -> 0.5, 4% -> 0
                in_tempo = max(0.0, 1.0 - rel["error_pct"] / 4.0)
            # Спорящие тональности — второй повод потерять проценты,
            # поэтому это не мягкий вес, а отказ при прочих равных.
            clash = -0.5 if harmonic < 0.5 else 0.0
            return (harmonic * 0.22 + el_fit * 0.13
                    + in_tempo * TEMPO_ORDER_WEIGHT + clash)

        remaining.sort(key=score, reverse=True)
        ordered.append(remaining.pop(0))
    return ordered


# --- выбор техники для перехода ---

def _struct(track: dict, key: str) -> list:
    return ((track.get("structure") or {}).get(key) or [])


def _has_long_intro(track: dict) -> bool:
    """Долгое интро без барабанов = есть куда заводить длинный бленд."""
    bd = _struct(track, "breakdowns")
    return bool(bd and float(bd[0].get("start_seconds", 99)) < 5.0
                and float(bd[0].get("end_seconds", 0)) > 25.0)


def _has_late_breakdown(track: dict) -> bool:
    """Брейкдаун ближе к концу — идеальное место, чтобы уводить трек."""
    bd = _struct(track, "breakdowns")
    dur = float(track.get("duration_seconds") or 0)
    return bool(dur > 60 and any(float(x.get("start_seconds", 0)) > dur * 0.55 for x in bd))


def technique_candidates(a: dict, b: dict, compat: float, mismatch: bool,
                         el_from: int, el_to: int,
                         slot_seconds: float | None = None) -> list[tuple[float, str, str]]:
    """Чем свести эту пару. Почти всегда — основным ходом.

    Здесь была лестница на 24 техники, потом на 10, потом с долей резов
    в 30%. Всё это было ошибкой постановки задачи. Диджей ведёт сет ОДНИМ
    приёмом: новый трек интро под старый, обмен низом, увод фильтром. Рез
    фейдером, дабл-дроп, дразнилка дропом — это перформанс, который
    делают осознанно, раз за сет, на паре, которая сама просит. Алгоритм,
    подсыпающий их «для разнообразия», звучит именно так, как и должен:
    невежественно.

    Поэтому автоматически выбирается только основной ход. Исключения —
    ровно два, и оба про то, что основной ход физически невозможен:
      * темпы не сводятся — держать треки вместе фразу нельзя, нужен рез;
      * тональности жёстко спорят — фразу вместе они не выдержат.
    Всё остальное остаётся в пресетах: диджей может поставить рукой, если
    захочет, но сам собой рез в плане не появится.
    """
    out: list[tuple[float, str, str]] = []

    def add(w: float, tid: str, why: str) -> None:
        if tid in TECHNIQUES and not TECHNIQUES[tid].requires_stems and TECHNIQUES[tid].requires_decks <= 2:
            out.append((w, tid, why))

    if mismatch:
        add(1.00, "DNB-02", f"темпы не сводятся ({a['bpm']:g} и {b['bpm']:g}) — держать их вместе фразу нельзя.")
        add(0.70, "DNB-19", "темпы не сводятся — рез по границе фразы.")
        return sorted(out, reverse=True)

    if compat < 0.35:
        add(1.00, "DNB-07", f"тональности жёстко спорят ({a['camelot']} и {b['camelot']}) — "
                            f"фразу вместе они не выдержат, эхо закрывает стык.")
        add(0.80, "DNB-25", "основной ход — если на слух конфликт не мешает.")
        return sorted(out, reverse=True)

    add(1.00, "DNB-25", "основной ход: новый заходит интро под старый со снятым низом, "
                        "потом один обмен низом и старый уводится фильтром.")
    return out


BLEND_BARS_MIN, BLEND_BARS_MAX = 4.0, 12.0

# Вес штрафа за два кита разом ПРИ ВЫБОРЕ ТОЧЕК. Ноль — и это результат
# замера, а не осторожность. Ставил 0.9, потом 0.3: и там и там штраф
# перевешивал качество шва и попадание дропа — drop_sync падал с 3 из 10
# до 1, и диджей сразу услышал, что «в старой версии попадал лучше».
#
# Правильное разделение такое: ТОЧКИ выбираются по музыке (брейкдаун,
# яма, дроп, гармония), а столкновение барабанов управляет ДЛИНОЙ —
# там, где два кита неизбежны, сведение режется до четырёх тактов. Это и
# есть «где-то долгий, а где-то лучше резче»: решает не настроение, а то,
# чему предстоит наложиться.
CLASH_WEIGHT = 0.0

# Вес близости темпа при построении порядка треков. Диджей сказал прямо:
# где переход показывает 100%, там и звучит хорошо. Проценты теряются
# ровно на двух вещах — разнице темпа больше 2% и спорящих тональностях,
# поэтому порядок строим по тем же критериям, по которым потом считаются
# проценты, а не по отдельной шкале.
TEMPO_ORDER_WEIGHT = 0.65


def _both_drums_share(a: dict, b: dict, from_t: float, to_t: float, bars: float) -> float:
    """Какую долю сведения обе деки играют барабанами одновременно.

    Это и есть источник «коняшек»: два кита в полосе 300-3000 Гц, где
    живут тела барабанов, складываются в спотыкание. Снятый низ тут не
    помогает — он разводит бочки, а не малые барабаны. Измерено на
    реальном плане: у 3 переходов из 10 обе деки идут барабанами ВСЮ
    длину сведения, в среднем 36%.
    """
    import beatgrid

    da = (a.get("structure") or {}).get("drum_map") or {}
    db = (b.get("structure") or {}).get("drum_map") or {}
    if not da or not db or bars <= 0:
        return 0.0
    bar_a = 60.0 / float(a.get("bpm") or 174.0) * 4
    bar_b = 60.0 / float(b.get("bpm") or 174.0) * 4
    n = int(max(1, round(bars)))
    both = sum(1 for k in range(n)
               if beatgrid.drums_at(da, from_t + k * bar_a)
               and beatgrid.drums_at(db, to_t + k * bar_b))
    return both / n


def _mid_duck_for(share: float) -> float:
    """Насколько убрать середину входящего. Ноль, когда он заходит своим
    интро: глушить пустую подложку незачем, станет ватно."""
    if share > 0.6:
        return 0.5
    if share > 0.25:
        return 0.3
    return 0.0


def _blend_bars(a: dict, b: dict, want_exit: float | None, slot_seconds: float | None,
                default: float = 8.0) -> float:
    """Сколько тактов длится сведение: 4, 8 или 12.

    Раньше это было 32 такта на всё — 44 секунды при 174 BPM. Живой
    диджей столько не тянет: сведение в драм-н-бейсе длится фразу или
    две. Длина не одна на всех, она зависит от того, есть ли чем
    заполнить наложение:

      * уходящий уходит в свой брейкдаун — низ освобождается сам, можно
        не спешить: 12 тактов;
      * у входящего почти нет интро (барабаны с первых тактов) — прятать
        его не под что, две плотные партии столкнутся лбами: 4 такта;
      * треку и так отведено меньше минуты — длинное наложение съест его
        целиком: не больше 4-8.
    """
    bar_a = 60.0 / float(a.get("bpm") or 174.0) * 4
    bar_b = 60.0 / float(b.get("bpm") or 174.0) * 4
    bars = default

    emap_b = (b.get("structure") or {}).get("energy_map") or {}
    dmap_b = (b.get("structure") or {}).get("drum_map") or {}
    intro_bars_b = float(dmap_b.get("drums_start") or 0.0) / bar_b if bar_b else 0.0

    emap_a = (a.get("structure") or {}).get("energy_map") or {}
    breaks_a = [float(x) for x, _y in (emap_a.get("breakdowns") or [])]
    near_break = (want_exit is not None and breaks_a
                  and min(abs(x - want_exit) for x in breaks_a) <= 16 * bar_a)

    if intro_bars_b < 8:
        bars = 4.0                      # прятать нечем — сводим коротко
    elif near_break:
        bars = 12.0                     # уходящий сам расчищает место
    if slot_seconds:
        if slot_seconds < 45:
            bars = min(bars, 4.0)
        elif slot_seconds < 90:
            bars = min(bars, 8.0)
    return float(max(BLEND_BARS_MIN, min(BLEND_BARS_MAX, bars)))


def _overlap_seconds(technique_id: str, bpm: float = 174.0) -> float:
    """Сколько СЕКУНД техника держит обе деки в звуке одновременно.

    Именно секунды, а не долю. По доле Quick Cut получал 80% — формально
    верно (его кроссфейд занимает 80% его же длины), но длится он треть
    секунды. Ухо слышит абсолютное время наложения, а не пропорцию.

    Считаем по её собственной огибающей кроссфейдера, а не по списку
    вручную. Списком я уже ошибся: Tension Riser числился блендом, а
    держит обе деки 5% времени — то есть это рез с фильтрующим разгоном
    перед ним, и точку входа ему подбирали как для бленда.

    Списком я уже ошибся дважды: Tension Riser числился блендом, а держит
    обе деки 0.5 секунды; Quick Cut по доле выглядел как бленд."""
    import numpy as np

    import demo_render

    try:
        events = build_plan(technique_id, "x", "s", "t", bpm, None)["events"]
    except Exception:
        return 0.5
    cf = [e for e in events if e["action"] == "crossfade"]
    if not cf:
        return 0.0
    spb = 60.0 / bpm
    span = max(e["beat_offset"] + e.get("duration_beats", 0) for e in events) * spb
    if span <= 0:
        return 0.0
    fn = demo_render._piecewise_ramp(cf, spb, 0.0)
    v = np.array([fn(t) for t in np.linspace(0, span, 400)])
    return float(((v > 0.1) & (v < 0.9)).mean() * span)


# Порог: держит обе деки меньше двух секунд (примерно такт) — это рез.
CUT_OVERLAP_MAX_SECONDS = 2.0
CUT_TECHNIQUES = {tid for tid in TECHNIQUES
                  if not TECHNIQUES[tid].requires_stems
                  and _overlap_seconds(tid) < CUT_OVERLAP_MAX_SECONDS}

# Сколько переходов в сете вообще имеет смысл делать резом. Рез — это
# акцент: он работает, когда вокруг него бленды. Сет, собранный из одних
# резов, звучит рвано, даже если каждый рез по отдельности правильный —
# ровно эту ошибку я и допустил, когда усилил резовые техники после
# «в DnB режут фейдером».
MAX_CUT_SHARE = 0.3

# Доля переходов, сделанных НЕ основным ходом. Сет, где каждый переход
# сделан по-своему, звучит как демонстрация приёмов, а не как сет.
DEFAULT_TECHNIQUE = "DNB-25"
MAX_ACCENT_SHARE = 0.25


PRESETS = (
    ("classic", "Классика", ("DNB-25", "DNB-01", "DNB-00"),
     "новый заходит интро под старый со снятым низом, потом один обмен низом — так сводят почти всё"),
    ("cut",    "Резом",   ("DNB-22", "DNB-24", "DNB-19"),
     "фейдер перекидывается между треками тактами — акцент, не способ вести весь сет"),
    ("drop",   "На дроп", ("DNB-23", "DNB-16", "DNB-20"),
     "новый дразнит вспышками, потом заходит на своём дропе"),
    ("quick",  "Быстро",  ("DNB-02", "DNB-19", "DNB-07"),
     "короткий рез почти без наложения — когда треки спорят"),
)


def transition_presets(cands: list[tuple[float, str, str]],
                       from_points: list[dict], to_points: list[dict],
                       chosen_id: str,
                       to_points_blend: list[dict] | None = None) -> list[dict]:
    """Четыре понятных способа свести вместо перебора 24 техник.

    Диджей решает «классика или рез», а не «DNB-16 плюс точка №4 плюс
    точка входа №2». Перебор из сотен комбинаций не даёт свободы, он
    парализует: проверить их на слух невозможно."""
    available = {tid for _w, tid, _why in cands}
    best_from = from_points[0] if from_points else None
    best_to = to_points[0] if to_points else None
    late_from = max(from_points, key=lambda p: p["time_seconds"]) if from_points else best_from

    out = []
    for key, label, ladder, hint in PRESETS:
        tid = next((t for t in ladder if t in available), None) or \
              next((t for t in ladder if t in TECHNIQUES), ladder[0])
        is_cut = tid in CUT_TECHNIQUES
        fp = late_from if key in ("cut", "quick") else best_from
        # рез заводит трек там, где у него уже есть бит; классика и бленд —
        # интро поверх ещё играющего старого. Это разные точки.
        tp = best_to if is_cut or not to_points_blend else to_points_blend[0]
        out.append({
            "key": key, "label": label,
            "technique_id": tid,
            "technique_name": TECHNIQUES[tid].name if tid in TECHNIQUES else tid,
            "from_seconds": (fp or {}).get("time_seconds"),
            "to_seconds": (tp or {}).get("time_seconds"),
            "from_label": (fp or {}).get("label"),
            "to_label": (tp or {}).get("label"),
            "hint": hint,
            "current": tid == chosen_id,
        })
    return out


RECENT_PENALTY = (0.55, 0.75, 0.90)  # штраф за повтор: предыдущий переход, позапрошлый, ...


def _pick_technique(a: dict, b: dict, compat: float, mismatch: bool,
                    el_from: int, el_to: int, recent: list[str] | None = None,
                    slot_seconds: float | None = None,
                    cut_share: float | None = None,
                    accent_share: float | None = None) -> tuple[str, str]:
    """Возвращает (technique_id, объяснение).

    recent — техники последних переходов, свежие первыми. Повтор не
    запрещён (иногда он объективно лучший вариант), но штрафуется: два
    одинаковых сведения подряд слышны как «оно всегда делает одно и то же»,
    даже когда каждое по отдельности выбрано правильно."""
    cands = technique_candidates(a, b, compat, mismatch, el_from, el_to, slot_seconds=slot_seconds)
    if not cands:
        return "DNB-00", "базовый случай — Long Blend."
    recent = recent or []
    scored = []
    for w, tid, why in cands:
        penalty = 1.0
        for pos, pen in enumerate(RECENT_PENALTY):
            if pos < len(recent) and recent[pos] == tid:
                penalty = pen
                break
        # рез подряд за резом — сет рассыпается на куски
        if tid in CUT_TECHNIQUES and recent and recent[0] in CUT_TECHNIQUES:
            penalty *= 0.45
        if tid in CUT_TECHNIQUES and cut_share is not None and cut_share > MAX_CUT_SHARE:
            penalty *= 0.5
        # акцентов в сете должно быть меньшинство
        if tid != DEFAULT_TECHNIQUE and accent_share is not None and accent_share > MAX_ACCENT_SHARE:
            penalty *= 0.4
        if tid != DEFAULT_TECHNIQUE and recent and recent[0] != DEFAULT_TECHNIQUE:
            penalty *= 0.6
        scored.append((w * penalty, w, tid, why))
    scored.sort(reverse=True)
    _, w, tid, why = scored[0]
    return tid, f"{why}"


def _alternatives(chosen_id: str, mismatch: bool, compat: float,
                  cands: list[tuple[float, str, str]] | None = None) -> list[dict]:
    if cands:
        out = [{"id": tid, "name": TECHNIQUES[tid].name, "why": why}
               for _, tid, why in cands if tid != chosen_id]
        return out[:4]
    pool = ["DNB-02", "DNB-07", "DNB-17"] if mismatch else (
        ["DNB-07", "DNB-12", "DNB-17"] if compat < 0.5 else ["DNB-00", "DNB-01", "DNB-20", "DNB-16"])
    return [{"id": t, "name": TECHNIQUES[t].name} for t in pool if t != chosen_id and t in TECHNIQUES][:3]


def _mix_point_bars(track: dict, bpm: float, want: str) -> float | None:
    """Ищет ближайший drop/breakdown/phrase-boundary в structure трека и
    переводит его в номер такта (не абсолютное время) — want: "drop" | "phrase"."""
    structure = track.get("structure") or {}
    if want == "drop" and structure.get("drops"):
        return structure["drops"][0]["bar_index"]
    if structure.get("phrase_boundaries"):
        bar_seconds = 60.0 / bpm * 4
        return round(structure["phrase_boundaries"][0] / bar_seconds)
    return None


# --- рекомендации пар треков ПОД конкретную технику ---
# (обратная задача к _pick_technique: там дана пара -> выбираем технику;
# здесь дана техника -> ищем в библиотеке пары, которые ей подходят.
# Используется вкладкой "Техника" — см. server.py::.../techniques/{id}/recommend)

def _el_pair(a: dict, b: dict, el_map: dict[str, int] | None) -> tuple[int, int]:
    if el_map is None:
        return energy_level(a["energy"]), energy_level(b["energy"])
    return (el_map.get(a["name"], energy_level(a["energy"])),
            el_map.get(b["name"], energy_level(b["energy"])))


def score_pair_for_technique(technique: Technique, a: dict, b: dict,
                             el_map: dict[str, int] | None = None) -> tuple[float, float, float]:
    """Возвращает (score 0..~1, camelot_compat, bpm_diff_pct) — чем выше
    score, тем лучше пара (a->b) подходит именно под критерии technique
    (bpm_delta_max/key_rule/energy_direction из techniques.py)."""
    compat = camelot_compatibility(a["camelot"], b["camelot"])
    bpm_diff_pct = abs(a["bpm"] - b["bpm"]) / max(a["bpm"], b["bpm"], 1.0) * 100

    score = 0.0
    if technique.bpm_delta_max is not None:
        score += 0.4 if bpm_diff_pct <= technique.bpm_delta_max else -0.3
    else:
        score += 0.15  # технике BPM не важен ("any") — небольшой нейтральный бонус

    if technique.key_rule == "compatible":
        score += compat * 0.4
    elif technique.key_rule == "clash":
        score += (1.0 - compat) * 0.4  # эта техника как раз ДЛЯ конфликта тональностей
    else:
        score += 0.2

    # Уровни — относительно библиотеки, если она передана: по абсолютной
    # шкале почти все треки одного жанра оказываются на одном EL, и
    # "рост энергии" никогда не срабатывает.
    el_a, el_b = _el_pair(a, b, el_map)
    if technique.energy_direction == "up":
        score += 0.2 if el_b > el_a else (0.0 if el_b == el_a else -0.1)
    elif technique.energy_direction == "down":
        score += 0.2 if el_b < el_a else (0.0 if el_b == el_a else -0.1)
    else:
        score += 0.1

    return score, compat, bpm_diff_pct


def _pair_reason(technique: Technique, compat: float, bpm_diff_pct: float, el_a: int, el_b: int) -> str:
    parts = []
    if technique.bpm_delta_max is not None:
        parts.append(f"BPM разница {bpm_diff_pct:.1f}% (порог {technique.bpm_delta_max}%)")
    if technique.key_rule == "compatible":
        parts.append(f"тональности совместимы ({compat:.2f})")
    elif technique.key_rule == "clash":
        parts.append(f"тональности конфликтуют ({compat:.2f}) — то, для чего эта техника")
    if technique.energy_direction != "any":
        if el_b > el_a:
            actual = "рост"
        elif el_b < el_a:
            actual = "спад"
        else:
            actual = "ровно"
        want = "нужен рост" if technique.energy_direction == "up" else "нужен спад"
        parts.append(f"энергия EL{el_a}→EL{el_b} ({actual}, технике {want})")
    return "; ".join(parts) if parts else "универсальная пара, без особых требований техники"


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _plan_length_beats(technique: Technique, bpm: float) -> tuple[float, float | None]:
    """(длительность техники в долях, длина лупа в тактах | None) — считаем
    по РЕАЛЬНЫМ событиям build_plan(), а не по отдельной табличке, чтобы
    подсказка не разъезжалась с тем, что технику потом исполнит companion."""
    try:
        plan = build_plan(technique.id, "hint", "A", "B", bpm or 174.0, None)
    except (NotImplementedError, KeyError):
        return 0.0, None

    end_beats = 0.0
    loop_bars = None
    loop_on = None
    for e in plan.get("events", []):
        offset = float(e.get("beat_offset", 0.0))
        end_beats = max(end_beats, offset + float(e.get("duration_beats", 0.0) or 0.0))
        if e.get("action") == "loop_activate":
            loop_on = offset
        elif e.get("action") == "loop_exit" and loop_on is not None and loop_bars is None:
            loop_bars = round((offset - loop_on) / 4.0, 2)
    return end_beats, loop_bars


def _outgoing_cue(track: dict, bpm: float, needed_seconds: float) -> dict:
    """Где в УХОДЯЩЕМ треке начинать переход.

    Диджейская логика по убыванию приоритета: (1) начало последнего
    брейкдауна, до конца которого влезает вся техника — классический выход
    "из ямы"; (2) последняя граница фразы, после которой ещё остаётся
    времени на всю технику; (3) если структура не нашлась — просто отступ
    от конца трака."""
    structure = track.get("structure") or {}
    duration = float(track.get("duration_seconds") or 0.0)
    bar_seconds = (60.0 / bpm * 4) if bpm else 1.4
    latest_start = max(0.0, duration - needed_seconds)

    for bd in reversed(structure.get("breakdowns") or []):
        start = float(bd.get("start_seconds", 0.0))
        if start <= latest_start:
            # bar_index из БД не берём: он посчитан на том темпе, что был у
            # сканера, а темп мог быть нормализован потом (DnB 86 -> 172),
            # и номер такта разъехался бы вдвое. Время — инвариант, такт
            # считаем от него.
            return {
                "time_seconds": round(start, 1),
                "bar_index": round(start / bar_seconds),
                "label": f"брейкдаун на {_fmt_time(start)}",
            }

    phrases = [float(x) for x in (structure.get("phrase_boundaries") or [])]
    usable = [x for x in phrases if x <= latest_start]
    if usable:
        start = usable[-1]
        return {
            "time_seconds": round(start, 1),
            "bar_index": round(start / bar_seconds),
            "label": f"граница фразы на {_fmt_time(start)}",
        }

    return {
        "time_seconds": round(latest_start, 1),
        "bar_index": round(latest_start / bar_seconds),
        "label": f"за {needed_seconds:.0f}с до конца ({_fmt_time(latest_start)})" if duration else "начало трека",
    }


def _incoming_cue(track: dict, bpm: float) -> dict:
    """Откуда запускать ВХОДЯЩИЙ трек: первая граница фразы (обычно самое
    начало), плюс — если есть дроп — через сколько тактов после старта он
    прилетит. Это то, ради чего диджей и считает такты: чтобы дроп нового
    трека попал куда задумано, а не «когда получится»."""
    structure = track.get("structure") or {}
    bar_seconds = (60.0 / bpm * 4) if bpm else 1.4
    phrases = [float(x) for x in (structure.get("phrase_boundaries") or [])]
    start = phrases[0] if phrases else 0.0

    out = {
        "time_seconds": round(start, 1),
        "bar_index": round(start / bar_seconds),
        "label": f"с {_fmt_time(start)}" if start > 0.5 else "с начала трека",
    }
    drops = structure.get("drops") or []
    if drops:
        drop_t = float(drops[0].get("time_seconds", 0.0))
        bars_to_drop = max(0, round((drop_t - start) / bar_seconds))
        out["drop_seconds"] = round(drop_t, 1)
        out["bars_to_drop"] = bars_to_drop
        out["label"] += f"; дроп на {_fmt_time(drop_t)} — через {bars_to_drop} тактов"
    return out


def technique_cue_hints(technique: Technique, a: dict, b: dict) -> dict:
    """Конкретика "откуда сводить": точка старта в уходящем треке, точка
    запуска входящего, длина перехода в тактах/секундах и длина лупа (если
    техника его использует). Считается по structure из track_analysis.py
    (дропы/брейкдауны/границы фраз) — то есть по реальному аудио, а не по
    среднему по больнице."""
    bpm = float(a.get("bpm") or 174.0)
    beats, loop_bars = _plan_length_beats(technique, bpm)
    length_bars = round(beats / 4.0, 1)
    length_seconds = round(beats * 60.0 / bpm, 1) if bpm else 0.0

    return {
        "from_track": _outgoing_cue(a, bpm, length_seconds),
        "into_track": _incoming_cue(b, float(b.get("bpm") or bpm)),
        "length_bars": length_bars,
        "length_seconds": length_seconds,
        "loop_bars": loop_bars,
    }


def recommend_tracks_for_technique(technique: Technique, tracks: list[dict], top_n: int = 3) -> list[dict]:
    """Перебирает все упорядоченные пары треков библиотеки и возвращает
    top_n, лучше всего подходящих под критерии technique. O(N²) по трекам —
    для типичной библиотеки ди-джея (сотни, не миллионы треков) это доли
    секунды, отдельной оптимизации/индекса не требуется."""
    if len(tracks) < 2:
        return []
    el_map = library_energy_levels(tracks)
    scored = []
    for a in tracks:
        for b in tracks:
            if a is b or a.get("name") == b.get("name"):
                continue
            score, compat, bpm_diff_pct = score_pair_for_technique(technique, a, b, el_map)
            scored.append((score, compat, bpm_diff_pct, a, b))
    scored.sort(key=lambda row: row[0], reverse=True)

    out = []
    seen_pairs = set()
    for score, compat, bpm_diff_pct, a, b in scored:
        key = frozenset((a.get("name"), b.get("name")))
        if key in seen_pairs:
            continue  # не показываем и A->B, и B->A одновременно — только лучшее направление
        seen_pairs.add(key)
        el_a, el_b = _el_pair(a, b, el_map)
        out.append({
            "track_a": {"name": a["name"], "path": a.get("path"), "bpm": a["bpm"], "camelot": a["camelot"]},
            "track_b": {"name": b["name"], "path": b.get("path"), "bpm": b["bpm"], "camelot": b["camelot"]},
            "score": round(score, 2),
            "reason": _pair_reason(technique, compat, bpm_diff_pct, el_a, el_b),
            "cue": technique_cue_hints(technique, a, b),
        })
        if len(out) >= top_n:
            break
    return out


def select_for_duration(tracks: list[dict], ordered: list[dict],
                        target_minutes: float | None) -> list[dict]:
    """Оставлено для режима «отобрать треки под длительность».

    По умолчанию НЕ используется: длительность сета теперь задаёт, сколько
    времени играет каждый трек (см. plan_layout), а не сколько треков
    выкинуть. Выкидывать треки — не то, что имеет в виду диджей, когда
    ставит 90 минут: он хочет свести то, что выбрал, а не получить обрезок
    своей же подборки."""
    if not target_minutes or not ordered:
        return ordered
    target = float(target_minutes) * 60.0
    acc, picked = 0.0, []
    for tr in ordered:
        dur = float(tr.get("duration_seconds") or 300.0)
        if acc and abs(acc - target) <= abs(acc + dur - target):
            break
        picked.append(tr)
        acc += dur
    return picked or ordered[:1]


# Меньше этого трек не успевает прозвучать как трек, а не как склейка.
MIN_SLOT_SECONDS = 35.0


MAX_DROP_SHARE = 0.3


def drop_unmixable(ordered: list[dict], max_share: float = MAX_DROP_SHARE) -> tuple[list[dict], list[dict]]:
    """Убирает треки, которые не свести НИ С ЧЕМ в этой библиотеке.

    «Лучше убрать трек, не подходящий к миксу, чем смешивать
    несмешиваемое» — верно, но только про темп. Трек чужого темпа
    действительно не сводится ни с чем: единственный доступный ход это
    резкий рез, и он слышен как ошибка.

    А вот тональности выкидыванием чинить НЕЛЬЗЯ, и первая версия этого
    кода на реальной библиотеке выбросила 43 трека из 48 именно поэтому:
    она удаляла всё, у чего совместимость с соседом ниже порога, хотя
    правильный ответ — переставить, а не удалить. Порядком занимается
    order_tracks; здесь только темп.
    """
    if len(ordered) < 3:
        return ordered, []
    center = tempo.library_center([t["bpm"] for t in ordered if t.get("bpm")]) or 174.0
    scored = []
    for t in ordered:
        rel = tempo.relate(center, float(t.get("bpm") or 0))
        scored.append((rel["compatible"], rel["error_pct"], t))

    outliers = sorted(((err, t) for ok, err, t in scored if not ok),
                      key=lambda p: p[0], reverse=True)   # словари не сравниваются — сортируем по ошибке
    limit = int(len(ordered) * max_share)
    victims = {id(t) for _err, t in outliers[:limit]}

    kept = [t for t in ordered if id(t) not in victims]
    dropped = [{"name": t["name"], "bpm": t["bpm"], "camelot": t["camelot"],
                "reason": f"{t['bpm']:.0f} BPM — вне темпа сета ({center:.0f}), свести не с чем"}
               for _err, t in outliers[:limit]]

    # Второй проход: два трека могут оба лежать в полосе сета и всё равно
    # не сводиться ДРУГ С ДРУГОМ (171 и 178 — оба рядом с 174, а между
    # собой 4%). Такая пара заставляет ставить рез посреди сета, а рез
    # посреди сета слышен как ошибка. Убираем того, кто ломает цепочку.
    # у второго прохода свой запас: пары, которые не сводятся между собой,
    # это жёсткий отказ, а не «лишний трек сверх лимита»
    hard_limit = limit + int(len(ordered) * 0.15)
    guard = 0
    while len(kept) > 2 and len(dropped) < hard_limit and guard < len(ordered):
        guard += 1
        bad = None
        for i in range(len(kept) - 1):
            if bpm_type_mismatch(kept[i]["bpm"], kept[i + 1]["bpm"]):
                bad = i
                break
        if bad is None:
            break
        i = bad
        # выкидываем того, кто хуже ложится к своему другому соседу
        left_ok = i == 0 or not bpm_type_mismatch(kept[i - 1]["bpm"], kept[i]["bpm"])
        right_ok = (i + 2 >= len(kept)) or not bpm_type_mismatch(kept[i + 1]["bpm"], kept[i + 2]["bpm"])
        victim_idx = i + 1 if left_ok and not right_ok else (i if not left_ok else i + 1)
        v = kept.pop(victim_idx)
        dropped.append({"name": v["name"], "bpm": v["bpm"], "camelot": v["camelot"],
                        "reason": f"{v['bpm']:.0f} BPM не сводится с соседями по сету"})

    return (kept, dropped) if len(kept) >= 2 else (ordered, [])


def plan_layout(ordered: list[dict], target_minutes: float | None) -> dict:
    """Сколько времени играет каждый трек, чтобы весь набор уложился в
    заданную длительность.

    Диджей ставит «90 минут» и ждёт, что прозвучат ВСЕ выбранные треки,
    просто короче: 50 треков за 90 минут — это чуть меньше двух минут на
    трек, нормальный быстрый сет. Раньше это же число означало «выкинуть
    31 трек», что и близко не то же самое.
    """
    n = len(ordered)
    if n < 2:
        return {"slot_seconds": None, "target_minutes": target_minutes, "tracks": n}
    if not target_minutes:
        # без заданной длительности играем треки как есть (условно 5 минут)
        return {"slot_seconds": None, "target_minutes": None, "tracks": n}

    target = float(target_minutes) * 60.0
    slot = target / n
    warning = None
    if slot < MIN_SLOT_SECONDS:
        warning = (f"{n} треков за {target_minutes:g} мин — это {slot:.0f}с на трек. "
                   f"Ставлю минимум {MIN_SLOT_SECONDS:.0f}с: сет выйдет "
                   f"{n * MIN_SLOT_SECONDS / 60:.0f} мин вместо {target_minutes:g}.")
        slot = MIN_SLOT_SECONDS
    # длиннее самого трека слот быть не может
    longest = max(float(t.get("duration_seconds") or 300.0) for t in ordered)
    slot = min(slot, longest)
    return {"slot_seconds": round(slot, 1), "target_minutes": target_minutes,
            "tracks": n, "warning": warning,
            "planned_minutes": round(slot * n / 60.0, 1)}


def plan_quality(strategy: dict) -> dict:
    """Насколько удачен план ЦЕЛИКОМ — одним числом.

    Нужно, чтобы длительность подбиралась не на глаз. Диджей заметил, что
    точки сведения зависят от заданных минут: на 120 минутах уводит не
    там, на 180 — почти в конце трека, а хорошо где-то посередине. Это не
    случайность: длительность задаёт, к какой секунде трек должен уйти, а
    рядом с этой секундой может не оказаться ни брейкдауна, ни ямы.
    Значит длительность можно ВЫБИРАТЬ по тому, сколько переходов легло
    на музыку.

    Три слагаемых, все три — уже посчитанные величины, а не новые догадки:
      * доля переходов без рисков (те самые 100% в интерфейсе);
      * средняя гармония накладываемых кусков;
      * доля точек, попавших на музыкальное событие, а не на «по времени».
    """
    trs = strategy.get("transitions") or []
    if not trs:
        return {"score": 0.0, "full_confidence": 0.0, "harmony": 0.0, "good_points": 0.0}

    full = sum(1 for t in trs if float(t.get("confidence") or 0) >= 0.999) / len(trs)
    hs = [t["harmony"] for t in trs if t.get("harmony") is not None]
    harmony = sum(hs) / len(hs) if hs else 0.0

    GOOD_FROM = {"breakdown", "pit", "drop", "phrase"}
    GOOD_TO = {"drop_sync", "intro_lead", "pre_drop", "body"}
    good = sum(1 for t in trs
               if (t.get("from_point") or {}).get("kind") in GOOD_FROM
               and (t.get("to_point") or {}).get("kind") in GOOD_TO) / len(trs)

    return {
        "score": round(full * 0.45 + harmony * 0.35 + good * 0.20, 4),
        "full_confidence": round(full, 3),
        "harmony": round(harmony, 3),
        "good_points": round(good, 3),
    }


def plan_strategy(tracks: list[dict], arc_shape: str = "rising",
                  plan_id_prefix: str = "strategy",
                  target_minutes: float | None = None,
                  variant: int = 0,
                  overrides: dict | None = None,
                  fit_mode: str = "compress",
                  track_order: list[str] | None = None,
                  only_mixable: bool = True,
                  exclude: list[str] | None = None) -> dict:
    """Главная точка входа: аналитические записи треков -> план сета.

    target_minutes — желаемая длительность; лишние треки отбрасываются.
    variant        — номер варианта последовательности (0 = основной).
                     Разные варианты стартуют с разных треков, поэтому
                     жадный отбор идёт другим путём и даёт другой сет.
    overrides      — правки диджея по конкретным переходам:
                     {"3": {"technique_id": "DNB-07", "bars": 16,
                            "from_point_seconds": 214.0,
                            "to_point_seconds": 0.0}}
    """
    # Треки, которые диджей выкинул из плана руками. Держим их отдельным
    # списком, а не «отбором подмножества»: подмножество пришлось бы
    # пересылать целиком на каждое перестроение, и один забытый трек
    # молча возвращался бы обратно.
    if exclude:
        dropped_by_hand = set(exclude)
        tracks = [t for t in tracks if t.get("name") not in dropped_by_hand]
        if not tracks:
            raise ValueError("Из плана убраны все треки — верните хотя бы один.")

    el_map = library_energy_levels(tracks)
    if track_order:
        # Порядок, выставленный диджеем руками, важнее любого алгоритма:
        # он слышал эти треки, а мы считали их спектры.
        by_name = {t["name"]: t for t in tracks}
        ordered = [by_name[n] for n in track_order if n in by_name]
        missing = [t for t in tracks if t["name"] not in set(track_order)]
        ordered += missing
    else:
        ordered = order_tracks(tracks, arc_shape, el_map, variant=variant)
    dropped_tracks: list[dict] = []
    if only_mixable and not track_order:
        ordered, dropped_tracks = drop_unmixable(ordered)
    if fit_mode == "select":
        ordered = select_for_duration(tracks, ordered, target_minutes)
    layout = plan_layout(ordered, target_minutes)
    slot = layout.get("slot_seconds")
    overrides = {str(k): v for k, v in (overrides or {}).items()}

    transitions = []
    total_confidence = 0.0
    # хронометраж: с какой секунды ТРЕКА он начал играть и в какой момент
    # СЕТА мы находимся
    entry_seconds = 0.0
    set_clock = 0.0

    def el_of(tr: dict) -> int:
        return el_map.get(tr["name"], energy_level(tr["energy"]))

    by_name = {tr["name"]: tr for tr in tracks}

    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        ov = overrides.get(str(i), {})
        compat = camelot_compatibility(a["camelot"], b["camelot"])
        mismatch = bpm_type_mismatch(a["bpm"], b["bpm"])
        el_from, el_to = el_of(a), el_of(b)

        cands = technique_candidates(a, b, compat, mismatch, el_from, el_to, slot_seconds=slot)
        tid, rule = _pick_technique(a, b, compat, mismatch, el_from, el_to,
                                    recent=[t["technique_id"] for t in reversed(transitions)][:3],
                                    slot_seconds=slot,
                                    cut_share=(sum(1 for t in transitions if t["technique_id"] in CUT_TECHNIQUES)
                                               / len(transitions)) if transitions else 0.0,
                                    accent_share=(sum(1 for t in transitions
                                                      if t["technique_id"] != DEFAULT_TECHNIQUE)
                                                  / len(transitions)) if transitions else 0.0)
        if ov.get("technique_id") in TECHNIQUES:
            tid = ov["technique_id"]
            rule = f"Выбрано вручную: {TECHNIQUES[tid].name}"
        technique = TECHNIQUES[tid]

        # Раньше здесь было «уверенность» = 0.55 + compat*0.3 + 0.1 + 0.05,
        # то есть у любой пары получалось 96-97% — число, которое ничего не
        # значило и только создавало видимость точности. Вместо него —
        # понятная оценка риска перехода из вещей, которые мы реально знаем.
        risk = []
        if mismatch:
            risk.append("темпы не сводятся")
        elif tempo.relate(a["bpm"], b["bpm"])["error_pct"] > 2.0:
            risk.append("темп придётся тянуть питчем")
        if compat < 0.5:
            risk.append("тональности спорят")
        if not ((b.get("structure") or {}).get("drum_map")):
            risk.append("не знаю, где у входящего барабаны")
        confidence = 1.0 - min(0.75, 0.25 * len(risk))
        total_confidence += confidence

        want_exit = (entry_seconds + slot) if slot else None
        if ov.get("bars"):
            bars = float(ov["bars"])
        elif tid == DEFAULT_TECHNIQUE:
            bars = _blend_bars(a, b, want_exit, slot)
        else:
            bars = float(technique.params[0].default if technique.params else 8)
        length_seconds = bars * 4 * 60.0 / (a["bpm"] or 174.0)
        from_points = mix_points.candidates_for_outgoing(
            a, a["bpm"], length_seconds, want_seconds=want_exit, entry_seconds=entry_seconds)
        # Считаем ОБА набора: рез и бленд заводят трек в разные места, а
        # пресеты предлагают и то, и другое одновременно.
        to_points_cut = mix_points.candidates_for_incoming(
            b, b["bpm"], slot_seconds=slot, want_drums=True, transition_seconds=length_seconds)
        to_points_blend = mix_points.candidates_for_incoming(
            b, b["bpm"], slot_seconds=slot, want_drums=False, transition_seconds=length_seconds)
        to_points = to_points_cut if tid in CUT_TECHNIQUES else to_points_blend
        from_chosen, to_chosen, harmony = _pick_point_pair(
            a, b, from_points, to_points,
            ov.get("from_point_seconds"), ov.get("to_point_seconds"), bars)

        both_share = _both_drums_share(
            a, b,
            (from_chosen or {}).get("time_seconds") or 0.0,
            (to_chosen or {}).get("time_seconds") or 0.0,
            bars)
        mid_duck = _mid_duck_for(both_share)
        # Если разойтись по барабанам не вышло, длинное наложение только
        # умножает спотыкание: режем сведение до четырёх тактов.
        if both_share > 0.35 and not ov.get("bars") and bars > 4.0:
            bars = 4.0
            length_seconds = bars * 4 * 60.0 / (a["bpm"] or 174.0)
            both_share = _both_drums_share(
                a, b,
                (from_chosen or {}).get("time_seconds") or 0.0,
                (to_chosen or {}).get("time_seconds") or 0.0, bars)
            mid_duck = _mid_duck_for(both_share)

        airtime = max(8.0, (from_chosen["time_seconds"] if from_chosen else 0.0) - entry_seconds)
        start_in_set = set_clock
        set_clock += airtime

        transitions.append({
            "index": i,
            "from": a["name"], "to": b["name"],
            "technique_id": tid, "technique_name": technique.name,
            "confidence": round(confidence, 2),
            "risks": risk,
            "bpm_from": a["bpm"], "bpm_to": b["bpm"],
            "tempo_note": tempo_note(a["bpm"], b["bpm"]),
            "key_from": a["camelot"], "key_to": b["camelot"],
            "el_from": el_from, "el_to": el_to,
            "bars": bars,
            "blend_bars": bars,
            "mid_duck": round(mid_duck, 2),
            "both_drums_share": round(both_share, 2),
            "length_seconds": round(length_seconds, 1),
            "rule": rule,
            "edited": bool(ov),
            "alternatives": _alternatives(tid, mismatch, compat, cands),
            "presets": transition_presets(cands, from_points, to_points_cut, tid, to_points_blend),
            "from_point": from_chosen,
            "to_point": to_chosen,
            "harmony": harmony,
            # хронометраж
            "entry_seconds": round(entry_seconds, 1),
            "airtime_seconds": round(airtime, 1),
            "set_time_seconds": round(start_in_set, 1),
            "slot_seconds": slot,
            "from_point_options": from_points,
            "to_point_options": to_points,
            # оставлено для совместимости со старым UI
            "mix_point_bar": from_chosen["bar_index"] if from_chosen else None,
        })

        entry_seconds = to_chosen["time_seconds"] if to_chosen else 0.0

    energy_arc = [el_of(tr) for tr in ordered]
    avg_confidence = round(total_confidence / len(transitions), 2) if transitions else None
    # Хвост последнего трека: он играет столько же, сколько остальные.
    last_tail = slot if slot else float(ordered[-1].get("duration_seconds") or 300.0) if ordered else 0.0
    total_seconds = set_clock + last_tail

    return {
        "tracks": [
            {"name": tr["name"], "bpm": tr["bpm"], "camelot": tr["camelot"], "key": tr.get("key"),
             "energy": tr["energy"], "el": el_of(tr), "duration_seconds": tr.get("duration_seconds"),
             "path": tr.get("path"),
             "set_time_seconds": (round(transitions[i]["set_time_seconds"], 1) if i < len(transitions)
                                  else round(set_clock, 1)),
             "airtime_seconds": (transitions[i]["airtime_seconds"] if i < len(transitions)
                                 else round(last_tail, 1))}
            for i, tr in enumerate(ordered)
        ],
        "transitions": transitions,
        "energy_arc": energy_arc,
        "transitions_count": len(transitions),
        "avg_confidence": avg_confidence,
        "total_duration_minutes": round(total_seconds / 60.0, 1),
        "arc_shape": arc_shape,
        "variant": variant,
        "target_minutes": target_minutes,
        "variants_available": VARIANT_COUNT,
        "layout": layout,
        "fit_mode": fit_mode,
        "dropped_tracks": dropped_tracks,
        "only_mixable": only_mixable,
        "excluded": sorted(exclude) if exclude else [],
        "manual_order": bool(track_order),
    }


def _pick_point_pair(a: dict, b: dict, from_points: list[dict], to_points: list[dict],
                     want_from, want_to,
                     blend_bars: float = 8.0) -> tuple[dict | None, dict | None, float | None]:
    """Выбирает точки ухода и входа ВМЕСТЕ, а не по отдельности.

    Раньше каждая сторона выбиралась сама по себе: A уводился с лучшей
    своей границы фразы, B заводился в лучшее своё место, и ничто не
    заставляло эти два куска подойти друг другу по гармонии. Измерено на
    реальном плане: у половины переходов накладываемые куски расходились
    (0.69-0.71 при том, что 0.85+ это «сходятся»), причём camelot по треку
    целиком этого не показывал вообще — у пары с совместимостью 0.90 куски
    спорили, у пары с 0.25 сходились.

    Здесь перебираем лучшие кандидаты с обеих сторон и берём ту пару, у
    которой сходится гармония ИМЕННО ТЕХ фраз, что будут звучать вместе.
    """
    import beatgrid

    fchosen = _chosen_point(from_points, want_from)
    tchosen = _chosen_point(to_points, want_to)
    if want_from is not None or want_to is not None:
        return fchosen, tchosen, None            # диджей выбрал руками — не спорим

    ca = ((a.get("structure") or {}).get("chroma_map")) or {}
    cb = ((b.get("structure") or {}).get("chroma_map")) or {}
    if not ca.get("frames") or not cb.get("frames"):
        return fchosen, tchosen, None            # карт нет — работаем как раньше

    da = ((a.get("structure") or {}).get("drum_map")) or {}
    db = ((b.get("structure") or {}).get("drum_map")) or {}
    bar_a = 60.0 / float(a.get("bpm") or 174.0) * 4

    best = None
    for fp in from_points[:4]:
        for tp in to_points[:4]:
            fit = beatgrid.harmony_fit(ca, fp["time_seconds"], cb, tp["time_seconds"])
            if fit is None:
                continue
            # Штраф за два кита разом. Диджей не накладывает две полные
            # партии барабанов на всю фразу: тела малых барабанов живут в
            # одной полосе, их микро-тайминг никогда не совпадает, и ухо
            # слышит спотыкание. Эквалайзером это не лечится (измерено:
            # -0.6 dB и та же плотность атак) — лечится тем, чтобы не
            # заводить трек туда, где у него уже идут барабаны, пока
            # старый ещё играет своими.
            clash = _both_drums_share(a, b, fp["time_seconds"], tp["time_seconds"],
                                      blend_bars) if (da and db) else 0.0
            score = fp["score"] * 0.25 + tp["score"] * 0.25 + fit * 1.4 - clash * CLASH_WEIGHT
            if best is None or score > best[0]:
                best = (score, fp, tp, fit)
    if best is None:
        return fchosen, tchosen, None
    _s, fp, tp, fit = best
    fp = dict(fp); tp = dict(tp)
    fp["harmony"] = tp["harmony"] = round(fit, 2)
    return fp, tp, round(fit, 2)


def _chosen_point(options: list[dict], wanted_seconds) -> dict | None:
    """Точка, выбранная диджеем (ближайшая к заданной секунде), иначе
    лучшая по оценке."""
    if not options:
        return None
    if wanted_seconds is None:
        return options[0]
    want = float(wanted_seconds)
    return min(options, key=lambda p: abs(p["time_seconds"] - want))


def build_transition_plan(strategy: dict, transition_index: int, source_deck: str, target_deck: str,
                            plan_id: str, param_overrides: dict | None = None) -> dict:
    """Материализует ОДИН переход из уже посчитанной стратегии в реальный
    MixPlan (для кнопки "▶" у перехода в UI — см. server.py). param_overrides
    — значения слайдеров параметров техники из вкладки "Техника" (см.
    static/chat.html), если диджей их поправил перед запуском."""
    tr = strategy["transitions"][transition_index]
    return build_plan(tr["technique_id"], plan_id, source_deck, target_deck, tr["bpm_from"], param_overrides)
