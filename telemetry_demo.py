"""
Демо приёма телеметрии: открывает MIDI-IN на том же виртуальном порту,
что и companion для команд, и печатает состояние дек по мере поступления
SysEx-пакетов от darave-controller-scripts.js.

Запуск (реальный порт, Windows, нативный Python — не WSL):
    python telemetry_demo.py --backend rtmidi

Запуск в mock-режиме (эмулирует поток телеметрии без реального Mixxx —
удобно, чтобы проверить сам вывод/форматирование до похода к реальному стенду):
    python telemetry_demo.py --backend mock
"""
from __future__ import annotations

import argparse
import platform
import sys
import time

from state_store import StateStore
from telemetry import build_mock_stream, make_telemetry_listener


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DARAVE telemetry demo: Mixxx -> companion (SysEx)")
    parser.add_argument("--backend", choices=["mock", "rtmidi"], default="mock")
    parser.add_argument("--port-name", default="DARAVE Virtual Controller")
    parser.add_argument(
        "--duration", type=float, default=15.0,
        help="Сколько секунд слушать/эмулировать телеметрию (по умолчанию 15с)",
    )
    return parser.parse_args()


def check_platform_sanity(backend_mode: str) -> None:
    if backend_mode != "rtmidi":
        return
    is_wsl = "microsoft" in platform.uname().release.lower()
    if is_wsl:
        print(
            "ВНИМАНИЕ: похоже, вы в WSL — виртуальный MIDI-порт отсюда недоступен. "
            "Запустите нативным Windows Python.",
            file=sys.stderr,
        )


def print_snapshot(store: StateStore) -> None:
    snapshot = store.snapshot()
    if not snapshot:
        print("  (телеметрии пока нет)")
        return
    for deck in sorted(snapshot):
        s = snapshot[deck]
        age_ms = (time.time() - s.received_at) * 1000
        status = "▶" if s.playing else "⏸"
        loaded = "трек загружен" if s.track_loaded else "дека пуста"
        print(f"  Deck {deck}: {status}  BPM={s.bpm:6.2f}  pos={s.position:5.1%}  "
              f"{loaded}  (обновлено {age_ms:.0f}мс назад)")


def main() -> None:
    args = parse_args()
    check_platform_sanity(args.backend)

    store = StateStore()

    print(f"Telemetry backend: {args.backend}")
    listener = make_telemetry_listener(args.backend, store, args.port_name)

    mock_stream = None
    if args.backend == "mock":
        # Без реального Mixxx эмулируем поток SysEx-пакетов такого же вида,
        # что реально пришлют в проде — только чтобы показать живой вывод.
        mock_stream = build_mock_stream(listener, tick_seconds=0.1)

    print(f"Слушаю телеметрию {args.duration:.0f}с... (Ctrl+C — прервать раньше)\n")
    start = time.perf_counter()
    try:
        while time.perf_counter() - start < args.duration:
            if mock_stream is not None:
                next(mock_stream)
            time.sleep(0.5)
            elapsed = time.perf_counter() - start
            print(f"[t+{elapsed:4.1f}с]")
            print_snapshot(store)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
    finally:
        listener.close()


if __name__ == "__main__":
    main()
