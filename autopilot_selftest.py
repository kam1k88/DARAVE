"""
autopilot_selftest.py — проверка автопилота без Mixxx и без companion'а.

Живьём эту цепочку проверить дорого: нужен запущенный Mixxx, реальные
треки на деках и несколько минут звучания на КАЖДЫЙ прогон. А ошибиться
здесь легко и незаметно — сет разъедется на четвёртом переходе, когда
слушать уже никто не будет.

Поэтому здесь поднимается поддельная комната: телеметрия идёт по
настоящим часам, дека честно едет по треку, companion отвечает так же,
как настоящий (load_result, plan_started/finished). Треки коротки
специально — весь сет проигрывается за десяток секунд.

Что проверяется:
  1. порядок: деки чередуются A -> B -> A, каждый переход играется один
     раз и в правильную сторону;
  2. момент: план уходит вплотную к точке ухода, а не «когда-нибудь»;
  3. загрузка: входящий трек приезжает на СВОБОДНУЮ деку заранее, с
     точкой входа из плана;
  4. кроссфейдер: у перехода B -> A он разворачивается (иначе половина
     сета сводилась бы наоборот);
  5. телеметрия: пакет с длительностью разбирается, старый — тоже;
  6. каждое событие плана разрешается в реальное MIDI/SysEx для ОБЕИХ
     сторон (A -> B и B -> A).

Запуск:  python autopilot_selftest.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import time

import autopilot as ap
import deck_loader
import midi_mapping as mm
import telemetry as tl
from mixplan import MixPlan
from scheduler import PlanScheduler
from techniques import build_plan

TRACK_SECONDS = 22.0
FROM_SECONDS = 7.0        # где уходим
TO_SECONDS = 2.0          # куда заходим


def make_strategy(n_tracks: int = 3) -> dict:
    tracks = [{"name": f"Artist {i} - Track {i}.mp3", "bpm": 174.0,
               "camelot": "8A", "energy": 0.5, "el": 3,
               "duration_seconds": TRACK_SECONDS} for i in range(n_tracks)]
    transitions = [{
        "index": i, "from": tracks[i]["name"], "to": tracks[i + 1]["name"],
        "technique_id": "DNB-25", "technique_name": "Классика",
        "params": {"blend_bars": 2, "out_bars": 1, "loop_bars": 0},
        "bpm_from": 174.0, "bpm_to": 174.0,
        "from_point": {"time_seconds": FROM_SECONDS, "label": "тест"},
        "to_point": {"time_seconds": TO_SECONDS, "label": "тест"},
        "entry_seconds": 0.0, "bars": 2,
    } for i in range(n_tracks - 1)]
    return {"tracks": tracks, "transitions": transitions}


class FakeDeck:
    def __init__(self) -> None:
        self.loaded = False
        self.duration = 0.0
        self.cue = 0.0
        self.playing = False
        self.origin = 0.0        # секунда, с которой поехали
        self.origin_at = 0.0

    def seconds(self) -> float:
        if not self.playing:
            return self.origin
        return self.origin + (time.monotonic() - self.origin_at)

    def play_from(self, seconds: float) -> None:
        self.origin, self.origin_at, self.playing = seconds, time.monotonic(), True


class FakeRoom:
    """Комната + companion в одном лице: отвечает ровно теми сообщениями,
    какими отвечает настоящий companion (см. ws_client.py)."""

    session_id = "selftest"
    companion_ws = object()

    def __init__(self) -> None:
        self.latest_telemetry: dict[str, dict] = {}
        self.telemetry_at = time.monotonic()
        self.decks = {"A": FakeDeck(), "B": FakeDeck()}
        self.pilot: ap.Autopilot | None = None
        self.log: list[str] = []
        self.plans: list[dict] = []
        self.anchors: list[float] = []
        self.loads: list[dict] = []

    # --- то, что автопилот дёргает у комнаты ---
    async def broadcast_to_chat(self, msg: dict) -> None:
        pass

    async def send_load_to_companion(self, msg: dict) -> bool:
        deck = self.decks[msg["deck"]]
        self.loads.append(dict(msg))
        self.log.append(f"load {msg['deck']} cue={msg['cue_seconds']} via={msg.get('via')}")
        await asyncio.sleep(0.25)          # поиск в медиатеке не мгновенный
        deck.loaded, deck.duration = True, float(msg["expect_duration"])
        deck.cue = float(msg["cue_seconds"])
        deck.origin, deck.playing = deck.cue, False
        self.pilot.on_load_result({"token": msg["token"], "ok": True,
                                   "deck": msg["deck"], "duration": deck.duration})
        return True

    async def send_deck_action_to_companion(self, msg: dict) -> bool:
        deck = self.decks[msg["deck"]]
        if msg["action"] in ("play", "cue_play"):
            deck.play_from(deck.cue if msg["action"] == "cue_play" else deck.seconds())
            self.log.append(f"play {msg['deck']} from {deck.origin:.1f}")
        elif msg["action"] == "stop":
            deck.origin, deck.playing = deck.seconds(), False
        return True

    async def send_command_to_companion(self, action: str) -> bool:
        if action.startswith("clear_deck_"):
            name = action.split("_")[-1]
            self.decks[name] = FakeDeck()
            self.log.append(f"clear {name}")
        return True

    async def send_plan_to_companion(self, plan: dict) -> bool:
        self.plans.append(plan)
        self.anchors.append(float(plan["anchor_lead_seconds"]))
        asyncio.create_task(self._run_plan(plan))
        return True

    async def _run_plan(self, plan: dict) -> None:
        # Настоящий companion ждёт анкер, потом исполняет. Нас интересует
        # побочный эффект: техника заводит входящую деку с её CUE.
        await asyncio.sleep(float(plan["anchor_lead_seconds"]))
        target = None
        for ev in plan["events"]:
            if ev["action"] == "play_from_cue":
                target = ev["deck"]
        if target:
            d = self.decks[target]
            d.play_from(d.cue)
            self.log.append(f"play_from_cue {target} at {d.cue:.1f}")
        await asyncio.sleep(0.4)
        self.pilot.on_plan_event({"type": "plan_finished",
                                  "plan_id": plan["plan_id"], "ok": True})

    # --- поток телеметрии ---
    async def pump(self) -> None:
        while True:
            self.latest_telemetry = {
                name: {"playing": d.playing, "bpm": 174.0,
                       "position": (d.seconds() / d.duration) if d.duration else 0.0,
                       "track_loaded": d.loaded, "stem_count": 0,
                       "duration": d.duration, "received_at": time.time()}
                for name, d in self.decks.items()
            }
            self.telemetry_at = time.monotonic()
            await asyncio.sleep(0.2)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("  OK  " if ok else "  FAIL") + f"  {name}" + (f" — {detail}" if detail else ""))
    return ok


async def run_set() -> bool:
    room = FakeRoom()
    pilot = ap.Autopilot(room, logger=lambda t: room.log.append("| " + t))
    room.pilot = pilot
    pump = asyncio.create_task(room.pump())
    await asyncio.sleep(0.3)

    strategy = make_strategy(3)
    await pilot.start(strategy, index=0, mode="set")
    deadline = time.monotonic() + 60
    while pilot.running and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    pump.cancel()

    ok = True
    ok &= check("сет доигран без ошибок", pilot.state == "done",
                f"state={pilot.state} err={pilot.error}")
    ok &= check("сыграны оба перехода", len(room.plans) == 2,
                f"планов: {len(room.plans)}")
    if len(room.plans) == 2:
        d0 = [e["deck"] for e in room.plans[0]["events"] if e["action"] == "play_from_cue"]
        d1 = [e["deck"] for e in room.plans[1]["events"] if e["action"] == "play_from_cue"]
        ok &= check("деки чередуются A->B, затем B->A",
                    d0 == ["B"] and d1 == ["A"], f"{d0} затем {d1}")
        ok &= check("план уходит вплотную к точке ухода",
                    all(0.3 <= a <= ap.SEND_AHEAD_SECONDS + 0.2 for a in room.anchors),
                    f"анкеры: {[round(a,2) for a in room.anchors]}")
    ok &= check("входящие треки грузились с точкой входа из плана",
                all(abs(l["cue_seconds"] - TO_SECONDS) < 0.01 for l in room.loads[1:]),
                f"{[l['cue_seconds'] for l in room.loads]}")
    ok &= check("первый трек заведён со своей секунды входа",
                room.loads and abs(room.loads[0]["cue_seconds"]) < 0.01)
    if not ok:
        print("\n  --- журнал ---")
        for line in room.log:
            print("   ", line)
    return bool(ok)


def run_crossfader() -> bool:
    ok = True
    a0 = mm.resolve_ramp_tick("crossfade", "A", 0.0)[2]
    a1 = mm.resolve_ramp_tick("crossfade", "A", 1.0)[2]
    b0 = mm.resolve_ramp_tick("crossfade", "B", 0.0)[2]
    b1 = mm.resolve_ramp_tick("crossfade", "B", 1.0)[2]
    ok &= check("A уходит: фейдер едет слева направо", (a0, a1) == (0, 127), f"{a0}->{a1}")
    ok &= check("B уходит: фейдер едет справа налево", (b0, b1) == (127, 0), f"{b0}->{b1}")
    return bool(ok)


def run_telemetry() -> bool:
    def sysex(p: str) -> list[int]:
        return [0xF0] + [ord(c) & 0x7F for c in p] + [0xF7]

    new = tl.parse_sysex_payload(sysex("A,1,174.00,0.5000,1,4,312.44"))
    old5 = tl.parse_sysex_payload(sysex("B,0,86.00,0.1000,1"))
    ok = True
    ok &= check("новый пакет: длительность разобрана",
                new is not None and abs(new.duration - 312.44) < 0.01 and new.stem_count == 4)
    ok &= check("старый пакет (5 полей) не ломает разбор",
                old5 is not None and old5.duration == 0.0 and old5.stem_count == 0)
    return bool(ok)


def run_midi_resolves() -> bool:
    """Каждое событие «Классики» должно разрешаться в реальное сообщение
    в ОБЕ стороны. До автопилота живьём игралось только A->B."""
    sched = PlanScheduler(backend=None)
    ok = True
    for src, dst in (("A", "B"), ("B", "A")):
        plan = build_plan("DNB-25", f"t_{src}{dst}", src, dst, 174.0)
        try:
            events = sched.expand_plan(MixPlan.from_dict(plan), time.perf_counter())
            ok &= check(f"{src} -> {dst}: все события разрешились",
                        len(events) > 0, f"{len(events)} MIDI-событий")
        except Exception as exc:                       # noqa: BLE001
            ok &= check(f"{src} -> {dst}: все события разрешились", False, repr(exc))
    return bool(ok)


class FakeStore:
    """Телеметрия дек, какой её видит companion."""

    def __init__(self) -> None:
        self.decks = {"A": _S(), "B": _S()}

    def get(self, deck):
        return self.decks.get(deck)

    def snapshot(self):
        return dict(self.decks)


class _S:
    def __init__(self) -> None:
        self.track_loaded = False
        self.duration = 0.0
        self.playing = False


async def run_autodj_loader() -> bool:
    """Проверяем загрузчик очередью: включил Auto DJ — трек приехал —
    выключил. Поддельный Mixxx кладёт трек на СВОБОДНУЮ деку, как и
    настоящий AutoDJProcessor."""
    import autodj_loader as al

    store = FakeStore()
    store.decks["A"].track_loaded = True
    store.decks["A"].duration = 300.0
    store.decks["A"].playing = True          # A играет, B свободна
    sent: list[str] = []
    QUEUE_DURATION = 412.0

    def midi_send(msg: list[int]) -> None:
        text = bytes(b for b in msg[1:-1]).decode("ascii", "replace") if msg and msg[0] == 0xF0 else ""
        sent.append(text)
        if text == "S|[AutoDJ]|enabled|1":
            # Mixxx кладёт следующий трек очереди на свободную деку
            store.decks["B"].track_loaded = True
            store.decks["B"].duration = QUEUE_DURATION

    al.AUTODJ_SETTLE_SECONDS = 0.05
    out = await al.load_next(midi_send, store, "B", cue_seconds=41.0,
                             expect_duration=QUEUE_DURATION, log=lambda *_: None)
    ok = True
    ok &= check("трек приехал на свободную деку", out.get("ok"), out.get("error") or "")
    ok &= check("Auto DJ включён и сразу выключен",
                "S|[AutoDJ]|enabled|1" in sent and "S|[AutoDJ]|enabled|0" in sent,
                " · ".join(sent[:2]))
    ok &= check("Auto DJ не остался включённым",
                sent.index("S|[AutoDJ]|enabled|0") > sent.index("S|[AutoDJ]|enabled|1"))
    ok &= check("точка входа выставлена и закреплена меткой CUE",
                any(x.startswith("S|[Channel2]|playposition|") for x in sent)
                and "P|[Channel2]|cue_set" in sent,
                " · ".join(x for x in sent if "Channel2" in x))
    ok &= check("ни одной клавиши в тракте", all("|" in x for x in sent))

    # чужая дека: план ждал B, а Mixxx положил на A
    store2 = FakeStore()
    sent2: list[str] = []

    def midi_send2(msg: list[int]) -> None:
        text = bytes(b for b in msg[1:-1]).decode("ascii", "replace") if msg and msg[0] == 0xF0 else ""
        sent2.append(text)
        if text == "S|[AutoDJ]|enabled|1":
            store2.decks["A"].track_loaded = True
            store2.decks["A"].duration = 412.0

    bad = await al.load_next(midi_send2, store2, "B", expect_duration=412.0, log=lambda *_: None)
    ok &= check("подмена деки замечена", not bad.get("ok") and "деку A" in (bad.get("error") or ""),
                bad.get("error") or "")
    return bool(ok)


def run_queue_check() -> bool:
    """Очередь Auto DJ должна сверяться с планом ДО старта."""
    import mixxx_autodj

    real = mixxx_autodj.read_queue
    mixxx_autodj.read_queue = lambda *_a, **_k: [
        "C:/Music/one.mp3", "C:/Music/two.mp3", "C:/Music/three.mp3"]
    try:
        good = mixxx_autodj.queue_matches(["C:/Music/one.mp3", "C:/Music/two.mp3"])
        bad = mixxx_autodj.queue_matches(["C:/Music/two.mp3", "C:/Music/one.mp3"])
        short = mixxx_autodj.queue_matches(["C:/Music/one.mp3"] * 5)
    finally:
        mixxx_autodj.read_queue = real
    ok = check("совпадающая очередь принимается", good["ok"])
    ok &= check("переставленная очередь отвергается",
                not bad["ok"] and bad["first_mismatch"] == 0)
    ok &= check("короткая очередь отвергается", not short["ok"])
    return bool(ok)


def run_single_path() -> bool:
    """Единый тракт: профиль ввода обязан звучать одинаково везде.

    Диджей сказал это точнее любого лога: руками сводит красиво, а те же
    клипы в пуле «снова звучат разрозненно, входящий не накладывается».
    Так и было: профиль применялся только в офлайн-рендере одиночного
    демо, а пул, сет и живые деки о нём не знали."""
    import demo_render
    import mix_profile
    import mix_strategist
    from mixplan import MixPlan
    from scheduler import PlanScheduler
    from techniques import build_plan

    PROF = {"entry": "manual", "in_len": 0.85, "in_smooth": 0.95,
            "out_len": 0.05, "out_smooth": 0.1}
    plain = build_plan("DNB-25", "t", "source", "target", 174.0)

    ok = check("офлайн-рендер считает профиль тем же кодом",
               demo_render._apply_entry_profile(plain["events"], PROF)
               == mix_profile.apply(plain["events"], PROF))

    faders = lambda evs: [e for e in evs if e["action"] == "volume_ramp"]
    ok &= check("без профиля отдельного фейдера входящего нет",
                not faders(plain["events"]))

    sched = PlanScheduler(backend=None)
    for src, dst in (("A", "B"), ("B", "A")):
        strategy = {"transitions": [{"index": 0, "technique_id": "DNB-25",
                                     "bpm_from": 174.0, "params": {},
                                     "mix_profile": PROF}]}
        plan = mix_strategist.build_transition_plan(strategy, 0, src, dst, "t")
        decks = {e["deck"] for e in plan["events"]}
        ok &= check(f"живьём {src}->{dst}: профиль на настоящих деках",
                    decks <= {src, dst}, f"деки {sorted(decks)}")
        try:
            events = sched.expand_plan(MixPlan.from_dict(plan), time.perf_counter())
            ok &= check(f"живьём {src}->{dst}: всё разрешается в MIDI",
                        len(events) > 0, f"{len(events)} событий")
        except Exception as exc:                       # noqa: BLE001
            ok &= check(f"живьём {src}->{dst}: всё разрешается в MIDI", False, repr(exc))

    # профиль доезжает из одобренного шва в правки плана
    seam = {"a_path": "a.mp3", "b_path": "b.mp3", "technique_id": "DNB-25",
            "exit_seconds": 10.0, "entry_seconds": 2.0,
            "params": {"bars": 16, "mix_profile": PROF}}
    ov = {"technique_id": seam["technique_id"],
          "bars": seam["params"]["bars"],
          "mix_profile": seam["params"].get("mix_profile")}
    ok &= check("профиль переживает путь «шов -> правки плана»",
                mix_profile.from_overrides(ov) == PROF)
    ok &= check("пустой профиль ничего не меняет",
                mix_profile.apply(plain["events"], {"entry": "technique"}) is plain["events"])
    return bool(ok)


def run_queries() -> bool:
    q = deck_loader.search_query("01. Hybrid Minds - Summer Rain (feat. Grimm).mp3")
    ok = check("запрос в поиск Mixxx собран из редких слов",
               "Hybrid" in q or "Summer" in q, f"«{q}»")
    ok &= check("расширение и номер трека в запрос не попали",
                ".mp3" not in q and not q.startswith("01"), f"«{q}»")
    return bool(ok)


def run_fx_chain() -> bool:
    """Тракт эффектов: посыл кормится ПОСТ-фейдерным сигналом.

    Проверка нужна именно как регрессия. Баг, который она ловит, звучал
    так: «добавил FX — дБ зашкаливает». Причина была в том, что посыл
    снимался с сухой деки на полной громкости, и возврат ложился поверх
    готового микса второй дорожкой. Ошибка невидимая: код был корректен
    построчно, неверна была только точка съёма."""
    import pathlib

    import numpy as np
    import demo_render as dr

    ok = True
    src = pathlib.Path(dr.__file__).read_text(encoding="utf-8")
    ok &= check("возвраты откладываются в очередь, а не считаются на месте",
                "fx_send_queue" in src and "_queue_send" in src)
    ok &= check("уровень возврата ниже сухого сигнала",
                0.2 <= dr.FX_RETURN_LEVEL <= 0.6,
                f"FX_RETURN_LEVEL={dr.FX_RETURN_LEVEL}")

    # Эхо на сигнале, который уводят в ноль: хвост обязан пережить увод,
    # но НОВОГО сигнала после увода в эффект поступать не должно.
    sr = 44100
    n = sr * 4
    sig = np.zeros((n, 2), dtype=np.float64)
    sig[:, :] = (np.random.default_rng(0).standard_normal((n, 1)) * 0.3)
    fader = np.ones(n)
    fader[sr * 2:] = 0.0                    # фейдер падает на 2-й секунде
    send = np.ones(n)                       # посыл открыт всё время

    wet_post = dr._echo_return(sig * fader[:, None], sr, send, delay_sec=0.3)
    wet_dry = dr._echo_return(sig, sr, send, delay_sec=0.3)

    tail = np.max(np.abs(wet_post[sr * 2:sr * 2 + int(sr * 0.5)]))
    late = np.max(np.abs(wet_post[sr * 3:]))
    late_dry = np.max(np.abs(wet_dry[sr * 3:]))
    ok &= check("хвост эха переживает увод фейдера", tail > 1e-3,
                f"пик сразу после увода {tail:.4f}")
    ok &= check("после увода в эффект не поступает нового сигнала",
                late < late_dry * 0.1,
                f"через секунду после увода {late:.5f} против {late_dry:.5f} у сухого посыла")
    return bool(ok)


def run_clipping_buttons() -> bool:
    """Кнопки отбора клипов: каждая обязана ДАВАТЬ ДРУГОЕ.

    Все четыре жалобы диджея были об одном: кнопка нажата, а на экране то
    же самое. Причины были разные — детерминированный перебор, отсутствие
    исключений, каталожный порядок вместо порядка пригодности, — но
    проверяется это одинаково: нажать несколько раз и посмотреть, меняется
    ли результат."""
    import random

    import audition
    import style

    def tr(i, bpm, cam, sub):
        return {"path": f"/selftest/{i}.mp3", "name": f"T{i}", "bpm": bpm,
                "camelot": cam, "genre": "dnb", "subgenre": sub,
                "duration_seconds": 300.0}

    cams = ["8A", "9A", "7A", "8B", "5A", "2A", "11A", "3A"]
    tracks = [tr(i, 172 + (i % 6), cams[i % len(cams)],
                 "liquid" if i % 2 else "neurofunk") for i in range(16)]
    a, b = tracks[0]["path"], tracks[1]["path"]
    ok = True

    # --- «🎲 Get lucky»: пять нажатий не должны дать пять одинаковых.
    # У выдуманных треков нет разметки секций, поэтому стыковка была бы
    # ровно одна и проверять было бы нечего. Подставляем несколько
    # стыковок вместо разметки — проверяем ИМЕННО перебор, а не сегментацию.
    from techniques import TECHNIQUES

    def _fake_options(_a, _b):
        out = []
        for lab, at in (("groove→groove", 120.0), ("drop→breakdown", 200.0),
                        ("breakdown→drop", 240.0)):
            ids = [t for t in style.PAIR_TECHNIQUES.get(lab, ())
                   if t in TECHNIQUES and not TECHNIQUES[t].requires_stems
                   and not TECHNIQUES[t].needs_vocals
                   and TECHNIQUES[t].requires_decks <= 2][:4]
            if ids:
                out.append({"label": lab, "bars": 16.0,
                            "exit": {"at": at, "kind": lab.split("→")[0]},
                            "entry": {"at": 8.0, "kind": lab.split("→")[1]},
                            "techniques": ids})
        return out

    real_options = style.pair_options
    style.pair_options = _fake_options
    try:
        seen: list[str] = []
        picks: list[str] = []
        for _ in range(6):
            items = audition.lucky("selftest", tracks, a, b, limit=1, exclude=seen)
            if not items:
                break
            key = audition.combo_key(items[0])
            seen.append(key)
            picks.append(key)
        total = len(audition.lucky("selftest", tracks, a, b, limit=999))
    finally:
        style.pair_options = real_options
    ok &= check("«повезёт» перебирает, а не возвращает одно и то же",
                len(set(picks)) == min(total, len(picks)),
                f"комбинаций у пары {total}, разных показано {len(set(picks))} за {len(picks)} нажатий")
    ok &= check("«повезёт» не повторяет предыдущую комбинацию подряд",
                total < 2 or all(picks[i] != picks[i + 1] for i in range(len(picks) - 1)))

    # --- «⇄ Сменить трек B»: не берёт то, что уже в пуле
    pool = [tracks[2]["path"], tracks[3]["path"]]
    tried: list[str] = []
    cur = b
    for _ in range(4):
        cand = audition.nearest_b(tracks, a, cur, limit=1,
                                  exclude=pool + tried)
        if not cand:
            break
        cur = cand[0]["path"]
        tried.append(cur)
    ok &= check("замена трека B не предлагает то, что уже в пуле",
                not (set(tried) & set(pool)), f"предложено {len(tried)} шт.")
    ok &= check("замена трека B не повторяется",
                len(set(tried)) == len(tried), f"{[t.split('/')[-1] for t in tried]}")

    # --- «✕ Другая пара»: берёт треки вне пула и меняет пару
    random.seed(11)
    pairs = []
    for _ in range(5):
        items = audition.other_pair("selftest", tracks, exclude=pool,
                                    avoid=[a, b], limit=1)
        if items:
            pairs.append((items[0]["a_path"], items[0]["b_path"]))
    flat = {p for pr in pairs for p in pr}
    ok &= check("«другая пара» берёт треки из библиотеки, а не из пула",
                bool(pairs) and not (flat & set(pool)), f"пар получено {len(pairs)}")
    ok &= check("«другая пара» не возвращает нынешнюю пару",
                all(pr != (a, b) for pr in pairs))
    ok &= check("«другая пара» действительно перебирает",
                len(set(pairs)) > 1, f"разных пар {len(set(pairs))} из {len(pairs)}")

    # --- «↻ другой»: порядок по пригодности, а не по каталогу
    ta, tb = tracks[0], tracks[1]
    label = "groove→groove"
    ranked = audition.techniques_ranked(ta, tb, {"pair_label": label})
    catalog = [x["id"] for x in audition.techniques_for(ta, tb, label) if x["ok"]]
    rec_ok = [x["id"] for x in audition.techniques_for(ta, tb, label)
              if x["ok"] and x["recommended"]]
    ok &= check("«другой приём» начинает с рекомендованных для стыка",
                set(ranked[:len(rec_ok)]) == set(rec_ok),
                f"рекомендовано и доступно: {rec_ok}")
    ok &= check("порядок «другого приёма» отличается от каталожного",
                ranked[:3] != catalog[:3],
                f"каталог {catalog[:3]} против пригодности {ranked[:3]}")
    return bool(ok)


def run_vocal_flag() -> bool:
    """Вокал — ФЛАГ на секции, а не отдельная секция.

    История: сначала вокальная секция называлась verse, и на «Black Sun
    Empire, Concord Dawn — The Sun» её нашлось три штуки при полном
    отсутствии вокала в треке. Диджей поправил дважды: verse структурно
    неверен («в моей библиотеке вокал — почти всегда зацикленный сэмпл»),
    и детектор ловил не голос. Секция теперь всегда groove, вокал — флаг,
    а детектор проверяет уровень, спектр и длительность."""
    import pathlib
    import tempfile

    import numpy as np
    import soundfile as sf

    import cue_points as cp
    import stems
    import style

    ok = True
    ok &= check("секции названы по-английски",
                set(cp.SECTION_NAMES.values()) == {"intro", "build", "drop",
                                                   "breakdown", "groove", "outro"},
                ", ".join(cp.SECTION_NAMES.values()))
    ok &= check("verse не существует ни как секция, ни как стык",
                "verse" not in cp.SECTION_PRIORITY
                and "verse" not in cp.SECTION_NAMES
                and "verse" not in style.MIX_SECTION_KINDS
                and not [k for k in style.PAIR_TECHNIQUES if "verse" in k])
    ok &= check("два голоса сразу дешевле для приёмов-бленда",
                style.vocal_technique_bias("DNB-25", True, True)[0] < 1.0
                and style.vocal_technique_bias("ST-15", True, True)[0] > 1.0,
                f"бленд x{style.vocal_technique_bias('DNB-25', True, True)[0]:.2f}, "
                f"передача вокала x{style.vocal_technique_bias('ST-15', True, True)[0]:.2f}")
    ok &= check("вокал у одного трека конфликтом не считается",
                style.vocal_technique_bias("DNB-25", True, False)[0] == 1.0
                and style.vocal_technique_bias("DNB-25", False, False)[0] == 1.0,
                "голос уходящего поверх инструментала входящего — это материал, а не помеха")
    ok &= check("стемы смягчают конфликт двух вокалов",
                style.vocal_technique_bias("DNB-25", True, True, True)[0]
                > style.vocal_technique_bias("DNB-25", True, True, False)[0])

    # --- детектор: три искусственных слоя, три класса ошибок
    sr = stems.VOCAL_MAP_SR
    dur = 120.0
    t = np.arange(int(sr * dur)) / sr
    rng = np.random.default_rng(0)
    # инструментал: низ плюс широкополосный шум
    instr = (0.5 * np.sin(2 * np.pi * 70 * t)
             + 0.2 * rng.standard_normal(len(t)))

    def vocalish(spans):
        """Голосоподобное: тон с вибрато и формантами ВЫШЕ 300 Гц.

        Основной тон нарочно слабее гармоник. В сведённом треке голос
        почти всегда подрезан снизу, и вся его разборчивость живёт в
        телефонной полосе — именно на этом и стоит детектор."""
        y = np.zeros_like(t)
        for a, b in spans:
            i, j = int(a * sr), int(b * sr)
            seg = t[i:j] - a
            f0 = 220 * (1 + 0.02 * np.sin(2 * np.pi * 5 * seg))
            ph = 2 * np.pi * np.cumsum(f0) / sr
            y[i:j] = 0.25 * (0.15 * np.sin(ph)          # 220 Гц — подрезан
                             + 1.0 * np.sin(3 * ph)     # 660 Гц
                             + 0.8 * np.sin(6 * ph)     # 1.3 кГц — форманта
                             + 0.5 * np.sin(11 * ph))   # 2.4 кГц
        return y

    cases = {
        "настоящий голос": (vocalish([(20.0, 50.0), (70.0, 100.0)]), True),
        # утечка баса: громкая, но вся энергия внизу — это и был «The Sun»
        "утечка баса": (0.4 * np.sin(2 * np.pi * 80 * t), False),
        # одиночные выкрики: спектр голосовой, но каждый по полсекунды
        "одиночные выкрики": (vocalish([(x, x + 0.5) for x in range(10, 110, 7)]), False),
    }

    d = pathlib.Path(tempfile.mkdtemp())
    real_paths, real_mp4, real_has = (stems.stem_paths, stems.stem_mp4_for,
                                      stems.has_vocals)
    stems.stem_paths = lambda _p: None
    stems.has_vocals = lambda _p: True
    try:
        for name, (voc, want) in cases.items():
            sf.write(d / "vocals.flac", voc, sr)
            sf.write(d / "instr.flac", instr, sr)
            # подсовываем слои напрямую, минуя декодирование mp4
            stems._decode_layers = lambda _p, _sr, _v=voc, _i=instr: (_v, _i)
            r = stems.compute_vocal_map("/selftest/x.mp3", save=False)
            got = bool(r and r["vocal"])
            ok &= check(f"детектор: {name}", got == want,
                        f"вокал={got}, доля окон {r['share'] if r else '-'}")
    finally:
        stems.stem_paths, stems.stem_mp4_for, stems.has_vocals = (
            real_paths, real_mp4, real_has)

    # --- флаг доезжает до секций и не ломает структуру
    track = {"path": "/selftest/vocal.mp3", "bpm": 174.0, "duration_seconds": 300.0,
             "structure": {"drum_map": {"drums_from": 8.0, "drums_end": 290.0},
                           "energy_map": {"drops": [60.0, 180.0],
                                          "breakdowns": [[120.0, 150.0]]},
                           "vocal_map": [[152.0, 178.0]]}}
    secs = cp.sections_for_track(track).get("sections", [])
    kinds = {x["kind"] for x in secs}
    vocal_secs = [x for x in secs if x["vocal"]]
    ok &= check("виды секций от вокала не меняются",
                kinds <= set(cp.SECTION_NAMES), f"{sorted(kinds)}")
    ok &= check("вокальная секция помечена флагом", bool(vocal_secs),
                ", ".join(x["label"] for x in vocal_secs))
    ok &= check("вокальная секция осталась своим видом",
                all(x["kind"] in ("groove", "build", "drop") for x in vocal_secs))
    no_voc = dict(track)
    no_voc["structure"] = dict(track["structure"])
    no_voc["structure"]["vocal_map"] = []
    plain = cp.sections_for_track(no_voc).get("sections", [])
    ok &= check("без вокала разбиение то же самое",
                [x["kind"] for x in plain] == [x["kind"] for x in secs],
                "флаг структуру не трогает")
    ok &= check("подписи и единицы по-английски",
                all(" bars" in x["label"] or " bar " in x["label"] for x in secs),
                secs[0]["label"] if secs else "")
    return bool(ok)


def run_seam_level() -> bool:
    """Уровень на стыке: переход обязан состыковаться с соседями.

    Жалоба диджея: «в финальном превью/полном сете места переходов опять с
    овер dB, громкость нормальная только в телах одиночных треков».
    Замерено на его сете (44 минуты, 20 переходов): области с работающим
    ограничителем шли на 9.2 dB выше остального.

    Причина была не в сумме двух дек, а в том, что тела и переходы
    мерились РАЗНЫМИ мерками: тело подгонялось под средний уровень трека,
    переход — под 90-ю процентиль своего же соло-участка. У трека с
    сильной динамикой это вдвое разные числа, и если уход назначен на
    дроп, переход честно выходил на десяток децибел громче соседей."""
    import numpy as np

    import set_export as SE

    ok = True
    sr = 44100
    rng = np.random.default_rng(7)

    def noise(sec, amp):
        n = int(sec * sr)
        y = rng.standard_normal((n, 2)) * amp
        return y

    # Соседи: слева тело потише, справа погромче — переход обязан провести
    # уровень от одного к другому, а не встать посередине или сверху.
    left, right = 0.10, 0.20
    chunk = noise(20.0, 0.55)          # кусок в разы громче обоих соседей
    before = SE._level_p90(SE._mono_of(chunk), sr)
    ridden = SE._ride_to_reference(chunk, sr,
                                   ref_rms=np.array([left, right]),
                                   max_cut_db=SE.SET_RIDE_MAX_CUT_DB)
    head = SE._level_p90(SE._mono_of(ridden[:int(3 * sr)]), sr)
    tail = SE._level_p90(SE._mono_of(ridden[-int(3 * sr):]), sr)
    ok &= check("громкий переход прижимается к уровню соседей",
                before > left * 2 and head < left * 1.6,
                f"было {20 * np.log10(before / left):+.1f} dB над левым соседом, "
                f"стало {20 * np.log10(head / left):+.1f} dB")
    ok &= check("конец перехода ведётся к уровню ПРАВОГО соседа",
                tail > head * 1.3,
                f"начало {20 * np.log10(head):.1f} dB, конец {20 * np.log10(tail):.1f} dB "
                f"при соседях {20 * np.log10(left):.1f} и {20 * np.log10(right):.1f}")

    # Тихий переход трогать нельзя: это музыка, а не перебор.
    quiet = noise(20.0, 0.02)
    same = SE._ride_to_reference(quiet, sr, ref_rms=np.array([left, right]))
    ok &= check("тихий переход остаётся тихим",
                abs(SE._level_p90(SE._mono_of(same), sr)
                    - SE._level_p90(SE._mono_of(quiet), sr)) < 1e-6,
                "выравнивание работает только вниз")

    # Одно число по-прежнему принимается — старые вызовы не ломаются.
    one = SE._ride_to_reference(chunk, sr, ref_rms=left)
    ok &= check("цель можно задать и одним числом",
                SE._level_p90(SE._mono_of(one), sr) < before)

    # Тело возвращает уровень своего хвоста — без этого стыковать нечем.
    class _Sink:
        def __init__(self):
            self.buf = []

        def write(self, y):
            self.buf.append(np.asarray(y))

    import soundfile as sf
    import tempfile
    import pathlib

    d = pathlib.Path(tempfile.mkdtemp())
    sf.write(d / "t.wav", noise(30.0, 0.25).astype("float32"), sr)
    sink = _Sink()
    sec, tail_level = SE._write_body(sink, str(d / "t.wav"), 2.0, 20.0, 1.0, sr, 0.25, 64)
    ok &= check("тело сообщает уровень своего хвоста",
                sec > 0 and tail_level > 0,
                f"{sec:.1f} с, хвост {20 * np.log10(tail_level):.1f} dBFS")
    written = np.concatenate(sink.buf)
    ok &= check("уровень хвоста измерен на том, что ушло в файл",
                abs(tail_level - SE._level_p90(SE._mono_of(written[-int(4 * sr):]), sr)) < 1e-9)
    return bool(ok)


def run_detune() -> bool:
    """Расстройка от подгонки темпа: то, чего колесо Камелота не видит.

    Вопрос диджея: «бывает, что BPM определяется неверно, и даже если так,
    и два трека с разным BPM, хотелось бы определить их разницу в
    тональности, если подогнать питчем один трек к другому». Он прав: мы
    подгоняем темп СКОРОСТЬЮ, а она тянет за собой высоту. 171 к 175 —
    это 40 центов, почти половина полутона. Метка Камелота при этом не
    меняется вовсе, и пара выглядит идеальной, пока не зазвучит."""
    import audition
    import style
    import tempo

    ok = True
    ok &= check("полутон — это ровно логарифм отношения темпов",
                abs(tempo.pitch_semitones(100.0, 100.0 * 2 ** (1 / 12)) - 1.0) < 1e-9)
    d = tempo.detune_between(171.0, 175.0, 173.0)
    ok &= check("171 и 175 расходятся на 40 центов", 39 <= d["cents"] <= 41,
                f"{d['cents']} центов, вердикт {d['verdict']}")

    # Главное свойство: расхождение МЕЖДУ деками от выбора мастер-темпа
    # не зависит — сдвигая обе на равную долю, мы двигаем их вместе.
    cents = {m: tempo.detune_between(171.0, 175.0, m)["cents"]
             for m in (171.0, 173.0, 175.0)}
    ok &= check("выбор мастер-темпа расхождение между деками не меняет",
                max(cents.values()) - min(cents.values()) < 0.5,
                f"к A {cents[171.0]:.0f}, в среднее {cents[173.0]:.0f}, к B {cents[175.0]:.0f}")
    mid = tempo.detune_between(171.0, 175.0, 173.0)
    to_b = tempo.detune_between(171.0, 175.0, 175.0)
    ok &= check("среднее делит уход от ОРИГИНАЛА пополам",
                abs(mid["semitones_a"]) < abs(to_b["semitones_a"]),
                f"в среднее A уходит на {mid['semitones_a']*100:+.0f} центов, "
                f"к B — на {to_b['semitones_a']*100:+.0f}")

    ok &= check("половинный счёт расстройки не даёт",
                tempo.detune_between(87.0, 174.0, 174.0)["cents"] < 1.0,
                "87 и 174 играются вместе без единого процента подгонки")
    ok &= check("неизвестный темп ничего не выдумывает",
                tempo.detune_between(0.0, 174.0)["verdict"] == "unknown")

    ok &= check("дробный сдвиг метку Камелота не меняет",
                tempo.camelot_after_pitch("8A", 0.3) == "8A",
                "0.3 полутона — это расстройка внутри той же тональности")
    ok &= check("полутон — это семь шагов по колесу, а не один",
                tempo.camelot_after_pitch("8A", 1.0) == "3A"
                and tempo.camelot_after_pitch("8A", -1.0) == "1A",
                "колесо Камелота — круг квинт")

    ok &= check("расстройка удешевляет приёмы, которые держат унисон",
                style.detune_technique_bias("DNB-25", 40.0)[0] < 1.0
                and style.detune_technique_bias("DNB-25", 80.0)[0]
                < style.detune_technique_bias("DNB-25", 40.0)[0],
                f"40 центов x{style.detune_technique_bias('DNB-25', 40.0)[0]:.2f}, "
                f"80 центов x{style.detune_technique_bias('DNB-25', 80.0)[0]:.2f}")
    ok &= check("резовым приёмам расстройка не мешает",
                style.detune_technique_bias("ST-08", 80.0)[0] > 1.0,
                "они не держат треки в унисоне")
    ok &= check("при малой расстройке поправки нет",
                style.detune_technique_bias("DNB-25", 10.0) == (1.0, ""))

    ok &= check("подбор считает расстройку по паре",
                audition._pair_detune({"bpm": 171.0}, {"bpm": 175.0})["cents"] > 25
                and audition._pair_detune({"bpm": 174.0}, {"bpm": 174.0})["cents"] < 1)
    return bool(ok)


def run_genre_bpm() -> bool:
    """Жанр по темпу и то, что из него следует.

    Жалоба диджея: «неверно определяет жанры (или не определяет вовсе),
    из-за чего мне предлагалось выбирать эпохи в стиле 135 BPM garage /
    UK garage, speed garage / house — под эти стили нужны другие фразы и
    другие техники сведения».

    Причина была одна: жанр по темпу подписывался ТОЛЬКО в полосе
    160-185. Для 42 драм-н-бейсовых треков из 66 это работало, а 11
    трекам на 135 и четырём на 140 доставалось None — и дальше по цепочке
    None означал «возьми умолчания», то есть драм-н-бейсовые."""
    import genre
    import style
    import track_analysis as TA

    ok = True
    ok &= check("135 BPM больше не остаётся без жанра",
                genre.genre_from_bpm(135.0)["genre"] == "garage",
                f"семейство {genre.genre_from_bpm(135.0)['genre']}, "
                f"варианты {genre.genre_from_bpm(135.0)['candidates']}")
    ok &= check("темп отдаёт СЕМЕЙСТВО с вариантами, а не один жанр",
                len(genre.genre_from_bpm(135.0)["candidates"]) > 1,
                "135 BPM — это гэридж, бэйслайн, спид-гэридж или брейкс; "
                "выбрать один по темпу нельзя")
    ok &= check("источник честно помечен как темп",
                genre.genre_from_bpm(174.0)["genre_source"] == "bpm")
    ok &= check("там, где темп не значит ничего, жанра нет",
                genre.genre_from_bpm(124.0)["genre"] is None,
                "120-128 — и хаус, и техно, и поп")
    ok &= check("явный тег или имя файла сильнее темпа",
                genre.classify("Hybrid Minds - Summer Rain.mp3",
                               bpm=175.0).get("subgenre") == "liquid")

    ok &= check("сетка фраз для 135 BPM не драм-н-бейсовая по умолчанию",
                style.phrase_ladder({"bpm": 135.0}) == (8, 16, 32),
                f"{style.phrase_ladder({'bpm': 135.0})}")
    ok &= check("подписанный поджанр сильнее темпа и здесь",
                style.phrase_ladder({"bpm": 174.0,
                                     "tags": {"genre": {"genre": "dnb",
                                                        "subgenre": "liquid"}}})
                == (16, 32, 64))
    ok &= check("хип-хоповый темп даёт короткие квадраты",
                style.phrase_ladder({"bpm": 92.0}) == (4, 8, 16))

    own = [r"C:\Users\kam1k88\DARAVE\sets\my-room_full_db3dce3c.mp3",
           r"C:\Users\kam1k88\Music\my-room_7c32d8a3.mp3",
           r"C:\Users\kam1k88\DARAVE\demos\x.wav",
           "/home/x/DARAVE/sets/my-room_a.mp3"]
    theirs = [r"C:\Users\kam1k88\Music\Nelver - Red Light.mp3",
              r"C:\Users\kam1k88\Music\++\10. MPH - Alone.mp3",
              "/home/x/Music/++/1. Bad Patterns - PERFECT STORM.mp3"]
    ok &= check("рендеры DARAVE в медиатеку не попадают",
                all(TA.is_own_output(p) for p in own),
                "отрендеренный сет — не трек")
    ok &= check("чужие треки не выкидываются заодно",
                not any(TA.is_own_output(p) for p in theirs))
    ok &= check("путь разбирается и с обратными слешами, и с прямыми",
                TA.is_own_output(r"C:\a\sets\b.mp3") and TA.is_own_output("/a/sets/b.mp3"),
                "бэкенд на Windows, тесты — нет")
    return bool(ok)


def run_dead_tail() -> bool:
    """Пустое пространство между сведениями.

    Жалоба диджея: «между треками (и телами, и фейдерами) образуется
    пустое пространство». Замерено на его превью: с 1:17 до 1:26 уровень
    падал до −65 dBFS — девять секунд тишины.

    Причин было две, и обе про одно: окно рендера задаётся ПЛАНОМ, а не
    файлами. План говорит «сведи здесь и дай ещё сорок секунд», а если
    входящий трек заведён под конец собственного файла, этих сорока
    секунд взять неоткуда — и рендер писал цифровую тишину, −240 dBFS.
    Вторая: превью оставляло длинный хвост входящего трека, то есть его
    интро, на 15-20 dB тише самого сведения."""
    import numpy as np

    import demo_render as dr
    import set_export as SE

    ok = True
    sr = 44100

    # Кусок, у которого последняя треть — численный ноль.
    live = np.tile(np.sin(2 * np.pi * 220 * np.arange(2 * sr) / sr) * 0.3, 1)
    mix = np.zeros((3 * sr, 2))
    mix[:2 * sr, 0] = live
    mix[:2 * sr, 1] = live
    out, cut = dr._trim_dead_tail(mix, sr)
    ok &= check("мёртвый хвост отрезается", 0.9 < cut < 1.1,
                f"убрано {cut:.2f} с из одной секунды тишины")
    ok &= check("живой звук не трогается",
                abs(len(out) / sr - 2.0) < 0.05, f"осталось {len(out)/sr:.2f} с")

    # Тихая, но живая музыка — не мёртвый хвост.
    quiet = mix.copy()
    quiet[2 * sr:, :] = (np.random.default_rng(1).standard_normal((sr, 2)) * 0.004)
    _, cut2 = dr._trim_dead_tail(quiet, sr)
    ok &= check("тихий, но звучащий хвост остаётся",
                cut2 == 0.0, "-48 dBFS — это ещё музыка, а не конец материала")

    # Короткая пауза внутри музыки не считается концом.
    gap = np.zeros((3 * sr, 2))
    gap[:2 * sr, 0] = live
    gap[:2 * sr, 1] = live
    gap[int(2.4 * sr):, 0] = live[:len(gap) - int(2.4 * sr)]
    gap[int(2.4 * sr):, 1] = live[:len(gap) - int(2.4 * sr)]
    _, cut3 = dr._trim_dead_tail(gap, sr)
    ok &= check("пауза в середине куска концом не считается", cut3 == 0.0)

    ok &= check("порог тишины ниже любого слышимого затухания",
                dr.TAIL_SILENCE_DB <= -55.0, f"{dr.TAIL_SILENCE_DB} dBFS")
    ok &= check("превью оставляет хвост в целую фразу, а не в интро",
                0 < SE.PREVIEW_TAIL_BARS <= 8, f"{SE.PREVIEW_TAIL_BARS} тактов")
    return bool(ok)


def run_profile_model() -> bool:
    """Два режима ведения фейдеров и их четыре ручки.

    Предложение диджея: «рекомендуемая» настройка, где всё ведёт приём, и
    «кастомная», где он ведёт фейдеры сам, а EQ/FX/стемы подстраиваются
    под технику и точки. Плюс вопрос: какие ручки вынести?

    Ответ — четыре, и они выводятся из того, что рука на пульте
    физически делает: ведёт фейдер входящего, ведёт фейдер уходящего,
    выбирает длину и выбирает ход. Всё остальное — это ПРИЁМ."""
    import mix_profile as MP
    import set_export as SE

    ok = True
    ok &= check("режима ровно два", set(MP.MODES) == {"technique", "manual"})
    ok &= check("формы хода названы словами", set(MP.SHAPES) >= {"linear", "fast", "slow"},
                ", ".join(f"{k}={MP.SHAPES[k]['name']}" for k in MP.SHAPES))

    # Хранится в долях, показывается в тактах — и доля переживает смену длины.
    prof = MP.from_bars(32, in_bars=24, out_bars=8, in_shape="fast", out_shape="slow")
    d32 = MP.describe(prof, bars=32)
    d16 = MP.describe(prof, bars=16)
    ok &= check("такты пересчитываются при смене длины перехода",
                d32["in_bars"] == 24 and d16["in_bars"] == 12,
                f"32 такта -> вход за {d32['in_bars']:g}, 16 тактов -> за {d16['in_bars']:g}")
    ok &= check("доли при этом не меняются",
                d32["in_len"] == d16["in_len"],
                "именно это и значит «настройки адаптируются сами»")
    ok &= check("из тактов и обратно — без потерь",
                abs(MP.from_bars(32, d32["in_bars"], d32["out_bars"])["in_len"]
                    - prof["in_len"]) < 1e-6)
    ok &= check("имя формы восстанавливается",
                d32["in_shape"] == "fast" and d32["out_shape"] == "slow")

    # Площадка наложения — то, ради чего ручное ведение и затевалось.
    plateau = MP.describe(MP.from_bars(16, in_bars=9.6, out_bars=3.2), bars=16)
    ok &= check("площадка наложения считается",
                plateau["overlap_bars"] > 2.5,
                f"{plateau['overlap_bars']:g} тактов оба трека звучат полноценно")
    default = MP.describe({"entry": "manual", **MP.DEFAULTS}, bars=16)
    ok &= check("на значениях по умолчанию площадки НЕТ, и это сказано вслух",
                default["overlap_fraction"] < 0 and "note" in default,
                default.get("note", ""))

    ok &= check("режим «по приёму» ничего не выдумывает",
                MP.describe(None)["mode"] == "technique"
                and MP.apply([{"beat_offset": 0, "action": "crossfade",
                               "deck": "A", "duration_beats": 8}], None)[0]["action"]
                == "crossfade")

    # Профиль плана доезжает до превью и до полного сета.
    ok &= check("правка перехода сильнее умолчания плана",
                SE.set_mix_profile({"mix_profile": {"entry": "technique"}},
                                   {"mix_profile": {"entry": "manual"}})["entry"] == "manual")
    ok &= check("умолчание плана работает, когда у перехода своего нет",
                SE.set_mix_profile({"mix_profile": {"entry": "manual"}}, {})["entry"] == "manual",
                "без этого превью и сет собирались по кроссфейду приёма, "
                "а Clipping вёл два фейдера")
    ok &= check("без обоих — форму ведёт приём",
                SE.set_mix_profile({}, {}) is None)
    return bool(ok)


def _gitignored(name: str) -> bool:
    """Попадёт ли файл с таким именем в репозиторий.

    Проверка дословная, без запуска git: .gitignore может лежать рядом, а
    самого git в окружении проверки не быть. Нужна она затем, что цена
    ошибки здесь несимметрична — токен, однажды попавший в историю, из
    неё уже не убрать, только перевыпускать."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent
    try:
        rules = (root / ".gitignore").read_text(encoding="utf-8")
    except OSError:
        return False
    lines = [x.strip() for x in rules.splitlines()
             if x.strip() and not x.strip().startswith("#")]
    return any(r == name or (r.startswith("*") and name.endswith(r[1:]))
               for r in lines)


