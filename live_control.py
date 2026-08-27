"""
Мгновенное управление одним контролом Mixxx — путь в обход MixPlan.

MixPlan — это ПЛАН по долям (техника: свип 16 тактов, потом кроссфейд...),
он проходит через scheduler с точным таймингом. А когда диджей в чате
просит «убери низ на деке A» или «включи эхо на B», плана не нужно: надо
дёрнуть один контрол прямо сейчас. Этот модуль — такой путь.

Общий для companion (исполняет) и backend (валидирует до отправки), чтобы
непонятная команда отсекалась с внятным текстом ещё в чате, а не молча
терялась где-то по дороге.
"""
from __future__ import annotations

import asyncio

import full_catalog
import midi_mapping as mm
import mixxx_controls as mc

MAX_HOLD_SECONDS = 30.0


class ControlError(ValueError):
    """Команду нельзя выполнить — текст предназначен для показа человеку."""


def validate(deck: str, control_id: str, value: float | str | None = None) -> dict:
    """Проверяет команду и приводит value к числу. Возвращает нормализованный
    словарь команды. Бросает ControlError с понятным текстом."""
    ctrl = mc.BY_ID.get(control_id)
    if ctrl is None:
        raise ControlError(
            f"Нет такого контрола: '{control_id}'. Доступные — см. список контролов."
        )

    deck = (deck or "A").upper()
    if ctrl.scope == "deck" and deck not in mc.DECK_CHANNEL:
        raise ControlError(f"Дека '{deck}' не существует, есть A/B/C/D.")

    # строковые синонимы — чтобы агенту было естественнее
    if isinstance(value, str):
        alias = value.strip().lower()
        table = {
            "neutral": ctrl.neutral, "нейтраль": ctrl.neutral, "норма": ctrl.neutral,
            "min": 0.0, "минимум": 0.0, "kill": 0.0, "убрать": 0.0, "off": 0.0,
            "max": 1.0, "максимум": 1.0, "full": 1.0, "on": 1.0,
        }
        if alias not in table:
            try:
                value = float(alias.replace(",", "."))
            except ValueError:
                raise ControlError(
                    f"Не понял значение '{value}'. Число 0..1 либо min/neutral/max."
                ) from None
        else:
            value = table[alias]

    if ctrl.kind == "range":
        v = ctrl.neutral if value is None else float(value)
        if not (0.0 <= v <= 1.0):
            raise ControlError(
                f"'{control_id}' принимает 0..1 (нейтраль {ctrl.neutral:g}), а не {v}."
            )
        return {"deck": deck, "control": control_id, "kind": "range", "value": v}

    if ctrl.kind == "loop":
        beats = 4.0 if value is None else float(value)
        snapped = mc.nearest_loop_size(beats)
        return {"deck": deck, "control": control_id, "kind": "loop",
                "value": snapped, "requested": beats}

    if ctrl.kind == "hotcue":
        number = 1 if value is None else int(value)
        if number not in ctrl.variants:
            raise ControlError(f"Hotcue бывает {min(ctrl.variants)}..{max(ctrl.variants)}, а не {number}.")
        return {"deck": deck, "control": control_id, "kind": "hotcue", "value": number}

    if ctrl.kind == "hold":
        seconds = 1.0 if value is None else float(value)
        if not (0.0 < seconds <= MAX_HOLD_SECONDS):
            raise ControlError(
                f"'{control_id}' удерживается от 0 до {MAX_HOLD_SECONDS:g} секунд, а не {seconds}."
            )
        return {"deck": deck, "control": control_id, "kind": "hold", "value": seconds}

    return {"deck": deck, "control": control_id, "kind": "button", "value": None}


def validate_raw(key: str, deck: str | None = None, unit: int | None = None,
                 slot: int | None = None, index: int | None = None,
                 value: float | str | None = None, family: str | None = None) -> dict:
    """Команда к ЛЮБОМУ контролу Mixxx по его ключу (не только к 84
    «горячим»). Уходит через SysEx — см. midi_mapping.sysex_set."""
    try:
        target = full_catalog.resolve(key, deck=deck, unit=unit, slot=slot,
                                      index=index, family=family)
    except full_catalog.ControlNotFound as exc:
        raise ControlError(str(exc)) from None

    lo, hi, neutral = target["min"], target["max"], target["neutral"]
    kind = target["kind"]

    if isinstance(value, str):
        alias = value.strip().lower()
        table = {"neutral": neutral, "нейтраль": neutral, "норма": neutral,
                 "min": lo, "минимум": lo, "off": lo, "выкл": lo,
                 "max": hi, "максимум": hi, "on": hi, "вкл": hi}
        if alias in table:
            value = table[alias]
        else:
            try:
                value = float(alias.replace(",", "."))
            except ValueError:
                raise ControlError(
                    f"Не понял значение '{value}'. Число {lo:g}..{hi:g} либо min/neutral/max."
                ) from None

    if kind == "range":
        v = neutral if value is None else float(value)
        if not (lo <= v <= hi):
            raise ControlError(
                f"'{key}' принимает {lo:g}..{hi:g} (нейтраль {neutral:g}), а не {v:g}."
            )
    elif kind == "toggle":
        v = None if value is None else float(value)
    else:  # button
        v = None

    return {"raw": True, "group": target["group"], "key": target["key"],
            "kind": kind, "value": v, "scope_label": target["family"]}


