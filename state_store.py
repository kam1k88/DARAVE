"""
StateStore — потокобезопасное хранилище последнего известного состояния
каждой деки. rtmidi доставляет входящие сообщения (в т.ч. SysEx) в отдельном
C++-потоке через callback — запись сюда идёт из того потока, чтение из
asyncio-луп companion'а, поэтому нужен простой lock, а не голый dict.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class DeckState:
    deck: str
    playing: bool
    bpm: float
    position: float  # 0..1, доля пройденного трека
    track_loaded: bool
    received_at: float  # time.time() локального companion, для контроля "свежести"
    # Сколько слоёв у загруженного трека: 0 — обычный файл, 4 — .stem.mp4.
    # По умолчанию 0, чтобы старый companion/скрипт не ломал разбор.
    stem_count: int = 0


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decks: dict[str, DeckState] = {}
        # Глобальный (не по декам) статус записи — из отдельного SysEx "R,<0|1|2>"
        # (см. telemetry.py::parse_sysex_payload). Mixxx [Recording] status:
        # 0=не пишет, 1=пишет, 2=не удалось начать (диск/путь).
        self._recording_status: int | None = None
        self._commands: list[str] = []

    def update(self, state: DeckState) -> None:
        with self._lock:
            self._decks[state.deck] = state

    def update_recording_status(self, status: int) -> None:
        with self._lock:
            self._recording_status = status

    def push_command(self, command: str) -> None:
        """Команда, нажатая кнопкой прямо в скине Mixxx.

        Скин не умеет запускать программы, а скрипт контроллера — тем
        более; всё, что он может, — сказать наружу «нажали». Поэтому
        кнопка в скине лишь дёргает контрол, скрипт видит это в своём
        тике и шлёт сюда SysEx, а разбирается уже companion."""
        with self._lock:
            self._commands.append(command)

    def pop_commands(self) -> list[str]:
        with self._lock:
            out, self._commands = list(self._commands), []
        return out

    def get(self, deck: str) -> DeckState | None:
        with self._lock:
            return self._decks.get(deck)

    def get_recording_status(self) -> int | None:
        with self._lock:
            return self._recording_status

    def snapshot(self) -> dict[str, DeckState]:
        with self._lock:
            return dict(self._decks)

    def is_stale(self, deck: str, max_age_seconds: float = 1.0) -> bool:
        """Пригодится для WS-моста (пункт 4): не слать в облако протухшее
        состояние, если телеметрия перестала приходить (Mixxx закрыт/завис)."""
        state = self.get(deck)
        if state is None:
            return True
        return (time.time() - state.received_at) > max_age_seconds