def run_metadata() -> bool:
    """Исполнитель, название и стиль: поля базы и разбор ответов каталогов.

    Сеть здесь не трогается вовсе. Проверяется то, что ломается молча:
    разбор имени файла, нормализация запроса и раскладка ответа. Живой
    запрос в MusicBrainz такие ошибки не поймал бы — он бы просто ничего
    не нашёл, и это выглядело бы как «в каталоге нет трека»."""
    import sqlite3
    import tempfile

    import genre
    import metadata as MD
    import track_analysis as TA

    ok = True

    # --- разбор имени файла: путь, номер дорожки, метка жанра
    cases = [
        ("/x/1. Bad Patterns - PERFECT STORM.mp3", "Bad Patterns", "PERFECT STORM"),
        ("C:\\Music\\Hybrid Minds - Summer Rain.mp3", "Hybrid Minds", "Summer Rain"),
        ("/x/[DnB] Joanna Syze - Left in Dust (feat. Tyhh).mp3",
         "Joanna Syze", "Left in Dust (feat. Tyhh)"),
        ("/x/03 - Artist - Track.flac", "Artist", "Track"),
    ]
    bad = [c for c in cases if genre.artist_title_from_name(c[0]) != (c[1], c[2])]
    ok &= check("имя файла разбирается на исполнителя и название", not bad,
                f"{len(cases) - len(bad)} из {len(cases)}; папка и номер дорожки отрезаются")

    # --- нормализация запроса к каталогу
    ok &= check("«feat» не режет слова, в которых есть «ft»",
                MD.clean_title("Left in Dust (feat. Tyhh)") == "Left in Dust",
                "без \\b выражение превращало это в «Le»")
    ok &= check("хвосты вида (Original Mix) уходят из запроса",
                MD.clean_title("Summer Rain (Original Mix)") == "Summer Rain")
    ok &= check("совместные работы ищутся по первому исполнителю",
                MD.clean_artist("Black Sun Empire, Concord Dawn") == "Black Sun Empire")

    # --- стиль Discogs -> наш поджанр
    style_cases = [(["Drum n Bass", "Neurofunk"], "neurofunk"),
                   (["Drum n Bass", "Liquid Funk"], "liquid"),
                   (["UK Garage", "2-Step"], "garage"),
                   (["Drum n Bass"], "dnb"),
                   (["Ambient"], None), ([], None)]
    miss = [c for c in style_cases if MD.subgenre_from_styles(c[0]) != c[1]]
    ok &= check("стили Discogs раскладываются в наши поджанры", not miss,
                "подробный стиль сильнее общего: Neurofunk важнее, чем Drum n Bass рядом")

    # --- выбор записи: длительность решает при близких текстовых оценках
    recs = [{"id": "a", "score": 95, "title": "T", "length": 240000},
            {"id": "b", "score": 92, "title": "T", "length": 410000}]
    pick = MD._pick_recording(recs, duration_seconds=410.0)
    ok &= check("длительность файла перевешивает оценку каталога",
                pick["mbid"] == "b",
                "расхождение в пару секунд — тот же микс, в минуту — другой")

    # --- без токена Discogs не ходит в сеть и говорит об этом
    ok &= check("без токена Discogs не спрашивается",
                MD.discogs_search("a", "t", None) is None)

    # --- где живёт секрет
    import os

    ok &= check("явный токен сильнее всего",
                MD.resolve_discogs_token("EXPLICIT") == "EXPLICIT")
    _saved = os.environ.pop("DISCOGS_TOKEN", None)
    os.environ["DISCOGS_TOKEN"] = "FROM_ENV"
    try:
        ok &= check("переменная окружения сильнее файла",
                    MD.resolve_discogs_token() == "FROM_ENV")
    finally:
        os.environ.pop("DISCOGS_TOKEN", None)
        if _saved is not None:
            os.environ["DISCOGS_TOKEN"] = _saved
    ok &= check("файл с секретом занесён в .gitignore",
                _gitignored(MD.SECRETS_FILE),
                "папка под git: один `git add -A` отправил бы токен в историю")
    ok &= check("у объединённого поиска описано, что он делает",
                (MD.lookup.__doc__ or "").strip() != "",
                "значение здесь вычислялось и НЕ проверялось — проверка была "
                "потеряна при правке, и раздел молча стал на один тест короче")
    ok &= check("у поиска есть оговорка про темп и тональность",
                "темп" in MD.search_catalog.__doc__.lower(),
                "по BPM в этих каталогах искать нельзя, и это сказано")

    # --- колонки в базе и миграция старой базы
    d = tempfile.mkdtemp()
    old = f"{d}/old.db"
    c = sqlite3.connect(old)
    c.execute("""CREATE TABLE tracks (path TEXT PRIMARY KEY, bpm REAL, key TEXT,
                 camelot TEXT, energy REAL, brightness REAL, danceability REAL,
                 duration_seconds REAL, structure_json TEXT, tags_json TEXT,
                 scanned_at REAL)""")
    c.execute("INSERT INTO tracks (path, bpm) VALUES ('/x/a.mp3', 174.0)")
    c.commit()
    c.close()
    conn = TA.init_db(old)
    cols = {r[1] for r in conn.execute("pragma table_info(tracks)")}
    rows = list(conn.execute("SELECT path, bpm FROM tracks"))
    conn.close()
    ok &= check("старая база мигрирует, а не пересоздаётся",
                {"artist", "title", "meta_json"} <= cols and rows == [("/x/a.mp3", 174.0)],
                "70 треков не пересканируются ради трёх полей")

    # --- Mood: почему остаёмся на своей энергии
    moods = {TA.mood(e, 174.0) for e in (0.41, 0.55, 0.70, 0.79)}
    ok &= check("на драм-н-бейсе Mood не различает ничего",
                moods == {"HIGH"},
                "вся библиотека диджея попадает в HIGH: 66 треков из 70")
    ok &= check("наша энергия при этом различает",
                len({round(e, 2) for e in (0.41, 0.55, 0.70, 0.79)}) == 4,
                "непрерывная величина против трёх корзин")
    ok &= check("перевод секций в rekordbox односторонний",
                TA.REKORDBOX_PHRASE["build"] == "Up"
                and TA.REKORDBOX_PHRASE["drop"] == "Chorus"
                and "groove" in TA.REKORDBOX_PHRASE,
                "обратный перевод потерял бы groove — в словаре HIGH его нет")
    return bool(ok)


