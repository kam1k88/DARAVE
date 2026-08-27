"""
SessionManager — комнаты (session_id, он же "код комнаты"/room code) для
мультиарендной модели: один companion + один или несколько браузерных
чат-клиентов на комнату. Комнаты создаются лениво при первом подключении
(companion или chat) с данным session_id и живут, пока в них кто-то есть.

Каждая комната полностью изолирована от других: своя телеметрия, свой
companion, своя история диалога с DJAgent — ни данные, ни MixPlan одного
арендатора никогда не попадают к другому.

MVP-уровень авторизации: session_id — это и есть "пароль" на комнату (как
код встречи в Zoom). Companion и браузер должны знать один и тот же код.
Для реальной сдачи в аренду это стоит усилить (случайные длинные ID,
возможно — привязка к аккаунту), но сам механизм изоляции уже такой,
каким должен быть.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import WebSocket

from agent import DJAgent
from llm_providers import LLMProvider
import persistence


@dataclass
class SessionRoom:
    session_id: str
    agent: DJAgent
    companion_ws: WebSocket | None = None
    chat_websockets: set[WebSocket] = field(default_factory=set)
    latest_telemetry: dict[str, dict] = field(default_factory=dict)
    # Последний отправленный MixPlan — для "повторить микс" и для персистентности
    # (переживает рестарт backend'а, см. persistence.py).
    last_plan: dict | None = None
    # Путь к последней загруженной companion'ом записи микса этой комнаты
    # (см. server.py: POST /api/rooms/{id}/recording) — None, пока не загружена.
    recording_path: str | None = None
    # Библиотека, загруженная track_analysis.py --upload (см. server.py:
    # POST /api/rooms/{id}/library) — список аналитических записей треков.
    library_tracks: list[dict] = field(default_factory=list)
    # Последний посчитанный mix_strategist.py::plan_strategy() план сета —
    # кэш для "▶ выполнить переход N" (POST /api/rooms/{id}/strategy/execute),
    # не персистится (пересчитывается по запросу, дёшево).
    last_strategy: dict | None = None

    async def broadcast_to_chat(self, message: dict) -> None:
        dead = set()
        for ws in self.chat_websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self.chat_websockets -= dead

    async def send_plan_to_companion(self, plan: dict) -> bool:
        if self.companion_ws is None:
            return False
        await self.companion_ws.send_json({"type": "mixplan", "plan": plan})
        self.last_plan = plan
        persistence.save_last_plan(self.session_id, plan)
        return True

    async def send_control_to_companion(self, cmd: dict) -> bool:
        """Шлёт companion'у команду «дёрни один контрол сейчас» (см.
        live_control.py). cmd уже проверен live_control.validate()."""
        if self.companion_ws is None:
            return False
        await self.companion_ws.send_json({"type": "control", **cmd})
        return True

    async def send_command_to_companion(self, action: str) -> bool:
        if self.companion_ws is None:
            return False
        await self.companion_ws.send_json({"type": "command", "action": action})
        return True


class SessionManager:
    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self._rooms: dict[str, SessionRoom] = {}

    def get_or_create(self, session_id: str) -> SessionRoom:
        room = self._rooms.get(session_id)
        if room is None:
            persisted_turns = persistence.load_history(session_id)
            agent = DJAgent(self._provider, persisted_turns=persisted_turns)
            room = SessionRoom(
                session_id=session_id,
                agent=agent,
                last_plan=persistence.load_last_plan(session_id),
                library_tracks=persistence.load_library(session_id),
            )
            self._rooms[session_id] = room
            print(
                f"[backend] новая комната '{session_id}' "
                f"(история: {len(persisted_turns)} реплик, всего активных комнат: {len(self._rooms)})"
            )
        return room

    def drop_if_empty(self, session_id: str) -> None:
        room = self._rooms.get(session_id)
        if room is None:
            return
        if room.companion_ws is None and not room.chat_websockets:
            del self._rooms[session_id]
            print(f"[backend] комната '{session_id}' закрыта (пусто), осталось: {len(self._rooms)}")
