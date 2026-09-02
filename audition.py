"""
audition.py — судейский цикл: система предлагает шов, диджей слушает и
судит, вердикты копятся и меняют дальнейшие предложения.

Почему не «построить сет и показать».

Алгоритм умеет оценить темп, тональность, энергию и стыковку секций —
всё, что измеримо. Он не умеет и не будет уметь оценить, ЛЯЖЕТ ли этот
конкретный переход в этом конкретном сете: это вкус, и он у каждого
диджея свой. Значит правильная роль алгоритма — не решать, а СУЖАТЬ
перебор: из миллиона возможных швов предложить тот, который стоит
послушать, а решение оставить человеку.

Отсюда устройство. Единица работы — ШОВ (seam): пара треков, техника и
две точки в секциях. Диджей слушает шов и выносит вердикт. Одобренные
швы копятся в пул; из пула в любой момент собирается сет. Отказ — не
просто «нет», а причина, и от причины зависит, что предложат следующим:
другую технику, другой стык, другой второй трек или другую пару целиком.

Вердикты не пропадают: отклонённая связка «техника + вид стыковки»
теряет вес во всех будущих предложениях. Это и есть обучение на вкусе
диджея — без моделей и без обучения в кавычках, простым счётом.
"""
from __future__ import annotations

import re
import json
import sqlite3
import time
from contextlib import contextmanager

import persistence

# Причины отказа. От причины зависит, что менять в следующем предложении,
# и это главное отличие от простого «не нравится».
REASONS = {
    "technique": "не та техника — оставить треки и стык, взять другой приём",
    "junction": "не тот стык — оставить треки и приём, взять другие секции",
    "first": "не тот первый трек — оставить второй, искать другой первый",
    "second": "не тот второй трек — оставить первый, искать другой второй",
    "pair": "не та пара — искать другую пару целиком",
    "genre_a": "не тот жанр первого трека — оставить второй, взять первый другого жанра",
    "genre_b": "не тот жанр второго трека — оставить первый, взять второй другого жанра",
}


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            a_path TEXT NOT NULL,
            b_path TEXT NOT NULL,
            technique_id TEXT NOT NULL,
            pair_label TEXT,
            exit_seconds REAL,
            entry_seconds REAL,
            params_json TEXT,
            verdict TEXT NOT NULL,          -- approved | rejected
            reason TEXT,                     -- см. REASONS, только для rejected
            note TEXT,
            created_at REAL NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS seams_room ON seams(session_id, verdict)")
    conn.commit()


@contextmanager
def _db():
    conn = persistence._connect()
    _init(conn)
    yield conn


# --------------------------------------------------------------- вердикты

def _seam_params(seam: dict) -> dict:
    """Что из шва нужно сохранить, кроме треков, приёма и точек.

    Длина в тактах — не украшение: стратегия по ней считает окно
    перехода, и без неё одобренный шов пересобирался с длиной «по
    умолчанию для этого приёма», то есть звучал не так, как его слушали
    и одобряли."""
    params = dict(seam.get("params") or {})
    if seam.get("bars") and "bars" not in params:
        params["bars"] = float(seam["bars"])
    return params


def record(session_id: str, seam: dict, verdict: str,
           reason: str | None = None, note: str | None = None) -> dict:
    """Записать приговор шву. verdict: approved | rejected."""
    if verdict not in ("approved", "rejected"):
        raise ValueError("вердикт бывает approved или rejected")
    if verdict == "rejected" and reason not in REASONS:
        raise ValueError(f"причина отказа должна быть из {sorted(REASONS)}")
    with _db() as conn:
        conn.execute(
            """INSERT INTO seams (session_id, a_path, b_path, technique_id, pair_label,
                                  exit_seconds, entry_seconds, params_json,
                                  verdict, reason, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, seam["a_path"], seam["b_path"], seam["technique_id"],
             seam.get("pair_label"), seam.get("exit_seconds"), seam.get("entry_seconds"),
             json.dumps(_seam_params(seam), ensure_ascii=False),
             verdict, reason, note, time.time()))
        conn.commit()
    return {"ok": True, "verdict": verdict}


def pool(session_id: str) -> list[dict]:
    """Одобренные швы, свежие последними — это и есть материал сета."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT a_path, b_path, technique_id, pair_label, exit_seconds,
                      entry_seconds, params_json, created_at
               FROM seams WHERE session_id = ? AND verdict = 'approved'
               ORDER BY created_at""", (session_id,)).fetchall()
    return [{"a_path": r[0], "b_path": r[1], "technique_id": r[2], "pair_label": r[3],
             "exit_seconds": r[4], "entry_seconds": r[5],
             "params": json.loads(r[6] or "{}"), "created_at": r[7]} for r in rows]


def rejections(session_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT a_path, b_path, technique_id, pair_label, reason
               FROM seams WHERE session_id = ? AND verdict = 'rejected'""",
            (session_id,)).fetchall()
    return [{"a_path": r[0], "b_path": r[1], "technique_id": r[2],
             "pair_label": r[3], "reason": r[4]} for r in rows]


