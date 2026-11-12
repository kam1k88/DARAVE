"""
SessionManager хранит состояние комнат приложения между подключениями
WebSocket. Данные библиотек и планы сетов загружаются из SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import persistence


@dataclass
class SessionRoom:
    session_id: str
    chat_websockets: set[WebSocket] = field(default_factory=set)
    library_tracks: list[dict] = field(default_factory=list)
    last_strategy: dict | None = None

    async def broadcast_to_chat(self, message: dict) -> None:
        dead = set()
        for ws in self.chat_websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self.chat_websockets -= dead



class SessionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, SessionRoom] = {}

    def get_or_create(self, session_id: str) -> SessionRoom:
        room = self._rooms.get(session_id)
        if room is None:
            room = SessionRoom(
                session_id=session_id,
                library_tracks=persistence.load_library(session_id),
                last_strategy=persistence.load_strategy(session_id),
            )
            self._rooms[session_id] = room
            print(
                f"[backend] новая комната '{session_id}' "
                f"(всего активных комнат: {len(self._rooms)})"
            )
        return room

    def drop_if_empty(self, session_id: str) -> None:
        room = self._rooms.get(session_id)
        if room is None:
            return
        if not room.chat_websockets:
            del self._rooms[session_id]
            print(f"[backend] комната '{session_id}' закрыта (пусто), осталось: {len(self._rooms)}")
