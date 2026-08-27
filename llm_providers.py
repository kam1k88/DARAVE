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

        self._client = genai.Client()
        self._model = model or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)

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

        self._client = ollama.AsyncClient(host=host or os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
        self._model = model or os.environ.get("OLLAMA_MODEL", self.DEFAULT_MODEL)

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


def _gemini_key_present() -> bool:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    # "PASTE_YOUR_..." — плейсхолдер из darave_config.example.ps1: считаем
    # его отсутствием ключа, иначе провайдер выберется и упадёт на первом же
    # запросе с 400 API_KEY_INVALID (ровно этот сценарий и наблюдался).
    return bool(key) and not key.upper().startswith("PASTE_")


def _ollama_reachable(timeout: float = 1.5) -> bool:
    """Быстрая проверка, что Ollama слушает. Нужна только для режима
    "auto" — при явном DARAVE_LLM_PROVIDER=ollama мы не мешаем пользователю
    поднять её чуть позже."""
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        import urllib.request

        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def make_provider(name: str | None = None) -> LLMProvider:
    """name — "gemini" | "ollama" | "auto"; по умолчанию берётся из
    DARAVE_LLM_PROVIDER, а если и её нет — "auto".

    "auto" существует, потому что самая частая ошибка настройки — оставить
    провайдер gemini с незаполненным ключом: backend поднимается, но каждый
    запрос в чат падает с 400 API_KEY_INVALID. В auto мы смотрим, что
    реально доступно: есть валидный на вид ключ — Gemini; нет ключа, но
    отвечает Ollama — Ollama; нет ничего — падаем сразу и внятно, при
    старте, а не на первом сообщении диджея.
    """
    provider = (name or os.environ.get("DARAVE_LLM_PROVIDER", "auto")).lower()

    if provider == "auto":
        if _gemini_key_present():
            print("[llm] auto: выбран Gemini (GEMINI_API_KEY задан)")
            return GeminiProvider()
        if _ollama_reachable():
            model = os.environ.get("OLLAMA_MODEL", OllamaProvider.DEFAULT_MODEL)
            print(f"[llm] auto: выбрана Ollama (модель {model}) — GEMINI_API_KEY не задан")
            return OllamaProvider()
        raise RuntimeError(
            "Не найден ни один рабочий LLM-провайдер.\n"
            "  - Gemini: переменная GEMINI_API_KEY не задана (или содержит плейсхолдер).\n"
            "  - Ollama: не отвечает на "
            + os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            + " (запущена ли она? 'ollama serve', модель: 'ollama pull llama3.1').\n"
            "Задайте GEMINI_API_KEY либо поднимите Ollama, либо укажите "
            "DARAVE_LLM_PROVIDER=gemini|ollama явно."
        )

    if provider == "ollama":
        return OllamaProvider()
    if provider == "gemini":
        return GeminiProvider()
    raise ValueError(
        f"Unknown DARAVE_LLM_PROVIDER: {provider!r} (ожидается 'auto', 'gemini' или 'ollama')"
    )