# ------------------------------------------------- вкус, выученный счётом

def taste(session_id: str) -> dict:
    """Что диджей одобрял и что отвергал — в виде весов.

    Никаких моделей: доля одобрений у связки «техника + вид стыковки».
    Связка, отклонённая трижды и ни разу не одобренная, получает вес
    0.25 и практически перестаёт предлагаться; одобренная — 1.5."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT technique_id, pair_label, verdict, COUNT(*)
               FROM seams WHERE session_id = ?
               GROUP BY technique_id, pair_label, verdict""", (session_id,)).fetchall()
    agg: dict[tuple, list[int]] = {}
    for tid, lab, verdict, n in rows:
        key = (tid, lab)
        cur = agg.setdefault(key, [0, 0])
        cur[0 if verdict == "approved" else 1] += n
    out = {}
    for (tid, lab), (yes, no) in agg.items():
        # Сглаживание: одно «нет» не должно хоронить связку навсегда.
        out[f"{tid}|{lab}"] = round((yes + 1.0) / (yes + no + 2.0) * 2.0, 3)
    return out


def taste_weight(taste_map: dict, technique_id: str, pair_label: str | None) -> float:
    return float(taste_map.get(f"{technique_id}|{pair_label}", 1.0))


# ------------------------------------------------------------ тональность

def camelot_distance(a: str | None, b: str | None) -> int | None:
    """Расстояние по колесу Камелота между двумя тональностями.

    0 — та же тональность, 1 — соседний шаг по кругу квинт ИЛИ
    параллельный лад (8A↔8B), дальше — просто число шагов. None, если
    тональность неизвестна хотя бы у одного трека: тогда её нельзя ни
    поощрять, ни штрафовать.

    Правило гармонического сведения (harmonic mixing, оно же правило
    Камелота) как раз и говорит: соседние по колесу тональности звучат
    вместе, дальние — бьются. Это не вкус, а измеримая штука: у соседей
    по кругу квинт общих нот шесть из семи, через два шага — пять."""
    ka, kb = _camelot_parse(a), _camelot_parse(b)
    if ka is None or kb is None:
        return None
    na, la = ka
    nb, lb = kb
    step = min((na - nb) % 12, (nb - na) % 12)
    if la == lb:
        return step
    # Смена лада бесплатна только на месте: 8A↔8B — параллельные
    # мажор и минор, у них один и тот же набор нот. Со сдвигом по кругу
    # она стоит ещё один шаг.
    return 0 if step == 0 else step + 1


def _camelot_parse(v: str | None) -> tuple[int, str] | None:
    m = re.fullmatch(r"\s*(\d{1,2})\s*([ABab])\s*", str(v or ""))
    if not m:
        return None
    n = int(m.group(1))
    if not 1 <= n <= 12:
        return None
    return n, m.group(2).upper()


# Во что превращается расстояние. Диджей просил приоритет ±0/±1 — здесь
# он и стоит: свои и соседние тональности тянут пару вверх, дальние
# заметно вниз, но не запрещены совсем (в дропе бас перекрывает
# гармонию, и «неправильный» стык иногда работает).
KEY_WEIGHTS = {0: 1.30, 1: 1.22, 2: 1.00, 3: 0.88}
KEY_FAR = 0.72


def key_weight(a: dict, b: dict) -> float:
    d = camelot_distance(a.get("camelot"), b.get("camelot"))
    if d is None:
        return 1.0
    return KEY_WEIGHTS.get(d, KEY_FAR)


# --------------------------------------------------------- что предложить