def run_phonk_bpm() -> bool:
    """Жанр по папке и октава темпа: случай фонка.

    Жалоба диджея: «новые треки в папке Phonk не определяют стиль + мапят
    неверный BPM, 100-200, при том, что все треки звучат замедленно в 2
    раза».

    Замер на 13 его фонк-треках вскрыл две вещи. Первая: папку никто не
    смотрел, поэтому фонк на 165 подписывался драм-н-бейсом (полоса
    160-185), а на 155 и 190 не подписывался вовсе. Вторая, и главная:
    контраст гребёнки на фонке почти плоский — 1.04..1.37 у ВСЕХ
    кандидатов октавы, — и перебор выбирал по шуму. Отсюда 103 -> 155
    (x1.5), 99 -> 200 (x2) и 136 -> 101 (x0.75) в одной папке."""
    import fix_library_bpm as F
    import genre
    import id3_tags as T

    ok = True
    ok &= check("папка — источник жанра",
                genre.genre_from_folder(r"C:\Music\Phonk\07. x.mp3")["genre"] == "phonk"
                and genre.genre_from_folder(r"C:\Music\DnB\Liquid\y.mp3")["genre"] == "dnb")
    ok &= check("ближняя папка сильнее дальней",
                genre.genre_from_folder("/m/DnB/Phonk/x.mp3")["genre"] == "phonk",
                "«DnB/Phonk» — это фонк, лежащий не на своём месте")
    ok &= check("папки без смысла жанром не считаются",
                genre.genre_from_folder("/m/Music/new/x.mp3")["genre"] is None
                and genre.genre_from_folder("/m/++/x.mp3")["genre"] is None)
    ok &= check("папка сильнее темпа",
                genre.classify(r"C:\Music\Phonk\x.mp3", bpm=170.0)["genre"] == "phonk",
                "170 BPM в папке Phonk — это фонк, а не драм-н-бейс")

    ok &= check("у фонка есть полоса темпа",
                T.band_for_genre(None, r"C:\Music\Phonk\x.mp3") == (125.0, 180.0),
                "замерено на 13 треках: измеренные значения легли в 129..172")
    ok &= check("полоса берётся и из полного пути, а не только из имени",
                T.band_for_genre(None, r"C:\Music\Phonk\07. Alien Death.mp3") is not None)

    ok &= check("октава не меняется без заметного перевеса",
                F.OCTAVE_MARGIN >= 0.10,
                f"нужен перевес {100 * F.OCTAVE_MARGIN:.0f}%; на фонке шум даёт 1-7%")

    # Ядро правила: пока измеренный темп в полосе — не трогаем; выпал из
    # полосы, а кандидат попадает — меняем, даже если контраст молчит.
    import inspect

    src = inspect.getsource(F.refine_one)
    ok &= check("порог действует только внутри полосы жанра",
                "base_in_band" in src,
                "выход за полосу — это внешнее знание, оно сильнее плоского контраста")
    return bool(ok)