def describe(cmd: dict) -> str:
    """Человекочитаемое описание того, что команда сделает."""
    if cmd.get("raw"):
        where = f"{cmd['group']} {cmd['key']}"
        if cmd["kind"] == "range":
            return f"{where} -> {cmd['value']:g}"
        if cmd["kind"] == "toggle":
            return f"{where} -> {'вкл' if cmd['value'] else 'выкл'}" if cmd["value"] is not None else f"{where} (переключить)"
        return f"{where} (нажать)"

    ctrl = mc.BY_ID[cmd["control"]]
    where = f" на деке {cmd['deck']}" if ctrl.scope == "deck" else ""
    kind = cmd["kind"]
    if kind == "range":
        pct = round(cmd["value"] * 100)
        mark = " (нейтраль)" if abs(cmd["value"] - ctrl.neutral) < 1e-6 else ""
        return f"{ctrl.label}{where} -> {pct}%{mark}"
    if kind == "loop":
        extra = ""
        if "requested" in cmd and abs(cmd["requested"] - cmd["value"]) > 1e-6:
            extra = f" (просили {cmd['requested']:g}, Mixxx умеет только {cmd['value']:g})"
        return f"{ctrl.label}{where}: {cmd['value']:g} доли{extra}"
    if kind == "hotcue":
        return f"{ctrl.label} {cmd['value']}{where}"
    if kind == "hold":
        return f"{ctrl.label}{where} на {cmd['value']:g}с"
    return f"{ctrl.label}{where}"


async def apply(midi_send, cmd: dict) -> None:
    """Исполняет проверенную команду. midi_send — синхронная функция,
    принимающая список байтов (midi_bridge.MidiBackend.send)."""
    if cmd.get("raw"):
        group, key, kind = cmd["group"], cmd["key"], cmd["kind"]

        # FX замаплен прямыми CC/Note на каналах 4-7 — идём этим путём, он
        # проверенно работает (им же работают ручки EQ). SysEx оставлен для
        # длинного хвоста: сэмплеры, редкие контролы деки и т.п.
        fx = mm.fx_message(group, key, cmd["value"])
        if fx is not None:
            midi_send(fx)
            if kind == "button":
                # кнопке нужно отпускание, иначе часть контролов Mixxx
                # остаётся «зажатой»
                await asyncio.sleep(0.05)
                off = mm.fx_message_off(group, key)
                if off:
                    midi_send(off)
            return

        # Универсальный путь: адресуем контрол по имени через SysEx.
        # Разбирает это darave-controller-scripts.js::onSysex.
        if kind == "button":
            midi_send(mm.sysex_press(group, key))
        elif kind == "toggle" and cmd["value"] is None:
            midi_send(mm.sysex_toggle(group, key))
        else:
            midi_send(mm.sysex_set(group, key, cmd["value"]))
        return

    deck, control_id, kind = cmd["deck"], cmd["control"], cmd["kind"]
    ctrl = mc.BY_ID[control_id]

    if kind == "range":
        midi_send(mm.resolve_ramp_tick(control_id, deck, cmd["value"]))
        return

    if kind == "loop":
        midi_send(mm.resolve_discrete(control_id, deck, {"beats": cmd["value"]}))
        return

    if kind == "hotcue":
        midi_send(mm.resolve_discrete(control_id, deck, {"hotcue": cmd["value"]}))
        return

    if kind == "hold":
        midi_send(mm.resolve_discrete(control_id, deck, {}))
        await asyncio.sleep(cmd["value"])
        midi_send(mm.resolve_discrete_off(control_id, deck))
        return

    # обычная кнопка
    if ctrl.scope == "master":
        midi_send(mm.resolve_master_discrete(control_id))
    else:
        midi_send(mm.resolve_discrete(control_id, deck, {}))


def catalog_for_ui() -> list[dict]:
    """Плоский список контролов для веб-UI и подсказок."""
    return [
        {
            "id": c.id, "label": c.label, "group": c.group, "kind": c.kind,
            "scope": c.scope, "neutral": c.neutral, "where": c.where,
            "variants": list(c.variants),
        }
        for c in mc.ALL_CONTROLS
    ]