# Насколько широко действует отказ. Это и была та самая поломка, из-за
# которой пара «не менялась постоянно»: отказ исключал ровно одну
# комбинацию «пара + техника + стык», и та же самая пара немедленно
# возвращалась со следующим приёмом в списке. Диджей жал «другие треки»,
# а видел те же треки — и был прав, что это не работает.
PAIR_WIDE = ("pair", "first", "second", "genre_a", "genre_b")


def _scopes(session_id: str) -> tuple[set, set, set]:
    """Что уже отвергнуто и НАСКОЛЬКО широко.

    Возвращает (мёртвые пары, мёртвые стыки пары, мёртвые комбинации):
      * отказ про треки хоронит ПАРУ целиком;
      * «другой стык» хоронит этот вид стыковки у этой пары;
      * «другая техника» хоронит только эту комбинацию.
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT a_path, b_path, technique_id, pair_label, verdict, reason
               FROM seams WHERE session_id = ?""", (session_id,)).fetchall()
    dead_pairs, dead_junctions, dead_combos = set(), set(), set()
    dead_techniques = set()
    for a, b, tid, lab, verdict, reason in rows:
        dead_combos.add((a, b, tid, lab))
        if verdict != "rejected":
            continue
        if reason in PAIR_WIDE:
            dead_pairs.add((a, b))
        elif reason == "junction":
            dead_junctions.add((a, b, lab))
        elif reason == "technique":
            # «Другая техника» — значит другая техника, а не тот же приём
            # на соседнем стыке. Раньше хоронилась комбинация «приём +
            # стык», и ST-08 возвращался трижды подряд, меняя только
            # подпись стыка: с точки зрения диджея кнопка не работала.
            dead_techniques.add((a, b, tid))
    return dead_pairs, dead_junctions, dead_combos, dead_techniques