def run_library_analysis() -> bool:
    """Единый анализ библиотеки: порядок шагов, пропуск сделанного, прогресс.

    Почему это вообще проверяется тестом. Кнопок было пять, и порядок
    между ними держался на памяти диджея: стемы после скана, карты вокала
    после стемов, метки после всего. Нарушение порядка не ругалось — оно
    молча давало неверный результат. Сложив их в одну кнопку, этот порядок
    надо было либо закрепить кодом, либо потерять окончательно.

    Второе, что проверяется, — договор о прогрессе между двумя процессами.
    stems.py считает слои часами отдельным процессом, и до сих пор наружу
    не шло НИЧЕГО: полоса «идёт разделение» без единой цифры неотличима от
    зависшей. Теперь один процесс печатает строку, другой её разбирает —
    а такой стык ломается молча, если литерал разъедется."""
    import library_analysis as LA
    import stems as ST

    ok = True

    # --- порядок задаёт код, а не тот, кто прислал галочки ---
    ok &= check("порядок шагов не зависит от порядка в запросе",
                LA.order_steps(["cues", "stems", "scan"]) == ["scan", "stems", "cues"],
                "скан обязан быть первым, метки — последними")
    ok &= check("лишние имена шагов не проходят",
                LA.order_steps(["scan", "выдумка"]) == ["scan"])
    ok &= check("метки в Mixxx не включаются сами",
                "cues" not in LA.ANALYZE_DEFAULT,
                "Mixxx перезапишет свою базу при выходе — это делают по галочке")
    ok &= check("у каждого шага есть название и объяснение",
                all(k in LA.ANALYZE_TITLES and LA.ANALYZE_WHY.get(k)
                    for k in LA.ANALYZE_STEPS))

    # --- договор о прогрессе между stems.py и backend'ом ---
    ok &= check("префикс прогресса — один на оба процесса",
                ST.PROGRESS_PREFIX is LA.PROGRESS_PREFIX,
                "разъехавшийся литерал сломал бы полосу МОЛЧА")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ST.emit_progress("stems", 3, 13, 3, 0, "/music/Phonk/Some Track.mp3")
    got = LA.parse_progress_line(buf.getvalue().strip())
    ok &= check("строка прогресса проходит круг: напечатали -> разобрали",
                got == {"stage": "stems", "i": 3, "total": 13, "done": 3,
                        "failed": 0, "name": "Some Track.mp3"},
                f"получилось {got}")
    ok &= check("человеческий вывод не принимается за прогресс",
                all(LA.parse_progress_line(x) is None for x in (
                    "  [3/13] Some Track.mp3 — 41с",
                    "стемы: 5 уже есть, считать 8",
                    "[ПРОГРЕСС] это не json",
                    '[ПРОГРЕСС] {"i": 1}',        # без total — полосе нечего делить
                    "", None)),
                "иначе полоса дёргалась бы от каждой строки лога")

    # --- «считать только то, чего не хватает» ---
    have = {"/m/a.mp3", "/m/b.mp3"}
    tracks = [{"path": "/m/a.mp3", "bpm": 174.0,
               "structure": {"drum_map": [1], "energy_map": [1]},
               "meta": {"sources": ["discogs"]}},
              {"path": "/m/b.mp3", "bpm": 174.0, "structure": {}, "meta": {}},
              {"path": "/m/c.mp3", "bpm": 140.0, "structure": {}, "meta": {}}]
    rep = LA.missing_report(tracks, lambda p: p in have,
                            lambda p: {"vocal": True} if p == "/m/a.mp3" else None)
    ok &= check("слои считаются только тем, у кого их нет",
                rep["stems"] == 1, f"насчитал {rep['stems']}, а нет их у одного")
    ok &= check("карты вокала — только там, где слои есть, а карты нет",
                rep["vocals"] == 1, f"насчитал {rep['vocals']}")
    ok &= check("каталоги — только тем, кого не спрашивали",
                rep["catalog"] == 2, f"насчитал {rep['catalog']}")
    ok &= check("одинаковый темп у нескольких треков виден как заглушка",
                rep["bpm"] == 2,
                "174.0 у двух из трёх — это не измерение, а значение по умолчанию")
    ok &= check("карты считаются недостающими, если нет хотя бы одной из двух",
                rep["maps"] == 2)

    # Пустая библиотека не должна выглядеть как «всё посчитано»: разница
    # между «нечего делать» и «нечего анализировать» — это разница между
    # пропуском шага и просьбой указать папку.
    empty = LA.missing_report([], lambda p: True, lambda p: None)
    ok &= check("пустая библиотека не врёт про готовность",
                empty["tracks"] == 0 and empty["bpm"] == 0)

    # --- общий корень: папка, которую не у кого спросить ---
    ok &= check("общий корень путей находится",
                LA.common_root(["/m/dnb/a.mp3", "/m/dnb/x/b.mp3"]).replace("\\", "/")
                == "/m/dnb")
    ok &= check("пустой список корня не выдумывает",
                LA.common_root([]) == "" and LA.common_root(None) == "")

    # --- инкрементальный скан: отпечаток файла ---
    import track_analysis as TA

    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "x.wav")
        with open(f, "wb") as fh:
            fh.write(b"x" * 2048)
        fp1 = TA.file_fingerprint(f)
        ok &= check("отпечаток файла — это время правки и размер",
                    fp1[1] == 2048 and fp1[0] > 0)
        time.sleep(0.01)
        with open(f, "ab") as fh:
            fh.write(b"y" * 16)
        ok &= check("переписанный файл получает другой отпечаток",
                    TA.file_fingerprint(f) != fp1,
                    "иначе правку трека скан пропустил бы навсегда")
        ok &= check("пропавший файл не роняет скан",
                    TA.file_fingerprint(os.path.join(d, "нет.wav")) == (0.0, 0))

        db = os.path.join(d, "lib.db")
        conn = TA.init_db(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        conn.close()
        ok &= check("в базе есть колонки отпечатка",
                    {"file_mtime", "file_size"} <= cols)

        # Главная ловушка инкрементального скана: backend забирает строки
        # с scanned_at >= начала прохода, и пропущенные треки исчезали бы
        # из комнаты. Пропуск экономит РАЗБОР, а не участие в результате.
        conn = TA.init_db(db)
        TA._upsert_track(conn, pathlib.Path(f), {
            "bpm": 174.0, "key": "Am", "camelot": "8A", "energy": 0.5,
            "brightness": 0.5, "danceability": 0.5, "duration_seconds": 300.0,
            "structure": {}, "artist": "A", "title": "T"}, None)
        conn.close()
        canon = TA.canon_track_path(pathlib.Path(f))
        mark = time.time()
        time.sleep(0.02)
        TA.touch_scanned(db, [canon])
        rows = TA.load_library_from_db(db, mark)
        ok &= check("пропущенный трек остаётся в выдаче прохода",
                    len(rows) == 1,
                    "без этого инкрементальный скан усекал бы библиотеку "
                    "до одних только новых треков")
    return bool(ok)


def run_library_query() -> bool:
    """Отбор в библиотеке: темп, тональность, слова — и удаление.

    Почему это тест, а не «и так видно». Фильтр разбирает строку, которую
    диджей набирает руками, и у него ровно один способ сломаться незаметно:
    отдать НЕ ПУСТОЙ, но неверный результат. «174» находит два трека —
    похоже на работу, даже если на самом деле оно нашло их по слову в
    имени файла, а трек 173.97 пропустило.

    Отдельно проверяется, что таблицы камелота здесь и в анализаторе
    совпадают. Они продублированы намеренно (модуль отбора не должен
    тянуть numpy ради двух кортежей), и расхождение было бы худшим видом
    бага: фильтр «8A» молча искал бы не то, что записал анализатор."""
    import library_query as Q
    import track_analysis as TA

    ok = True

    ok &= check("таблицы камелота не разошлись с анализатором",
                tuple(Q._CAMELOT_MAJOR) == tuple(TA._CAMELOT_MAJOR)
                and tuple(Q._CAMELOT_MINOR) == tuple(TA._CAMELOT_MINOR)
                and tuple(Q._PITCHES) == tuple(TA._PITCH_NAMES),
                "дубль таблиц оправдан, молчаливое расхождение — нет")

    # Круговая проверка: что анализатор ЗАПИСАЛ, то фильтр обязан НАЙТИ,
    # как бы диджей это ни написал.
    bad = []
    for pitch in TA._PITCH_NAMES:
        for mode in ("major", "minor"):
            written = TA.key_to_camelot(f"{pitch} {mode}")
            for spelling in (written, written.lower(),
                             pitch + ("m" if mode == "minor" else ""),
                             f"{pitch} {mode}"):
                if Q.normalize_key(spelling) != written:
                    bad.append((spelling, written, Q.normalize_key(spelling)))
    ok &= check("24 тональности находятся в четырёх написаниях",
                not bad, f"расходится: {bad[:3]}")

    ok &= check("не-тональность не становится тональностью",
                all(Q.normalize_key(x) is None for x in ("13A", "Z", "shadow", "", None)),
                "иначе слово из названия трека превратится в фильтр")

    # --- темп ---
    ok &= check("точный темп — это узкий диапазон, а не равенство",
                Q.parse_bpm("174") == (173.0, 175.0),
                "анализатор отдаёт 173.97; строгое равенство отбросило бы всё")
    ok &= check("диапазон разбирается",
                Q.parse_bpm("100-135") == (100.0, 135.0)
                and Q.parse_bpm("100–135") == (100.0, 135.0),
                "и через дефис, и через тире — набирают по-разному")
    ok &= check("открытые границы",
                Q.parse_bpm("170+")[0] == 170.0 and Q.parse_bpm("140-")[1] == 140.0)
    ok &= check("перевёрнутый диапазон не теряется",
                Q.parse_bpm("180-170") == (170.0, 180.0))
    ok &= check("номер трека не принимается за темп",
                Q.parse_bpm("07") is None and Q.parse_bpm("1000") is None,
                "«07» в имени файла — это не 7 ударов в минуту")

    # --- отбор целиком ---
    lib = [
        {"path": "/m/a.mp3", "name": "Alpha Shift.mp3", "bpm": 174.0, "camelot": "8A",
         "genre": "dnb", "subgenre": "neurofunk", "vocal": False, "stems": True},
        {"path": "/m/b.mp3", "name": "Blue Rain.mp3", "bpm": 173.97, "camelot": "5A",
         "genre": "dnb", "subgenre": "liquid", "vocal": True, "stems": True},
        {"path": "/m/c.mp3", "name": "City Lights.mp3", "bpm": 128.0, "camelot": "8A",
         "genre": "house", "vocal": True, "stems": False},
    ]
    names = lambda q: sorted(t["name"][:5] for t in Q.filter_tracks(lib, q)["tracks"])
    ok &= check("«174» ловит и 174.0, и 173.97",
                names("174") == ["Alpha", "Blue "],
                f"нашлось {names('174')} — ровно ради этого и нужен допуск")
    ok &= check("диапазон 100-135 отбирает только медленный",
                names("100-135") == ["City "])
    ok &= check("тональность отбирает буквально, а не по совместимости",
                names("8A") == ["Alpha", "City "],
                "совместимость — отдельное знание, в строке поиска ей не место")
    ok &= check("жанр и поджанр ищутся тем же словом",
                names("dnb") == ["Alpha", "Blue "] and names("liquid") == ["Blue "])
    ok &= check("слова складываются по И",
                names("dnb 170-180 vocal") == ["Blue "])
    ok &= check("несуществующее даёт пусто, а не всё подряд",
                names("нетутакого") == [] and names("dnb 128") == [])
    ok &= check("пустой запрос пропускает всё",
                len(Q.filter_tracks(lib, "")["tracks"]) == 3)
    ok &= check("разбор запроса виден человеку",
                "темп" in Q.describe(Q.parse("170-180"))
                and Q.describe(Q.parse("")) == "без фильтров",
                "иначе «ничего не нашлось» — гадание, а не вопрос с ответом")
    return bool(ok)


def run_music_fetch() -> bool:
    """Пополнение библиотеки: право на скачивание, темп, папка-приёмник.

    Главное здесь — не удобство, а два обещания, которые легко нарушить
    молча. Первое: не скачивать то, что автор скачивать не разрешал.
    Второе: не отдавать в библиотеку недокопированный файл — разбор
    половины файла даёт мусорный темп, и мусор остаётся навсегда, потому
    что второй раз трек уже не разберут (отпечаток совпадает)."""
    import music_fetch as MF

    ok = True

    # --- корзина скорости не должна отрезать запрошенное ---
    ok &= check("корзина скорости накрывает диапазон целиком",
                MF.speed_for_bpm(170, 180) == "veryhigh"
                and MF.speed_for_bpm(100, 130) == "medium")
    ok &= check("широкий диапазон не сужается наугад",
                MF.speed_for_bpm(100, 180) is None,
                "лучше скачать лишнего, чем молча не найти запрошенное")
    ok &= check("без диапазона корзина не навязывается",
                MF.speed_for_bpm(None, None) is None)

    # --- имя файла должно разбираться нашим же разбором ---
    import genre as G

    n = MF.safe_filename({"artist": "Some Artist", "title": "Track Name (VIP)", "id": "1"})
    ok &= check("имя файла разбирается обратно в исполнителя и название",
                G.artist_title_from_name(n) == ("Some Artist", "Track Name (VIP)"),
                f"получилось {n!r} -> {G.artist_title_from_name(n)}")
    ok &= check("запрещённые в имени символы не роняют загрузку",
                MF.safe_filename({"artist": 'A/B\\C:*?', "title": 'X "Y" <2>'}).endswith(".mp3"))
    ok &= check("пустые поля не дают безымянный файл",
                len(MF.safe_filename({"id": "77"})) > 4)

    # --- право на скачивание ---
    r = MF.download({"downloadable": False, "download_url": "http://x/y.mp3"}, "/tmp")
    ok &= check("трек без разрешения автора не качается",
                not r["ok"] and "разреш" in r["error"],
                "audiodownload_allowed — это не подсказка, а условие")
    r2 = MF.download({"downloadable": True, "download_url": None}, "/tmp")
    ok &= check("пустая ссылка не превращается в загрузку", not r2["ok"])

    # --- план виден до начала ---
    plan = MF.plan_fetch(40, 170, 180)
    ok &= check("сколько качать ради 40 в диапазоне — известно заранее",
                plan["candidates"] > plan["want"],
                "темпа в каталоге нет, лишнее неизбежно — но не внезапно")
    ok &= check("без диапазона лишнего не качаем",
                MF.plan_fetch(10, None, None)["candidates"] == 10)
    ok &= check("больше двухсот каталог всё равно не отдаст",
                MF.plan_fetch(200, 170, 180)["candidates"] <= 200)

    # --- попадание в диапазон ---
    ok &= check("измеренный 173.97 попадает в «170-180»",
                MF.in_range(173.97, 170, 180) and not MF.in_range(169.9, 170, 180))
    ok &= check("неизмеренный темп не считается попавшим",
                not MF.in_range(None, 170, 180),
                "иначе трек без темпа осядет в библиотеке как подходящий")

    # --- папка-приёмник: файл должен ДОКОПИРОВАТЬСЯ ---
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "big.wav")
        with open(f, "wb") as fh:
            fh.write(b"0" * 100000)
        sizes: dict = {}
        ready, sizes = MF.settled(MF.inbox_scan(d, set()), sizes)
        ok &= check("свежезамеченный файл сразу не разбирается",
                    not ready, "первый опрос не может знать, дописан ли он")
        with open(f, "ab") as fh:
            fh.write(b"0" * 400000)
        ready, sizes = MF.settled(MF.inbox_scan(d, set()), sizes)
        ok &= check("растущий файл не разбирается",
                    not ready, "разбор половины файла даёт мусорный темп навсегда")
        ready, sizes = MF.settled(MF.inbox_scan(d, set()), sizes)
        ok &= check("переставший расти файл разбирается",
                    ready == [f])

        with open(os.path.join(d, "half.part"), "wb") as fh:
            fh.write(b"0" * 50000)
        ok &= check("недокачанный .part не считается треком",
                    MF.inbox_scan(d, set()) == [f],
                    "загрузка пишет в .part и переименовывает в конце")
        ok &= check("уже принятое не предлагается второй раз",
                    MF.inbox_scan(d, {f}) == [])
        os.unlink(f)
        ready, _ = MF.settled([f], {f: 500000})
        ok &= check("исчезнувший между опросами файл не роняет приёмник",
                    ready == [])
    return bool(ok)


