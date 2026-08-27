"""
Генератор DARAVE-Virtual-Controller.midi.xml из mixxx_controls.py.

Зачем генератор, а не правка XML руками: XML и таблица нот — две половины
одного контракта. Пока их правили порознь, половина контролов
существовала только в одной из них (ручек EQ не было в XML вовсе,
FX-юнит не был подключён к деке, длина лупа не задавалась) — отсюда
«агент крутит только кроссфейдер и глушилки». Теперь XML — производная.

Запуск:
    python make_mixxx_mapping.py --out DARAVE-Virtual-Controller.midi.xml
"""
from __future__ import annotations

import argparse
import sys

import mixxx_controls as mc
from midi_mapping import CC_NUMBER, NOTE_NUMBER

HEADER = '''<?xml version="1.0" encoding="utf-8"?>
<!--
  DARAVE Virtual Controller — маппинг companion -> Mixxx.

  ФАЙЛ СГЕНЕРИРОВАН: make_mixxx_mapping.py (источник истины —
  mixxx_controls.py). Руками не править: пересоберите генератором, иначе
  XML и таблица нот снова разъедутся.

  Контракт:
    Дека A = MIDI-канал 0 = [Channel1], B = 1, C = 2, D = 3
    дискретные действия = Note On на канале деки (мастер — канал 15)
    отпускание удержания = Note On с velocity 0 (НЕ статус 0x80: биндинг
      привязан к 0x90, и 0x80 не совпал бы ни с чем)
    ramp-действия = CC на канале деки
    EffectUnit N закреплён за декой N

  Диапазоны: <control> линейно растягивает MIDI 0..127 на min..max
  контрола Mixxx. Поэтому DARAVE всегда шлёт нормализованное 0..1, а где у
  конкретной ручки «ноль» — записано в mixxx_controls.Control.neutral
  (у ручек EQ и gain диапазон 0..4, нейтраль 1.0 -> CC 32, а не 64).

  Положить рядом с darave-controller-scripts.js:
    Windows: C:\\Users\\<username>\\AppData\\Local\\Mixxx\\controllers
    Linux:   ~/.mixxx/controllers
    macOS:   ~/Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx/controllers
  Затем в Mixxx: Preferences -> Controllers -> снять и поставить галочку.
-->
<MixxxControllerPreset schemaVersion="1" mixxxVersion="2.4+">
  <info>
    <name>DARAVE Virtual Controller</name>
    <author>DARAVE</author>
    <description>Виртуальный контроллер для приёма команд от DARAVE companion по MIDI</description>
  </info>
  <controller id="DARAVE Virtual Controller">
    <scriptfiles>
      <file filename="darave-controller-scripts.js" functionprefix="DaraveController"/>
    </scriptfiles>
    <controls>
'''

FOOTER = '''    </controls>
    <outputs>
    </outputs>
  </controller>
</MixxxControllerPreset>
'''


def _control_xml(group: str, key: str, status: int, midino: int, button: bool, comment: str) -> str:
    opt = "<button/>" if button else "<normal/>"
    return (
        f"      <!-- {comment} -->\n"
        f"      <control>\n"
        f"        <group>{group}</group>\n"
        f"        <key>{key}</key>\n"
        f"        <status>0x{status:02X}</status>\n"
        f"        <midino>0x{midino:02X}</midino>\n"
        f"        <options>{opt}</options>\n"
        f"      </control>\n"
    )


def build_xml() -> str:
    out = [HEADER]
    seen: dict[tuple[int, int], str] = {}

    def emit(group, key, status, midino, button, comment):
        sig = (status, midino)
        if sig in seen:
            raise RuntimeError(
                f"Коллизия: 0x{status:02X}/0x{midino:02X} уже занято '{seen[sig]}', "
                f"пытаемся привязать '{comment}'"
            )
        seen[sig] = comment
        out.append(_control_xml(group, key, status, midino, button, comment))

    for deck, channel in mc.DECK_CHANNEL.items():
        n = mc.DECK_NUMBER[deck]
        out.append(f"\n      <!-- ===== Дека {deck}: канал {channel}, [Channel{n}], EffectUnit{n} ===== -->\n")
        for ctrl in mc.DECK_CONTROLS:
            if ctrl.kind == "range":
                g, k = mc.resolve_group_key(ctrl, deck)
                emit(g, k, 0xB0 | channel, CC_NUMBER[ctrl.id], False, f"{ctrl.id} — {ctrl.label}")
            elif ctrl.kind in ("button", "hold"):
                g, k = mc.resolve_group_key(ctrl, deck)
                emit(g, k, 0x90 | channel, NOTE_NUMBER[ctrl.id], True, f"{ctrl.id} — {ctrl.label}")
            else:  # loop / hotcue — по контролу на каждый вариант
                for v in ctrl.variants:
                    g, k = mc.resolve_group_key(ctrl, deck, variant=v)
                    emit(g, k, 0x90 | channel, mc.variant_note(ctrl.id, v), True,
                         f"{ctrl.id}[{v:g}] — {ctrl.label}")

    out.append("\n      <!-- ===== FX: юниты 1-4 на каналах 4-7, полная раскладка ===== -->\n")
    _fx_marker = len(out)
    out.append("")
    out.append("\n      <!-- ===== Мастер (канал 15) ===== -->\n")
    for ctrl in mc.MASTER_CONTROLS:
        g, k = ctrl.mixxx_group, ctrl.mixxx_key
        if ctrl.kind == "range":
            emit(g, k, 0xB0 | mc.MASTER_CHANNEL, CC_NUMBER[ctrl.id], False, f"{ctrl.id} — {ctrl.label}")
        else:
            emit(g, k, 0x90 | mc.MASTER_CHANNEL, NOTE_NUMBER[ctrl.id], True, f"{ctrl.id} — {ctrl.label}")

    # Универсальный вход: SysEx -> DaraveController.onSysex.
    # Одна строчка вместо тысяч <control>: через неё адресуется ЛЮБОЙ
    # контрол Mixxx по имени группы и ключа (см. midi_mapping.sysex_set и
    # darave-controller-scripts.js::onSysex). Быстрый путь по CC/Note выше
    # остаётся для того, что критично ко времени.
    # FX целиком: 4 юнита на отдельных каналах 4-7, по 155 контролов на
    # юнит (4 слота × 16 параметров + 16 кнопок, META, выбор эффекта,
    # вкл/выкл, маршрутизация на деки). Прямыми CC/Note, а не через SysEx —
    # это тот же механизм, которым работают ручки EQ.
    for b in mc.fx_bindings():
        emit(b["group"], b["key"], b["status"], b["number"],
             b["status"] & 0xF0 == 0x90, b["label"])

    # SysEx НЕ требует своего <control>: Mixxx сам вызывает
    # DaraveController.incomingData(data, length) для системных сообщений.
    # Раньше здесь стоял <control status="0xF0"> на DaraveController.onSysex —
    # такой функции Mixxx не ищет, и на каждое сообщение сыпался
    # "TypeError: Property 'incomingData' ... is not a function", а Mixxx
    # показывал "Код сценария должен быть исправлен".

    out.append(FOOTER)
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="куда записать XML (по умолчанию stdout)")
    args = ap.parse_args()
    xml = build_xml()
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(xml)
        n = xml.count("<control>")
        print(f"Записано: {args.out} — {n} контролов, {len(xml)} байт", file=sys.stderr)
    else:
        sys.stdout.write(xml)


if __name__ == "__main__":
    main()