def candidates(session_id: str, tracks: list[dict], a_path: str | None = None,
               b_path: str | None = None, limit: int = 6,
               avoid_a_genre: str | None = None,
               avoid_b_genre: str | None = None) -> list[dict]:
    """Швы, которые стоит послушать. Уже судимые не предлагаются повторно.

    a_path/b_path сужают перебор: так работают причины отказа. «Не та
    техника» оставляет обоих и меняет приём; «не тот второй трек»
    оставляет первый; «не та пара» не задаёт ничего."""
    import style
    from techniques import TECHNIQUES

    def genre_of(t: dict) -> str:
        g, sub = style.track_genre(t)
        return sub or g

    by_path = {t.get("path"): t for t in tracks if t.get("path")}
    dead_pairs, dead_junctions, dead_combos, dead_techniques = _scopes(session_id)
    tm = taste(session_id)
    approved_b = {s["b_path"] for s in pool(session_id)}

    # Продолжение цепочки. Диджей слушает переходы ПОДРЯД, начиная с
    # первого, поэтому следующий шов по умолчанию начинается там, где
    # кончился последний одобренный. Без этого одобренные швы ложились
    # случайным графом: даже когда диджей соглашался на ВСЁ, из двадцати
    # швов складывалась цепочка из четырёх треков, а не из двадцати.
    chain_used: set[str] = set()
    if a_path is None and b_path is None:
        built = build_set(session_id, tracks)
        if built.get("ok") and built.get("tracks"):
            chain_used = {t["path"] for t in built["tracks"]}
            a_path = built["tracks"][-1]["path"]

    # Шаг 1 — откуда берём пары. Фиксированные a_path/b_path сужают выбор;
    # полный перебор — только когда ни один трек не закреплён.
    pairs: list[tuple[dict, dict]] = []
    if a_path and b_path and a_path in by_path and b_path in by_path:
        pairs = [(by_path[a_path], by_path[b_path])]
    elif a_path and a_path in by_path:
        pairs = [(by_path[a_path], t) for p, t in by_path.items() if p != a_path]
    elif b_path and b_path in by_path:
        # «не тот первый трек»: входящий диджею нравится, меняем уходящий.
        pairs = [(t, by_path[b_path]) for p, t in by_path.items() if p != b_path]
    else:
        allt = list(by_path.values())
        for a in allt[:12]:
            for b in allt:
                if b["path"] != a["path"]:
                    pairs.append((a, b))

    # Шаг 2 — фильтры жанра применяются ПОСЛЕ выбора пар, не вместо него.
    if avoid_a_genre:
        pairs = [(a, b) for a, b in pairs if genre_of(a) != avoid_a_genre.lower()]
    if avoid_b_genre:
        pairs = [(a, b) for a, b in pairs if genre_of(b) != avoid_b_genre.lower()]

    out: list[dict] = []
    for a, b in pairs:
        # Трек, уже стоящий в пуле как входящий, второй раз не нужен —
        # иначе сет зациклится на нём.
        # Трек, уже стоящий в цепочке, вторым не предлагаем — иначе сет
        # закольцуется на нём.
        if b["path"] in approved_b or b["path"] in chain_used:
            continue
        # Пара, отвергнутая «по трекам», больше не предлагается вовсе —
        # ни с какой техникой и ни с каким стыком.
        if (a["path"], b["path"]) in dead_pairs:
            continue
        for opt in style.pair_options(a, b):
            vocal_ok = style.has_vocals(a) and style.has_vocals(b)
            if (a["path"], b["path"], opt["label"]) in dead_junctions:
                continue
            for k, tid in enumerate(opt["techniques"]):
                t = TECHNIQUES.get(tid)
                if t is None or t.requires_decks > 2:
                    continue
                if t.needs_vocals and not vocal_ok:
                    continue
                key = (a["path"], b["path"], tid, opt["label"])
                if key in dead_combos:
                    continue
                if (a["path"], b["path"], tid) in dead_techniques:
                    continue
                # Ничьи разрешаем устойчивым «шумом» от самой пары, а не
                # порядком перебора. Оценки у разных пар совпадают часто
                # (интересность стыка и вкус — одни и те же числа), и без
                # этого первая пара библиотеки выигрывала КАЖДЫЙ раз:
                # диджей видел «1. Bad Patterns → 10. MPH» бесконечно.
                # Шум детерминированный — один и тот же список при
                # перезагрузке страницы, но разный для разных пар.
                jitter = (hash((a["path"], b["path"], tid, opt["label"])) % 1000) / 12000.0
                w = (style.PAIR_INTEREST.get(opt["label"], 1.0)
                     * taste_weight(tm, tid, opt["label"])
                     * key_weight(a, b)
                     * (1.0 - 0.08 * k) + jitter)
                ga, sa = style.track_genre(a)
                gb, sb = style.track_genre(b)
                kd = camelot_distance(a.get("camelot"), b.get("camelot"))
                out.append({
                    "score": round(w, 3),
                    "a_path": a["path"], "a_name": a.get("name"),
                    "b_path": b["path"], "b_name": b.get("name"),
                    "technique_id": tid, "technique": t.name,
                    "pair_label": opt["label"],
                    "exit_seconds": opt["exit"]["at"], "entry_seconds": opt["entry"]["at"],
                    "bars": opt["bars"],
                    # Всё, что диджей хочет видеть на карточке, не открывая
                    # библиотеку: темпы, тональности и расстояние между
                    # ними, поджанры (по ним же работают кнопки «другой
                    # жанр») и готовы ли стемы — без них половина приёмов
                    # звучит не так, как задумано.
                    "a_bpm": a.get("bpm"), "b_bpm": b.get("bpm"),
                    "a_key": a.get("camelot") or a.get("key"),
                    "b_key": b.get("camelot") or b.get("key"),
                    "key_distance": kd,
                    "a_genre": sa or ga, "b_genre": sb or gb,
                    "a_stems": style.has_stems(a), "b_stems": style.has_stems(b),
                    "why": f"стыкуются {opt['label']}, {opt['bars']} тактов; {t.name}",
                })
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def next_seam(session_id: str, tracks: list[dict], **kw) -> dict | None:
    c = candidates(session_id, tracks, limit=1, **kw)
    return c[0] if c else None


# ------------------------------------------------------- сет из пула