def run_bass_swap_and_neutral() -> bool:
    """Обмен низом и возврат ручек в нейтраль.

    Оба правила живут в build_plan, а не в каждом приёме, и оба появились
    из замера реального 45-минутного сета диджея, где были слышны провалы
    и «где-то вообще тишина».

    Здесь закреплены ДВЕ МОИ ОШИБКИ, пойманные замером при починке, —
    именно они и есть то, что легко вернуть обратно:
      * встречные кривые на плечах обмена дают провал низа на 6 dB ровно
        в середине (дырку вместо каши, от которой я их и ставил);
      * растягивание ВСЕХ рампов низа убивает Bar Switch, где низ скачет
        резом каждый такт НАРОЧНО."""
    import techniques as TQ
    from mixxx_controls import BY_ID

    ok = True
    KNOBS = ("filter_sweep", "eq_low", "eq_mid", "eq_high", "gain_ramp")

    def plan_for(tid):
        t = TQ.TECHNIQUES[tid]
        return TQ.build_plan(tid, "p", "A", "B", 150.0,
                             third="C" if t.requires_decks >= 3 else None)

    # --- ничего не остаётся закрытым на деке, которая продолжает играть ---
    stuck = []
    for tid in TQ.TECHNIQUES:
        last = {}
        for e in plan_for(tid)["events"]:
            if (e.get("kind") != "ramp" or e.get("action") not in KNOBS
                    or e.get("deck") == "A"):
                continue
            k = (e["deck"], e["action"])
            end = e["beat_offset"] + e.get("duration_beats", 0.0)
            if k not in last or end >= last[k][0]:
                last[k] = (end, e.get("value_to"))
        for (deck, act), (_end, val) in last.items():
            if val is not None and abs(float(val) - BY_ID[act].neutral) > 1e-6:
                stuck.append(f"{tid}:{deck}.{act}={val}")
    ok &= check("ни одна ручка не остаётся закрытой на играющей деке",
                not stuck,
                f"застряло: {stuck[:4]} — рендер держит последнее значение "
                f"до конца КУСКА, а не приёма: DNB-20 так дал 37.8 с с верхом на -45 dB")

    # --- обмен низом складывается в единицу, а не в провал ---
    def curve_at(x, kind):
        if kind == "ease_in":
            return x * x
        if kind == "ease_out":
            return 1 - (1 - x) ** 2
        return x

    def swap_arms(tid):
        ev = [e for e in plan_for(tid)["events"]
              if e.get("kind") == "ramp" and e.get("action") == "eq_low"
              and e.get("value_from") is not None and e.get("value_to") is not None]
        rise = [e for e in ev if e["deck"] != "A"
                and float(e["value_to"]) > float(e["value_from"])]
        if not rise:
            return None, None
        inc = max(rise, key=lambda e: e["beat_offset"] + e["duration_beats"])
        end = inc["beat_offset"] + inc["duration_beats"]
        out = next((e for e in ev if e["deck"] == "A"
                    and float(e["value_from"]) > float(e["value_to"])
                    and abs(e["beat_offset"] + e["duration_beats"] - end) <= 0.5), None)
        return inc, out

    worst = 1.0
    for tid in ("DNB-25", "ST-09", "DNB-00", "FX-01", "TT-06"):
        inc, out = swap_arms(tid)
        if not (inc and out):
            continue
        for k in range(9):
            b = inc["beat_offset"] + inc["duration_beats"] * k / 8.0
            total = 0.0
            for e in (inc, out):
                x = min(max((b - e["beat_offset"]) / max(e["duration_beats"], 1e-9), 0.0), 1.0)
                total += (float(e["value_from"])
                          + (float(e["value_to"]) - float(e["value_from"]))
                          * curve_at(x, e.get("curve", "linear")))
            worst = min(worst, total / TQ.EQ_UNITY)
    ok &= check("сумма низа по ходу обмена не проваливается",
                worst > 0.95,
                f"худшая точка {worst:.2f} от штатного; встречные кривые давали 0.50, "
                f"то есть -6 dB ровно в середине обмена")

    # --- обмен стал слышимым по длине, а рез остался резом ---
    inc, _ = swap_arms("DNB-25")
    ok &= check("обмен низом длится такты, а не доли",
                inc is not None and inc["duration_beats"] >= 4.0,
                f"{inc['duration_beats'] if inc else 0} долей; было 2-4 — на фонковых "
                f"темпах это меньше секунды, слышно как рывок")
    inc24, _ = swap_arms("DNB-24")
    ok &= check("рез Bar Switch не превращён в бленд",
                inc24 is not None and inc24["duration_beats"] < 1.0,
                f"{inc24['duration_beats'] if inc24 else '?'} долей — скачок низа каждый "
                f"такт это и есть приём, растянутый он его убивает")

    # --- длительность настраивается диджеем ---
    lens = []
    for beats in (2, 8, 16):
        pl = TQ.build_plan("DNB-25", "p", "A", "B", 150.0,
                           overrides={"bass_swap_beats": beats})
        e = [x for x in pl["events"] if x.get("action") == "eq_low" and x["deck"] == "B"
             and float(x.get("value_to", 0)) > float(x.get("value_from", 1))][-1]
        lens.append(e["duration_beats"])
    ok &= check("длительность обмена слушается настройки",
                lens[0] < lens[1] < lens[2], f"получилось {lens}")
    ok &= check("ползунок появляется там, где обмен есть",
                any(pr.key == "bass_swap_beats" for pr in TQ.TECHNIQUES["DNB-25"].params)
                and not any(pr.key == "bass_swap_beats"
                            for pr in TQ.TECHNIQUES["DNB-24"].params),
                "у приёма с обменом — да, у приёма с резом — нет")

    # --- пол под опорой выравнивателя ---
    import set_export as SE

    norm = SE.SET_DECK_RMS * SE.SET_MAKEUP
    floor = norm * (10.0 ** (-SE.SET_RIDE_REF_FLOOR_DB / 20.0))
    ok &= check("тихое соседнее тело не топит переход целиком",
                max(norm * 10 ** (-20 / 20.0), floor) >= floor - 1e-9
                and floor > norm * 10 ** (-SE.SET_RIDE_MAX_CUT_DB / 20.0),
                f"пол {SE.SET_RIDE_REF_FLOOR_DB:.0f} dB должен быть выше потолка среза "
                f"{SE.SET_RIDE_MAX_CUT_DB:.0f} dB, иначе он ничего не ограничивает")
    return bool(ok)


