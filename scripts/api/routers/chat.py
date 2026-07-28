"""
scripts/api/routers/chat.py — AI Chat endpoints.

POST /chat              — Stream chat response via SSE (simple Ollama pass-through)
GET  /chat/history      — Recent conversation history (in-memory)
DELETE /chat/history    — Clear conversation history
GET  /chat/status       — Check Ollama connection status
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from scripts.api.schemas import ChatMessage, ChatRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# ---------------------------------------------------------------------------
# In-memory conversation store (per-server-process, lost on restart)
# ---------------------------------------------------------------------------

_MAX_HISTORY = 100  # keep last N messages
_history: deque[Dict[str, Any]] = deque(maxlen=_MAX_HISTORY)


def _msg_to_dict(msg: ChatMessage) -> Dict[str, Any]:
    d: Dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.timestamp:
        d["timestamp"] = msg.timestamp
    return d


# ---------------------------------------------------------------------------
# POST /chat — SSE streaming response (simple Ollama pass-through)
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="Stream chat response (SSE)",
    description=(
        "Send a conversation and receive a streaming response via Server-Sent Events. "
        "The backend is a simple pass-through to Ollama. "
        "All tool execution is handled by the frontend."
    ),
)
async def chat_stream(req: ChatRequest):
    from scripts.core.ai_chat import AIChatEngine

    engine = AIChatEngine(model=req.model)

    # Convert ChatMessage objects to dicts for the engine
    raw_messages = [_msg_to_dict(m) for m in req.messages]

    # Store user messages in history
    for m in req.messages:
        if m.role == "user":
            _history.append(_msg_to_dict(m))

    async def event_generator() -> AsyncIterator[str]:
        loop = asyncio.get_event_loop()
        chunks: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=100)

        def _run_engine():
            try:
                for chunk in engine.chat_stream(raw_messages):
                    loop.call_soon_threadsafe(chunks.put_nowait, chunk)
            except Exception as e:
                log.error("[chat] Engine error: %s", e, exc_info=True)
                err = json.dumps({"type": "error", "data": {"message": str(e)}})
                loop.call_soon_threadsafe(chunks.put_nowait, err)
            finally:
                loop.call_soon_threadsafe(chunks.put_nowait, None)

        # Run the synchronous engine in a thread to avoid blocking the event loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_run_engine)

            while True:
                chunk = await chunks.get()
                if chunk is None:
                    break

                # Parse and store assistant messages in history
                try:
                    parsed = json.loads(chunk)
                    msg_type = parsed.get("type", "")
                    data = parsed.get("data", {})

                    if msg_type == "chunk" and data.get("content"):
                        _history.append({
                            "role": "assistant",
                            "content": data["content"],
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        })
                except (json.JSONDecodeError, KeyError):
                    pass

                yield f"data: {chunk}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# GET /chat/history — recent messages
# ---------------------------------------------------------------------------

@router.get(
    "/history",
    summary="Chat conversation history",
    description="Returns the last N messages from the current conversation.",
)
async def chat_history(limit: int = 50) -> List[Dict[str, Any]]:
    msgs = list(_history)
    if limit > 0:
        msgs = msgs[-limit:]
    return msgs


# ---------------------------------------------------------------------------
# DELETE /chat/history — clear
# ---------------------------------------------------------------------------

@router.delete(
    "/history",
    summary="Clear chat history",
)
async def clear_chat_history() -> Dict[str, str]:
    _history.clear()
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# GET /chat/status — Ollama health check
# ---------------------------------------------------------------------------

@router.get(
    "/status",
    summary="Check Ollama connection",
    description="Quick check: is Ollama running and reachable?",
)
async def chat_status() -> Dict[str, Any]:
    import requests as req

    try:
        from scripts.core.config import cfg
        host = getattr(cfg.ollama, "host", "http://localhost:11501")
        model = getattr(cfg.ollama, "model", "llama3.1:8b")
    except Exception:
        host = "http://localhost:11501"
        model = "llama3.1:8b"

    try:
        resp = req.get(f"{host}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m.get("name", "") for m in data.get("models", [])]
        model_available = any(model in m for m in models)
        return {
            "connected": True,
            "host": host,
            "model": model,
            "model_available": model_available,
            "available_models": models,
        }
    except Exception as e:
        return {
            "connected": False,
            "host": host,
            "model": model,
            "model_available": False,
            "error": str(e),
        }
