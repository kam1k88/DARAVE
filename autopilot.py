"""
autopilot.py — сет играет в самом Mixxx, подряд, без нажатий.

## Зачем

До этого модуля живьём исполнялся ОДИН переход по кнопке «▶ на деках».
План сета при этом существовал в вебе, а Mixxx о нём ничего не знал:
чтобы услышать сет, диджей рендерил его в файл. Файл — это уже готовый
звук, в нём нечего смотреть: не видно, как расходятся волны, где
открывается фейдер и в какой момент уходит низ. А диджей правит сет
именно по этой картинке — «на слух иногда сложно понять, что менять».

Автопилот закрывает разрыв: он идёт по плану сам — грузит треки на деки,
ставит иглу в нужную секунду, ждёт по телеметрии момент ухода и отдаёт
технику companion'у. В Mixxx это выглядит ровно так, как если бы за
пультом стоял человек: две волны, движущийся фейдер, живые ручки.

## Кто чем занят (и почему именно так)

* **Backend (этот модуль)** знает план: какой трек следующий, в какую
  секунду уходить, каким приёмом. Он же видит телеметрию всех дек.
* **Companion** исполняет. Точный тайминг перехода остаётся у него:
  MixPlan уходит целиком, с `anchor_lead_seconds` = «сколько осталось до
  момента ухода», и дальше companion отсчитывает по своим монотонным
  часам (см. scheduler.py). Облако из критического пути исключено —
  иначе доля такта зависела бы от сетевого джиттера.

То есть автопилот НЕ дёргает ручки по одной. Он принимает одно решение
на переход — «пора» — и отдаёт его вниз целым планом.

## Ожидание момента: почему по телеметрии, а не по таймеру

Позиция иглы известна раз в 100 мс (SysEx от скрипта контроллера) и
приезжает в backend раз в 200 мс. Спать «сколько-то секунд» нельзя:
диджей может сам подвинуть иглу, поставить луп, нажать паузу — и таймер
уедет. Поэтому цикл каждые 200 мс пересчитывает «сколько осталось до
секунды ухода» по свежей позиции, и только когда остаётся меньше
SEND_AHEAD_SECONDS, отправляет план с этим остатком в качестве анкера.

## Чередование дек и кроссфейдер

Треки ложатся A, B, A, B… Значит каждый второй переход идёт B->A. Все
техники написаны как «кроссфейд от уходящей деки», и до автопилота живьём
игрался только A->B, поэтому разворот стороны никого не беспокоил.
Теперь он обязателен — сделан в `midi_mapping.crossfader_raw`.
"""
from __future__ import annotations

import asyncio
import secrets
import time

import mix_strategist

# За сколько до момента ухода отдаём план companion'у. Меньше — рискуем
# не успеть доехать по сети; больше — дольше живём с решением, принятым
# по устаревшей позиции иглы.
SEND_AHEAD_SECONDS = 2.0
# Минимальный анкер: если мы всё-таки опоздали, план всё равно должен
# успеть развернуться в события, а не начать исполняться «вчера».
MIN_ANCHOR_SECONDS = 0.35
# Дорога backend -> companion. Анкер отсчитывается от МОМЕНТА ПРИЁМА
# плана companion'ом, а «сколько осталось» мы посчитали у себя.
LATENCY_COMPENSATION_SECONDS = 0.03
# Как часто пересчитываем остаток и рассылаем состояние в UI.
TICK_SECONDS = 0.2
# Сколько ждать ответа companion'а на загрузку трека.
LOAD_TIMEOUT_SECONDS = 25.0
# Запас поверх расчётной длины перехода, прежде чем считать его зависшим.
PLAN_TIMEOUT_MARGIN_SECONDS = 25.0
# Сколько дека может стоять, прежде чем признать, что ждать нечего.
STALL_LIMIT_SECONDS = 12.0
# Сколько играть после последнего перехода, прежде чем закончить сет.
TAIL_SECONDS = 20.0
# «Клип»: сколько играть до точки ухода, чтобы было видно разбег.
CLIP_LEAD_BARS = 8
CLIP_TAIL_SECONDS = 12.0

DECK_PAIR = ("A", "B")


class AutopilotError(RuntimeError):
    """Не смогли вести сет — текст для показа диджею."""


def _seconds_of_plan(plan: dict) -> float:
    """Сколько длится MixPlan в секундах — для таймаута ожидания."""
    bpm = float(plan.get("bpm") or 174.0) or 174.0
    beat = 60.0 / bpm
    last = 0.0
    for ev in plan.get("events") or []:
        end = float(ev.get("beat_offset") or 0.0) + float(ev.get("duration_beats") or 0.0)
        last = max(last, end)
    return last * beat + float(plan.get("anchor_lead_seconds") or 1.0)


