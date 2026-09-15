"""
llm_providers.py — два взаимозаменяемых провайдера LLM с tool-use для
DJAgent (agent.py): Gemini (облачный, бесплатный тир, но недоступен из РФ —
см. README) и Ollama (локальная модель на машине диджея — бесплатно всегда,
без geo-блоков, ценой требований к железу и своей настройки).

Общий контракт — LLMProvider.run_turn(): каждый провайдер САМ ведёт свой
внутренний цикл "ответ -> tool_call -> результат инструмента -> ответ" в
родном для SDK формате сообщений и возвращает только финальный текст.
История между ходами пользователя хранится в agent.py в прои+ provider-
агностичном виде (list[tuple[role, text]]) и на каждый вызов run_turn()
пересобирается в родной формат заново — так провайдеры не текут друг в
друга через общее состояние, а persistence.py (SQLite) видит только простые
(role, text) пары, независимо от того, какой SDK их произвёл.

tool_executor: Callable[[str, dict], dict] — синхронная функция
"имя инструмента, аргументы -> {"message": str для LLM}"; используется как
есть (agent.py передаёt closure, который параллельно кладёт построенный
MixPlan в result_holder — сайд-канал, т.к. u Gemini, u Ollama по-разному
устроен сам tool-protocol, а результат нужен один).
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Callable

MAX_TOOL_ROUNDS = 2

ToolExecutor = Callable[[str, dict], dict]


class LLMProvider(ABC):
    @abstractmethod
    async def run_turn(
        self, system_prompt: str, history: list[tuple[str, str]], user_text: str,
        tool_schemas: list[dict], tool_executor: ToolExecutor,
    ) -> str:
        ...


class GeminiProvider(LLMProvider):
    """Google Gemini API — см. https://aistudio.google.com. Недоступен из
    РФ/Беларуси/Ирана/КНР на уровне geo-блока (не биллинга), см. README."""

    DEFAULT_MODEL = "gemini-3.6-flash"

    def __init__(self, model: str | None = None) -> None:
        from google import genai

        # genai.Client() читает ТОЛЬКО переменную окружения. У установленной
        # версии её нет — ключ лежит в darave_secrets.json, поэтому передаём
        # явно, а не надеемся на окружение.
        key = gemini_key()
        self._client = genai.Client(api_key=key) if key else genai.Client()
        self._model = model or setting("GEMINI_MODEL", "gemini_model", self.DEFAULT_MODEL)

    async def run_turn(self, system_prompt, history, user_text, tool_schemas, tool_executor) -> str:
        from google.genai import types

        contents = [
            types.Content(role=("user" if role == "user" else "model"), parts=[types.Part(text=text)])
            for role, text in history
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

        tool = types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=schema["name"], description=schema["description"],
                parameters_json_schema=schema["parameters"],
            )
            for schema in tool_schemas
        ])

        reply_parts: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, tools=[tool]),
            )
            candidate = response.candidates[0] if response.candidates else None
            if candidate is not None and candidate.content is not None:
                contents.append(candidate.content)
            if response.text:
                reply_parts.append(response.text)

            function_calls = response.function_calls or []
            if not function_calls:
                break
            call = function_calls[0]
            result = tool_executor(call.name, dict(call.args or {}))
            # ВАЖНО: у Gemini роль контента может быть ТОЛЬКО "user" или
            # "model" (это же проверяет сам SDK, google.genai.chats —
            # "Role must be user or model") — "tool" здесь не существует,
            # даже для ответа на function_call. Именно это и роняло чат
            # с "Ошибка на стороне ассистента" ровно в момент, когда модель
            # реально вызывала build_transition_plan (а не просто отвечала
            # текстом) — на следующем шаге цикла SDK такой контент отвергал.
            contents.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name=call.name, response={"result": result["message"]}),
            ]))

        return " ".join(p for p in reply_parts if p).strip() or "Готово."


class OllamaProvider(LLMProvider):
    """Локальная модель через Ollama (https://ollama.com) — работает на
    машине диджея (companion) или на backend'е, если там достаточно
    ресурсов. Бесплатно, без geo-блоков, без отправки данных наружу.

    Установка (один раз):
        1. Скачать и поставить Ollama: https://ollama.com/download
        2. ollama pull llama3.1        (или другую модель с tool-use, см. ниже)
        3. export DARAVE_LLM_PROVIDER=ollama
           export OLLAMA_MODEL=llama3.1   # опционально, это и так дефолт

    Модели с надёжным tool-calling в Ollama (на выбор — не жанр-специфичные,
    любая справится с одним простым инструментом build_transition_plan):
    llama3.1 (8B, дефолт — компромисс качество/скорость на обычном ПК),
    qwen2.5, mistral-nemo, firefunction-v2 (крупнее и точнее, но тяжелее).
    """

    DEFAULT_MODEL = "llama3.1"

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        import ollama

        self._client = ollama.AsyncClient(host=host or ollama_host())
        self._model = model or setting("OLLAMA_MODEL", "ollama_model", self.DEFAULT_MODEL)

    async def run_turn(self, system_prompt, history, user_text, tool_schemas, tool_executor) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        for role, text in history:
            messages.append({"role": "user" if role == "user" else "assistant", "content": text})
        messages.append({"role": "user", "content": user_text})

        tools = [
            {"type": "function", "function": {
                "name": schema["name"], "description": schema["description"],
                "parameters": schema["parameters"],
            }}
            for schema in tool_schemas
        ]

        reply_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._client.chat(model=self._model, messages=messages, tools=tools)
            message = response.message
            messages.append({
                "role": "assistant", "content": message.content or "",
                **({"tool_calls": [
                    {"function": {"name": tc.function.name, "arguments": dict(tc.function.arguments)}}
                    for tc in message.tool_calls
                ]} if message.tool_calls else {}),
            })
            if message.content:
                reply_text = message.content

            if not message.tool_calls:
                break
            call = message.tool_calls[0]
            result = tool_executor(call.function.name, dict(call.function.arguments))
            messages.append({
                "role": "tool", "tool_name": call.function.name,
                "content": json.dumps({"result": result["message"]}, ensure_ascii=False),
            })

        return reply_text.strip() or "Готово."


SECRETS_FILE = "darave_secrets.json"


def _secrets_paths() -> list:
    """Где искать файл с ключами — папка данных, потом папка кода.

    Из исходников ключ задаётся переменной окружения (darave_config.ps1).
    У установленной версии переменных нет ни одной, и без этого файла
    программа вообще не поднималась: make_provider бросал RuntimeError
    прямо при импорте server.py, то есть первый же запуск после установки
    заканчивался трейсбеком в консоли."""
    from pathlib import Path

    out = []
    try:
        import paths as _p
        out.append(_p.data_dir() / SECRETS_FILE)
    except Exception:
        pass
    out.append(Path(__file__).resolve().parent / SECRETS_FILE)
    return out


def secret(field: str) -> str | None:
    """Значение из darave_secrets.json, если оно там есть и непустое."""
    for f in _secrets_paths():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        v = data.get(field)
        if v and str(v).strip():
            return str(v).strip()
    return None


def save_secret(field: str, value: str) -> None:
    """Записать ключ в darave_secrets.json папки данных, не потеряв остальные."""
    path = _secrets_paths()[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if value:
        data[field] = value
    else:
        data.pop(field, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def setting(env_name: str, secret_name: str, default: str = "") -> str:
    """Настройка: переменная окружения -> darave_secrets.json -> умолчание.

    Порядок именно такой: кто задал переменную явно, тот знает, что
    делает, и файл не должен его переопределять."""
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    return (secret(secret_name) or default).strip()


def gemini_key() -> str:
    return setting("GEMINI_API_KEY", "gemini_api_key")


def ollama_host() -> str:
    return setting("OLLAMA_HOST", "ollama_host", "http://localhost:11434").rstrip("/")


def _gemini_key_present() -> bool:
    key = gemini_key()
    # "PASTE_YOUR_..." — плейсхолдер из darave_config.example.ps1: считаем
    # его отсутствием ключа, иначе провайдер выберется и упадёт на первом же
    # запросе с 400 API_KEY_INVALID (ровно этот сценарий и наблюдался).
    return bool(key) and not key.upper().startswith("PASTE_")


def _ollama_reachable(timeout: float = 1.5) -> bool:
    """Быстрая проверка, что Ollama слушает. Нужна только для режима
    "auto" — при явном DARAVE_LLM_PROVIDER=ollama мы не мешаем пользователю
    поднять её чуть позже."""
    host = ollama_host()
    try:
        import urllib.request

        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


class NoProvider(LLMProvider):
    """Заглушка: ключа нет, но программа обязана работать.

    Раньше здесь был RuntimeError прямо из make_provider(), а make_provider
    вызывается на уровне модуля server.py — то есть при импорте. Первый
    запуск установленной версии, где переменных окружения нет ни одной,
    заканчивался трейсбеком в консоли и закрытым окном. Причём отсутствие
    ключа НЕ мешает почти ничему: разбор библиотеки, слои, демо техник,
    правка швов, сборка и рендер сета не обращаются к LLM вовсе. Без ключа
    выключается ровно одно — разговорный ассистент.

    Поэтому программа поднимается, а несостоявшийся провайдер честно
    отвечает в чат, чего не хватает и что с этим делать."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def run_turn(self, system_prompt, history, user_text, tool_schemas, tool_executor) -> str:
        return self.reason


def verify(provider: LLMProvider) -> tuple[bool, str]:
    """Дёрнуть провайдера по-настоящему: работает ключ или нет.

    Нужно потому, что наличие ключа и рабочий ключ — разные вещи.
    GeminiProvider() с мусорной строкой создаётся молча, и без этой
    проверки кнопка «Подключить» гасила бы баннер, а первое же сообщение
    в чат падало бы с 400 API_KEY_INVALID — то есть ровно то поведение,
    от которого auto-режим и придумывался."""
    if isinstance(provider, NoProvider):
        return False, provider.reason
    try:
        if isinstance(provider, GeminiProvider):
            # Лёгкий запрос к метаданным модели: ключ проверяется, токены
            # не тратятся.
            provider._client.models.get(model=provider._model)
            return True, ""
        if isinstance(provider, OllamaProvider):
            if not _ollama_reachable():
                return False, f"Ollama не отвечает на {ollama_host()}"
            return True, ""
    except Exception as exc:
        text = str(exc)
        if "API_KEY_INVALID" in text or "API key not valid" in text.replace("_", " "):
            return False, "Ключ не принят Google — проверьте, что скопирован целиком."
        if "PERMISSION_DENIED" in text or "location" in text.lower():
            return False, ("Google не пускает с этого IP (Gemini заблокирован в РФ). "
                           "Нужен VPN на всю программу либо Ollama вместо Gemini.")
        return False, f"Провайдер не ответил: {text[:200]}"
    return True, ""


def provider_status() -> dict:
    """Что сейчас с ассистентом — для UI и для стартового сообщения."""
    if _gemini_key_present():
        return {"ok": True, "provider": "gemini",
                "model": setting("GEMINI_MODEL", "gemini_model", GeminiProvider.DEFAULT_MODEL)}
    if _ollama_reachable():
        return {"ok": True, "provider": "ollama",
                "model": setting("OLLAMA_MODEL", "ollama_model", OllamaProvider.DEFAULT_MODEL),
                "host": ollama_host()}
    return {"ok": False, "provider": None, "host": ollama_host(),
            "reason": ("Разговорный ассистент выключен: нет ни ключа Gemini, ни запущенной Ollama. "
                       "Всё остальное — библиотека, техники, отбор, сборка сета — работает.")}


NO_PROVIDER_REPLY = (
    "Ассистент пока не подключён, поэтому отвечать в чат мне нечем.\n\n"
    "Нужно одно из двух:\n"
    "• ключ Gemini — бесплатный, aistudio.google.com; вставьте его в поле над чатом;\n"
    "• либо Ollama на этом компьютере: ollama serve и ollama pull llama3.1.\n\n"
    "Остальное работает без ассистента: библиотека, разбор, техники и демо, "
    "отбор переходов, сборка и рендер сета."
)


def make_provider(name: str | None = None, required: bool = False) -> LLMProvider:
    """name — "gemini" | "ollama" | "auto"; по умолчанию берётся из
    DARAVE_LLM_PROVIDER, а если и её нет — "auto".

    "auto" существует, потому что самая частая ошибка настройки — оставить
    провайдер gemini с незаполненным ключом: backend поднимается, но каждый
    запрос в чат падает с 400 API_KEY_INVALID. В auto мы смотрим, что
    реально доступно: есть валидный на вид ключ — Gemini; нет ключа, но
    отвечает Ollama — Ollama; нет ничего — падаем сразу и внятно, при
    старте, а не на первом сообщении диджея.
    """
    provider = (name or setting("DARAVE_LLM_PROVIDER", "llm_provider", "auto")).lower()

    if provider == "auto":
        # Каждое создание — в try. Провайдер строится при ИМПОРТЕ server.py,
        # и любое исключение здесь означает не «чат не работает», а
        # «программа не запустилась»: именно так сохранённый нерабочий
        # ключ один раз и убил старт целиком.
        if _gemini_key_present():
            try:
                prov = GeminiProvider()
                print("[llm] auto: выбран Gemini (ключ задан)")
                return prov
            except Exception as exc:
                print(f"[llm] Gemini не поднялся ({exc}) — пробую дальше")
        if _ollama_reachable():
            try:
                prov = OllamaProvider()
                print(f"[llm] auto: выбрана Ollama ({prov._model}) — ключа Gemini нет")
                return prov
            except Exception as exc:
                print(f"[llm] Ollama не поднялась: {exc}")
        message = (
            "Не найден ни один рабочий LLM-провайдер.\n"
            "  - Gemini: ключ не задан (переменная GEMINI_API_KEY или поле "
            "gemini_api_key в darave_secrets.json).\n"
            "  - Ollama: не отвечает на " + ollama_host()
            + " (запущена ли она? 'ollama serve', модель: 'ollama pull llama3.1').\n"
            "Задайте ключ Gemini либо поднимите Ollama, либо укажите "
            "DARAVE_LLM_PROVIDER=gemini|ollama явно."
        )
        if required:
            raise RuntimeError(message)
        print("[llm] " + message.replace("\n", " "))
        return NoProvider(NO_PROVIDER_REPLY)

    # Явный выбор провайдера. Даже здесь не падаем при отсутствии ключа,
    # если нас не просили падать: указать gemini и забыть ключ — ровно та
    # же ошибка настройки, что и ничего не указать, и наказывать за неё
    # неработающей программой не за что.
    if provider == "ollama":
        try:
            return OllamaProvider()
        except Exception as exc:
            if required:
                raise
            print(f"[llm] Ollama не поднялась: {exc}")
            return NoProvider(NO_PROVIDER_REPLY)
    if provider == "gemini":
        if not _gemini_key_present() and not required:
            print("[llm] выбран Gemini, но ключа нет — ассистент выключен")
            return NoProvider(NO_PROVIDER_REPLY)
        return GeminiProvider()
    raise ValueError(
        f"Unknown DARAVE_LLM_PROVIDER: {provider!r} (ожидается 'auto', 'gemini' или 'ollama')"
    )
