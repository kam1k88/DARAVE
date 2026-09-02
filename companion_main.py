"""
companion_main.py — полный companion-процесс (сменяет main.py как основную
точку входа): MIDI-команды + SysEx-телеметрия + WebSocket-связь с backend,
всё в одном asyncio-луп.

--companion-id — это же "код комнаты" в облачном backend (server.py):
диджей вводит этот же код в браузерном чате (http://<backend>/?room=<id>),
чтобы его companion и его чат оказались в одной изолированной комнате.
Для сдачи сервиса в аренду каждому клиенту выдаётся свой уникальный код.

Запуск (mock — всё внутри, без реального MIDI/backend, для проверки):
    python companion_main.py --midi-backend mock --telemetry-backend mock

Запуск против реального облачного backend (server.py, FastAPI):
    python companion_main.py --midi-backend mock --telemetry-backend mock \
        --ws-url ws://localhost:8765 --companion-id my-club-fri

Полностью боевой запуск на Windows:
    python companion_main.py --midi-backend rtmidi --telemetry-backend rtmidi ^
        --ws-url wss://<ваш-backend> --companion-id my-club-fri
"""
from __future__ import annotations

import argparse
import asyncio
import platform
import sys
import time

from midi_bridge import make_backend
from midi_mapping import resolve_master_discrete
from mixplan import MixPlan
from recording_uploader import find_latest_recording, upload_recording, ws_url_to_http
from scheduler import PlanScheduler
from state_store import StateStore
from telemetry import build_mock_stream, make_telemetry_listener
from ws_client import CompanionWSClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DARAVE companion — полный процесс")
    parser.add_argument("--midi-backend", choices=["mock", "rtmidi"], default="mock")
    parser.add_argument("--telemetry-backend", choices=["mock", "rtmidi"], default="mock")
    parser.add_argument("--port-name", default="DARAVE Virtual Controller")
    parser.add_argument("--ws-url", default="ws://localhost:8765")
    parser.add_argument("--companion-id", default="companion-dev-1")
    parser.add_argument(
        "--recordings-dir", default=None,
        help="Папка записей Mixxx (Preferences -> Recordings). Без неё кнопка "
             "'скачать запись' в чате работать не будет.",
    )
    parser.add_argument(
        "--http-url", default=None,
        help="HTTP(S)-адрес backend'а для загрузки записи (по умолчанию выводится из --ws-url).",
    )
    return parser.parse_args()


def check_platform_sanity(midi_backend: str) -> None:
    if midi_backend != "rtmidi":
        return
    is_wsl = "microsoft" in platform.uname().release.lower()
    if is_wsl:
        print(
            "ВНИМАНИЕ: похоже, вы в WSL — виртуальный MIDI-порт отсюда недоступен. "
            "Запустите нативным Windows Python.",
            file=sys.stderr,
        )


