import argparse
import asyncio
import platform
import sys
import time

from midi_bridge import make_backend
from mixplan import MixPlan
from scheduler import PlanScheduler

# Пример плана перехода, который мог бы прислать backend/LLM-агент.
# 128 BPM -> такт = 0.46875с. anchor_lead_seconds=1.0 -> план начнёт
# исполняться через 1с после получения (условный "бюджет на доставку").
SAMPLE_PLAN = {
    "plan_id": "transition_demo_01",
    "bpm": 128,
    "anchor_lead_seconds": 1.0,
    "events": [
        {
            "beat_offset": 0, "action": "filter_sweep", "deck": "A",
            "kind": "ramp", "duration_beats": 8, "value_from": 0.0, "value_to": 1.0,
            "curve": "ease_in",
        },
        {"beat_offset": 8, "action": "sync", "deck": "B"},
        {
            "beat_offset": 8, "action": "crossfade", "deck": "A",
            "kind": "ramp", "duration_beats": 16, "value_from": 0.0, "value_to": 1.0,
        },
        {"beat_offset": 24, "action": "loop_exit", "deck": "A"},
        {"beat_offset": 24, "action": "hotcue_jump", "deck": "B", "params": {"hotcue": 1}},
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DARAVE companion prototype: MixPlan -> MIDI")
    parser.add_argument(
        "--backend", choices=["mock", "rtmidi"], default="mock",
        help="mock — только замер таймингов (по умолчанию); "
             "rtmidi — реальный виртуальный MIDI-порт",
    )
    parser.add_argument(
        "--port-name", default="DARAVE Virtual Controller",
        help="Имя виртуального MIDI-порта (должно совпадать с именем в loopMIDI на Windows "
             "и с id контроллера в Mixxx)",
    )
    return parser.parse_args()


def check_platform_sanity(backend_mode: str) -> None:
    if backend_mode != "rtmidi":
        return
    is_wsl = "microsoft" in platform.uname().release.lower()
    if is_wsl:
        print(
            "ВНИМАНИЕ: похоже, вы в WSL. WSL не видит Windows MIDI-подсистему "
            "(как и ASIO/WASAPI) — виртуальный порт loopMIDI отсюда недоступен. "
            "Запустите companion нативным Windows Python (PowerShell/cmd), не из WSL-шелла.",
            file=sys.stderr,
        )


async def main() -> None:
    args = parse_args()
    check_platform_sanity(args.backend)

    print(f"MIDI backend: {args.backend}"
          + (f" (порт: {args.port_name})" if args.backend == "rtmidi" else ""))
    backend = make_backend(args.backend, port_name=args.port_name)
    scheduler = PlanScheduler(backend)

    plan = MixPlan.from_dict(SAMPLE_PLAN)

    received_at = time.perf_counter()
    timed_events = scheduler.expand_plan(plan, received_at)
    print(f"План развёрнут в {len(timed_events)} MIDI-событий "
          f"(включая тики ramp-параметров, шаг {20}мс)")
    print(f"Первое событие через {timed_events[0].exec_time - received_at:.3f}с, "
          f"последнее через {timed_events[-1].exec_time - received_at:.3f}с\n")

    t0 = time.perf_counter()
    jitters = await scheduler.run(timed_events)
    total = time.perf_counter() - t0

    jitters_ms = [j * 1000 for j in jitters]
    print(f"Исполнено за {total:.3f}с (план предполагал "
          f"{timed_events[-1].exec_time - received_at:.3f}с)")
    print(f"Джиттер (факт-план) по {len(jitters_ms)} событиям:")
    print(f"  среднее : {sum(jitters_ms)/len(jitters_ms):+.3f} мс")
    print(f"  максимум: {max(jitters_ms):+.3f} мс")
    print(f"  минимум : {min(jitters_ms):+.3f} мс")

    # log есть только у MockMidiBackend — у rtmidi сообщения реально ушли на порт,
    # смотреть их нужно в самом Mixxx (--controllerDebug) или в MIDI-OX
    if hasattr(backend, "log"):
        print("\nПервые 6 отправленных MIDI-сообщений (mock backend):")
        for t, msg in backend.log[:6]:
            print(f"  t+{t - t0:.3f}s  {msg}")

    backend.close()


if __name__ == "__main__":
    asyncio.run(main())
