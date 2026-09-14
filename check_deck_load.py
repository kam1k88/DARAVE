"""
check_deck_load.py — почему трек не встаёт на деку. Проверка по шагам.

Загрузка трека — единственное место DARAVE, которое идёт не через MIDI, а
через клавиатуру и окно Mixxx (контрола «загрузи файл по пути» в Mixxx
нет — см. deck_loader.py). Значит и сломаться она может там, где обычная
телеметрия ничего не покажет: окно не нашлось, Windows не отдала фокус,
ввод заблокирован разницей прав, Ctrl+F не открыл поиск, поиск ничего не
нашёл по нашему запросу.

Снаружи все пять случаев выглядят одинаково — «ничего не загрузилось».
Здесь они разделены, и каждый проверяется отдельно.

Главный приём: **буфер обмена как зеркало**. Напечатали запрос в поле
поиска, выделили его (Ctrl+A) и скопировали (Ctrl+C) — если в буфере наш
текст, значит ввод дошёл до Mixxx, и виноват не он. Другого способа
заглянуть в чужое поле ввода снаружи нет.

Запуск (на машине с Mixxx, при ОТКРЫТОМ Mixxx):

    python check_deck_load.py
    python check_deck_load.py --query "Hybrid Minds Summer"
    python check_deck_load.py --library     # ещё и поиск по базе Mixxx
"""
from __future__ import annotations

import argparse
import sys
import time

import deck_loader as dl

OK, BAD = "  OK  ", "  СБОЙ"


def step(name: str, ok: bool, detail: str = "") -> bool:
    print((OK if ok else BAD) + f"  {name}" + (f" — {detail}" if detail else ""))
    return ok


def check_input(query: str) -> bool:
    if not dl.IS_WINDOWS:
        return step("Windows", False, "клавиатурный ввод есть только в Windows-сборке")

    hwnd = dl.find_mixxx_window()
    if not step("окно Mixxx найдено", hwnd is not None,
                "запустите Mixxx" if hwnd is None else f"hwnd={hwnd}"):
        return False

    import ctypes
    prev = ctypes.windll.user32.GetForegroundWindow()
    try:
        if not step("Windows отдала фокус окну Mixxx", dl.focus_window(hwnd)):
            print("       фокус забирают полноэкранные приложения и окна с более "
                  "высокими правами; сверните лишнее и повторите")
            return False

        try:
            dl.press_ctrl(0x46)                     # Ctrl+F
            time.sleep(0.2)
            dl.select_all_and_clear()
            dl.type_text(query)
        except dl.LoadError as exc:
            return step("клавиатурный ввод принят Windows", False, str(exc))
        step("клавиатурный ввод принят Windows", True)

        time.sleep(0.5)
        dl.press_ctrl(0x41)                          # Ctrl+A
        time.sleep(0.1)
        dl.press_ctrl(0x43)                          # Ctrl+C
        time.sleep(0.25)
        got = dl.read_clipboard()
        landed = (got or "").strip() == query.strip()
        step("запрос дошёл до поля поиска Mixxx", landed,
             f"в буфере: {got!r}" if not landed else f"«{got}»")
        if not landed:
            print("       если в буфере пусто или чужой текст — Ctrl+F открыл не "
                  "поиск, либо ввод ушёл в другое окно")

        dl.press(dl.VK_RETURN)                       # фокус в таблицу треков
        time.sleep(0.2)
        print("\n       Посмотрите на Mixxx: в медиатеке должна остаться отфильтрованная")
        print("       выборка, и в ней выделена строка. Если строк нет — поиск не нашёл")
        print("       трек по этим словам; если есть, но выделения нет — Enter не")
        print("       перевёл фокус в таблицу.")
        return landed
    finally:
        try:
            dl.press(dl.VK_ESCAPE)
            if prev:
                ctypes.windll.user32.SetForegroundWindow(prev)
        except Exception:
            pass


def check_library(query: str) -> None:
    """Найдёт ли Mixxx этот запрос по своей базе — без окна и клавиатуры."""
    import sqlite3

    from mixxx_cues import DEFAULT_MIXXXDB

    words = [w for w in query.split() if w]
    conn = sqlite3.connect(f"file:{DEFAULT_MIXXXDB}?mode=ro", uri=True)
    where = " AND ".join(
        ["(l.artist LIKE ? OR l.title LIKE ? OR l.album LIKE ? OR tl.location LIKE ?)"] * len(words))
    args: list[str] = []
    for w in words:
        args += [f"%{w}%"] * 4
    rows = conn.execute(
        f"""SELECT l.artist, l.title, l.duration, tl.location FROM library l
              JOIN track_locations tl ON tl.id = l.location
             WHERE COALESCE(l.mixxx_deleted,0)=0 AND {where}
             ORDER BY tl.location LIMIT 10""", args).fetchall()
    conn.close()
    print(f"\n  Медиатека Mixxx по запросу «{query}»: строк {len(rows)}")
    for artist, title, dur, loc in rows:
        print(f"    {float(dur or 0):7.1f}с  {artist} — {title}   [{loc.rsplit(chr(92),1)[-1].rsplit('/',1)[-1]}]")
    if not rows:
        print("    Ничего. Значит дело не в клавиатуре: этих слов нет ни в тегах,")
        print("    ни в пути. Проверьте, что папка отсканирована в самом Mixxx.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Почему трек не встаёт на деку")
    ap.add_argument("--query", default="Hybrid Minds Summer Rain")
    ap.add_argument("--library", action="store_true", help="ещё и поиск по базе Mixxx")
    args = ap.parse_args()

    print(f"Запрос: «{args.query}»")
    print("Не трогайте мышь и клавиатуру три секунды — окно Mixxx выйдет вперёд.\n")
    ok = check_input(args.query)
    if args.library:
        check_library(args.query)
    print("\nИТОГ:", "путь ввода рабочий" if ok else "путь ввода оборван — см. первый СБОЙ выше")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