async def main() -> None:
    args = parse_args()
    check_platform_sanity(args.midi_backend)

    store = StateStore()

    midi_out = make_backend(args.midi_backend, port_name=args.port_name)
    scheduler = PlanScheduler(midi_out)

    telemetry = make_telemetry_listener(args.telemetry_backend, store, args.port_name)

    # В mock-режиме телеметрии реального Mixxx нет — эмулируем поток,
    # чтобы можно было проверить весь путь end-to-end прямо тут.
    mock_stream = None
    if args.telemetry_backend == "mock":
        mock_stream = build_mock_stream(telemetry, tick_seconds=0.1)

    async def fake_telemetry_pump() -> None:
        while True:
            if mock_stream is not None:
                next(mock_stream)
            await asyncio.sleep(0.1)

    async def execute_plan(plan: MixPlan) -> None:
        received_at = time.perf_counter()
        timed_events = scheduler.expand_plan(plan, received_at)
        print(f"[companion] исполняю план '{plan.plan_id}': {len(timed_events)} MIDI-событий")
        jitters = await scheduler.run(timed_events)
        jitters_ms = [j * 1000 for j in jitters]
        print(f"[companion] план '{plan.plan_id}' исполнен, "
              f"джиттер: среднее {sum(jitters_ms)/len(jitters_ms):+.3f}мс, "
              f"максимум {max(jitters_ms):+.3f}мс")

    async def execute_control(msg: dict) -> None:
        """Мгновенно дёрнуть один контрол Mixxx — команда из чата/веб-пульта,
        без MixPlan и без планировщика."""
        import live_control
        cmd = {
            "deck": msg.get("deck", "A"),
            "control": msg.get("control"),
            "kind": msg.get("kind"),
            "value": msg.get("value"),
        }
        print(f"[companion] контрол: {live_control.describe(cmd)}")
        await live_control.apply(midi_out.send, cmd)

    async def execute_command(action: str) -> None:
        # Составные команды по деке: «освободить деку» — это стоп, снятие
        # трека и возврат всех ручек в стандартное положение. Одной
        # кнопкой, потому что вручную это семь движений и одно из них
        # всегда забывается — чаще всего открытый посыл на эхо.
        import midi_mapping as _mm

        for prefix, builder in (("clear_deck_", _mm.clear_deck_messages),
                                ("neutral_deck_", _mm.neutral_deck_messages)):
            if action.startswith(prefix):
                name = action[len(prefix):].strip().upper()
                num = _mm.DECK_NUMBER.get(name)
                if num is None:
                    print(f"[companion] неизвестная дека в команде '{action}'")
                    return
                for msg in builder(num):
                    midi_out.send(msg)
                print(f"[companion] дека {name}: "
                      + ("снят трек, всё в нейтраль" if prefix.startswith("clear")
                         else "ручки в нейтраль"))
                return

        try:
            message = resolve_master_discrete(action)
        except ValueError as exc:
            print(f"[companion] {exc}")
            return
        midi_out.send(message)

    http_url = args.http_url or ws_url_to_http(args.ws_url)

    async def recording_upload_watcher() -> None:
        """Следит за статусом записи (см. state_store.StateStore) и, как только
        Mixxx перестаёт писать (переход 1 -> 0/2), грузит самый свежий файл
        из --recordings-dir backend'у — не важно, остановлена запись кнопкой
        в чате или прямо в интерфейсе Mixxx."""
        if not args.recordings_dir:
            print("[companion] --recordings-dir не задан — скачивание записи из чата будет недоступно")
            return
        was_recording = False
        recording_started_at = 0.0
        while True:
            status = store.get_recording_status()
            is_recording = status == 1
            if is_recording and not was_recording:
                recording_started_at = time.time()
            elif was_recording and not is_recording:
                await asyncio.sleep(1.5)  # дать Mixxx дописать/закрыть файл на диске
                latest = find_latest_recording(args.recordings_dir, newer_than=recording_started_at - 5)
                if latest is not None:
                    await upload_recording(http_url, args.companion_id, latest)
                else:
                    print(f"[companion] запись остановлена, но файл в '{args.recordings_dir}' не найден")
            was_recording = is_recording
            await asyncio.sleep(1.0)

    async def skin_command_watcher() -> None:
        """Кнопки DARAVE, нажатые прямо в скине Mixxx.

        Скин не может запустить программу, а скрипт контроллера — тем
        более: всё, что он умеет, — сообщить наружу «нажали». Поэтому
        решение принимается здесь, где уже есть и HTTP до backend'а, и
        MIDI до Mixxx."""
        import httpx

        while True:
            for cmd in store.pop_commands():
                if cmd == "stems":
                    url = f"{http_url.rstrip('/')}/api/rooms/{args.companion_id}/stems/build"
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            r = await client.post(url, json={})
                        detail = ""
                        try:
                            detail = (r.json() or {}).get("detail") or (r.json() or {}).get("detail", "")
                        except Exception:
                            detail = r.text[:200]
                        print(f"[companion] кнопка «Стемы» в Mixxx: {r.status_code} {detail}")
                    except Exception as exc:
                        print(f"[companion] кнопка «Стемы»: backend не ответил — {exc}")
                else:
                    print(f"[companion] неизвестная кнопка скина: '{cmd}'")
            await asyncio.sleep(0.2)

    ws_client = CompanionWSClient(
        args.ws_url, args.companion_id, store, execute_plan,
        execute_command=execute_command,
        execute_control=execute_control,
    )
    print(f"[companion] код комнаты: '{args.companion_id}' — этот же код диджей "
          f"вводит в браузерном чате на backend'е")

    # Квантование и slip — не «настройка по вкусу»: без quantize нажатия
    # и метки встают не на долю, и sync разъезжается на слух даже при
    # точной бит-сетке. Ставим сами при старте, а не надеемся, что диджей
    # не забудет включить их на каждой деке.
    try:
        import midi_mapping as _mm

        for msg in _mm.deck_startup_messages():
            midi_out.send(msg)
        print("[companion] на всех деках включены квантование и slip")
    except Exception as exc:  # MIDI-порта может не быть в mock-режиме
        print(f"[companion] не смог выставить quantize/slip: {exc}")

    tasks = [
        asyncio.create_task(ws_client.run()),
        asyncio.create_task(recording_upload_watcher()),
        asyncio.create_task(skin_command_watcher()),
    ]
    if mock_stream is not None:
        tasks.append(asyncio.create_task(fake_telemetry_pump()))

    try:
        await asyncio.gather(*tasks)
    finally:
        telemetry.close()
        midi_out.close()


if __name__ == "__main__":
    asyncio.run(main())
