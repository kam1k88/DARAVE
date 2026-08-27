"""
DJAgent — LLM с tool-use поверх выбираемого провайдера (llm_providers.py):
Gemini (облако, бесплатный тир, но недоступен из РФ) или Ollama (локальная
модель у диджея, без geo-блоков и без денег вообще). Выбор — переменная
окружения DARAVE_LLM_PROVIDER=gemini|ollama (по умолчанию gemini).

Контракт вокруг не меняется от выбора провайдера: вход (текст пользователя
+ телеметрия дек), выход (текстовый ответ в чат + опционально MixPlan для
companion). Модель решает КОГДА звать build_transition_plan и (опционально)
КАКУЮ технику сведения (см. techniques.py — 20+ техник с параметрами) —
саму механику перехода (тайминги, MIDI Note/CC) по-прежнему считает
детерминированный код в techniques.py::build_plan, не LLM: доверять модели
точный тайминг перехода рискованно, а "когда/какими деками/какая техника"
— как раз то, что она умеет и должна решать.

Один DJAgent — на одну комнату (SessionRoom, см. session.py): у каждого
арендатора своя история диалога, комнаты друг друга не видят.
"""
from __future__ import annotations

import json

from llm_providers import LLMProvider, make_provider
from techniques import TECHNIQUES, build_plan

import full_catalog
import live_control
import mixxx_controls

# Сколько последних (role, text) реплик истории держим — грубый предохранитель
# от неограниченного роста контекста на длинном сете, не настоящее резюме.
MAX_HISTORY_MESSAGES = 40


def _techniques_context() -> str:
    lines = [
        f"- {t.id} «{t.name}» (сложность {t.difficulty}/5): {t.description}"
        for t in TECHNIQUES.values()
    ]
    return "\n".join(lines)


def _system_prompt() -> str:
    controls = mixxx_controls.describe_for_agent()
    catalog_covered = full_catalog.stats()["covered"]
    catalog_families = full_catalog.describe_families()
    effect_params = full_catalog.describe_effect_params()
    return f"""\
Ты — DJAgent, ИИ-ассистент диджея в системе DARAVE. Ты помогаешь диджею \
делать переходы между треками, общаясь с ним в чате коротко и по делу, как \
опытный напарник за пультом. Отвечай на том языке, на котором пишет диджей.

Тебе на каждом ходу присылают актуальную телеметрию дек (какие треки \
загружены, играют ли, BPM, позиция в треке 0..1). Опирайся на неё — не \
выдумывай состояние дек, которого там нет.

Когда диджей просит сделать переход/микс/свитч — вызови build_transition_plan \
с decks, между которыми переходим, и, если из запроса ясно, КАКОЙ техникой \
(например "с эхом" -> Echo Cut, "с бэкспином" -> Backspin Transition) — \
technique_id из библиотеки ниже. Если техника не важна или не указана — не \
указывай technique_id, будет использован базовый Long Blend. Сама механика \
перехода (тайминги, MIDI Note/CC) считается детерминированно на сервере, \
тебе не нужно и не следует придумывать MIDI-параметры самому.

Библиотека техник сведения (technique_id — название: описание):
{_techniques_context()}

Кроме переходов ты управляешь ЛЮБОЙ ручкой и кнопкой Mixxx напрямую — \
инструментом set_mixxx_control. Им делается всё, что диджей крутит руками: \
EQ, фильтр, gain, громкость, кроссфейдер, FX (не забудь fx_enable — без \
него FX-юнит не подключён к деке и крутилки эффекта не слышны), лупы, \
hotcue, питч, keylock, quantize, запись. Один вызов = одна ручка; чтобы \
сделать несколько движений, вызывай инструмент несколько раз подряд.

Значения ручек — 0..1, где 0 это минимум контрола Mixxx, 1 — максимум. \
ВАЖНО: «нейтраль» у ручек разная и указана в списке ниже: у EQ и gain \
нейтраль 0.25 (это штатная единица, диапазон Mixxx 0..4), у питч-фейдера \
0.5, у фейдера громкости 1.0. Вместо числа можно передать "neutral", \
"min" или "max". Если диджей просит «убрать низ» — это eq_low = 0, а \
«вернуть низ» — eq_low = "neutral", НЕ 1.0 (1.0 это буст +12дБ).

Частые контролы (id — что это — [где применимо, диапазон]):
{controls}

Это лишь ходовая часть. Полный движок Mixxx — {catalog_covered} управляемых \
контролов, включая КАЖДЫЙ параметр КАЖДОГО эффекта (parameter1..16 и \
button_parameter1..16 в каждом из 4 слотов каждого из 4 FX-юнитов), выбор \
эффекта в слоте, сэмплеры, микрофоны, Auto DJ, прыжки и лупы всех длин. \
Если нужного нет в списке выше — НЕ отказывайся и не выдумывай id: вызови \
find_mixxx_control с описанием («параметр эхо», «beatjump», «выбор \
эффекта»), получи точный ключ и адресуй его через set_mixxx_raw.

Разделы полного каталога:
{catalog_families}

Какой parameterN за что отвечает — зависит от того, какой эффект загружен в \
слот. Ходовые встроенные эффекты Mixxx:
{effect_params}

Про FX важно помнить: сама крутилка эффекта ничего не даст, пока FX-юнит не \
подключён к деке. Порядок такой: fx_enable на нужной деке -> выбрать эффект \
в слоте (next_effect / effect_selector) -> включить слот (enabled) -> \
крутить meta или parameterN -> поднять fx_mix.

Если непонятно, какие деки использовать (не видно двух загруженных треков, \
или не ясно откуда-куда переходим) — не вызывай инструмент, а уточни у \
диджея коротким вопросом.

Отвечай кратко (1-3 предложения) — это чат во время живого сета, а не эссе.
"""