class Autopilot:
    """Один автопилот на комнату. Живёт ровно столько, сколько идёт сет."""

    def __init__(self, room, logger=None) -> None:
        self._room = room
        self._log = logger
        self._task: asyncio.Task | None = None

        self.state = "idle"        # idle|loading|waiting|mixing|done|error|stopped
        self.mode = "set"
        self.index = 0             # индекс СЛЕДУЮЩЕГО перехода
        self.message = ""
        self.error: str | None = None
        self.started_at: float | None = None
        self.decks: dict[str, str | None] = {"source": None, "target": None}
        self.eta_seconds: float | None = None   # до ближайшего перехода
        self.strategy: dict | None = None

        self._plan_waiters: dict[str, asyncio.Future] = {}
        self._load_waiters: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------ статус
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        st = self.strategy or {}
        trs = st.get("transitions") or []
        tracks = st.get("tracks") or []
        cur = trs[self.index] if 0 <= self.index < len(trs) else None
        return {
            "type": "autopilot",
            "running": self.running,
            "state": self.state,
            "mode": self.mode,
            "index": self.index,
            "total": len(trs),
            "message": self.message,
            "error": self.error,
            "eta_seconds": (round(self.eta_seconds, 1)
                            if self.eta_seconds is not None else None),
            "source_deck": self.decks.get("source"),
            "target_deck": self.decks.get("target"),
            "now_playing": (tracks[self.index]["name"]
                            if 0 <= self.index < len(tracks) else None),
            "next_track": (tracks[self.index + 1]["name"]
                           if 0 <= self.index + 1 < len(tracks) else None),
            "technique": (cur or {}).get("technique_name"),
        }

    # ------------------------------------------------ ответы companion'а
    def on_plan_event(self, msg: dict) -> None:
        """plan_started / plan_finished / plan_rejected из companion'а."""
        fut = self._plan_waiters.get(msg.get("plan_id") or "")
        if fut is None or fut.done():
            return
        if msg.get("type") == "plan_finished":
            fut.set_result(dict(msg))
        elif msg.get("type") == "plan_rejected":
            fut.set_result({"ok": False, "error": msg.get("reason") or "план отклонён"})

    def on_load_result(self, msg: dict) -> None:
        fut = self._load_waiters.get(msg.get("token") or "")
        if fut is not None and not fut.done():
            fut.set_result(dict(msg))

    # ------------------------------------------------------- управление
    async def start(self, strategy: dict, index: int = 0, mode: str = "set") -> dict:
        if self.running:
            raise AutopilotError("Автопилот уже ведёт сет — сначала «Стоп».")
        if self._room.companion_ws is None:
            raise AutopilotError("Companion не подключён — играть некому.")
        trs = strategy.get("transitions") or []
        if not trs:
            raise AutopilotError("В плане нет ни одного перехода.")
        if not (0 <= index < len(trs)):
            raise AutopilotError(f"Нет перехода №{index + 1}.")

        self.strategy = strategy
        self.mode = mode if mode in ("set", "clip") else "set"
        self.index = index
        self.state = "loading"
        self.error = None
        self.message = "готовлю деки"
        self.started_at = time.monotonic()
        self._task = asyncio.create_task(self._run())
        await self._broadcast()
        return self.status()

    async def stop(self, reason: str = "остановлено диджеем") -> dict:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.state = "stopped"
        self.message = reason
        self.eta_seconds = None
        await self._broadcast()
        return self.status()

    # ------------------------------------------------------------- ход
    async def _run(self) -> None:
        try:
            await self._prepare()
            await self._loop()
        except asyncio.CancelledError:
            self._say("автопилот остановлен")
            raise
        except AutopilotError as exc:
            self.state, self.error = "error", str(exc)
            self.message = str(exc)
            self._say(f"автопилот остановлен: {exc}")
            await self._broadcast()
        except Exception as exc:                      # noqa: BLE001
            self.state, self.error = "error", repr(exc)
            self.message = f"сбой автопилота: {exc}"
            self._say(f"автопилот упал: {exc!r}")
            await self._broadcast()

    def _deck_for(self, track_index: int) -> str:
        return DECK_PAIR[track_index % 2]

    async def _prepare(self) -> None:
        """Первый трек — на деку и в игру с нужной секунды."""
        st = self.strategy or {}
        tracks, trs = st["tracks"], st["transitions"]
        i = self.index
        src = self._deck_for(i)
        self.decks = {"source": src, "target": self._deck_for(i + 1)}

        tr = trs[i]
        from_sec = float((tr.get("from_point") or {}).get("time_seconds") or 0.0)
        if self.mode == "clip":
            bar = 4 * 60.0 / float(tr.get("bpm_from") or 174.0)
            start_at = max(0.0, from_sec - CLIP_LEAD_BARS * bar)
        else:
            start_at = float(tr.get("entry_seconds") or 0.0)

        self.state = "loading"
        self.message = f"кладу «{tracks[i]['name']}» на деку {src}"
        await self._broadcast()

        await self._send_command(f"clear_deck_{src}")
        await self._load(src, tracks[i], cue_seconds=start_at)
        await self._deck_action(src, "cue_play")
        self._say(f"сет пошёл: дека {src} играет «{tracks[i]['name']}» с {start_at:.0f}с")

    async def _loop(self) -> None:
        st = self.strategy or {}
        tracks, trs = st["tracks"], st["transitions"]

        while self.index < len(trs):
            i = self.index
            tr = trs[i]
            src, dst = self._deck_for(i), self._deck_for(i + 1)
            self.decks = {"source": src, "target": dst}

            # 1. Входящая дека: трек и точка входа. Делается СРАЗУ после
            #    предыдущего перехода — там окно в минуты, а загрузка на
            #    пару секунд забирает фокус клавиатуры (см. deck_loader).
            to_sec = float((tr.get("to_point") or {}).get("time_seconds") or 0.0)
            self.state = "loading"
            self.message = f"готовлю «{tracks[i + 1]['name']}» на деке {dst}"
            await self._broadcast()
            await self._send_command(f"clear_deck_{dst}")
            await self._load(dst, tracks[i + 1], cue_seconds=to_sec)

            # 2. Ждём секунду ухода на уходящей деке.
            from_sec = float((tr.get("from_point") or {}).get("time_seconds") or 0.0)
            self.state = "waiting"
            self.message = (f"жду ухода на {from_sec:.0f}с "
                            f"→ {tr.get('technique_name') or ''}".strip())
            remaining = await self._wait_for_second(src, from_sec)

            # 3. Отдаём переход целиком — дальше тайминг у companion'а.
            plan_id = f"autopilot_{self._room.session_id}_{i}_{secrets.token_hex(3)}"
            overrides = (tr.get("params") or None)
            plan = mix_strategist.build_transition_plan(
                self.strategy, i, src, dst, plan_id, overrides)
            plan["anchor_lead_seconds"] = max(
                MIN_ANCHOR_SECONDS, remaining - LATENCY_COMPENSATION_SECONDS)

            self.state = "mixing"
            self.message = f"{tr.get('technique_name')}: {src} → {dst}"
            self.eta_seconds = 0.0
            await self._broadcast()

            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._plan_waiters[plan_id] = fut
            try:
                if not await self._room.send_plan_to_companion(plan):
                    raise AutopilotError("Companion отключился посреди сета.")
                done = await self._await_future(
                    fut, _seconds_of_plan(plan) + PLAN_TIMEOUT_MARGIN_SECONDS,
                    f"переход №{i + 1} не отчитался о завершении")
            finally:
                self._plan_waiters.pop(plan_id, None)
            if not done.get("ok", True):
                raise AutopilotError(
                    f"переход №{i + 1} не доиграл: {done.get('error') or '?'}")

            self._say(f"переход №{i + 1} сыгран ({src} → {dst})")
            self.index = i + 1

            if self.mode == "clip":
                await asyncio.sleep(CLIP_TAIL_SECONDS)
                await self._send_command(f"clear_deck_{src}")
                self.state, self.message = "done", "клип сыгран"
                self.eta_seconds = None
                await self._broadcast()
                return

            # 4. Уходящая дека свободна — снимаем трек и возвращаем в
            #    нейтраль, иначе следующий приедет с чужим фильтром и
            #    открытым посылом на эхо.
            await self._send_command(f"clear_deck_{src}")

        # последний трек доигрывает сам
        self.state = "done"
        self.message = "сет доигран"
        self.eta_seconds = None
        await self._broadcast()
        self._say("автопилот: сет доигран")

    # ------------------------------------------------------ ожидание
    async def _wait_for_second(self, deck: str, target_seconds: float) -> float:
        """Ждёт, пока игла на `deck` не подойдёт к `target_seconds`.

        Возвращает остаток в секундах на момент выхода — он же становится
        анкером плана."""
        stalled_since: float | None = None
        while True:
            now = self._deck_seconds(deck)
            if now is None:
                raise AutopilotError(
                    f"дека {deck} молчит — Mixxx закрыт или трек снят с деки")

            # Дека может стоять: диджей нажал паузу, трек кончился, Mixxx
            # отказался играть. Без этой проверки автопилот молча ждал бы
            # секунду ухода, которая уже никогда не наступит, и снаружи
            # это выглядело как «пишет, что играет, а ничего не
            # происходит».
            if (self._room.latest_telemetry or {}).get(deck, {}).get("playing"):
                stalled_since = None
            else:
                stalled_since = stalled_since or time.monotonic()
                if time.monotonic() - stalled_since > STALL_LIMIT_SECONDS:
                    raise AutopilotError(
                        f"дека {deck} не играет уже {STALL_LIMIT_SECONDS:.0f} секунд — "
                        f"ждать момента ухода бессмысленно. Нажат ли Play, "
                        f"и тот ли трек на деке?")

            remaining = target_seconds - now
            self.eta_seconds = max(0.0, remaining)
            if remaining <= SEND_AHEAD_SECONDS:
                return max(MIN_ANCHOR_SECONDS, remaining)
            await self._broadcast()
            await asyncio.sleep(min(1.0, max(TICK_SECONDS, remaining - SEND_AHEAD_SECONDS)))

    def _deck_seconds(self, deck: str) -> float | None:
        """Где сейчас игла, в секундах от начала трека.

        Позиция приходит долей 0..1 и запаздывает на один такт телеметрии,
        поэтому доигрываем прошедшее время сами — иначе каждое решение
        принималось бы по картинке двухсотмиллисекундной давности."""
        tel = (self._room.latest_telemetry or {}).get(deck)
        if not tel or not tel.get("track_loaded"):
            return None
        duration = float(tel.get("duration") or 0.0)
        if duration <= 0:
            return None
        seconds = float(tel.get("position") or 0.0) * duration
        if tel.get("playing"):
            age = time.monotonic() - float(getattr(self._room, "telemetry_at", 0.0) or 0.0)
            if 0.0 <= age < 3.0:
                seconds += age
        return seconds

    # ---------------------------------------------------- вниз, к companion
    async def _load(self, deck: str, track: dict, cue_seconds: float) -> None:
        """Следующий трек очереди Auto DJ — на деку, с точкой входа.

        Имени трека здесь нет намеренно: деку наполняет Mixxx из очереди,
        а мы только проверяем по длительности, что приехало ожидаемое.
        Очередь и есть план — она пишется до запуска Mixxx."""
        token = secrets.token_hex(6)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._load_waiters[token] = fut
        try:
            ok = await self._room.send_load_to_companion({
                "token": token,
                "deck": deck,
                "via": "autodj",
                "cue_seconds": round(float(cue_seconds), 2),
                "expect_duration": track.get("duration_seconds"),
                "play": False,
            })
            if not ok:
                raise AutopilotError("Companion не подключён — некому грузить трек.")
            res = await self._await_future(
                fut, LOAD_TIMEOUT_SECONDS,
                f"companion не ответил на загрузку «{track.get('name')}»")
        finally:
            self._load_waiters.pop(token, None)
        if not res.get("ok"):
            raise AutopilotError(res.get("error") or f"не загрузился «{track.get('name')}»")
        if res.get("warning"):
            self._say(f"автопилот: {res['warning']}")

    async def _deck_action(self, deck: str, action: str, **extra) -> None:
        await self._room.send_deck_action_to_companion(
            {"deck": deck, "action": action, **extra})

    async def _send_command(self, action: str) -> None:
        await self._room.send_command_to_companion(action)
        # Полтора такта телеметрии: eject доходит до Mixxx, Mixxx его
        # применяет, и только следующий пакет расскажет, что дека пуста.
        # На 0.15 с загрузчик успевал увидеть ещё ИГРАЮЩУЮ деку и отказать.
        await asyncio.sleep(0.45)

    @staticmethod
    async def _await_future(fut: asyncio.Future, timeout: float, on_timeout: str) -> dict:
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise AutopilotError(on_timeout) from None

    # ------------------------------------------------------------- прочее
    async def _broadcast(self) -> None:
        try:
            await self._room.broadcast_to_chat(self.status())
        except Exception:
            pass

    def _say(self, text: str) -> None:
        if self._log is not None:
            self._log(text)
