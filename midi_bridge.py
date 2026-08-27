"""
Абстракция над MIDI-выходом. Два бэкенда:

  RtMidiBackend  — реальный виртуальный MIDI-порт через python-rtmidi.
                   На Linux/macOS rtmidi умеет создавать virtual port сам.
                   На Windows у RtMidi нет virtual-port API — нужен
                   сторонний loopMIDI, порт открывается по имени.

  MockMidiBackend — ничего не шлёт по железу, только логирует сообщения
                    с точным timestamp'ом. Нужен для проверки таймингов
                    scheduler'а без реального MIDI-устройства/loopMIDI —
                    именно им проверяем прототип в этой песочнице.
"""
from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod


class MidiBackend(ABC):
    @abstractmethod
    def send(self, message: list[int]) -> None:
        ...

    def close(self) -> None:
        pass


class MockMidiBackend(MidiBackend):
    """Логирует (perf_counter_time, message) для анализа джиттера."""

    def __init__(self) -> None:
        self.log: list[tuple[float, list[int]]] = []

    def send(self, message: list[int]) -> None:
        self.log.append((time.perf_counter(), list(message)))


class RtMidiBackend(MidiBackend):
    def __init__(self, port_name: str = "DARAVE Virtual Controller") -> None:
        import rtmidi  # локальный импорт: не требуем rtmidi для mock-режима

        self._midiout = rtmidi.MidiOut()
        available = self._midiout.get_ports()

        if sys.platform.startswith("win"):
            # На Windows нужен уже созданный виртуальный порт (loopMIDI).
            match = next((i for i, p in enumerate(available) if port_name in p), None)
            if match is None:
                raise RuntimeError(
                    f"Виртуальный MIDI-порт '{port_name}' не найден среди {available}. "
                    "На Windows нужно предварительно создать порт с этим именем в loopMIDI."
                )
            self._midiout.open_port(match)
        else:
            # Linux/macOS: rtmidi создаёт virtual port сам.
            self._midiout.open_virtual_port(port_name)

    def send(self, message: list[int]) -> None:
        self._midiout.send_message(message)

    def close(self) -> None:
        self._midiout.close_port()
        del self._midiout


def make_backend(mode: str, port_name: str = "DARAVE Virtual Controller") -> MidiBackend:
    if mode == "mock":
        return MockMidiBackend()
    if mode == "rtmidi":
        return RtMidiBackend(port_name)
    raise ValueError(f"Unknown backend mode: {mode}")