def _control_tool_schema() -> dict:
    return {
        "name": "set_mixxx_control",
        "description": (
            "Дёргает ОДИН контрол Mixxx прямо сейчас: ручку EQ, фильтр, gain, "
            "громкость, кроссфейдер, FX, луп, hotcue, питч, кнопку транспорта и т.п. "
            "Это то же самое, что диджей сделал бы рукой на пульте. Для многошаговых "
            "переходов по долям используй build_transition_plan, а не этот инструмент."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "control": {
                    "type": "string", "enum": sorted(mixxx_controls.BY_ID),
                    "description": "ID контрола из списка в системной подсказке (например eq_low, fx_enable, loop_activate).",
                },
                "deck": {
                    "type": "string", "enum": list(mixxx_controls.DECK_CHANNEL),
                    "description": "Дека A/B/C/D. Для мастер-контролов (кроссфейдер, запись) игнорируется.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Для ручек — число 0..1 либо 'neutral'/'min'/'max' (нейтраль у EQ и gain "
                        "0.25, у питча 0.5, у громкости 1.0). Для лупа — длина в долях "
                        "(0.5/1/2/4/8/16). Для hotcue — номер 1..8. Для удержаний "
                        "(fx_enable, sync_lock, reverse_hold) — сколько секунд держать. "
                        "Для обычных кнопок не нужно."
                    ),
                },
            },
            "required": ["control"],
        },
    }


def _find_tool_schema() -> dict:
    return {
        "name": "find_mixxx_control",
        "description": (
            "Ищет контрол в ПОЛНОМ каталоге Mixxx по описанию или части имени. "
            "Вызывай, когда нужного контрола нет в списке частых: параметры "
            "конкретных эффектов, выбор эффекта, сэмплеры, редкие лупы/прыжки. "
            "Возвращает точные ключи — их потом передавай в set_mixxx_raw."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Что ищем: 'параметр эффекта', 'beatjump', 'effect_selector', 'сэмплер громкость'.",
                },
            },
            "required": ["query"],
        },
    }


def _raw_tool_schema() -> dict:
    return {
        "name": "set_mixxx_raw",
        "description": (
            "Дёргает ЛЮБОЙ контрол Mixxx по его ключу — то, чего нет среди частых. "
            "Ключ бери из find_mixxx_control, а не выдумывай. Обязательно укажи "
            "адрес: deck для контролов деки, unit+slot для параметров эффекта, "
            "index для сэмплера/микрофона."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Точный ключ Mixxx, например parameter3 или beatjump_32_forward."},
                "deck": {"type": "string", "enum": ["A", "B", "C", "D"], "description": "Дека — для контролов деки, её EQ и фильтра."},
                "unit": {"type": "integer", "description": "Номер FX-юнита 1..4."},
                "slot": {"type": "integer", "description": "Номер слота эффекта 1..4 внутри юнита."},
                "index": {"type": "integer", "description": "Номер сэмплера/микрофона/aux."},
                "value": {"type": "string", "description": "Число в диапазоне контрола либо min/neutral/max. Для кнопок не нужно."},
            },
            "required": ["key"],
        },
    }


def _tool_schema() -> dict:
    return {
        "name": "build_transition_plan",
        "description": (
            "Строит и отправляет companion'у MixPlan — план перехода с одной деки на "
            "другую. Вызывать, когда диджей просит сделать переход/микс/свитч между "
            "двумя конкретными (или очевидными по контексту) деками."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_deck": {"type": "string", "description": "Дека, с которой уходим (та, что сейчас играет)."},
                "target_deck": {"type": "string", "description": "Дека, на которую переходим (загруженный следующий трек)."},
                "technique_id": {
                    "type": "string", "enum": list(TECHNIQUES.keys()),
                    "description": "ID техники сведения из библиотеки (например DNB-07). Необязательно — по умолчанию DNB-00 (Long Blend).",
                },
            },
            "required": ["source_deck", "target_deck"],
        },
    }


