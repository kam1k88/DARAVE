// DARAVE Virtual Controller — телеметрия + универсальный SysEx-мост.
//
// Здесь ДВА канала:
//
// 1. Mixxx -> companion: телеметрия (BPM/позиция/play/track_loaded) раз в
//    TICK_MS. Декларативно не выразить: <output> в XML умеет слать только
//    on/off по одному контролу, а нам нужно собрать несколько значений в
//    один пакет.
//
// 2. companion -> Mixxx: УНИВЕРСАЛЬНЫЙ мост (onSysex ниже). Быстрые,
//    критичные ко времени вещи (кроссфейдер, EQ, свипы внутри MixPlan)
//    идут прямыми CC/Note через DARAVE-Virtual-Controller.midi.xml — так
//    меньше задержка и джиттер. Но в Mixxx около 38 000 контролов, и
//    расписать их все отдельными <control> нереально: только на одной деке
//    их 1150, а MIDI даёт 128 нот и 128 CC на канал. Поэтому всё
//    остальное — в том числе КАЖДЫЙ параметр КАЖДОГО эффекта, выбор
//    эффекта в слоте, сэмплеры, микрофоны, Auto DJ — адресуется по имени
//    через SysEx: DARAVE шлёт "S|группа|ключ|значение", скрипт выполняет
//    engine.setValue(). Ничего не нужно предварительно маплить.
//
// А вот обратный канал (Mixxx -> companion: BPM/позиция/play/track_loaded)
// декларативно не выразить — <output> в XML умеет слать только on/off по
// одному контролу, а нам нужно раз в TICK_MS собрать несколько значений в
// один SysEx-пакет. Поэтому телеметрия — здесь, таймером.
//
// Формат payload (см. companion/telemetry.py::parse_sysex_payload):
//   "<deck>,<play 0|1>,<bpm>,<pos 0..1>,<track_loaded 0|1>"
//   пример: "A,1,128.00,0.4123,1"
// Плюс раз в тик — глобальный статус записи (не по деке): "R,<status>"
//   status = engine.getValue("[Recording]", "status"): 0=нет, 1=пишет, 2=ошибка
// SysEx-обвязка: [0xF0, ...ascii байты payload..., 0xF7]

var DaraveController = {};

// По умолчанию телеметрия по декам A/B — большинство сетапов двухдечные.
// C/D уже замаплены в DARAVE-Virtual-Controller.midi.xml (облегчённая
// раскладка — см. её докстринг) для техник вроде Triple Drop, которым
// нужна 3-я/4-я дека одновременно. Если у вас реально 4 деки в Mixxx —
// раскомментируйте две строки ниже, чтобы телеметрия шла и по ним:
DaraveController.decks = [
    {group: "[Channel1]", label: "A"},
    {group: "[Channel2]", label: "B"},
    // {group: "[Channel3]", label: "C"},
    // {group: "[Channel4]", label: "D"},
];

DaraveController.TICK_MS = 100;
DaraveController.timerId = 0;

DaraveController.init = function (id, debugging) {
    if (debugging) {
        console.log("DARAVE Virtual Controller: init(), id=" + id);
    }
    DaraveController.timerId = engine.beginTimer(
        DaraveController.TICK_MS, DaraveController.sendTelemetry, false);
};

DaraveController.shutdown = function () {
    if (DaraveController.timerId) {
        engine.stopTimer(DaraveController.timerId);
        DaraveController.timerId = 0;
    }
    console.log("DARAVE Virtual Controller: shutdown()");
};

DaraveController.sendTelemetry = function () {
    for (let i = 0; i < DaraveController.decks.length; i++) {
        DaraveController.sendDeckTelemetry(DaraveController.decks[i]);
    }
    DaraveController.sendRecordingStatus();
};

DaraveController.sendRecordingStatus = function () {
    const status = engine.getValue("[Recording]", "status") || 0;
    DaraveController.sendSysex("R," + status);
};