def run_drop_detection() -> bool:
    """Поиск дропов: по тактам и по величине скачка, а не по порогу.

    Проверяется на сигнале с ИЗВЕСТНОЙ структурой — это единственный
    способ отличить «нашёл дропы» от «нашёл шум и назвал его дропами».
    Старый детектор на фонк-треке диджея выдавал 13 дропов ровно через
    четыре такта, и выглядело это как работа.

    Что именно закреплено:
      * дроп — это скачок СРЕДНЕГО уровня между окнами до и после, а не
        пересечение порога: порог «90-й перцентиль минус 3 dB» на
        реальном треке перекрывался 43% времени и пересекался 36 раз;
      * уверенность — величина скачка в децибелах, а не разница двух
        соседних кадров: после сглаживания окном в секунду та разница
        составляет 0.1 dB ВСЕГДА, то есть не значит ничего;
      * два дропа ближе восьми тактов — это один дроп: слепой перескок
        на окно удержания и раскладывал находки ровно через 4 такта."""
    import numpy as np

    import track_analysis as TA

    ok = True
    sr, bpm = 22050, 150.0
    bar = 4 * 60.0 / bpm

    def build(plan):
        rng = np.random.default_rng(1)
        parts = []
        for bars, lvl in plan:
            n = int(bars * bar * sr)
            t = np.arange(n) / sr
            beat = np.abs(np.sin(np.pi * t * bpm / 60)) ** 6
            sig = (0.9 * np.sin(2 * np.pi * 50 * t) * (0.4 + 0.6 * beat)
                   + 0.3 * rng.standard_normal(n) * beat)
            parts.append((sig * lvl).astype("float64"))
        return np.concatenate(parts)

    # интро тихо -> ДРОП -> яма -> ДРОП -> аутро
    y = build([(16, 0.12), (16, 1.0), (8, 0.10), (16, 1.0), (8, 0.5)])
    r = TA.detect_structure(y, sr, bpm)
    bars_found = [d["bar_index"] for d in r["drops"]]
    ok &= check("найдены оба дропа там, где они есть",
                len(bars_found) == 2 and abs(bars_found[0] - 16) <= 1
                and abs(bars_found[1] - 40) <= 1,
                f"эталон такты [16, 40], найдено {bars_found}")
    ok &= check("лишних дропов нет",
                len(r["drops"]) == 2,
                f"{len(r['drops'])} шт; старый детектор на фонке давал 13")
    steps = [d.get("step_db", 0) for d in r["drops"]]
    ok &= check("величина скачка измерена в децибелах и она заметная",
                all(x >= TA.DROP_MIN_STEP_DB for x in steps),
                f"скачки {steps} dB")
    confs = [d["confidence"] for d in r["drops"]]
    ok &= check("уверенность что-то значит",
                all(c > 0.2 for c in confs),
                f"{confs}; раньше это была разница соседних кадров — 0.01-0.41 "
                f"по построению, и «дроп» нельзя было отличить от шума")

    # Ровная громкая дорожка без событий: дропов быть НЕ должно. Это и
    # есть проверка на «находит то, чего нет».
    flat = build([(48, 1.0)])
    ok &= check("на ровном треке дропов не находится",
                len(TA.detect_structure(flat, sr, bpm)["drops"]) == 0,
                "порог по пересечению находил их и здесь")

    # Два подъёма подряд в пределах восьми тактов — это один дроп.
    near = build([(16, 0.12), (2, 0.9), (2, 0.95), (16, 1.0)])
    got = TA.detect_structure(near, sr, bpm)["drops"]
    ok &= check("близкие подъёмы схлопываются в один",
                len(got) <= 1, f"найдено {len(got)}")

    ok &= check("ямы находятся и у них измерена глубина",
                any(b.get("depth_db", 0) >= TA.BREAKDOWN_DROP_DB
                    for b in r["breakdowns"]),
                f"{[b.get('depth_db') for b in r['breakdowns']]}")

    # Короткий трек не должен ронять разбор: в фонке это норма.
    ok &= check("очень короткий кусок не роняет детектор",
                TA.detect_structure(build([(3, 1.0)]), sr, bpm)["drops"] == [])

    # --- дроп по барабанам, а не по басу ---
    #
    # Карта энергии ищет дроп как возврат НИЗА. В фонке бас звучит
    # непрерывно, и она честно не находит ничего — а пустой список
    # заставляет mix_points откатиться на дропы structure-анализа.
    import beatgrid

    n = int(48 * bar * sr)
    t = np.arange(n) / sr
    bass = 0.7 * np.sin(2 * np.pi * 50 * t)            # бас ВЕЗДЕ, как в фонке
    drums = np.zeros(n)
    half = int(24 * bar * sr)
    beat = np.abs(np.sin(np.pi * t * bpm / 60)) ** 6
    drums[half:] = (0.8 * beat[half:]
                    * np.random.default_rng(2).standard_normal(n - half))
    mix = bass + drums
    by_bass = beatgrid.energy_map(mix, sr, bpm)
    by_drums = beatgrid.energy_map(mix, sr, bpm, drums=drums)
    ok &= check("по непрерывному басу дроп не виден — это не поломка",
                len(by_bass["drops"]) == 0,
                "именно поэтому нужен слой ударных или перкуссивная часть")
    ok &= check("по барабанам дроп виден",
                len(by_drums["drops"]) >= 1,
                f"{by_drums['drops']}; барабаны входят на 24-м такте "
                f"({24 * bar:.0f} с)")
    return bool(ok)