class DJAgent:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        persisted_turns: list[tuple[str, str]] | None = None,
    ) -> None:
        self._provider = provider or make_provider()
        # persisted_turns — (role, text) из persistence.py::load_history(), роль
        # уже в общем формате ("user"/"model"). Так комната переживает рестарт
        # backend'а, не начиная диалог с чистого листа каждый раз.
        self._history: list[tuple[str, str]] = list(persisted_turns or [])

    async def handle_message(
        self, text: str, telemetry: dict[str, dict],
    ) -> tuple[str, dict | None]:
        """Возвращает (текст в чат, MixPlan или None, список команд-контролов).

        Контролы отдаются отдельным списком, а не внутри плана: план идёт
        через планировщик с таймингом по долям, а это — «дёрни сейчас»."""
        telemetry_note = (
            f"[Текущая телеметрия дек: {json.dumps(telemetry, ensure_ascii=False)}]\n\n{text}"
            if telemetry else
            "[Телеметрии пока нет — companion не подключён или деки пустые]\n\n" + text
        )

        result_holder: dict = {"plan": None}

        def tool_executor(name: str, args: dict) -> dict:
            return self._run_tool(name, args, telemetry, result_holder)

        result_holder["controls"] = []

        reply = await self._provider.run_turn(
            _system_prompt(), self._history, telemetry_note,
            [_tool_schema(), _control_tool_schema(),
             _find_tool_schema(), _raw_tool_schema()], tool_executor,
        )

        self._history.append(("user", telemetry_note))
        self._history.append(("model", reply))
        self._trim_history()

        return reply, result_holder["plan"], result_holder["controls"]

    @staticmethod
    def _run_tool(name: str, args: dict, telemetry: dict[str, dict], result_holder: dict) -> dict:
        if name == "find_mixxx_control":
            hits = full_catalog.search(args.get("query") or "", limit=12)
            if not hits:
                return {"message": "Ничего не нашёл. Попробуйте другое слово."}
            lines = []
            for h in hits:
                addr = "+".join(h["needs"]) or "без адреса"
                extra = f", {h['min']:g}..{h['max']:g}" if h["kind"] == "range" else ""
                lines.append(f"{h['key']} [{h['family']}, {h['kind']}{extra}, нужен: {addr}]")
            return {"message": "Найдено: " + "; ".join(lines)}

        if name == "set_mixxx_raw":
            try:
                cmd = live_control.validate_raw(
                    key=args.get("key") or "",
                    deck=args.get("deck"), unit=args.get("unit"),
                    slot=args.get("slot"), index=args.get("index"),
                    value=args.get("value"),
                )
            except live_control.ControlError as exc:
                return {"message": str(exc)}
            result_holder.setdefault("controls", []).append(cmd)
            return {"message": f"Сделано: {live_control.describe(cmd)}"}

        if name == "set_mixxx_control":
            try:
                cmd = live_control.validate(
                    args.get("deck") or "A", args.get("control") or "", args.get("value"),
                )
            except live_control.ControlError as exc:
                return {"message": str(exc)}
            result_holder.setdefault("controls", []).append(cmd)
            return {"message": f"Сделано: {live_control.describe(cmd)}"}

        if name != "build_transition_plan":
            return {"message": f"Неизвестный инструмент: {name}"}

        source = args.get("source_deck")
        target = args.get("target_deck")
        technique_id = args.get("technique_id") or "DNB-00"
        if source not in telemetry or target not in telemetry:
            return {
                "message": (
                    f"Не могу построить план: дека {source} или {target} не видна "
                    f"в телеметрии (сейчас есть: {list(telemetry.keys())})."
                ),
            }
        if technique_id not in TECHNIQUES:
            technique_id = "DNB-00"

        bpm = telemetry[source].get("bpm") or 128.0
        plan_id = f"transition_{source}_to_{target}_{technique_id}"
        try:
            plan = build_plan(technique_id, plan_id, source, target, bpm)
        except NotImplementedError as exc:
            return {"message": f"Техника {technique_id} пока недоступна: {exc}"}

        result_holder["plan"] = plan
        technique_name = TECHNIQUES[technique_id].name
        return {"message": f"MixPlan '{plan_id}' ({technique_name}) построен и отправлен companion'у."}

    def _trim_history(self) -> None:
        if len(self._history) > MAX_HISTORY_MESSAGES:
            self._history = self._history[-MAX_HISTORY_MESSAGES:]
