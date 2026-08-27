"""
DARAVE MIDI-пульт — ручная проверка и регулировка ВСЕХ контролов Mixxx.

Зачем: когда «агент крутит только кроссфейдер», непонятно, где обрыв —
техника не шлёт нужное действие, midi_mapping шлёт не ту ноту, XML не
привязан к контролу Mixxx, или Mixxx не подхватил маппинг. Этот пульт
шлёт MIDI напрямую, минуя backend/агента/техники, поэтому отвечает ровно
на один вопрос: «доходит ли ЭТА крутилка до Mixxx».

Источник истины — midi_mapping.py, тот же, из которого генерируется
DARAVE-Virtual-Controller.midi.xml. Значит пульт физически не может
проверять не то, что шлёт companion.

Режимы:
    python midi_console.py                    интерактивный пульт (регулировка)
    python midi_console.py --list             перечислить все контролы
    python midi_console.py --check            пошаговая проверка всего подряд
    python midi_console.py --set eq_low A 0   выставить один контрол
    python midi_console.py --sweep filter_sweep A
    python midi_console.py --dry-run ...      не слать MIDI, только печатать

Перед запуском: Mixxx открыт, в Preferences -> Controllers выбран
DARAVE Virtual Controller, на деке загружен трек.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import full_catalog
import live_control
import midi_mapping as mm
import mixxx_controls as mc

DEFAULT_PORT = "DARAVE Virtual Controller"

# Каталог берём общий (mixxx_controls.py) — раньше он дублировался здесь
# и мог разъехаться с тем, что реально шлёт companion.
CATALOG = [
    (c.group, c.id, c.kind, c.label, c.where) for c in mc.ALL_CONTROLS
]


def neutral_of(action: str) -> float:
    ctrl = mc.BY_ID.get(action)
    return ctrl.neutral if ctrl is not None else 0.5


class Port:
    """Обёртка над rtmidi с честным --dry-run."""

    def __init__(self, port_name: str, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.name = port_name
        self._out = None
        if dry_run:
            return
        try:
            import rtmidi
        except ImportError:
            raise SystemExit(
                "Нет модуля rtmidi в этом Python.\n"
                f"  Текущий: {sys.executable}\n"
                "Запускайте тем же Python, что и companion (там rtmidi точно есть) —\n"
                "обычно C:\\Users\\<вы>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe\n"
                "или используйте midi_console.bat, он найдёт подходящий сам."
            )
        out = rtmidi.MidiOut()
        ports = out.get_ports()
        match = [i for i, p in enumerate(ports) if port_name.lower() in p.lower()]
        if not match:
            raise SystemExit(
                f"MIDI-порт '{port_name}' не найден. Доступные: {ports or '(нет ни одного)'}\n"
                "Создайте порт с этим именем в loopMIDI (значок в трее -> имя -> '+')."
            )
        out.open_port(match[0])
        self._out = out
        print(f"Порт открыт: {ports[match[0]]}")

    def send(self, msg: list[int], label: str = "") -> None:
        if self.dry_run:
            print(f"  [dry-run] {[hex(b) for b in msg]}  {label}")
            return
        self._out.send_message(msg)

    def close(self) -> None:
        if self._out is not None:
            self._out.close_port()


# ---------- отправка одного действия ----------

def send_action(port: Port, action: str, deck: str, value=None) -> None:
    """Проверка и исполнение — через live_control, тот же код, которым
    пользуется ИИ-чат. Пульт обязан шевелить ровно то же, что агент, иначе
    «проверил пультом — работает, а из чата не работает»."""
    try:
        cmd = live_control.validate(deck, action, value)
    except live_control.ControlError as exc:
        raise SystemExit(str(exc)) from None
    asyncio.run(live_control.apply(lambda m: port.send(m, live_control.describe(cmd)), cmd))


def sweep(port: Port, action: str, deck: str, v_from: float = 0.0, v_to: float = 1.0,
          seconds: float = 2.0, steps: int = 40) -> None:
    """Плавно прогоняет крутилку — чтобы движение было ВИДНО в интерфейсе."""
    for i in range(steps + 1):
        v = v_from + (v_to - v_from) * i / steps
        port.send(mm.resolve_ramp_tick(action, deck, v))
        time.sleep(seconds / steps)


def _kind_of(action: str) -> str:
    ctrl = mc.BY_ID.get(action)
    if ctrl is None:
        raise SystemExit(f"Неизвестное действие: {action}. Список — python midi_console.py --list")
    return ctrl.kind


# ---------- режимы ----------

def cmd_list() -> None:
    print(f"\n{'группа':11} {'действие':16} {'тип':7} подпись")
    print("-" * 78)
    group = None
    for g, act, kind, label, where in CATALOG:
        if g != group:
            print(); group = g
        extra = ""
        if kind == "ramp":
            extra = f"  [0.0..1.0, нейтраль {neutral_of(act):g}]"
        elif kind == "loop":
            extra = f"  [длины: {', '.join(str(x) for x in mm.LOOP_SIZES)} долей]"
        print(f"{g:11} {act:16} {kind:7} {label}{extra}")
    print(f"\nДеки: A B C D. Смотреть в Mixxx: см. колонку в --check.")


def cmd_check(port: Port, deck: str, pause: float, only: str | None = None) -> None:
    """Пошагово шевелит каждый контрол. Диджей смотрит в Mixxx и отвечает."""
    items = CATALOG
    if only:
        items = [row for row in CATALOG if row[0].lower() == only.lower()]
        if not items:
            groups = sorted({row[0] for row in CATALOG})
            raise SystemExit(f"Нет группы '{only}'. Есть: {', '.join(groups)}")
    print(f"\nПроверяю контролы ({only or 'все'}) на деке {deck}: {len(items)} шт.")
    print("На деке должен быть загружен трек. После каждого шага смотрите в Mixxx.")
    print("Enter — дальше, 'п' — повторить, 'q' — выйти.\n")

    results = []
    skipped = [row for row in items if row[1] in DESTRUCTIVE]
    items = [row for row in items if row[1] not in DESTRUCTIVE]
    if skipped:
        print("Пропускаю (меняют состояние необратимо, проверяйте вручную): "
              + ", ".join(r[1] for r in skipped))
    for g, act, kind, label, where in items:
        if kind == "master":
            title = f"[{g}] {label}"
        else:
            title = f"[{g}] {label} — дека {deck}"
        while True:
            print(f"\n>>> {title}")
            print(f"    смотрите: {where}")
            try:
                _demo_action(port, act, kind, deck)
            except Exception as exc:
                print(f"    ОШИБКА отправки: {exc!r}")
                results.append((title, "ошибка"))
                break
            ans = input("    сработало? [Enter=да / н=нет / п=повторить / q=выход]: ").strip().lower()
            if ans in ("п", "p", "r"):
                continue
            if ans == "q":
                _summary(results); return
            results.append((title, "нет" if ans in ("н", "n") else "да"))
            break
        time.sleep(pause)
    _summary(results)


# Эти контролы МЕНЯЮТ состояние необратимо, их нельзя «прогнать свипом»
# при проверке: effect_selector перебирает загруженные эффекты (именно так
# в стойке FX внезапно оказываются не те эффекты, что были), eject
# выгружает трек, LoadSelectedTrack загружает поверх.
DESTRUCTIVE = {
    "effect_selector", "eject", "load_selected", "clear",
    "next_effect", "prev_effect",
}


def _demo_action(port: Port, act: str, kind: str, deck: str) -> None:
    ctrl = mc.BY_ID[act]
    if kind == "range":
        neutral = ctrl.neutral
        print("    веду вниз...");  sweep(port, act, deck, neutral, 0.0, 1.2)
        print("    веду вверх..."); sweep(port, act, deck, 0.0, 1.0, 1.2)
        print("    возвращаю в нейтраль"); sweep(port, act, deck, 1.0, neutral, 0.6)
    elif kind == "hold":
        print("    зажимаю на 2 секунды...")
        send_action(port, act, deck, value=2.0)
    elif kind == "loop":
        for beats in (4, 1):
            print(f"    луп на {beats} доли...")
            send_action(port, act, deck, value=beats); time.sleep(2.0)
        send_action(port, "loop_exit", deck); print("    вышел из лупа")
    elif kind == "hotcue":
        for n in (1, 2):
            print(f"    hotcue {n}...")
            send_action(port, act, deck, value=n); time.sleep(1.0)
    else:
        send_action(port, act, deck)
        if act in ("play_toggle", "recording_toggle", "keylock", "quantize", "slip", "pfl", "mute"):
            time.sleep(1.5)
            send_action(port, act, deck)  # переключатель — возвращаем как было
            print("    (вернул в исходное — это переключатель)")


def _summary(results: list[tuple[str, str]]) -> None:
    bad = [t for t, r in results if r != "да"]
    print("\n" + "=" * 66)
    print(f"Проверено: {len(results)}.  Работает: {len(results) - len(bad)}.  Не работает: {len(bad)}.")
    if bad:
        print("\nНЕ СРАБОТАЛО:")
        for t in bad:
            print("  -", t)
        print("\nЕсли не сработала ЦЕЛАЯ группа — почти наверняка Mixxx не подхватил")
        print("маппинг: Preferences -> Controllers -> DARAVE Virtual Controller,")
        print("снять и поставить галочку заново. Если отдельные контролы —")
        print("пришлите этот список, поправлю привязку.")
    else:
        print("Все контролы доходят до Mixxx.")
    print("=" * 66)


def cmd_fx(port: Port) -> None:
    """FX-раздел: юнит -> слот -> параметр. Отдельным экраном, потому что
    FX-контролов 620 и одним списком они нечитаемы."""
    unit, slot = 1, 1
    while True:
        ug = f"[EffectRack1_EffectUnit{unit}]"
        sg = f"[EffectRack1_EffectUnit{unit}_Effect{slot}]"
        print(f"\n=== FX: юнит {unit}, слот {slot} ===")
        print("  Юнит:  m. dry/wet(MIX)   s. суперручка   e. вкл/выкл   r. подключить к деке")
        print("  Слот:  t. META           o. вкл/выкл слота")
        print("         n. следующий эффект   p. предыдущий эффект  (МЕНЯЮТ эффект в слоте)")
        print("         1..16  — параметр слота")
        print("         b1..b16 — кнопка-параметр слота")
        print("  u. юнит 1-4    l. слот 1-4    q. назад")
        raw = input("выбор: ").strip().lower()

        if raw == "q":
            return
        if raw in ("u", "l"):
            v = input(("юнит" if raw == "u" else "слот") + " [1-4]: ").strip()
            if v in ("1", "2", "3", "4"):
                if raw == "u":
                    unit = int(v)
                else:
                    slot = int(v)
            continue
        if raw == "r":
            d = input("дека [A/B/C/D]: ").strip().upper()
            if d not in mc.DECK_CHANNEL:
                continue
            on = input("подключить? [Y/n]: ").strip().lower() != "n"
            _send_target(port, ug, f"group_[Channel{mc.DECK_NUMBER[d]}]_enable", 1.0 if on else 0.0)
            continue
        if raw in ("e", "o"):
            g = ug if raw == "e" else sg
            on = input("включить? [Y/n]: ").strip().lower() != "n"
            _send_target(port, g, "enabled", 1.0 if on else 0.0)
            continue
        if raw == "m":
            _adjust_target(port, ug, "mix", f"FX{unit} dry/wet"); continue
        if raw == "s":
            _adjust_target(port, ug, "super1", f"FX{unit} суперручка"); continue
        if raw == "t":
            _adjust_target(port, sg, "meta", f"FX{unit}.{slot} META"); continue
        if raw in ("n", "p"):
            key = "next_effect" if raw == "n" else "prev_effect"
            print("  ВНИМАНИЕ: это сменит загруженный в слот эффект.")
            if input("  продолжить? [y/N]: ").strip().lower() == "y":
                _send_target(port, sg, key, 1.0)
                _send_target(port, sg, key, 0.0)
            continue
        if raw.startswith("b") and raw[1:].isdigit() and 1 <= int(raw[1:]) <= 16:
            on = input("нажать? [Y/n]: ").strip().lower() != "n"
            _send_target(port, sg, f"button_parameter{int(raw[1:])}", 1.0 if on else 0.0)
            continue
        if raw.isdigit() and 1 <= int(raw) <= 16:
            n = int(raw)
            _adjust_target(port, sg, f"parameter{n}", f"FX{unit}.{slot} параметр {n}")
            continue


def _send_target(port: Port, group: str, key: str, value: float) -> None:
    msg = mm.fx_message(group, key, value)
    if msg is None:
        print(f"  нет прямой привязки для {group} {key}")
        return
    port.send(msg, f"{group} {key} = {value:g}")


def _adjust_target(port: Port, group: str, key: str, label: str) -> None:
    """Крутилка, адресованная по (group, key) — для FX, где имён 620."""
    value = 0.5
    print(f"\n{label}")
    print("  + / -  шаг 0.05    * / /  шаг 0.20    0 / 1 — края    Enter — назад")
    while True:
        msg = mm.fx_message(group, key, value)
        if msg is None:
            print("  нет прямой привязки"); return
        port.send(msg, label)
        print(f"  [{'#' * int(value * 30):<30}] {value:.2f}  -> CC {msg[2]}")
        raw = input("  > ").strip().lower()
        if raw == "":
            return
        step = {"+": .05, "=": .05, "-": -.05, "*": .20, "/": -.20}.get(raw)
        if step is not None:
            value = max(0.0, min(1.0, value + step))
        elif raw in ("0", "1"):
            value = float(raw)
        else:
            try:
                value = max(0.0, min(1.0, float(raw.replace(",", "."))))
            except ValueError:
                pass


def cmd_interactive(port: Port, deck: str) -> None:
    ramps = [(g, a, l) for g, a, k, l, _ in CATALOG if k == "range" and g != "FX"]
    while True:
        print(f"\n=== ПУЛЬТ (дека {deck}) ===")
        for i, (g, a, l) in enumerate(ramps, 1):
            print(f"  {i:2}. {g:10} {l}")
        print("\n   f. FX — все 620 контролов (4 юнита x 4 слота x 16 параметров)")
        print("   d. сменить деку     c. проверка всего     l. список всего     q. выход")
        raw = input("выбор: ").strip().lower()
        if raw == "q":
            return
        if raw == "f":
            cmd_fx(port); continue
        if raw == "d":
            new = input("дека [A/B/C/D]: ").strip().upper()
            if new in mc.DECK_CHANNEL:
                deck = new
            continue
        if raw == "c":
            cmd_check(port, deck, 0.3); continue
        if raw == "l":
            cmd_list(); continue
        if not raw.isdigit() or not (1 <= int(raw) <= len(ramps)):
            continue
        _, act, label = ramps[int(raw) - 1]
        _adjust_loop(port, act, deck, label)


def _adjust_loop(port: Port, act: str, deck: str, label: str) -> None:
    neutral = neutral_of(act)
    value = neutral
    print(f"\n{label} — дека {deck}")
    print("  + / -  шаг 0.05     * / /  шаг 0.20     0 = минимум, 1 = максимум")
    print(f"  n = нейтраль ({neutral}),  s = плавный прогон,  Enter = назад")
    while True:
        msg = mm.resolve_ramp_tick(act, deck, value)
        port.send(msg, f"{act}@{deck}")
        bar = "#" * int(value * 30)
        print(f"  [{bar:<30}] {value:.2f}  -> CC {msg[2]}")
        raw = input("  > ").strip().lower()
        if raw == "":
            return
        if raw in ("+", "="):
            value = min(1.0, value + 0.05)
        elif raw == "-":
            value = max(0.0, value - 0.05)
        elif raw == "*":
            value = min(1.0, value + 0.20)
        elif raw == "/":
            value = max(0.0, value - 0.20)
        elif raw == "0":
            value = 0.0
        elif raw == "1":
            value = 1.0
        elif raw == "n":
            value = neutral
        elif raw == "s":
            sweep(port, act, deck, 0.0, 1.0, 2.0); value = 1.0
        else:
            try:
                value = max(0.0, min(1.0, float(raw.replace(",", "."))))
            except ValueError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=DEFAULT_PORT, help="имя виртуального MIDI-порта loopMIDI")
    ap.add_argument("--deck", default="A", choices=list(mm.DECK_CHANNEL), help="дека по умолчанию")
    ap.add_argument("--dry-run", action="store_true", help="не слать MIDI, только печатать сообщения")
    ap.add_argument("--list", action="store_true", help="перечислить все контролы и выйти")
    ap.add_argument("--check", action="store_true", help="пошаговая проверка всех контролов")
    ap.add_argument("--pause", type=float, default=0.3, help="пауза между шагами --check")
    ap.add_argument("--find", metavar="ЗАПРОС",
                    help="искать контрол в полном каталоге Mixxx (2968 управляемых)")
    ap.add_argument("--raw", nargs="+", metavar="KEY=V",
                    help="дёрнуть любой контрол по ключу: --raw parameter3 unit=1 slot=2 value=0.7")
    ap.add_argument("--only", metavar="ГРУППА",
                    help="проверять только одну группу: EQ, Микшер, Темп, Транспорт, Луп, "
                         "Hotcue, FX, Мастер, Библиотека (регистр не важен)")
    ap.add_argument("--set", nargs="+", metavar="ACTION DECK [VALUE]",
                    help="выставить один контрол: --set eq_low A 0 (для кнопок значение не нужно)")
    ap.add_argument("--sweep", nargs=2, metavar=("ACTION", "DECK"), help="плавно прогнать крутилку")
    args = ap.parse_args()

    if args.list:
        cmd_list(); return 0

    if args.find:
        st = full_catalog.stats()
        print(f"\nКаталог: {st['covered']} управляемых контролов Mixxx "
              f"(из {st['in_dump']} всего), {st['templates']} шаблонов\n")
        hits = full_catalog.search(args.find, limit=30)
        if not hits:
            print("Ничего не найдено."); return 1
        print(f"{'ключ':32} {'раздел':10} {'тип':7} нужен адрес")
        print("-" * 74)
        for h in hits:
            print(f"{h['key']:32} {h['family']:10} {h['kind']:7} {'+'.join(h['needs']) or '—'}")
        return 0

    port = Port(args.port, dry_run=args.dry_run)
    try:
        if args.raw:
            kw = {"key": args.raw[0]}
            for tok in args.raw[1:]:
                if "=" not in tok:
                    raise SystemExit(f"ожидалось имя=значение, получено '{tok}'")
                k, v = tok.split("=", 1)
                kw[k] = v if k in ("value", "deck", "family") else int(v)
            try:
                cmd = live_control.validate_raw(**kw)
            except live_control.ControlError as exc:
                raise SystemExit(str(exc)) from None
            asyncio.run(live_control.apply(
                lambda m: port.send(m, live_control.describe(cmd)), cmd))
        elif args.set:
            if len(args.set) < 2:
                raise SystemExit("Нужно как минимум: --set <контрол> <дека>")
            action, deck = args.set[0], args.set[1].upper()
            raw = args.set[2] if len(args.set) > 2 else None
            send_action(port, action, deck, value=raw)
        elif args.sweep:
            action, deck = args.sweep[0], args.sweep[1].upper()
            sweep(port, action, deck)
        elif args.check:
            cmd_check(port, args.deck, args.pause, args.only)
        else:
            cmd_interactive(port, args.deck)
    finally:
        port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
