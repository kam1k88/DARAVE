"""
deck_loader.py — «положи вот этот трек на деку N и поставь иглу сюда».

## Почему это отдельный модуль, а не ещё один контрол

Контрола «загрузи файл по пути» в Mixxx НЕТ. Проверено по сборке диджея
(2.7.0-alpha), а не по документации: строка `loadTrackFromPath` в
`mixxx.exe` встречается 0 раз, в API скрипта контроллера такой функции
тоже нет. Есть только `LoadSelectedTrack` — «загрузи то, что выделено в
медиатеке», — и навигация по списку. А поле поиска через контролы не
задаётся вовсе.

Отсюда единственный надёжный путь, и он проходит через клавиатуру:

    вывести окно Mixxx вперёд -> Ctrl+F -> напечатать кусок имени ->
    Enter (фокус уходит в таблицу, выделяется первая строка) ->
    SysEx [ChannelN] LoadSelectedTrack -> [Library] clear_search

Отвергнутые альтернативы (и почему):

* **навигация по номеру строки** (`MoveVertical` на N шагов) — ломается
  от любой смены сортировки, фильтра или раздела медиатеки; диджей
  кликнул колонку BPM — и загрузился не тот трек, молча;
* **плейлист M3U8** — умеет «следующий по списку», а не «положи вот
  этот»; порядок в нём есть, адресации нет;
* **очередь Auto DJ** — это тоже «следующий», плюс Mixxx должен быть
  закрыт в момент записи очереди (см. mixxx_autodj.py).

## Что делает загрузку безопасной

1. **Фокус забирается на 1-2 секунды**, поэтому загрузка ставится ТОЛЬКО
   когда целевая дека стоит и сведение не идёт. В плане сета такое окно
   всегда есть: следующий трек грузится сразу после конца предыдущего
   перехода, а до следующего — минуты.
2. **Проверка по длительности.** Телеметрия несёт `duration` загруженного
   трека (седьмое поле пакета, см. darave-controller-scripts.js). Если
   поиск нашёл не тот трек, длительность не совпадёт — и мы честно
   сообщаем об этом, а не играем чужой трек «как будто всё хорошо».
3. **Печатаем Unicode-событиями** (KEYEVENTF_UNICODE), а не сканкодами:
   имена треков бывают на любом языке, а раскладка клавиатуры у диджея
   своя, и сканкоды напечатали бы кириллицу латиницей.

Отдельно: «положить трек на ИГРАЮЩУЮ деку» Mixxx по умолчанию запрещает
(`[Controls] AllowTrackLoadToPlayingDeck`). Мы туда и не грузим — только
на остановленную, — но если дека вдруг играет, честно отказываемся.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
import unicodedata

import midi_mapping as mm

IS_WINDOWS = sys.platform.startswith("win")

# Сколько ждать, пока Mixxx отфильтрует медиатеку после ввода запроса.
SEARCH_SETTLE_SECONDS = 0.55
# Сколько ждать появления трека на деке (телеметрия идёт раз в 100 мс).
LOAD_TIMEOUT_SECONDS = 6.0
# Расхождение длительности, после которого считаем, что загрузился не тот трек.
DURATION_TOLERANCE_SECONDS = 2.0
# Сколько строк отфильтрованного списка перебрать, если первая оказалась не той.
MAX_ROW_ATTEMPTS = 4
RETRY_TIMEOUT_SECONDS = 2.5

# --- как попасть в поле поиска медиатеки ---------------------------------
#
# Ctrl+F оказался ненадёжным: в реальном прогоне он не открывал поиск, и
# набранный текст уходил в ТАБЛИЦУ треков, где Qt трактует буквы как
# «прыжок к строке по первой букве». Отсюда и симптом, по которому это
# нашлось: на деку приезжали случайные треки — 448с, 327с, 161с — на
# запрос, которому в медиатеке отвечают ровно две записи по 390с.
#
# Надёжный путь — контрол `[Library] focused_widget`: он переводит фокус
# внутри медиатеки без единой клавиши. Беда в том, что номера виджетов в
# документации не описаны и между сборками не обещаны. Поэтому не гадаем,
# а ПОДБИРАЕМ: пробуем кандидатов по очереди, и как только загрузка дала
# трек с нужной длительностью, номер запоминается на всю сессию.
#
# "ctrl_f" в конце списка — прежний путь: если контрола в сборке нет,
# он всё ещё может сработать.
FOCUS_STRATEGIES: tuple = (3, 1, 2, "ctrl_f")
_focus_choice: object | None = None      # что сработало в этой сессии
# Сколько строк вверх «промотать» после фильтра, чтобы наверняка встать на
# первую. Дешевле, чем гадать, сохранилось ли выделение с прошлого раза.
ROWS_TO_TOP = 50


class LoadError(RuntimeError):
    """Не смогли загрузить трек — текст предназначен человеку."""


# --------------------------------------------------------------- запрос
_NOISE = re.compile(r"[\[\](){}_\-–—,;!·•.'’\"]+")
# Слова-связки: в имени файла они есть у половины треков, а значит
# ничего не отсеивают — зато занимают место в И-шном запросе и ломают
# его, если в теге написано «ft.» там, где в файле «feat.».
_STOPWORDS = {"feat", "ft", "vs", "pres", "presents", "the", "and",
              "official", "video", "free", "download", "www"}
_MULTISPACE = re.compile(r"\s+")
_LEADING_NUM = re.compile(r"^\s*\d{1,3}\s*[.\-)]?\s+")


def search_query(track_name: str, max_words: int = 4) -> str:
    """Из имени файла — короткий запрос для поля поиска Mixxx.

    Поиск в Mixxx И-шный: каждое слово должно найтись. Значит длинный
    запрос надёжнее по точности, но ломается от любой мелочи в имени
    (лишний артикль, другой регистр тире, «feat.» против «ft.»). Берём
    несколько самых длинных слов — они и самые редкие, и по ним запрос
    почти всегда единственный.
    """
    stem = track_name.rsplit(".", 1)[0] if "." in track_name[-6:] else track_name
    stem = _LEADING_NUM.sub("", stem)
    stem = _NOISE.sub(" ", stem)
    stem = _MULTISPACE.sub(" ", stem).strip()
    words = [w for w in stem.split(" ")
             if len(w) >= 3 and w.lower() not in _STOPWORDS]
    if not words:
        return stem[:24]
    # Длинные слова редки, а редкие слова — это и есть хороший запрос.
    picked = sorted(sorted(words, key=len, reverse=True)[:max_words],
                    key=lambda w: words.index(w))
    return " ".join(picked)


# ------------------------------------------------------- ввод в Windows
if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    VK_CONTROL, VK_RETURN, VK_MENU, VK_ESCAPE = 0x11, 0x0D, 0x12, 0x1B
    SW_RESTORE = 9

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUTUNION(ctypes.Union):
        # Размер должен быть не меньше MOUSEINPUT, иначе SendInput
        # отвергнет cbSize. 32 байта покрывают все варианты на x64.
        _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def _send(inputs: list) -> int:
        """Отправить события ввода. Возвращает, сколько приняла система.

        Проверять возврат обязательно: SendInput умеет вернуть 0 и не
        сказать ничего. Так бывает при UIPI — когда Mixxx запущен от
        администратора, а companion нет: Windows молча выбрасывает ввод
        в окно более привилегированного процесса. Снаружи это выглядит
        как «поиск ничего не нашёл», хотя в поле поиска просто ничего
        не напечаталось."""
        n = len(inputs)
        arr = (_INPUT * n)(*inputs)
        sent = _user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))
        if sent != n:
            raise LoadError(
                f"Windows приняла {sent} из {n} событий клавиатуры "
                f"(код {ctypes.get_last_error()}). Обычно это значит, что Mixxx "
                f"запущен с правами администратора, а DARAVE — нет: ввод в такое "
                f"окно система блокирует. Запустите их одинаково.")
        return sent

    def _key(vk: int, up: bool = False) -> _INPUT:
        return _INPUT(type=INPUT_KEYBOARD,
                      u=_INPUTUNION(ki=_KEYBDINPUT(
                          wVk=vk, wScan=0,
                          dwFlags=KEYEVENTF_KEYUP if up else 0,
                          time=0, dwExtraInfo=None)))

    def _char(ch: str, up: bool = False) -> _INPUT:
        return _INPUT(type=INPUT_KEYBOARD,
                      u=_INPUTUNION(ki=_KEYBDINPUT(
                          wVk=0, wScan=ord(ch),
                          dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                          time=0, dwExtraInfo=None)))

    def find_mixxx_window() -> int | None:
        """HWND главного окна Mixxx. Ищем по заголовку: имя класса у Qt
        одинаковое у всех Qt-приложений и ничего не различает."""
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            if "mixxx" in buf.value.lower():
                found.append(hwnd)
                return False
            return True

        _user32.EnumWindows(_cb, 0)
        return found[0] if found else None

    def focus_window(hwnd: int) -> bool:
        """Вывести окно вперёд. Windows не даёт забрать фокус фоновому
        процессу просто так — поэтому сначала «нажимаем» ALT: система
        считает это вводом пользователя и снимает блокировку."""
        _user32.ShowWindow(hwnd, SW_RESTORE)
        _send([_key(VK_MENU), _key(VK_MENU, up=True)])
        _user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        return _user32.GetForegroundWindow() == hwnd

    def read_clipboard() -> str | None:
        """Что сейчас в буфере обмена. Нужно только для самопроверки:
        напечатали запрос -> выделили -> скопировали -> сравнили. Это
        единственный способ снаружи убедиться, что текст реально дошёл до
        поля поиска Mixxx, а не потерялся по дороге."""
        CF_UNICODETEXT = 13
        if not _user32.OpenClipboard(None):
            return None
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GlobalLock.restype = ctypes.c_void_p
            ptr = k32.GlobalLock(ctypes.c_void_p(handle))
            if not ptr:
                return None
            try:
                return ctypes.c_wchar_p(ptr).value
            finally:
                k32.GlobalUnlock(ctypes.c_void_p(handle))
        finally:
            _user32.CloseClipboard()

    def type_text(text: str) -> None:
        seq = []
        for ch in unicodedata.normalize("NFC", text):
            seq.append(_char(ch))
            seq.append(_char(ch, up=True))
        # Пачками, иначе длинный запрос упирается в лимит очереди ввода.
        for i in range(0, len(seq), 40):
            _send(seq[i:i + 40])
            time.sleep(0.005)

    def press_ctrl(vk: int) -> None:
        _send([_key(VK_CONTROL), _key(vk), _key(vk, up=True), _key(VK_CONTROL, up=True)])

    def press(vk: int) -> None:
        _send([_key(vk), _key(vk, up=True)])

    def select_all_and_clear() -> None:
        """Ctrl+A, затем стереть: поле поиска может быть не пустым."""
        press_ctrl(0x41)  # 'A'
        press(0x08)       # Backspace

else:  # не Windows — модуль импортируется (тесты), но грузить не умеет
    def find_mixxx_window():  # type: ignore[misc]
        return None

    def focus_window(hwnd):  # type: ignore[misc]
        return False

    def type_text(text):  # type: ignore[misc]
        raise LoadError("клавиатурный ввод есть только в Windows-сборке companion'а")

    def press_ctrl(vk):  # type: ignore[misc]
        raise LoadError("клавиатурный ввод есть только в Windows-сборке companion'а")

    def read_clipboard():  # type: ignore[misc]
        return None

    def press(vk):  # type: ignore[misc]
        raise LoadError("клавиатурный ввод есть только в Windows-сборке companion'а")

    def select_all_and_clear():  # type: ignore[misc]
        raise LoadError("клавиатурный ввод есть только в Windows-сборке companion'а")

    VK_RETURN = 0x0D
    VK_ESCAPE = 0x1B


# --------------------------------------------------------- сама загрузка
def _group(deck: str) -> str:
    return f"[Channel{mm.DECK_NUMBER[deck.upper()]}]"


async def load_track(midi_send, store, deck: str, query: str,
                     cue_seconds: float | None = None,
                     expect_duration: float | None = None,
                     play: bool = False,
                     log=print) -> dict:
    """Найти трек поиском, положить на деку `deck`, поставить cue.

    Возвращает {"ok": bool, "duration": float|None, "error": str|None}.

    cue_seconds — куда встать иглой И где поставить точку CUE: техники
    заводят входящую деку через `cue_gotoandplay`, то есть играют
    ИМЕННО с CUE, а не с текущей позиции. Без этого шага новый трек
    заводился бы с начала файла, где у драм-н-бейса минута эмбиента.
    """
    deck = deck.upper()
    group = _group(deck)

    # Играющую деку сначала останавливаем сами. Раньше здесь стоял отказ —
    # и он срабатывал даже там, где дека уже остановлена: телеметрия
    # приходит раз в 100 мс, и «играет» могло быть картинкой полусекундной
    # давности. Отказываться было не за что: Mixxx у диджея настроен
    # «Разрешить [загрузку], но остановить деку», а мы и так грузим только
    # туда, где сведение не идёт.
    state = store.get(deck)
    if state is not None and state.playing:
        log(f"[loader] дека {deck} играла — останавливаю перед загрузкой")
        midi_send(mm.sysex_set(group, "play", 0))
        await asyncio.sleep(0.35)

    # Есть ли в телеметрии длительность. Её шлёт только обновлённый
    # darave-controller-scripts.js; пока в Mixxx лежит старая копия
    # скрипта, поле не приходит вовсе — и проверять «тот ли трек» нечем.
    # Это не повод отказываться грузить: длительность мы знаем из своей
    # базы, а факт загрузки виден по track_loaded. Просто перебирать
    # строки списка в этом режиме нельзя, и об этом надо сказать вслух.
    have_duration = any(float(getattr(st, "duration", 0.0) or 0.0) > 0
                        for st in (store.snapshot() or {}).values())

    if not IS_WINDOWS:
        return {"ok": False, "error": "загрузка треков работает только в Windows-сборке companion'а"}

    hwnd = find_mixxx_window()
    if hwnd is None:
        return {"ok": False, "error": "не нашёл окно Mixxx — он запущен?"}
    prev = ctypes.windll.user32.GetForegroundWindow()  # type: ignore[name-defined]

    before = store.get(deck)
    before_duration = getattr(before, "duration", 0.0) if before else 0.0

    global _focus_choice
    duration: float | None = None
    tried = 0
    # Запомненный способ пробуем первым, но не держимся за него: Mixxx
    # мог перезапуститься с другой раскладкой виджетов, и тогда честнее
    # перебрать заново, чем каждый раз грузить не тот трек.
    strategies = ([_focus_choice] if _focus_choice is not None else []) + [
        st for st in FOCUS_STRATEGIES if st != _focus_choice]
    try:
        if not focus_window(hwnd):
            return {"ok": False, "error": "Windows не отдал фокус окну Mixxx"}

        for strategy in strategies:
            duration, tried = await _search_and_load(
                midi_send, store, deck, group, query, strategy,
                expect_duration, have_duration, before_duration, log)
            good = duration is not None and (
                not (expect_duration and have_duration)
                or abs(duration - float(expect_duration)) <= DURATION_TOLERANCE_SECONDS)
            if good:
                _remember_focus(strategy, log)
                break
            _focus_choice = None
            if strategy != strategies[-1]:
                log(f"[loader] фокус «{strategy}» дал не тот трек — пробую следующий")
    finally:
        # Поле поиска чистим ВСЕГДА: иначе диджей вернётся к медиатеке,
        # отфильтрованной нашим запросом, и решит, что половина
        # библиотеки пропала.
        try:
            midi_send(mm.sysex_set("[Library]", "clear_search", 1))
        except Exception:
            pass
        try:
            if prev:
                ctypes.windll.user32.SetForegroundWindow(prev)  # type: ignore[name-defined]
        except Exception:
            pass

    if duration is None:
        st = store.get(deck)
        seen = ("деки не видно вовсе" if st is None else
                f"track_loaded={int(bool(st.track_loaded))}, bpm={st.bpm:.1f}, "
                f"duration={float(getattr(st, 'duration', 0.0) or 0.0):.1f}")
        hint = ("" if have_duration else
                "; длительность в телеметрии не приходит — проверьте, что в Mixxx "
                "перезагружен маппинг контроллера")
        return {"ok": False,
                "error": (f"поиск «{query}» ничего не загрузил на деку {deck} "
                          f"(в телеметрии: {seen}{hint}). "
                          f"Проверить весь путь ввода: python check_deck_load.py")}

    # Длительности в телеметрии нет — верим своей базе. Проверить, тот ли
    # трек нашёлся, в этом режиме невозможно, и мы этого не скрываем.
    warning = None
    if not have_duration:
        duration = float(expect_duration or 0.0) or duration
        warning = ("длительность из телеметрии не приходит — не могу проверить, "
                   "тот ли трек загрузился")

    if expect_duration and have_duration and abs(duration - float(expect_duration)) > DURATION_TOLERANCE_SECONDS:
        return {"ok": False, "duration": duration,
                "error": (f"на деку {deck} загрузился не тот трек: длительность "
                          f"{duration:.0f}с вместо {float(expect_duration):.0f}с "
                          f"(запрос «{query}», способов фокуса перебрано: "
                          f"{len(strategies)}, строк в последнем: {tried}). "
                          f"Похоже, поиск в медиатеке не фильтруется — проверьте "
                          f"путь ввода: python check_deck_load.py --library")}

    if cue_seconds is not None and duration and duration > 0:
        frac = max(0.0, min(1.0, float(cue_seconds) / duration))
        midi_send(mm.sysex_set(group, "playposition", frac))
        await asyncio.sleep(0.12)
        # CUE ставится ИМЕННО так: дека стоит, cue_set фиксирует точку
        # там, где сейчас игла. Дальше техника делает cue_gotoandplay.
        midi_send(mm.sysex_press(group, "cue_set"))
        await asyncio.sleep(0.05)

    if play:
        midi_send(mm.sysex_set(group, "play", 1))

    if warning:
        log(f"[loader] {warning}")
    log(f"[loader] дека {deck}: «{query}» -> {duration:.0f}с"
        + (f", cue {float(cue_seconds):.0f}с" if cue_seconds is not None else "")
        + (", играет" if play else ""))
    return {"ok": True, "duration": duration, "error": None, "warning": warning}


def _remember_focus(strategy, log) -> None:
    """Запомнить сработавший способ фокуса на всю сессию companion'а."""
    global _focus_choice
    if _focus_choice != strategy:
        _focus_choice = strategy
        log(f"[loader] фокус в поиск: «{strategy}» — дальше использую его")