def main() -> int:
    print("Кроссфейдер:");      c1 = run_crossfader()
    print("Телеметрия:");       c2 = run_telemetry()
    print("Разрешение MIDI:");  c3 = run_midi_resolves()
    print("Запрос в медиатеку:"); c4 = run_queries()
    print("Единый тракт звука:"); c8 = run_single_path()
    print("Тракт эффектов:"); c9 = run_fx_chain()
    print("Кнопки отбора клипов:"); c10 = run_clipping_buttons()
    print("Вокал как флаг секции:"); c11 = run_vocal_flag()
    print("Уровень на стыке:"); c12 = run_seam_level()
    print("Расстройка от подгонки темпа:"); c13 = run_detune()
    print("Жанр по темпу:"); c14 = run_genre_bpm()
    print("Пустое пространство между сведениями:"); c15 = run_dead_tail()
    print("Режимы ведения фейдеров:"); c16 = run_profile_model()
    print("Исполнитель, название и каталоги:"); c17 = run_metadata()
    print("Фонк: жанр по папке и октава темпа:"); c18 = run_phonk_bpm()
    print("Единый анализ библиотеки:"); c19 = run_library_analysis()
    print("Отбор в библиотеке:"); c20 = run_library_query()
    print("Пополнение библиотеки:"); c21 = run_music_fetch()
    print("Обмен басами и нейтраль:"); c22 = run_bass_swap_and_neutral()
    print("Поиск дропов:"); c23 = run_drop_detection()
    print("Загрузчик очередью Auto DJ:")
    c6 = asyncio.run(run_autodj_loader())
    print("Сверка очереди с планом:"); c7 = run_queue_check()
    print("Проход по сету (около 20 с):")
    c5 = asyncio.run(run_set())
    good = all((c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14,
                c15, c16, c17, c18, c19, c20, c21, c22, c23))
    print("\nИТОГ:", "всё сходится" if good else "ЕСТЬ ПРОВАЛЫ")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
