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
    # Стык — это ДВЕ точки, и не нравится обычно одна из них. Общая
    # кнопка «другой стык» меняла обе сразу, и подобрать нужное место
    # было нельзя: диджей жал её, чтобы сдвинуть уход, а вместе с ним
    # уезжал и вход, который его устраивал.
    "junction_a": "не та точка ухода — оставить треки, приём и вход, взять другую секцию у A",
    "junction_b": "не та точка входа — оставить треки, приём и уход, взять другую секцию у B",
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
    # Коды швов: состояние целиком по короткому числу (см. seam_code).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seam_codes (
            code INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (code, session_id)
        )"""
    )
    # Имя последнего отрендеренного демо этого шва. Молча, как и прочие
    # миграции в проекте: база живёт у диджея с первого дня, и ронять её
    # ради одной колонки незачем.
    try:
        conn.execute("ALTER TABLE seams ADD COLUMN demo_file TEXT")
    except sqlite3.OperationalError:
        pass                                    # колонка уже есть
    conn.commit()


@contextmanager
def _db():
    conn = persistence._connect()
    _init(conn)
    yield conn


# --------------------------------------------------------------- вердикты


# ------------------------------------------------------- код шва
#
# Диджей: «seed не показывает значение всех параметров — треки, стыки,
# техники, FX и прочее; он всегда должен показывать при генерации Roll».
#
# Прежний seed был номером РАСКЛАДА: он задавал порядок перебора и
# больше ничего. По нему нельзя было вернуться к конкретному переходу —
# только к той же последовательности предложений. А вернуться хочется
# именно к переходу: «вот эта пара, этим приёмом, с этих точек, на 32
# такта, с эхо-хвостом на уходе».
#
# Поэтому здесь не seed, а КОД ШВА: короткое число, посчитанное от всего
# состояния сразу. Одинаковое состояние — одинаковый код, всегда.
#
# Почему число, а не самодостаточная строка. Самодостаточный код должен
# нести в себе пути двух файлов — это сотни символов, такое не
# показывают в поле и не диктуют голосом. Число же надо где-то
# расшифровать, поэтому состояние кладётся в ту же базу комнаты. Цена —
# одна запись на показанный шов; выгода — код из девяти цифр, который
# можно продиктовать.

# Что входит в код. Порядок фиксирован: от него считается хэш, и
# переставить поля значит обнулить все выданные раньше коды.
CODE_FIELDS = ("a_path", "b_path", "technique_id", "pair_label",
               "exit_seconds", "entry_seconds", "bars",
               "fx_out", "fx_in", "fx_out_opts", "fx_in_opts",
               # Эффект самого приёма: юнит и его ручки. Без них код
               # описывал бы «тот же приём», а звучал бы он иначе —
               # DNB-25 в реверб и DNB-25 в эхо это разные швы.
               "fx_own_out", "fx_own_in",
               "mix_profile", "params")


def _code_payload(state: dict) -> str:
    """Каноническая запись состояния — то, от чего считается хэш.

    Секунды округляем до десятой: точка стыка 1:49.03 и 1:49.04 — это
    одна и та же точка, а разные коды у них означали бы, что код меняется
    сам по себе."""
    out = {}
    for k in CODE_FIELDS:
        v = state.get(k)
        if k in ("exit_seconds", "entry_seconds"):
            v = round(float(v or 0.0), 1)
        elif k == "bars":
            v = int(round(float(v or 0)))
        out[k] = v
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def seam_code(session_id: str, state: dict) -> int:
    """Код состояния. Считает, запоминает и возвращает число."""
    import hashlib

    payload = _code_payload(state)
    # 30 бит: девять цифр в десятичной записи — предел того, что человек
    # перепишет с экрана без ошибки. Совпадение двух РАЗНЫХ состояний в
    # одной комнате при таком размере — событие на миллиарды швов, а
    # цена его мала: вернётся соседний переход, а не поломка.
    code = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:4],
                          "big") % 1_000_000_000
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO seam_codes (code, session_id, state_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (code, session_id, payload, time.time()))
        conn.commit()
    return code


def seam_by_code(session_id: str, code) -> dict | None:
    """Состояние по коду. None — такого кода в этой комнате не видели."""
    try:
        code = int(str(code).strip())
    except (TypeError, ValueError):
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT state_json FROM seam_codes WHERE code = ? AND session_id = ?",
            (code, session_id)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


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
                      entry_seconds, params_json, created_at, id, demo_file
               FROM seams WHERE session_id = ? AND verdict = 'approved'
               ORDER BY created_at""", (session_id,)).fetchall()
    return [{"a_path": r[0], "b_path": r[1], "technique_id": r[2], "pair_label": r[3],
             "exit_seconds": r[4], "entry_seconds": r[5],
             "params": json.loads(r[6] or "{}"), "created_at": r[7],
             "id": r[8], "demo_file": r[9]} for r in rows]


def set_demo(session_id: str, seam_id: int, demo_file: str | None) -> None:
    """Запомнить, каким файлом шов звучит сейчас.

    Демо не кэшируются по построению: «послушать» каждый раз собирает
    план заново, и после правки логики сведения пул звучит по-новому сам.
    Здесь хранится результат ПАКЕТНОЙ пересборки — чтобы полсотни швов
    не пришлось ждать по одному, и чтобы перезагрузка вкладки не
    выбрасывала уже посчитанное."""
    with _db() as conn:
        conn.execute("UPDATE seams SET demo_file = ? WHERE id = ? AND session_id = ?",
                     (demo_file, int(seam_id), session_id))
        conn.commit()


def drop(session_id: str, seam_id: int) -> dict:
    """Убрать один шов из пула.

    Именно УДАЛИТЬ, а не переписать в отказ: отказ учится на причине, а
    здесь причины нет — диджей просто передумал. Записать это отказом
    значило бы испортить вкус выдуманной причиной."""
    with _db() as conn:
        cur = conn.execute("DELETE FROM seams WHERE id = ? AND session_id = ?",
                           (int(seam_id), session_id))
        conn.commit()
    return {"ok": cur.rowcount > 0}


