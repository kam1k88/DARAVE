"""
autodj_loader.py — треки попадают на деки очередью Auto DJ, а не поиском.

## Почему так, а не «загрузи файл по пути»

Такого контрола в Mixxx нет. Проверено по сборке диджея (2.7.0-alpha),
а не по документации:

* `loadTrackFromPath` — 0 вхождений в `mixxx.exe`; наружу загрузка по
  пути не выведена ни контролом, ни скриптовым API. Внутри функция есть
  (`slotLoadLocationToPlayer`, 3 вхождения) — её дёргают командная строка
  и перетаскивание мышью, но снаружи до неё не дотянуться;
* single-instance нет (`QLocalServer`, `QSharedMemory` — по нулям), так
  что `mixxx.exe "файл"` при уже запущенном Mixxx ничего не загрузит:
  командная строка работает только при старте;
* OSC в сборке нет (`liblo`, `oscpack` — по нулям).

Прошлая попытка шла через поле поиска и клавиатуру. Она провалилась не
случайно, а по существу: Ctrl+F не открывал поиск, набранный текст уходил
в ТАБЛИЦУ треков, где Qt трактует буквы как «прыжок к строке по первой
букве», и на деку приезжали случайные треки. Даже когда это чинится,
подход остаётся негодным: он отбирает у диджея фокус и клавиатуру,
зависит от того, что открыто в медиатеке, и работает через раз.

## Что осталось — и почему этого достаточно

**Очередь Auto DJ.** Это единственное место, где Mixxx сам решает, какой
трек положить на деку. По AutoDJProcessor: если одна дека уже играет,
включение `[AutoDJ] enabled` кладёт следующий трек очереди **в свободную
деку** и переходит в ожидание, ничего не сводя. Значит Auto DJ можно
использовать не как «пусть Mixxx играет сам», а как ЗАГРУЗЧИК:

    включили [AutoDJ] enabled -> Mixxx положил следующий трек -> выключили

Полсекунды, ни одной клавиши, ни фокуса, ни выделения — всё по SysEx из
бэкенда. Кроссфейдер при включении Auto DJ уводит к играющей деке, то
есть ровно туда, где он и должен стоять перед переходом; при выключении
не трогается (`[Auto DJ] center_xfader_when_disabling 0`).

## Чем за это платим

Очередь — это плейлист `id=1` в библиотеке Mixxx, и записать её можно
только при ЗАКРЫТОМ Mixxx: он держит библиотеку в памяти. То есть
порядок сета готовится до запуска Mixxx, а поменять план на ходу нельзя —
надо закрыть Mixxx и переписать очередь.

Взамен исчезает единственное место DARAVE, которое лезло в чужой
интерфейс. Порядок дек при этом всё равно наш: очередь и есть план.
"""
from __future__ import annotations

import asyncio
import time

import midi_mapping as mm

# Сколько ждать, пока Mixxx положит трек на свободную деку.
LOAD_TIMEOUT_SECONDS = 10.0
# Пауза между «включили Auto DJ» и «выключили»: нужна, только чтобы он
# успел взять трек из очереди. Фейд за это время не начнётся — он
# считается от позиции играющего трека, до неё минуты.
AUTODJ_SETTLE_SECONDS = 0.25
# Расхождение длительности, после которого считаем, что приехал не тот трек.
DURATION_TOLERANCE_SECONDS = 2.0

AUTODJ = "[AutoDJ]"


def _group(deck: str) -> str:
    return f"[Channel{mm.DECK_NUMBER[deck.upper()]}]"


def _loaded_map(store) -> dict[str, tuple[bool, float]]:
    out = {}
    for name, st in (store.snapshot() or {}).items():
        out[name] = (bool(getattr(st, "track_loaded", False)),
                     float(getattr(st, "duration", 0.0) or 0.0))
    return out


async def load_next(midi_send, store, deck: str,
                    cue_seconds: float | None = None,
                    expect_duration: float | None = None,
                    play: bool = False,
                    log=print) -> dict:
    """Взять следующий трек очереди Auto DJ и положить его на деку.

    Деку выбирает сам Mixxx — он кладёт на свободную. Мы её не называем,
    а ПРОВЕРЯЕМ: если приехало не туда, где ждали, это рассинхрон плана и
    очереди, и молчать об этом нельзя.
    """
    deck = deck.upper()
    group = _group(deck)

    state = store.get(deck)
    if state is not None and state.playing:
        log(f"[autodj] дека {deck} играла — останавливаю перед загрузкой")
        midi_send(mm.sysex_set(group, "play", 0))
        await asyncio.sleep(0.35)

    before = _loaded_map(store)
    if before.get(deck, (False, 0.0))[0]:
        # На целевой деке ещё лежит трек — Auto DJ сочтёт её занятой и
        # положит мимо. Снимаем.
        log(f"[autodj] на деке {deck} ещё лежал трек — снимаю")
        midi_send(mm.sysex_press(group, "eject"))
        await asyncio.sleep(0.4)
        before = _loaded_map(store)

    midi_send(mm.sysex_set(AUTODJ, "enabled", 1))
    await asyncio.sleep(AUTODJ_SETTLE_SECONDS)
    landed_on, duration = await _wait_landed(store, before)
    midi_send(mm.sysex_set(AUTODJ, "enabled", 0))
    await asyncio.sleep(0.15)

    if landed_on is None:
        return {"ok": False,
                "error": ("Auto DJ не положил трек ни на одну деку. Очередь пуста? "
                          "Порядок сета кладётся в неё кнопкой «Очередь для Mixxx» "
                          "при ЗАКРЫТОМ Mixxx.")}
    if landed_on != deck:
        return {"ok": False, "duration": duration,
                "error": (f"Auto DJ положил трек на деку {landed_on}, а план ждал {deck}. "
                          f"Освободите вторую деку и повторите.")}
    if expect_duration and duration and abs(duration - float(expect_duration)) > DURATION_TOLERANCE_SECONDS:
        return {"ok": False, "duration": duration,
                "error": (f"из очереди Auto DJ приехал не тот трек: длительность "
                          f"{duration:.0f}с вместо {float(expect_duration):.0f}с. "
                          f"Очередь не совпадает с планом — перезапишите её "
                          f"при закрытом Mixxx.")}

    if cue_seconds is not None and duration and duration > 0:
        frac = max(0.0, min(1.0, float(cue_seconds) / duration))
        midi_send(mm.sysex_set(group, "playposition", frac))
        await asyncio.sleep(0.12)
        # Дека стоит, поэтому cue_set фиксирует метку там, где сейчас
        # игла. Техники заводят входящую деку через cue_gotoandplay —
        # то есть играют именно с этой метки.
        midi_send(mm.sysex_press(group, "cue_set"))
        await asyncio.sleep(0.05)

    if play:
        midi_send(mm.sysex_set(group, "play", 1))

    log(f"[autodj] дека {deck}: трек {duration:.0f}с"
        + (f", cue {float(cue_seconds):.0f}с" if cue_seconds is not None else "")
        + (", играет" if play else ""))
    return {"ok": True, "duration": duration, "error": None, "deck": deck}


async def _wait_landed(store, before: dict) -> tuple[str | None, float]:
    """Ждём, на какой деке появился трек. Возвращает (дека, длительность)."""
    deadline = time.monotonic() + LOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        now = _loaded_map(store)
        for name, (loaded, duration) in now.items():
            was_loaded, was_duration = before.get(name, (False, 0.0))
            if loaded and duration > 0 and (not was_loaded or abs(duration - was_duration) > 0.05):
                return name, duration
        await asyncio.sleep(0.1)
    return None, 0.0
