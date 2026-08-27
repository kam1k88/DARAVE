"""
CompanionWSClient — сеть companion <-> backend.

Подключается на {base_url}/ws/companion/{session_id} — session_id это же
"код комнаты", который диджей вводит в браузерном чате (см. static/chat.html
и server.py). companion_main.py по умолчанию использует --companion-id как
session_id: один арендатор = один companion = один код комнаты, без лишней
CLI-поверхности под MVP.

Две независимые задачи, идущие параллельно на одном соединении:
  - telemetry_sender: раз в TELEMETRY_SEND_INTERVAL читает StateStore и шлёт
    снимок состояния дек в облако
  - plan_receiver: слушает входящие сообщения, при получении MixPlan
    сразу же прогоняет его через переданный execute_plan callback

execute_plan — асинхронная функция (обычно PlanScheduler.expand_plan +
PlanScheduler.run), которую передаёт вызывающий код (companion_main.py),
чтобы этот модуль не был завязан на конкретный scheduler/бэкенд MIDI.

execute_command — асинхронная функция для одноразовых команд без MixPlan
("recording_toggle" из кнопки в чате, см. midi_mapping.resolve_master_discrete).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Awaitable, Callable

import websockets

from mixplan import MixPlan
from state_store import StateStore

TELEMETRY_SEND_INTERVAL_SECONDS = 0.2

ExecutePlanCallback = Callable[[MixPlan], Awaitable[None]]
ExecuteCommandCallback = Callable[[str], Awaitable[None]]
ExecuteControlCallback = Callable[[dict], Awaitable[None]]


class CompanionWSClient:
    def __init__(
        self,
        url: str,
        companion_id: str,
        store: StateStore,
        execute_plan: ExecutePlanCallback,
        execute_command: ExecuteCommandCallback | None = None,
        session_id: str | None = None,
        execute_control: ExecuteControlCallback | None = None,
    ) -> None:
        # session_id по умолчанию = companion_id (см. докстринг модуля)
        room = session_id or companion_id
        self._url = f"{url.rstrip('/')}/ws/companion/{room}"
        self._companion_id = companion_id
        self._store = store
        self._execute_plan = execute_plan
        self._execute_command = execute_command
        self._execute_control = execute_control
        self._ws = None
        # Текущий исполняющийся MixPlan. Раньше _execute_plan вызывался
        # прямо из цикла приёма сообщений — а техника длится десятки секунд,
        # и всё это время companion НЕ ЧИТАЛ сокет. Второе нажатие "Выполнить"
        # висело в очереди и срабатывало только после конца первого: со
        # стороны диджея это выглядело как "работает через раз и с
        # задержкой". Теперь план исполняется отдельной задачей, а
        # параллельный запрос честно отклоняется с причиной.
        self._plan_task: asyncio.Task | None = None
        self._plan_id: str | None = None

    async def run(self) -> None:
        async with websockets.connect(self._url) as ws:
            self._ws = ws
            await ws.send(json.dumps({"type": "hello", "companion_id": self._companion_id}))
            print(f"[companion] подключился к {self._url}")

            await asyncio.gather(
                self._telemetry_sender(),
                self._message_receiver(),
            )

    async def _telemetry_sender(self) -> None:
        while True:
            snapshot = self._store.snapshot()
            if snapshot:
                decks_payload = {
                    deck: {k: v for k, v in asdict(state).items() if k != "deck"}
                    for deck, state in snapshot.items()
                }
                payload = {"type": "telemetry", "decks": decks_payload}
                recording_status = self._store.get_recording_status()
                if recording_status is not None:
                    payload["recording_status"] = recording_status
                await self._ws.send(json.dumps(payload))
            await asyncio.sleep(TELEMETRY_SEND_INTERVAL_SECONDS)

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:  # соединение уже рвётся — не роняем приём
            print(f"[companion] не смог отправить {payload.get('type')}: {exc!r}")

    async def _run_control(self, msg: dict) -> None:
        if self._execute_control is None:
            return
        try:
            await self._execute_control(msg)
            await self._send_json({"type": "control_done", "control": msg.get("control"),
                                   "deck": msg.get("deck"), "ok": True})
        except Exception as exc:
            print(f"[companion] контрол {msg.get('control')} не сработал: {exc!r}")
            await self._send_json({"type": "control_done", "control": msg.get("control"),
                                   "deck": msg.get("deck"), "ok": False, "error": str(exc)})

    async def _run_plan(self, plan: MixPlan, plan_id: str) -> None:
        """Исполняет MixPlan, не блокируя приём сообщений, и сообщает
        backend'у о старте/конце — чтобы UI мог показать, что техника
        реально играет, а не молчать все 30 секунд перехода."""
        await self._send_json({"type": "plan_started", "plan_id": plan_id})
        try:
            await self._execute_plan(plan)
            await self._send_json({"type": "plan_finished", "plan_id": plan_id, "ok": True})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[companion] MixPlan {plan_id} упал: {exc!r}")
            await self._send_json({
                "type": "plan_finished", "plan_id": plan_id, "ok": False, "error": repr(exc),
            })

    async def _message_receiver(self) -> None:
        async for raw in self._ws:
            msg = json.loads(raw)
            if msg["type"] == "mixplan":
                plan_id = msg["plan"].get("plan_id", "?")
                if self._plan_task is not None and not self._plan_task.done():
                    print(
                        f"[companion] MixPlan {plan_id} ОТКЛОНЁН: ещё исполняется {self._plan_id}",
                    )
                    await self._send_json({
                        "type": "plan_rejected",
                        "plan_id": plan_id,
                        "reason": f"Предыдущая техника ({self._plan_id}) ещё исполняется — дождитесь её конца.",
                    })
                    continue
                print(f"[companion] получен MixPlan: {plan_id}")
                plan = MixPlan.from_dict(msg["plan"])
                self._plan_id = plan_id
                self._plan_task = asyncio.create_task(self._run_plan(plan, plan_id))
            elif msg["type"] == "control":
                # Мгновенная правка одного контрола Mixxx (чат: «убери низ на
                # A»). В отличие от MixPlan здесь нет тайминга по долям —
                # дёргаем сразу. Отдельной задачей, чтобы удержание
                # (fx_enable на N секунд) не застопорило приём сообщений.
                asyncio.create_task(self._run_control(msg))

            elif msg["type"] == "command":
                action = msg.get("action", "")
                print(f"[companion] получена команда: {action}")
                if self._execute_command is not None:
                    await self._execute_command(action)