def build_set(session_id: str, tracks: list[dict]) -> dict:
    """Собрать самую длинную цепочку из одобренных швов.

    Одобренные швы — это рёбра A->B. Сет это путь по ним без повторов
    треков. Ищем самый длинный жадно от каждого возможного начала: на 66
    треках это мгновенно, а точного решения (гамильтонов путь) здесь и
    не требуется — диджею нужна лучшая из возможных цепочек, а не
    доказанный оптимум."""
    seams = pool(session_id)
    if not seams:
        return {"ok": False, "error": "пул пуст — ни один шов ещё не одобрен"}
    edges: dict[str, list[dict]] = {}
    for s in seams:
        edges.setdefault(s["a_path"], []).append(s)
    names = {t.get("path"): t.get("name") for t in tracks}

    # Поиск с возвратом, а не жадный обход. Жадный брал первое
    # попавшееся ребро и умирал через два шага: на 20 одобренных швах,
    # покрывающих все 20 треков, он находил цепочку из ТРЁХ. Разница не
    # в качестве, а в самой задаче: жадность не умеет отступить, а здесь
    # почти всегда нужно.
    #
    # Точного решения (гамильтонов путь) не требуется: диджею нужна
    # лучшая из найденных цепочек, а не доказанный оптимум. Поэтому
    # обход ограничен бюджетом шагов — на реальных объёмах он до него не
    # доходит, но защищает от вырожденных случаев.
    budget = [200_000]
    best_path: list[dict] = []

    def dfs(cur: str, used: set[str], path: list[dict]) -> None:
        if len(path) > len(best_path):
            best_path[:] = path
        if budget[0] <= 0:
            return
        budget[0] -= 1
        for e in edges.get(cur, []):
            if e["b_path"] in used:
                continue
            used.add(e["b_path"])
            path.append(e)
            dfs(e["b_path"], used, path)
            path.pop()
            used.discard(e["b_path"])

    import sys as _sys

    limit = _sys.getrecursionlimit()
    _sys.setrecursionlimit(max(limit, len(seams) * 4 + 1000))
    try:
        for start in edges:
            dfs(start, {start}, [])
    finally:
        _sys.setrecursionlimit(limit)
    best = best_path
    if not best:
        return {"ok": False, "error": "из одобренных швов не складывается ни одной цепочки"}

    order = [best[0]["a_path"]] + [e["b_path"] for e in best]
    return {
        "ok": True,
        "tracks": [{"path": p, "name": names.get(p)} for p in order],
        "transitions": [{
            "a_name": names.get(e["a_path"]), "b_name": names.get(e["b_path"]),
            "technique_id": e["technique_id"], "pair_label": e["pair_label"],
            "exit_seconds": e["exit_seconds"], "entry_seconds": e["entry_seconds"],
            "bars": (e.get("params") or {}).get("bars"),
        } for e in best],
        # Готовые правки для plan_strategy: ключ — пара имён, а не номер
        # перехода. Номер сдвинется, если планировщик выкинет трек, и
        # приём диджея уедет на чужую пару; имена не сдвигаются.
        #
        # Без этих правок стратегия пересчитывала приём с нуля и половина
        # одобренных швов превращалась в DNB-25: подбор в стратегии
        # строже, чем в отборе (там свои пороги по темпу и тональности),
        # и стемовые приёмы у него не проходили. Диджей слушал и одобрял
        # одно, а в плане получал другое — то есть отбор не работал.
        "overrides": {
            f"{names.get(e['a_path'])}||{names.get(e['b_path'])}": {
                k: v for k, v in {
                    "technique_id": e["technique_id"],
                    "bars": (e.get("params") or {}).get("bars"),
                    "from_point_seconds": e["exit_seconds"],
                    "to_point_seconds": e["entry_seconds"],
                }.items() if v is not None
            } for e in best
        },
        "covered": len(order),
        "library": len(tracks),
        "complete": len(order) >= len([t for t in tracks if t.get("path")]),
    }


def progress(session_id: str, tracks: list[dict]) -> dict:
    """Сколько отсуждено и близок ли сет к готовности."""
    seams = pool(session_id)
    rej = rejections(session_id)
    covered = {s["a_path"] for s in seams} | {s["b_path"] for s in seams}
    total = len([t for t in tracks if t.get("path")])
    # «Все треки засветились в пуле» и «из пула складывается сет» — не
    # одно и то же: швы могут покрывать всю библиотеку и при этом не
    # выстраиваться в цепочку. Показываем и то, и другое, а готовность
    # считаем по ЦЕПОЧКЕ — иначе обещание «сет соберётся сам» окажется
    # неправдой ровно в тот момент, когда его проверят.
    chain = build_set(session_id, tracks) if seams else {"covered": 0}
    longest = int(chain.get("covered") or 0)
    return {
        "approved": len(seams), "rejected": len(rej),
        "tracks_in_pool": len(covered), "tracks_total": total,
        "longest_chain": longest,
        "share": round(len(covered) / total, 3) if total else 0.0,
        "ready": bool(total and longest >= total),
    }
