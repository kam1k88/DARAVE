"""
scripts/core/ai_chat.py — AI Chat engine for DJ assistant.

Connects to a local Ollama instance for conversational AI.
The chat endpoint streams responses via SSE; this module handles
the Ollama API interaction.

The frontend handles ALL tool execution (load_track, play, etc.)
The backend is a simple pass-through to Ollama — no agent loop,
no backend tools, no conflicting system prompts.

Usage (called from the chat router, never directly):
    from scripts.core.ai_chat import AIChatEngine
    engine = AIChatEngine()
    async for chunk in engine.chat_stream(messages):
        ...
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import requests

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AI Chat Engine — simple Ollama pass-through
# ---------------------------------------------------------------------------

class AIChatEngine:
    """
    Ollama-backed chat engine. No agent loop, no tool execution.

    Streams responses as SSE events:
      - {"type": "chunk", "data": {"content": "..."}}
      - {"type": "done", "data": {"model": "..."}}
      - {"type": "error", "data": {"message": "..."}}
    """

    def __init__(self, model: Optional[str] = None) -> None:
        try:
            from scripts.core.config import cfg
            ollama_cfg = getattr(cfg, "ollama", None)
            self._host = getattr(ollama_cfg, "host", "http://localhost:11501") if ollama_cfg else "http://localhost:11501"
            self._model = model or (getattr(ollama_cfg, "model", "llama3.1:8b") if ollama_cfg else "llama3.1:8b")
            self._timeout = getattr(ollama_cfg, "timeout_sec", 120) if ollama_cfg else 120
        except Exception:
            self._host = "http://localhost:11501"
            self._model = model or "llama3.1:8b"
            self._timeout = 120

    def _call_ollama_chat(
        self,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Synchronous Ollama /api/chat call — no tools, no agent loop."""
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }

        url = f"{self._host}/api/chat"
        resp = requests.post(url, json=payload, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
    ) -> AsyncIterator[str]:
        """
        Single Ollama call. No agent loop. No tool execution.
        Frontend handles all tool execution via agentTools.ts.
        """
        try:
            response = self._call_ollama_chat(messages)
        except requests.ConnectionError:
            yield json.dumps({"type": "error", "data": {"message": "Cannot connect to Ollama. Is it running on " + self._host + "?"}})
            return
        except requests.Timeout:
            yield json.dumps({"type": "error", "data": {"message": f"Ollama request timed out after {self._timeout}s"}})
            return
        except Exception as e:
            log.error("[chat] Ollama call failed: %s", e, exc_info=True)
            yield json.dumps({"type": "error", "data": {"message": f"Ollama error: {e}"}})
            return

        msg = response.get("message", {})
        content = msg.get("content", "")

        if content:
            yield json.dumps({"type": "chunk", "data": {"content": content}})

        model_name = response.get("model", self._model)
        yield json.dumps({"type": "done", "data": {"model": model_name}})
