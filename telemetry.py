"""
Telemetry — обратный канал Mixxx -> companion.

darave-controller-scripts.js раз в 100мс шлёт SysEx-пакет с состоянием
каждой деки на тот же виртуальный MIDI-порт. Здесь это принимается,
парсится и складывается в StateStore.

Формат payload внутри SysEx (см. .js): "<deck>,<play 0|1>,<bpm>,<pos>,<track_loaded 0|1>"
Пример: "A,1,128.00,0.4123,1"

Отдельно — глобальный (не по декам) статус записи, тем же таймером:
  "R,<status>"  — status: 0=не пишет, 1=пишет, 2=ошибка (см. Mixxx [Recording] status)
Пример: "R,1"

Два бэкенда, как и у midi_bridge.py:
  MockTelemetryListener — для тестов парсинга без реального MIDI-порта
  RtMidiTelemetryListener — реальный приём через python-rtmidi
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Union

from state_store import DeckState, StateStore

SYSEX_START = 0xF0
SYSEX_END = 0xF7


@dataclass
class RecordingStatus:
    status: int


@dataclass
class SkinCommand:
    """Нажатие кнопки DARAVE, встроенной в скин Mixxx: "X,<команда>"."""

    command: str


ParsedTelemetry = Union[DeckState, RecordingStatus, SkinCommand, None]


def parse_sysex_payload(message: list[int]) -> ParsedTelemetry:
    """[0xF0, <ascii bytes...>, 0xF7] -> DeckState | RecordingStatus | None."""
    if len(message) < 3 or message[0] != SYSEX_START or message[-1] != SYSEX_END:
        return None
    try:
        payload = bytes(message[1:-1]).decode("ascii")
        parts = payload.split(",")
        if parts[0] == "R":
            return RecordingStatus(status=int(parts[1]))
        if parts[0] == "X":
            return SkinCommand(command=parts[1].strip())
        # Шестое поле (stem_count) появилось вместе с живыми стемами.
        # Разбираем и старый формат: скрипт контроллера обновляется в
        # Mixxx руками, и рассинхрон на один запуск — норма, а не сбой.
        deck, play_flag, bpm, pos, track_loaded_flag = parts[:5]
        stem_count = int(parts[5]) if len(parts) > 5 else 0
        return DeckState(
            deck=deck,
            playing=bool(int(play_flag)),
            bpm=float(bpm),
            position=float(pos),
            track_loaded=bool(int(track_loaded_flag)),
            received_at=time.time(),
            stem_count=stem_count,
        )
    except (UnicodeDecodeError, ValueError, IndexError):
        # Чужой/битый SysEx (например, от другого приложения на том же порту) —
        # молча игнорируем, а не роняем companion.
        return None


class TelemetryListener(ABC):
    @abstractmethod
    def close(self) -> None:
        ...


class MockTelemetryListener(TelemetryListener):
    """Не открывает никакого порта — только даёт руками скормить сообщения
    в parse_sysex_payload через feed(), чтобы протестировать связку без
    реального Mixxx/MIDI-устройства (как здесь, в песочнице)."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def feed(self, message: list[int]) -> None:
        _route_parsed(self._store, parse_sysex_payload(message))

    def close(self) -> None:
        pass


def _route_parsed(store: StateStore, parsed: ParsedTelemetry) -> None:
    if isinstance(parsed, RecordingStatus):
        store.update_recording_status(parsed.status)
    elif isinstance(parsed, SkinCommand):
        store.push_command(parsed.command)
    elif isinstance(parsed, DeckState):
        store.update(parsed)


class RtMidiTelemetryListener(TelemetryListener):
    def __init__(self, store: StateStore, port_name: str = "DARAVE Virtual Controller") -> None:
        import rtmidi
        import sys

        self._store = store
        self._midiin = rtmidi.MidiIn()
        # По умолчанию rtmidi ИГНОРИРУЕТ sysex-сообщения на приёме —
        # это единственная строчка, без которой телеметрия молча потеряется.
        self._midiin.ignore_types(sysex=False, timing=True, active_sense=True)

        available = self._midiin.get_ports()
        if sys.platform.startswith("win"):
            match = next((i for i, p in enumerate(available) if port_name in p), None)
            if match is None:
                raise RuntimeError(
                    f"MIDI-IN порт '{port_name}' не найден среди {available}. "
                    "Он должен быть тем же loopMIDI-портом, что и для исходящих команд."
                )
            self._midiin.open_port(match)
        else:
            self._midiin.open_virtual_port(port_name + " IN")

        self._midiin.set_callback(self._on_message)

    def _on_message(self, event, _data=None) -> None:
        # rtmidi callback: event = (message_bytes_as_ints, delta_time)
        message, _delta_time = event
        _route_parsed(self._store, parse_sysex_payload(list(message)))

    def close(self) -> None:
        self._midiin.cancel_callback()
        self._midiin.close_port()
        del self._midiin


def make_telemetry_listener(mode: str, store: StateStore, port_name: str = "DARAVE Virtual Controller") -> TelemetryListener:
    if mode == "mock":
        return MockTelemetryListener(store)
    if mode == "rtmidi":
        return RtMidiTelemetryListener(store, port_name)
    raise ValueError(f"Unknown telemetry backend: {mode}")


def build_mock_stream(listener: "MockTelemetryListener", tick_seconds: float = 0.1):
    """Генератор, эмулирующий поток SysEx-пакетов от darave-controller-scripts.js
    (два трека играют с разным BPM, деки постепенно продвигаются по позиции).
    Используется только в демо/тестах, когда нет реального Mixxx."""
    def sysex(payload: str) -> list[int]:
        return [SYSEX_START] + [ord(c) & 0x7F for c in payload] + [SYSEX_END]

    bpm_a, bpm_b = 128.0, 126.0
    pos_a, pos_b = 0.10, 0.35
    track_seconds = 210.0  # условная длина трека для перевода BPM-времени в position

    while True:
        pos_a = min(1.0, pos_a + tick_seconds / track_seconds)
        pos_b = min(1.0, pos_b + tick_seconds / track_seconds)
        listener.feed(sysex(f"A,1,{bpm_a:.2f},{pos_a:.4f},1"))
        listener.feed(sysex(f"B,1,{bpm_b:.2f},{pos_b:.4f},1"))
        yield