async def _search_and_load(midi_send, store, deck: str, group: str, query: str,
                           strategy, expect_duration, have_duration: bool,
                           before_duration: float, log):
    """Один проход: сфокусировать поиск, отфильтровать, забрать строку.

    Возвращает (длительность загруженного трека | None, сколько строк
    просмотрено)."""
    if strategy == "ctrl_f":
        press_ctrl(0x46)
        await asyncio.sleep(0.15)
        select_all_and_clear()
    else:
        midi_send(mm.sysex_set("[Library]", "focused_widget", float(strategy)))
        await asyncio.sleep(0.15)
        # Своим контролом чище, чем Ctrl+A и Backspace: не зависит ни от
        # раскладки, ни от того, куда на самом деле смотрит фокус.
        midi_send(mm.sysex_press("[Library]", "delete_search_query"))
        await asyncio.sleep(0.1)

    type_text(query)
    await asyncio.sleep(SEARCH_SETTLE_SECONDS)
    press(VK_RETURN)                  # фокус в таблицу
    await asyncio.sleep(0.15)
    # Наверх списка: выделение могло остаться с прошлого запроса, и тогда
    # «первая строка» оказалась бы вовсе не первой.
    midi_send(mm.sysex_set("[Library]", "MoveVertical", -float(ROWS_TO_TOP)))
    await asyncio.sleep(0.2)

    duration = None
    attempts = MAX_ROW_ATTEMPTS if (expect_duration and have_duration) else 1
    prev = before_duration
    tried = 0
    for tried in range(1, attempts + 1):
        midi_send(mm.sysex_press(group, "LoadSelectedTrack"))
        duration = await _wait_loaded(
            store, deck, prev,
            LOAD_TIMEOUT_SECONDS if tried == 1 else RETRY_TIMEOUT_SECONDS,
            require_duration=have_duration)
        if duration is None:
            break
        if not (expect_duration and have_duration):
            break
        if abs(duration - float(expect_duration)) <= DURATION_TOLERANCE_SECONDS:
            break
        prev = duration
        midi_send(mm.sysex_press("[Library]", "MoveDown"))
        await asyncio.sleep(0.12)
    return duration, tried


async def _wait_loaded(store, deck: str, before_duration: float,
                       timeout: float = LOAD_TIMEOUT_SECONDS,
                       require_duration: bool = True) -> float | None:
    """Ждём, пока телеметрия покажет загруженный трек.

    Ориентир — ДЛИТЕЛЬНОСТЬ, а не флаг track_loaded: на деке мог уже
    лежать предыдущий трек, и флаг был бы «1» с самого начала.

    require_duration=False — режим совместимости со старым скриптом
    контроллера, который длительность не шлёт: там единственный доступный
    признак и есть track_loaded, и возвращаем условную «1»."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = store.get(deck)
        dur = float(getattr(st, "duration", 0.0) or 0.0) if st else 0.0
        if st is not None and st.track_loaded:
            if not require_duration:
                return 1.0
            if dur > 0 and abs(dur - before_duration) > 0.05:
                return dur
        await asyncio.sleep(0.08)
    st = store.get(deck)
    dur = float(getattr(st, "duration", 0.0) or 0.0) if st else 0.0
    if st is not None and st.track_loaded and dur > 0:
        return dur             # тот же трек уже лежал — тоже результат
    return None