def clear(session_id: str, what: str = "approved") -> dict:
    """Очистить пул целиком.

    what: approved — только одобренные (пул), rejected — только отказы
    (вместе с ними уходит и выученный вкус), all — и то, и другое."""
    where = {"approved": "AND verdict = 'approved'",
             "rejected": "AND verdict = 'rejected'",
             "all": ""}.get(str(what), "AND verdict = 'approved'")
    with _db() as conn:
        cur = conn.execute(f"DELETE FROM seams WHERE session_id = ? {where}",
                           (session_id,))
        conn.commit()
    return {"ok": True, "removed": cur.rowcount, "what": what}


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

    Возвращает (мёртвые пары, мёртвые стыки пары, мёртвые комбинации,
    мёртвые приёмы, мёртвые точки ухода, мёртвые точки входа):
      * отказ про треки хоронит ПАРУ целиком;
      * «другой стык» хоронит этот вид стыковки у этой пары;
      * «другой стык A/B» хоронит одну КОНКРЕТНУЮ точку, а не вид;
      * «другая техника» хоронит только эту комбинацию.
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT a_path, b_path, technique_id, pair_label, verdict, reason,
                      exit_seconds, entry_seconds
               FROM seams WHERE session_id = ?""", (session_id,)).fetchall()
    dead_pairs, dead_junctions, dead_combos = set(), set(), set()
    dead_techniques = set()
    dead_exits, dead_entries = set(), set()
    for a, b, tid, lab, verdict, reason, ex, en in rows:
        dead_combos.add((a, b, tid, lab))
        if verdict != "rejected":
            continue
        if reason in PAIR_WIDE:
            dead_pairs.add((a, b))
        elif reason == "junction":
            dead_junctions.add((a, b, lab))
        elif reason == "junction_a" and ex is not None:
            dead_exits.add((a, b, round(float(ex), 1)))
        elif reason == "junction_b" and en is not None:
            dead_entries.add((a, b, round(float(en), 1)))
        elif reason == "technique":
            # «Другая техника» — значит другая техника, а не тот же приём
            # на соседнем стыке. Раньше хоронилась комбинация «приём +
            # стык», и ST-08 возвращался трижды подряд, меняя только
            # подпись стыка: с точки зрения диджея кнопка не работала.
            dead_techniques.add((a, b, tid))
    return (dead_pairs, dead_junctions, dead_combos, dead_techniques,
            dead_exits, dead_entries)


def candidates(session_id: str, tracks: list[dict], a_path: str | None = None,
               b_path: str | None = None, limit: int = 6,
               avoid_a_genre: str | None = None,
               avoid_b_genre: str | None = None,
               hold_exit: float | None = None,
               hold_entry: float | None = None,
               hold_technique: str | None = None,
               include_judged: bool = False) -> list[dict]:
    """Швы, которые стоит послушать. Уже судимые не предлагаются повторно.

    a_path/b_path сужают перебор: так работают причины отказа. «Не та
    техника» оставляет обоих и меняет приём; «не тот второй трек»
    оставляет первый; «не та пара» не задаёт ничего.

    include_judged=True снимает ВСЕ фильтры «это уже судили»: и отказы, и
    пул, и цепочку. Нужно ровно для одного случая — вернуть в отбор шов,
    который УЖЕ лежит в пуле, чтобы его поправить. Такой шов по
    построению одобрен, а значит и его пара в approved_b, и сама
    комбинация в dead_combos: обычным перебором он не вернётся никогда.
    Отсюда и была ошибка «не смог собрать шов для этой пары»."""
    import style
    import mix_profile as _mp
    from techniques import TECHNIQUES

    def genre_of(t: dict) -> str:
        g, sub = style.track_genre(t)
        return sub or g

    by_path = {t.get("path"): t for t in tracks if t.get("path")}
    (dead_pairs, dead_junctions, dead_combos, dead_techniques,
     dead_exits, dead_entries) = _scopes(session_id)
    tm = taste(session_id)
    approved_b = set() if include_judged else {s["b_path"] for s in pool(session_id)}
    if include_judged:
        dead_pairs = dead_junctions = dead_combos = set()
        dead_techniques = dead_exits = dead_entries = set()

    # Продолжение цепочки. Диджей слушает переходы ПОДРЯД, начиная с
    # первого, поэтому следующий шов по умолчанию начинается там, где
    # кончился последний одобренный. Без этого одобренные швы ложились
    # случайным графом: даже когда диджей соглашался на ВСЁ, из двадцати
    # швов складывалась цепочка из четырёх треков, а не из двадцати.
    chain_used: set[str] = set()
    if a_path is None and b_path is None and not include_judged:
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
            # Точки, которые диджей уже отверг по отдельности («другой
            # стык A» / «другой стык B»), и точки, которые он наоборот
            # просил оставить.
            ex_at = round(float(opt["exit"]["at"]), 1)
            en_at = round(float(opt["entry"]["at"]), 1)
            if (a["path"], b["path"], ex_at) in dead_exits:
                continue
            if (a["path"], b["path"], en_at) in dead_entries:
                continue
            if hold_exit is not None and abs(opt["exit"]["at"] - hold_exit) > 0.5:
                continue
            if hold_entry is not None and abs(opt["entry"]["at"] - hold_entry) > 0.5:
                continue
            for k, tid in enumerate(opt["techniques"]):
                if hold_technique and tid != hold_technique:
                    continue
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
                # Длинный переход лучше короткого — по прямой просьбе
                # диджея. Множитель мягкий: 64 такта дают +50% к весу,
                # 8 тактов — ничего, но стык «дроп в дроп» на 8 тактах
                # всё равно обгонит «тело в тело» на 32.
                long_bonus = 1.0 + min(float(opt["bars"]), 64.0) / 128.0
                ga, sa = style.track_genre(a)
                gb, sb = style.track_genre(b)
                kd = camelot_distance(a.get("camelot"), b.get("camelot"))
                stems_ok = style.has_stems(a) and style.has_stems(b)
                # Темп у пары разный, и сводим мы его скоростью, а не
                # растяжением — значит вместе с темпом уедет и высота.
                # Считается один раз на пару, не на каждый приём.
                detune = _pair_detune(a, b)
                # Раскладка слоёв: кто с кем спорит в низу и в верху.
                # Считается тоже один раз на пару — признаки лежат в
                # meta.json рядом со слоями и от приёма не зависят.
                lay = pair_layers(a, b)
                lay_pair, lay_note = layer_weight(lay)
                # Тональность влияет не только на выбор ПАРЫ (key_weight),
                # но и на выбор ПРИЁМА: при разрыве длинное наложение
                # тональных частей обнажает его, а рез или обмен низом —
                # нет. Стемы смягчают: конфликтующий слой можно снять.
                key_mult, key_why = style.key_technique_bias(tid, kd, stems_ok)
                # Вокал с обеих сторон — отдельный конфликт, не сводимый
                # к тональности: два голоса спорят даже в одной
                # тональности, а приёмы против этого свои.
                voc_mult, voc_why = style.vocal_technique_bias(
                    tid, bool(opt["exit"].get("vocal")),
                    bool(opt["entry"].get("vocal")), stems_ok)
                # Подгонка темпа двигает высоту: метки Камелота этого не
                # видят, а уши слышат (см. style.detune_technique_bias).
                det_mult, det_why = style.detune_technique_bias(
                    tid, detune["cents"], stems_ok)
                # Конфликты ПО СЛОЯМ: два непрерывных баса в одной
                # полосе, две плотные гармонии одной яркости. Тональность
                # их не видит — она про ноты, а это про плотность.
                lay_mult, lay_why = style.layer_technique_bias(
                    tid, lay, stems_ok)
                # Где стоят точки по хронометражу: уход ближе к концу
                # уходящего, вход ближе к началу входящего.
                point_mult, point_why = style.point_bias(
                    opt["exit"]["at"], a.get("duration_seconds"),
                    opt["entry"]["at"], b.get("duration_seconds"))
                w = (style.PAIR_INTEREST.get(opt["label"], 1.0)
                     * taste_weight(tm, tid, opt["label"])
                     * key_weight(a, b)
                     * long_bonus
                     * key_mult
                     * voc_mult
                     * det_mult
                     * lay_mult
                     * lay_pair
                     * point_mult
                     * (1.0 - 0.08 * k) + jitter)
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
                    # Почему этот приём и эти точки: диджей должен видеть
                    # не только «что», но и «на каком основании».
                    "key_note": key_why, "point_note": point_why,
                    "vocal_note": voc_why,
                    # Почему слои спорят или дополняют друг друга. Пусто,
                    # если слоёв нет у обоих: молчание честнее нуля.
                    "layer_note": lay_why or lay_note,
                    "layers": lay,
                    # Форма ведения фейдеров едет ВМЕСТЕ со швом, а не
                    # живёт настройкой экрана: иначе подобранное на одной
                    # паре молча уезжает на следующую (см. mix_profile.auto).
                    "mix_profile": _mp.auto(opt["bars"], opt["label"], lay, tid),
                    "bars_options": bars_options(a, b, opt["bars"], tid),
                    # Рекомендованные стыки СТИЛЯ — всегда, а не только когда
                    # угадали. Диджей: «нужно всегда писать рекомендуемые стыки,
                    # как например Uplifting trance смешивается в брейкдаунах».
                    "genre_junctions": style.preferred_junctions(b),
                    "genre_junction_note": style.genre_junction_bias(b, opt["label"])[1],
                    "detune_cents": detune["cents"],
                    "detune_note": det_why or detune["note"],
                    "a_vocal_section": bool(opt["exit"].get("vocal")),
                    "b_vocal_section": bool(opt["entry"].get("vocal")),
                    "why": f"стыкуются {opt['label']}, {opt['bars']} тактов; {t.name}",
                })
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ------------------------------------------------ совместимость по слоям

_LAYER_CACHE: dict[tuple, dict | None] = {}


def pair_layers(a: dict, b: dict) -> dict | None:
    """Чем пара спорит по слоям. None — слои посчитаны не у обоих.

    Кэш по путям, а не по содержимому: подбор перебирает сотни пар и
    спрашивает про каждую по нескольку раз (на каждый приём и на каждый
    стык), а чтение признаков — это обращение к meta.json. Кэш живёт до
    конца процесса сознательно: признаки меняются только при пересчёте
    слоёв, а он всегда идёт отдельным шагом и заканчивается перезагрузкой
    библиотеки."""
    import stem_features as _sf

    key = (a.get("path"), b.get("path"))
    if key in _LAYER_CACHE:
        return _LAYER_CACHE[key]
    out = _sf.gap(_sf.have(a), _sf.have(b))
    _LAYER_CACHE[key] = out
    return out


def invalidate_layers() -> None:
    """Забыть разметку по слоям — после их пересчёта."""
    _LAYER_CACHE.clear()


def layer_weight(gap: dict | None) -> tuple[float, str]:
    """Множитель ПАРЕ (не приёму) за раскладку слоёв.

    Отличие от layer_technique_bias в том, что здесь решается, стоит ли
    вообще предлагать эту пару, а там — каким приёмом её брать. Обе
    поправки нужны: пара с двумя непрерывными басами играбельна — обменом
    низа, — но при прочих равных пара, где бас одного сидит под басом
    другого, играбельна ЛЮБЫМ приёмом, и её честнее показать выше.

    Множитель нарочно узкий (0,85..1,15). Раскладка слоёв — это условие,
    а не приговор: пара с редким и точным стыком должна обгонять пару с
    удобными слоями и скучным стыком, а не наоборот."""
    import stem_features as _sf

    if not gap:
        return 1.0, ""
    mult = 1.0
    if gap["bass"] >= _sf.BASS_CLASH:
        mult *= 0.90
    elif gap["bass"] <= 0.30:
        mult *= 1.08
    if gap["harm"] >= _sf.HARM_CLASH:
        mult *= 0.93
    if gap["drum_fit"] >= _sf.DRUM_APART:
        mult *= 1.06
    return max(0.85, min(1.15, mult)), _sf.note(gap)


# ------------------------------------------------- ручной выбор приёма

_DETUNE_CACHE: dict[tuple, dict] = {}


def _pair_detune(a: dict, b: dict, master_bpm: float | None = None) -> dict:
    """Расхождение по высоте между деками после приведения к общему темпу.

    Мастер-темп по умолчанию — СРЕДНЕЕ геометрическое двух: так каждый
    трек уходит от своего оригинала вдвое меньше, чем если тянуть один к
    другому. На расхождение МЕЖДУ деками выбор не влияет вовсе (оно равно
    разнице темпов), но на то, насколько каждый трек звучит непохоже на
    себя, — влияет, и среднее здесь честнее."""
    import math

    import tempo

    ba, bb = float(a.get("bpm") or 0.0), float(b.get("bpm") or 0.0)
    if ba <= 0 or bb <= 0:
        return {"cents": 0.0, "verdict": "unknown", "note": "",
                "semitones_a": 0.0, "semitones_b": 0.0}
    master = master_bpm or math.sqrt(ba * tempo.nearest_octave(bb, ba))
    key = (round(ba, 2), round(bb, 2), round(float(master), 2))
    hit = _DETUNE_CACHE.get(key)
    if hit is None:
        hit = tempo.detune_between(ba, bb, master)
        _DETUNE_CACHE[key] = hit
    return hit


def _best_for(technique_id: str) -> list[str]:
    """На каких стыках этот приём вообще рекомендован.

    Считается из той же таблицы PAIR_TECHNIQUES, по которой работает
    подбор, — а не пишется руками рядом с каждой техникой. Иначе список
    «где приём хорош» разъедется с тем, где его реально предлагают, и
    подсказка станет враньём."""
    import style

    return [lab for lab, ids in style.PAIR_TECHNIQUES.items()
            if technique_id in ids]


# ------------------------------------------ пригодность по полям каталога
#
# У каждого приёма в `techniques.py` записаны три требования: порог по
# темпу (`bpm_delta_max`, В ПРОЦЕНТАХ), требование к тональности
# (`key_rule`) и направление энергии (`energy_direction`). До сих пор их
# читал ТОЛЬКО живой стратег (`mix_strategist.score_pair_for_technique`),
# а подбор приёма в Clipping про них не знал вовсе: он взвешивал место в
# таблице стыка, вкус диджея, тональность и слои — но не темп и не
# энергию. Получалось два разных мнения об одной паре, и одно из них
# молча игнорировало то, что записано в самом каталоге.
#
# Тональность здесь НЕ считаем намеренно: её уже считает
# `style.key_technique_bias`, и второй множитель по тому же основанию
# просто возвёл бы её в квадрат.

# Во сколько раз можно превысить порог темпа, прежде чем приём считается
# неиграбельным. Замер по библиотеке диджея (140 треков, 6000 случайных
# пар): жёсткий порог отсекает 73% сочетаний и оставляет 19 приёмов из
# 62 на средней паре — слишком строго для СПИСКА, который человек листает
# руками. Двойной порог отсекает 54% и оставляет 38. Настоящие пары идут
# через `nearest_b`, то есть по темпу гораздо ближе случайных, — на них
# отсекается заметно меньше.
BPM_HARD_STOP = 2.0

# Тональность: с какого расстояния по Камелоту приём, которому нужна
# совместимая тональность, считается неиграбельным. style.camelot: 0 —
# та же, 1 — соседняя или параллельный лад, 2 — уже конфликт. Отказ
# ставим на 3: две ступени можно вытянуть эквалайзером, три — нет.
# Со стемами дальше: конфликтующий слой можно просто не накладывать.
KEY_HARD_STOP = 3
KEY_HARD_STOP_STEMS = 5

_ENERGY_BY_LABEL = {
    "breakdown→drop": "up", "breakdown→groove": "up", "groove→drop": "up",
    "build→drop": "up", "intro→groove": "up",
    "drop→breakdown": "down", "groove→breakdown": "down",
    "drop→outro": "down", "outro→intro": "down",
}


def catalog_fit(tid: str, a: dict, b: dict,
                label: str | None = None) -> tuple[float, str, str]:
    """Множитель, пояснение и стоп-причина по полям каталога.

    Стоп-причина непустая — приём на эту пару не ставится вовсе, и её
    показывают человеку словами, а не прячут. Диджей просил именно так:

        если тональности сильно различаются, нужно, чтобы на выбор
        давались только те техники, что не требуют одной тональности,
        так же можно сказать и про BPM и про энергию

    То есть по всем трём осям — отказ, а не тихое понижение веса."""
    import style
    from techniques import TECHNIQUES

    t = TECHNIQUES.get(tid)
    if t is None:
        return 1.0, "", ""
    mult, why = 1.0, []

    # --- темп ---
    try:
        bpm_a = float(a.get("bpm") or 0.0)
        bpm_b = float(b.get("bpm") or 0.0)
    except (TypeError, ValueError):
        bpm_a = bpm_b = 0.0
    # Приём, СДЕЛАННЫЙ ради разрыва, этим же разрывом не отсекается.
    # Без этой оговорки Tempo Ramp выбывал по порогу темпа, то есть
    # ровно по той причине, ради которой существует, — а диджей
    # оставался с отказом и без выхода.
    solves = set(getattr(t, "solves", ()) or ())
    if t.bpm_delta_max is not None and bpm_a > 20 and bpm_b > 20 \
            and "tempo_gap" not in solves:
        pct = abs(bpm_a - bpm_b) / max(bpm_a, bpm_b) * 100.0
        thr = float(t.bpm_delta_max)
        if pct > thr * BPM_HARD_STOP:
            return 0.0, "", (f"разница темпа {pct:.0f}% — вдвое больше "
                             f"порога приёма ({thr:.0f}%)")
        if pct > thr:
            # Между порогом и двойным порогом — не отказ, а штраф: приём
            # поедет, но растяжка будет слышна.
            over = (pct - thr) / max(thr * (BPM_HARD_STOP - 1.0), 1e-6)
            mult *= 1.0 - 0.5 * min(1.0, over)
            why.append(f"темп расходится на {pct:.0f}% при пороге {thr:.0f}%")
        else:
            mult *= 1.1
            why.append(f"темп сходится ({pct:.0f}% при пороге {thr:.0f}%)")

    # --- тональность ---
    #
    # `key_rule == "compatible"` — приём ДЕРЖИТ два тональных слоя
    # вместе. На далёких тональностях это не «похуже», а разъезд, и
    # предлагать такой приём незачем. Стемы смягчают: конфликтующий слой
    # можно просто не накладывать, поэтому порог отказа с ними дальше.
    #
    # `key_rule == "clash"` — приём сделан РАДИ столкновения. На
    # сошедшихся тональностях он теряет смысл, но не ломается, поэтому
    # здесь штраф, а не отказ.
    dist = camelot_distance(a.get("camelot"), b.get("camelot"))
    if dist is not None and "key_gap" not in solves:
        stems_ok = style.has_stems(a) and style.has_stems(b)
        stop_at = KEY_HARD_STOP_STEMS if stems_ok else KEY_HARD_STOP
        if t.key_rule == "compatible":
            if dist >= stop_at:
                return 0.0, "", (f"тональности далеко по Камелоту "
                                 f"(расстояние {dist}) — приём держит их вместе"
                                 + ("" if stems_ok else "; со стемами было бы можно"))
            if dist >= style.KEY_CONFLICT_DISTANCE:
                mult *= 0.8
                why.append(f"тональности расходятся (расстояние {dist} по Камелоту)")
        elif t.key_rule == "clash" and dist < style.KEY_CONFLICT_DISTANCE:
            mult *= 0.7
            why.append("приём сделан ради столкновения тональностей, а они сошлись")

    # --- энергия ---
    #
    # Отказ только на ПРЯМОМ противоречии: приём тянет вверх, а стык
    # ведёт вниз. «Всё равно» с обеих сторон — не возражение.
    want = t.energy_direction
    if want in ("up", "down"):
        got = _ENERGY_BY_LABEL.get(label or "")
        if got == want:
            mult *= 1.15
            why.append("стык ведёт энергию туда же, куда приём")
        elif got and got != want:
            return 0.0, "", ("приём ведёт энергию "
                             + ("вверх" if want == "up" else "вниз")
                             + ", а стык — "
                             + ("вверх" if got == "up" else "вниз"))

    # И наоборот: там, где разрыв ЕСТЬ, решатель поднимается. Иначе он
    # лежал бы в каталоге мёртвым грузом — формально применимый, но
    # никогда не первый.
    if "key_gap" in solves and dist is not None and dist >= KEY_HARD_STOP:
        mult *= 1.6
        why.append("приём сделан ради разрыва тональности")
    if "tempo_gap" in solves and bpm_a > 20 and bpm_b > 20:
        import tempo as _tempo

        rel = _tempo.relate(bpm_a, bpm_b)
        if not rel.get("compatible"):
            mult *= 1.6
            why.append("приём сделан ради разрыва темпа")

    return mult, "; ".join(why), ""


def techniques_for(a: dict, b: dict, label: str | None = None) -> list[dict]:
    """Все приёмы, которые можно поставить на этот шов, — для ручного
    выбора.

    Диджей просил менять ТОЛЬКО приём, не трогая треки и точки, и
    выбирать его руками. Значит нужен полный список, а не «следующий по
    внутреннему рейтингу»: с пометкой, что рекомендовано именно на этом
    стыке, что применимо вообще, а что не поедет — и почему."""
    import mix_profile as _mp
    import style
    from techniques import TECHNIQUES

    # Рез или сведение — та самая ось, по которой делятся «входит сразу
    # на полном» и «входит под профилем фейдеров». Берём из mix_profile,
    # где она и живёт, а не заводим второй список.
    _CUT_IDS = _mp.CUT_TECHNIQUES
    recommended = list(style.PAIR_TECHNIQUES.get(label or "", ()))
    stems_ok = style.has_stems(a) and style.has_stems(b)
    vocals_ok = style.has_vocals(a) and style.has_vocals(b)
    out: list[dict] = []
    for tid, t in TECHNIQUES.items():
        problem = ""
        if t.requires_decks > 2:
            problem = f"нужно {t.requires_decks} деки"
        elif t.requires_stems and not stems_ok:
            problem = "нужны стемы у обоих треков"
        elif t.needs_vocals and not vocals_ok:
            problem = "нужен вокальный слой у обоих треков"
        # Поля каталога: темп и энергия. Раньше их читал только живой
        # стратег, а здесь приём предлагался так, будто требований нет.
        fit_mult, fit_why, fit_stop = catalog_fit(tid, a, b, label)
        if not problem and fit_stop:
            problem = fit_stop
        rank = recommended.index(tid) if tid in recommended else 99
        out.append({
            "id": tid, "name": t.name, "category": t.category,
            "difficulty": t.difficulty, "description": t.description,
            "recommended": tid in recommended,
            "rank": rank,
            "best_for": _best_for(tid),
            "requires_stems": t.requires_stems,
            "needs_vocals": t.needs_vocals,
            # Оси, по которым диджей просил различать приёмы. Отдаём их
            # наружу, а не держим в голове у подбора: по ним фильтруют в
            # интерфейсе и по ним же объясняют выбор.
            "cut": tid in _CUT_IDS,
            "energy": t.energy_direction,
            "key_rule": t.key_rule,
            "bpm_delta_max": t.bpm_delta_max,
            "decks": t.requires_decks,
            "fit": round(fit_mult, 3),
            "fit_why": fit_why,
            "ok": not problem, "problem": problem,
        })
    # Порядок — ПО ТИПУ приёма, а не по названию: диджей ищет «тот, что
    # по слоям», а не «тот, что на букву П». Внутри типа — по номеру.
    # Рекомендованные для этого стыка выносим отдельной группой сверху,
    # иначе они разбредаются по списку и подсказка пропадает.
    def _key(x: dict) -> tuple:
        pref, _, num = str(x["id"]).partition("-")
        try:
            n = int(num)
        except ValueError:
            n = 999
        return (not x["ok"], pref, n)

    out.sort(key=_key)
    return out


# --------------------------------------------------- «повезёт» и замена B

def combo_key(seam: dict) -> str:
    """Отпечаток КОМБИНАЦИИ: приём + стык + место ухода.

    «Повезёт» обязано каждый раз давать другое, иначе кнопка мертва:
    перебор детерминированный, лучшая комбинация всегда одна и та же, и
    нажатие вторым разом возвращало ту же карточку. Ключ считается
    одинаково на сервере и в браузере, поэтому браузер копит список уже
    показанного и присылает его сюда."""
    return "|".join((
        str(seam.get("technique_id") or ""),
        str(seam.get("pair_label") or ""),
        str(round(float(seam.get("exit_seconds") or 0.0), 1)),
    ))


def seed_shuffle(items: list[dict], seed: int | None) -> list[dict]:
    """Перетасовать варианты воспроизводимо.

    Без seed перебор детерминированный: лучшая комбинация одна, и
    «повезёт» со второго нажатия возвращало то же самое — оттого и
    список показанного, которым это обходили.

    Seed — не «немного случайности», а НОМЕР РАСКЛАДА. Одно и то же
    число всегда даёт один и тот же порядок, поэтому находку можно
    передать словами: «seed 4127, вторая пара» — и человек услышит ровно
    то же. Порядок при этом не ломается в кашу: сортировка по весу
    остаётся, seed лишь тасует варианты ВНУТРИ близкого веса, иначе
    кнопка начала бы предлагать заведомо плохое.
    """
    if seed is None or not items:
        return items
    import random

    rnd = random.Random(int(seed))
    out: list[dict] = []
    bucket: list[dict] = []
    cur: float | None = None
    for x in items:
        w = round(float(x.get("score") or 0.0), 2)
        if cur is None or abs(w - cur) < 1e-9:
            cur = w
            bucket.append(x)
        else:
            rnd.shuffle(bucket)
            out.extend(bucket)
            bucket, cur = [x], w
    rnd.shuffle(bucket)
    out.extend(bucket)
    return out


def lucky(session_id: str, tracks: list[dict], a_path: str, b_path: str,
          limit: int = 1, exclude: list[str] | None = None,
          seed: int | None = None) -> list[dict]:
    """Перебрать ВСЁ для этой пары и отдать лучшее.

    Обычный подбор бережёт вкус диджея: он не предлагает то, что уже
    отвергнуто, и держит связки, которые тот одобрял. Здесь наоборот —
    полный перебор всех стыковок на все подходящие приёмы. У пары обычно
    5-15 содержательных стыковок и по 4-6 приёмов на каждую, то есть
    полсотни-сотня комбинаций; из них и выбирается лучшая по той же
    оценке, что и всегда.

    Мёртвые комбинации (уже отвергнутые) при этом НЕ учитываются: смысл
    кнопки в том, чтобы дать паре ещё один шанс, а не повторить отказ."""
    import style
    from techniques import TECHNIQUES

    by_path = {t.get("path"): t for t in tracks if t.get("path")}
    a, b = by_path.get(a_path), by_path.get(b_path)
    if a is None or b is None:
        return []
    tm = taste(session_id)
    vocal_ok = style.has_vocals(a) and style.has_vocals(b)
    stems_ok = style.has_stems(a) and style.has_stems(b)
    out: list[dict] = []
    for opt in style.pair_options(a, b):
        for k, tid in enumerate(opt["techniques"]):
            t = TECHNIQUES.get(tid)
            if t is None or t.requires_decks > 2:
                continue
            if t.needs_vocals and not vocal_ok:
                continue
            if t.requires_stems and not stems_ok:
                continue
            long_bonus = 1.0 + min(float(opt["bars"]), 64.0) / 128.0
            voc_mult, _ = style.vocal_technique_bias(
                tid, bool(opt["exit"].get("vocal")),
                bool(opt["entry"].get("vocal")), stems_ok)
            w = (style.PAIR_INTEREST.get(opt["label"], 1.0)
                 * taste_weight(tm, tid, opt["label"])
                 * key_weight(a, b) * long_bonus * voc_mult * (1.0 - 0.05 * k))
            out.append(_seam_dict(a, b, t, tid, opt, round(w, 3)))
    # При равном весе (а он совпадает часто — числа одни и те же) берём
    # тот вариант, где переход длиннее и где уходящий успел отыграть
    # больше: иначе «повезёт» выдавало уход на 26-й секунде трека.
    out.sort(key=lambda x: (-x["score"], -float(x["bars"] or 0),
                            -float(x["exit_seconds"] or 0)))
    out = seed_shuffle(out, seed)
    for x in out:
        x["seed"] = seed
    # Уже показанное уходит в конец, а не выбрасывается: когда перебор
    # исчерпан, кнопка должна начать круг заново, а не ответить «нет
    # содержательных стыковок» на паре, где их полсотни. А когда круг
    # исчерпан ЦЕЛИКОМ, список прокручивается за последнюю показанную
    # комбинацию — иначе кнопка навсегда застревала бы на лучшей.
    order = list(exclude or ())
    seen = set(order)
    if seen:
        fresh = [x for x in out if combo_key(x) not in seen]
        if fresh:
            out = fresh + [x for x in out if combo_key(x) in seen]
        elif order:
            keys = [combo_key(x) for x in out]
            last = next((k for k in reversed(order) if k in keys), None)
            if last is not None:
                i = keys.index(last) + 1
                out = out[i:] + out[:i]
    return out[:max(1, limit)]


def other_junction(session_id: str, tracks: list[dict], seam: dict,
                   exclude: list[str] | None = None) -> list[dict]:
    """Те же треки и ТОТ ЖЕ приём — другие точки стыка.

    Отдельно от lucky, и разница принципиальная. lucky перебирает ещё и
    приёмы, то есть карточка после него другая целиком. А диджею часто
    нужно обратное: приём он уже выбрал и слышит, что тот годится, —
    не нравится, ГДЕ он случается. Раньше эта кнопка записывала отказ и
    приносила нового кандидата, у которого мог смениться и приём, и
    пара; спрашивали одно, получали другое.

    Порядок: сначала стыковки, на которых приём рекомендован таблицей
    PAIR_TECHNIQUES, потом все остальные. Вторые честно помечаются
    экспериментальными: приём на них сыграет, но задуман он не для них.
    """
    import style
    from techniques import TECHNIQUES

    by_path = {t.get("path"): t for t in tracks if t.get("path")}
    a = by_path.get(seam.get("a_path"))
    b = by_path.get(seam.get("b_path"))
    tid = str(seam.get("technique_id") or "")
    t = TECHNIQUES.get(tid)
    if a is None or b is None or t is None:
        return []

    best = set(_best_for(tid))
    tm = taste(session_id)
    out: list[dict] = []
    for opt in style.pair_options(a, b):
        recommended = opt["label"] in best
        long_bonus = 1.0 + min(float(opt["bars"]), 64.0) / 128.0
        # Жанр ВХОДЯЩЕГО говорит, какой стык для него характерен: транс
        # сводят в брейкдаунах, техно — тело в тело. Одна таблица на все
        # стили одинаково охотно предлагала трансу groove→groove.
        gj, _gwhy = style.genre_junction_bias(b, opt["label"])
        w = (style.PAIR_INTEREST.get(opt["label"], 1.0)
             * taste_weight(tm, tid, opt["label"])
             * key_weight(a, b) * long_bonus * gj)
        item = _seam_dict(a, b, t, tid, opt, round(w, 3))
        item["junction_kind"] = "recommended" if recommended else "experimental"
        item["why"] = (f"стыкуются {opt['label']}, {opt['bars']} тактов; {t.name}"
                       + ("" if recommended else " — приём задуман не для этого стыка"))
        out.append(item)

    # Рекомендованные первыми, внутри группы — по весу, потом длиннее и
    # позже по уходящему (та же развязка, что и в lucky).
    out.sort(key=lambda x: (0 if x["junction_kind"] == "recommended" else 1,
                            -x["score"], -float(x["bars"] or 0),
                            -float(x["exit_seconds"] or 0)))

    # Показанное уходит в конец, а не выбрасывается: кнопка обязана
    # крутиться по кругу, а не упираться в «больше нечего».
    order = list(exclude or ())
    seen = set(order)
    if seen:
        fresh = [x for x in out if combo_key(x) not in seen]
        if fresh:
            out = fresh + [x for x in out if combo_key(x) in seen]
        elif order:
            keys = [combo_key(x) for x in out]
            last = next((k for k in reversed(order) if k in keys), None)
            if last is not None:
                i = keys.index(last) + 1
                out = out[i:] + out[:i]
    return out


def nearest_b(tracks: list[dict], a_path: str, b_path: str,
              limit: int = 1, exclude: list[str] | None = None) -> list[dict]:
    """Другой входящий, максимально близкий к A по темпу, тональности и
    жанру.

    Не «другой трек» вообще, а именно близкий: диджей просил заменить
    деку B «на близкий по BPM, тональности и тем же жанром, что и A».
    Считаем расстояние по трём осям и берём ближайший, исключая сам A и
    нынешний B."""
    import style

    by_path = {t.get("path"): t for t in tracks if t.get("path")}
    a = by_path.get(a_path)
    if a is None:
        return []
    ga, sa = style.track_genre(a)
    bpm_a = float(a.get("bpm") or 174.0)
    # Треки, уже отобранные в пул, кандидатами быть не должны: диджей
    # жаловался, что кнопка «подставляет тот, который уже в пуле». Пул
    # — это то, что УЖЕ поставлено в сет; предлагать его второй раз
    # значит предлагать повтор.
    skip = {a_path, b_path} | set(exclude or ())
    scored: list[tuple[float, dict]] = []
    for p, t in by_path.items():
        if p in skip:
            continue
        bpm = float(t.get("bpm") or 0.0)
        if bpm <= 20:
            continue
        # темп: относительная разница, 3% — уже заметно
        d_bpm = abs(bpm - bpm_a) / max(bpm_a, 1.0)
        kd = camelot_distance(a.get("camelot"), t.get("camelot"))
        d_key = 0.0 if kd is None else min(kd, 6) / 6.0
        gb, sb = style.track_genre(t)
        d_gen = 0.0 if (sb or gb) == (sa or ga) else (0.3 if gb == ga else 0.6)
        d_stem = 0.0 if style.has_stems(t) == style.has_stems(a) else 0.15
        # Раскладка слоёв — четвёртая ось. Вес маленький (0..0.25 против
        # темповых 8.0) сознательно: «бас сидит под басом» это удобство,
        # а темп мимо семейства — невозможность. Но при равных темпе,
        # тональности и жанре выбирать по ней честнее, чем по алфавиту.
        lay = pair_layers(a, t)
        if lay:
            import stem_features as _sf
            d_lay = 0.25 * max(0.0, lay["bass"] - _sf.BASS_CLASH) / max(1e-6, 1.0 - _sf.BASS_CLASH)
            d_lay += 0.10 * (1.0 - lay["drum_fit"])
        else:
            d_lay = 0.0
        # Темп весит больше всего, и это не вкус: 175 в 186 не сводится
        # никаким приёмом, а «другой поджанр» — вопрос настроения. При
        # весе 3.0 трек на 186 BPM обгонял трек на 173 только потому, что
        # у него совпала тональность.
        d_bpm = d_bpm * 8.0 + (1.0 if d_bpm > 0.06 else 0.0)
        scored.append((d_bpm + d_key + d_gen + d_stem + d_lay, t))
    scored.sort(key=lambda x: x[0])
    return [t for _d, t in scored[:max(1, limit)]]


# Порог, с которого конфликт слоёв считается настоящим. Те же числа,
# по которым stem_features их и объявляет, — держать вторую копию порога
# нельзя, она разойдётся при первой же перекалибровке.
def _solve_bias(tid: str, gap: dict | None) -> float:
    """Множитель приёму за то, что он лечит ИЗМЕРЕННЫЙ конфликт пары.

    `gap` — то, что посчитал `stem_features.gap()`: насколько спорят
    басы, гармонии и вокал у этих двух треков. Раньше эти числа влияли
    только на `style.layer_technique_bias` с его рукописными списками;
    теперь они встречаются с осью `solves`, объявленной у самого приёма,
    и список приёмов «для этого случая» больше не надо вести отдельно."""
    if not gap:
        return 1.0
    from techniques import TECHNIQUES
    import stem_features as _sf

    t = TECHNIQUES.get(tid)
    if t is None or not t.solves:
        return 1.0
    hot = set()
    if float(gap.get("bass") or 0.0) >= _sf.BASS_CLASH:
        hot.add("bass_clash")
    if float(gap.get("harm") or 0.0) >= _sf.HARM_CLASH:
        hot.add("harm_clash")
    if float(gap.get("vocal") or 0.0) >= 0.5:
        hot.add("vocal_clash")
    if not hot:
        return 1.0
    return 1.0 + 0.35 * len(hot & set(t.solves))


def techniques_ranked(a: dict, b: dict, seam: dict,
                      session_id: str | None = None) -> list[str]:
    """Подходящие приёмы В ПОРЯДКЕ ПРИГОДНОСТИ для этого шва.

    `techniques_for` отдаёт список для ГЛАЗ: по типу приёма, чтобы в нём
    можно было искать. Кнопке «↻ другой» нужен другой порядок — по тому,
    насколько приём годится именно здесь, иначе она просто листает
    каталог по алфавиту категорий и с третьего нажатия предлагает заведомо
    неподходящее. Считаем тем же весом, что и обычный подбор: место в
    таблице стыка, вкус диджея, поправка на тональность."""
    import style

    label = seam.get("pair_label") or ""
    recommended = list(style.PAIR_TECHNIQUES.get(label, ()))
    kd = seam.get("key_distance")
    if kd is None:
        kd = camelot_distance(a.get("camelot"), b.get("camelot"))
    stems_ok = style.has_stems(a) and style.has_stems(b)
    tm = taste(session_id) if session_id else {}
    # Профиль стиля ВХОДЯЩЕГО: это он должен начаться с начала фразы, и
    # это его жанр решает, как в нём принято заходить.
    prof = style.profile_for(b) if (b.get("genre") or b.get("subgenre")) \
        else style.profile_for(a)
    prof_main = prof.get("main")
    prof_accent = prof.get("accent")

    scored: list[tuple[float, str]] = []
    for x in techniques_for(a, b, label):
        if not x["ok"]:
            continue
        tid = x["id"]
        # Рекомендованные для стыка идут первыми и в своём порядке; для
        # остальных база ниже, но вкус и тональность могут их поднять.
        #
        # Основной ход СТИЛЯ получает такую же базу, как рекомендованный
        # стыком. Он и есть рекомендация — только не от таблицы стыков, а
        # от жанра, и до сих пор она в Clipping не участвовала вовсе.
        # Замер на трансовой паре со стемами: на drop→drop подбор выдавал
        # ST-12, ST-07, ST-04, ST-08 — четыре реза и дабл-дропа подряд, а
        # DNB-25 (основной ход транса, длинный бленд) стоял шестым или не
        # доходил. Диджей: «всегда рекомендуется ST-08 вне зависимости от
        # жанра» и «входящий трек слишком резко входит».
        if tid in recommended:
            base = 1.0 - 0.06 * recommended.index(tid)
        elif tid == prof_main:
            base = 0.94
        elif tid == prof_accent:
            base = 0.80
        else:
            base = 0.55
        key_mult, _ = style.key_technique_bias(tid, kd, stems_ok)
        # Тот же множитель по слоям, что и в общем подборе. Без него
        # кнопка «↻ Другой приём» советовала бы длинное наложение паре,
        # которой подбор только что назначил обмен низом, — и диджей
        # видел бы два разных мнения об одной и той же паре.
        lay_mult, _ = style.layer_technique_bias(tid, pair_layers(a, b), stems_ok)
        # Ось «какую задачу решает приём» против ИЗМЕРЕННОГО конфликта.
        # До сих пор подбор знал, что у пары спорят басы (stem_features
        # это меряет), и никак не связывал это с тем, что половина
        # каталога существует ровно для таких случаев.
        solve_mult = _solve_bias(tid, pair_layers(a, b))
        # Темп и энергия — из полей каталога. Без них «↻ другой приём»
        # предлагал растяжку вдвое больше той, что приём допускает, и
        # тянул энергию против стыка, не считая это возражением.
        # И множитель стиля поверх базы — тот же, которым пользуется
        # сборка сета (style.bias). Держать два разных мнения о том, что
        # принято в жанре, нельзя: диджей видел бы в Clipping одно, а в
        # ControlC другое.
        style_mult, _ = style.bias(tid, a, b)
        scored.append((base * taste_weight(tm, tid, label) * key_mult * lay_mult
                       * solve_mult * style_mult * float(x.get("fit") or 1.0), tid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [tid for _w, tid in scored]


def other_pair(session_id: str, tracks: list[dict],
               exclude: list[str] | None = None,
               avoid: list[str] | None = None,
               limit: int = 1) -> list[dict]:
    """Другая пара ЦЕЛИКОМ: два трека из библиотеки и лучший их стык.

    Кнопка «✕ Другая пара» раньше была обычным отказом: она писала
    вердикт и просила общий подбор дать следующего кандидата. Подбор же
    ранжирует всю библиотеку одинаково, поэтому следующим приходило
    соседнее по рейтингу — почти то же самое. Диджей просил другого:
    взять из библиотеки два трека и посмотреть, что выйдет.

    «Случайно» здесь не значит «как попало». Первый трек берётся
    случайно из того, что ещё не в пуле, — это и даёт неожиданность.
    Второй ищется как ближайший к нему по темпу, тональности и жанру
    (`nearest_b`), потому что случайная ВТОРАЯ половина пары — это не
    смелость, а несводимая пара. Приём и точки считает `lucky`."""
    import random

    by_path = {t.get("path"): t for t in tracks if t.get("path")}
    skip = set(exclude or ())
    pool = [p for p in by_path if p not in skip
            and float(by_path[p].get("bpm") or 0) > 20]
    if len(pool) < 2:
        pool = [p for p in by_path if float(by_path[p].get("bpm") or 0) > 20]
    if len(pool) < 2:
        return []
    # Треки нынешней пары исключаем из ПЕРВОГО места, но не из второго:
    # «другая пара» — это другая пара, а не изгнание треков из библиотеки.
    first_pool = [p for p in pool if p not in set(avoid or ())] or pool

    out: list[dict] = []
    for a_path in random.sample(first_pool, min(len(first_pool), 6)):
        near = nearest_b(tracks, a_path, "", limit=1, exclude=sorted(skip))
        if not near:
            continue
        items = lucky(session_id, tracks, a_path, near[0].get("path"), limit=1)
        if items:
            out.append(items[0])
        if len(out) >= max(1, limit):
            break
    return out


def swap_technique(seam: dict, a: dict, b: dict,
                   technique_id: str | None = None,
                   session_id: str | None = None) -> dict:
    """Тот же шов с другим приёмом. Треки и точки не трогаем.

    Без явного technique_id берём следующий по ПРИГОДНОСТИ — это и есть
    кнопка «другой приём». Прежняя кнопка «другая техника» была отказом:
    она записывала вердикт и пересобирала шов целиком, поэтому вместе с
    приёмом менялись и точки, а иногда и трек."""
    from techniques import TECHNIQUES

    ids = techniques_ranked(a, b, seam, session_id)
    if not ids:
        return dict(seam)
    if technique_id and technique_id in TECHNIQUES:
        pick = technique_id
    else:
        cur = seam.get("technique_id")
        pick = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
    t = TECHNIQUES[pick]
    out = dict(seam)
    out["technique_id"] = pick
    out["technique"] = t.name
    out["why"] = (f"стыкуются {seam.get('pair_label')}, {seam.get('bars')} тактов; "
                  f"{t.name}")
    return out


def bars_options(a: dict, b: dict, bars: float | None,
                 technique_id: str | None = None) -> list[int]:
    """Какие длины перехода предлагать кнопкой «такты».

    Не 4/8/16/32/64 на всё подряд: лесенка берётся из жанра ВХОДЯЩЕГО
    (style.phrase_ladder), потому что квадрат считает он — это он должен
    начаться с начала фразы. У фонка и трапа это 8/16/32, у транса и
    техно 16/32/64, и предлагать трансу 4 такта значит предлагать то,
    что в нём не звучит.

    К лесенке добавляем половину нижней и текущее значение шва: первая
    даёт короткий рез там, где лесенка начинается с 16, второе — чтобы
    кнопка не выкидывала диджея из длины, которую он уже слушает."""
    import style

    lad = list(style.phrase_ladder(b) or ()) or [8, 16, 32]
    out = set(int(x) for x in lad if x > 0)
    out.add(max(2, int(min(lad) // 2)))
    if bars:
        out.add(int(round(float(bars))))
    # И не длиннее, чем приём в состоянии заполнить. Рез живёт один такт;
    # растянуть его на 64 нельзя, и рендер упрётся в потолок — а кнопка
    # при этом обещала бы 64 и молчала о том, что прозвучит восемь.
    cap = _technique_max_bars(technique_id, b)
    if cap:
        fits = sorted(x for x in out if x <= cap)
        out = set(fits) or {min(out)}
    return sorted(out)


def _technique_max_bars(technique_id: str | None, b: dict) -> float:
    """Самая длинная фраза, которую приём способен заполнить целиком.

    Считается из САМОГО приёма: его родная длина, умноженная на потолок
    растяжения рендера. Держать это числом в двух местах нельзя — оно
    разойдётся при первом же новом приёме."""
    if not technique_id:
        return 0.0
    try:
        import demo_render as _dr
        import techniques as _tq

        bpm = float(b.get("bpm") or 0.0) or 174.0
        spb = 60.0 / bpm
        plan = _tq.build_plan(technique_id, "p", "source", "target", bpm, {})
        ev = plan["events"] if isinstance(plan, dict) else plan
        raw = max([4.0 * spb] + [_dr._event_window(e, spb)[1] for e in ev])
        # Потолок спрашиваем у самого рендера: растянуть окно он может
        # ровно настолько, насколько скажет technique_scale.
        bar = spb * 4.0
        top = _dr.technique_scale(raw, 1e9, True)
        return raw * top / bar
    except Exception:                                # noqa: BLE001
        return 0.0


def _seam_dict(a: dict, b: dict, t, tid: str, opt: dict, score: float) -> dict:
    """Карточка шва в том же виде, в каком её отдаёт candidates."""
    import mix_profile as _mp
    import style

    ga, sa = style.track_genre(a)
    gb, sb = style.track_genre(b)
    _lay = pair_layers(a, b)
    return {
        "score": score,
        "a_path": a["path"], "a_name": a.get("name"),
        "b_path": b["path"], "b_name": b.get("name"),
        "technique_id": tid, "technique": t.name,
        "pair_label": opt["label"],
        "exit_seconds": opt["exit"]["at"], "entry_seconds": opt["entry"]["at"],
        "bars": opt["bars"],
        "a_bpm": a.get("bpm"), "b_bpm": b.get("bpm"),
        "a_key": a.get("camelot") or a.get("key"),
        "b_key": b.get("camelot") or b.get("key"),
        "key_distance": camelot_distance(a.get("camelot"), b.get("camelot")),
        "a_genre": sa or ga, "b_genre": sb or gb,
        "a_stems": style.has_stems(a), "b_stems": style.has_stems(b),
        "a_vocal_section": bool(opt["exit"].get("vocal")),
        "b_vocal_section": bool(opt["entry"].get("vocal")),
        # Карточка из lucky/«другой стык» обязана нести то же, что и из
        # обычного подбора: иначе объяснение про слои появлялось бы и
        # пропадало в зависимости от того, какой кнопкой пришёл шов.
        "layers": _lay,
        "mix_profile": _mp.auto(opt["bars"], opt["label"], _lay, tid),
        "bars_options": bars_options(a, b, opt["bars"], tid),
        # Рекомендованные стыки СТИЛЯ — всегда, а не только когда
        # угадали. Диджей: «нужно всегда писать рекомендуемые стыки,
        # как например Uplifting trance смешивается в брейкдаунах».
        "genre_junctions": style.preferred_junctions(b),
        "genre_junction_note": style.genre_junction_bias(b, opt["label"])[1],
        "layer_note": (style.layer_technique_bias(tid, _lay,
                                                  style.has_stems(a) and style.has_stems(b))[1]
                       or layer_weight(_lay)[1]),
        "why": f"стыкуются {opt['label']}, {opt['bars']} тактов; {t.name}",
    }


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
            "mix_profile": (e.get("params") or {}).get("mix_profile"),
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
                    # Ручное ведение фейдеров — часть одобренного шва.
                    # Без него сет из пула звучит не тем, что слушали.
                    "mix_profile": (e.get("params") or {}).get("mix_profile"),
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