// ---------------------------------------------------------------------
// Универсальный вход: companion -> Mixxx.
//
// Формат (ASCII внутри SysEx, 7-битные байты):
//     S|<группа>|<ключ>|<значение>      engine.setValue()
//     T|<группа>|<ключ>                 переключить 0<->1
//     P|<группа>|<ключ>                 «нажатие»: 1, затем 0
// Разделитель "|" выбран потому, что в именах групп есть запятые не
// встречаются, а вот сами группы содержат скобки: [EffectRack1_EffectUnit1].
//
// Пример: S|[EffectRack1_EffectUnit1_Effect2]|parameter3|0.75

DaraveController.PRESS_RELEASE_MS = 60;

// ВАЖНО про имя: Mixxx доставляет SysEx НЕ в произвольную функцию из
// <control>, а строго в <functionprefix>.incomingData(data, length).
// Пока обработчик назывался onSysex, Mixxx на каждое сообщение бросал
// "TypeError: Property 'incomingData' ... is not a function" и показывал
// "Код сценария должен быть исправлен". Никакого <control> со статусом
// 0xF0 для этого не нужно — вызов происходит автоматически.
DaraveController.incomingData = function (data, length) {
    let payload = "";
    // data[0] = 0xF0, data[length-1] = 0xF7 — тело между ними
    for (let i = 1; i < length - 1; i++) {
        payload += String.fromCharCode(data[i]);
    }
    DaraveController.applyCommand(payload);
};

DaraveController.applyCommand = function (payload) {
    const parts = payload.split("|");
    const op = parts[0];

    if (op === "S" && parts.length >= 4) {
        const value = parseFloat(parts[3]);
        if (isNaN(value)) {
            print("[DARAVE] не число в S: " + payload);
            return;
        }
        DaraveController.safeSet(parts[1], parts[2], value);
        return;
    }

    if (op === "T" && parts.length >= 3) {
        const cur = engine.getValue(parts[1], parts[2]);
        DaraveController.safeSet(parts[1], parts[2], cur ? 0 : 1);
        return;
    }

    if (op === "P" && parts.length >= 3) {
        // Кнопка: нажать и отпустить. Без отпускания часть контролов Mixxx
        // (те, что реагируют на фронт) остаются «зажатыми».
        DaraveController.safeSet(parts[1], parts[2], 1);
        engine.beginTimer(DaraveController.PRESS_RELEASE_MS, function () {
            DaraveController.safeSet(parts[1], parts[2], 0);
        }, true);
        return;
    }

    print("[DARAVE] не разобрал команду: " + payload);
};

DaraveController.safeSet = function (group, key, value) {
    // Оборачиваем в try/catch, а не проверяем через getValue: у
    // несуществующего контрола Mixxx возвращает не undefined, а 0, так что
    // отличить «контрола нет» от «он равен нулю» этой проверкой нельзя, а
    // необработанное исключение здесь роняет весь скрипт-движок.
    try {
        engine.setValue(group, key, value);
    } catch (err) {
        print("[DARAVE] не смог выставить " + group + " " + key + ": " + err);
    }
};

DaraveController.sendSysex = function (payload) {
    const bytes = [0xF0];
    for (let i = 0; i < payload.length; i++) {
        bytes.push(payload.charCodeAt(i) & 0x7F);
    }
    bytes.push(0xF7);
    midi.sendSysexMsg(bytes, bytes.length);
};

DaraveController.sendDeckTelemetry = function (deck) {
    const group = deck.group;

    const trackLoaded = engine.getValue(group, "track_loaded") ? 1 : 0;
    const playing = engine.getValue(group, "play") ? 1 : 0;
    const bpm = engine.getValue(group, "bpm") || 0;

    // playposition официально в диапазоне [-0.14, 1.14] (пре/пост-ролл до
    // начала/после конца трека) — companion/state_store.py ждёт 0..1,
    // поэтому клампим на границах.
    let position = engine.getValue(group, "playposition") || 0;
    if (position < 0) {
        position = 0;
    } else if (position > 1) {
        position = 1;
    }

    const payload = deck.label + "," + playing + "," + bpm.toFixed(2) + "," +
        position.toFixed(4) + "," + trackLoaded;

    DaraveController.sendSysex(payload);
};
