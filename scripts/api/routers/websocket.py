"""
scripts/api/routers/websocket.py — WebSocket for real-time DJ control.

Bidirectional communication:
- Client sends commands (load track, play, crossfade, etc.)
- Server pushes state updates (deck state, levels, waveform)
- Agent commands from AI chat execute in parallel across decks
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


# ---------------------------------------------------------------------------
# Deck state — one per deck (A, B, C, D)
# ---------------------------------------------------------------------------

class DeckID(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class PlayState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class DeckState:
    deck_id: str
    track_name: str = ""
    track_id: str = ""
    bpm: float = 0.0
    key: str = ""
    camelot: str = ""
    duration: float = 0.0
    position: float = 0.0
    play_state: str = PlayState.IDLE.value
    volume: float = 1.0
    crossfade: float = 0.5  # 0.0=full A, 1.0=full B
    eq_low: float = 0.0
    eq_mid: float = 0.0
    eq_high: float = 0.0
    effect: str = "none"
    stem_volumes: Dict[str, float] = field(default_factory=lambda: {
        "drums": 1.0, "bass": 1.0, "other": 1.0, "vocals": 1.0
    })
    cue_point: float = 0.0
    loop_start: float = 0.0
    loop_end: float = 0.0
    loop_active: bool = False
    pitch: float = 0.0  # -12 to +12 semitones
    modified_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# DJ Session — manages all decks + crossfader
# ---------------------------------------------------------------------------

@dataclass
class DJSession:
    session_id: str
    decks: Dict[str, DeckState] = field(default_factory=dict)
    master_bpm: float = 0.0
    master_key: str = ""
    crossfade_position: float = 0.5
    master_volume: float = 1.0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        for deck_id in ["A", "B", "C", "D"]:
            if deck_id not in self.decks:
                self.decks[deck_id] = DeckState(deck_id=deck_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "decks": {k: v.to_dict() for k, v in self.decks.items()},
            "master_bpm": self.master_bpm,
            "master_key": self.master_key,
            "crossfade_position": self.crossfade_position,
            "master_volume": self.master_volume,
        }


# ---------------------------------------------------------------------------
# Connection manager — multiple clients can connect to same session
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, Set[WebSocket]] = {}  # session_id → set of websockets
        self._sessions: Dict[str, DJSession] = {}  # session_id → session
        self._ws_to_session: Dict[str, str] = {}  # websocket id → session_id
        self._lock = asyncio.Lock()

    def get_or_create_session(self, session_id: str) -> DJSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = DJSession(session_id=session_id)
        return self._sessions[session_id]

    async def connect(self, ws: WebSocket, session_id: str) -> DJSession:
        await ws.accept()
        async with self._lock:
            if session_id not in self._connections:
                self._connections[session_id] = set()
            self._connections[session_id].add(ws)
            self._ws_to_session[id(ws)] = session_id
        session = self.get_or_create_session(session_id)
        log.info("[ws] Client connected to session %s (%d clients)", session_id, len(self._connections[session_id]))
        return session

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            session_id = self._ws_to_session.pop(id(ws), None)
            if session_id and session_id in self._connections:
                self._connections[session_id].discard(ws)
                if not self._connections[session_id]:
                    del self._connections[session_id]
                log.info("[ws] Client disconnected from session %s", session_id)

    async def broadcast_to_session(self, session_id: str, message: Dict[str, Any]):
        """Send a message to all clients in a session."""
        payload = json.dumps(message)
        async with self._lock:
            clients = self._connections.get(session_id, set()).copy()
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    async def send_to_all(self, message: Dict[str, Any]):
        """Broadcast to ALL connected sessions."""
        for session_id in list(self._connections.keys()):
            await self.broadcast_to_session(session_id, message)

    def get_session(self, session_id: str) -> Optional[DJSession]:
        return self._sessions.get(session_id)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_command(ws: WebSocket, session: DJSession, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Process a command from the client and return a response."""
    action = cmd.get("action", "")
    deck_id = cmd.get("deck", "A")
    deck = session.decks.get(deck_id)

    if action == "ping":
        return {"ok": True, "ts": time.time()}

    elif action == "load_track":
        track_name = cmd.get("track_name", "")
        track_id = cmd.get("track_id", str(uuid.uuid4()))
        if deck:
            deck.track_name = track_name
            deck.track_id = track_id
            deck.play_state = PlayState.LOADING.value
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "track": track_name}

    elif action == "play":
        if deck:
            deck.play_state = PlayState.PLAYING.value
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "state": "playing"}

    elif action == "pause":
        if deck:
            deck.play_state = PlayState.PAUSED.value
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "state": "paused"}

    elif action == "stop":
        if deck:
            deck.play_state = PlayState.IDLE.value
            deck.position = 0.0
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "state": "idle"}

    elif action == "set_crossfader":
        position = float(cmd.get("position", 0.5))
        session.crossfade_position = max(0.0, min(1.0, position))
        session.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "crossfader_update",
            "data": {"position": session.crossfade_position},
        })
        return {"ok": True, "position": session.crossfade_position}

    elif action == "set_volume":
        volume = float(cmd.get("volume", 1.0))
        if deck:
            deck.volume = max(0.0, min(1.0, volume))
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "volume": volume}

    elif action == "set_eq":
        band = cmd.get("band", "mid")
        value = float(cmd.get("value", 0.0))
        if deck:
            if band == "low":
                deck.eq_low = max(-12, min(12, value))
            elif band == "mid":
                deck.eq_mid = max(-12, min(12, value))
            elif band == "high":
                deck.eq_high = max(-12, min(12, value))
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "band": band, "value": value}

    elif action == "set_effect":
        effect = cmd.get("effect", "none")
        if deck:
            deck.effect = effect
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "effect": effect}

    elif action == "set_stem_volume":
        stem = cmd.get("stem", "drums")
        volume = float(cmd.get("volume", 1.0))
        if deck:
            deck.stem_volumes[stem] = max(0.0, min(1.0, volume))
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "stem": stem, "volume": volume}

    elif action == "seek":
        position = float(cmd.get("position", 0.0))
        if deck:
            deck.position = max(0.0, position)
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "position": position}

    elif action == "set_pitch":
        pitch = float(cmd.get("pitch", 0.0))
        if deck:
            deck.pitch = max(-12, min(12, pitch))
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "pitch": pitch}

    elif action == "loop":
        start = float(cmd.get("start", 0.0))
        end = float(cmd.get("end", 0.0))
        active = bool(cmd.get("active", True))
        if deck:
            deck.loop_start = start
            deck.loop_end = end
            deck.loop_active = active
            deck.modified_at = time.time()
        await manager.broadcast_to_session(session.session_id, {
            "type": "deck_update",
            "data": session.to_dict(),
        })
        return {"ok": True, "deck": deck_id, "loop": active}

    elif action == "get_state":
        return {"ok": True, "session": session.to_dict()}

    elif action == "batch":
        # Execute multiple commands atomically
        results = []
        for sub_cmd in cmd.get("commands", []):
            sub_cmd["deck"] = sub_cmd.get("deck", deck_id)
            result = await handle_command(ws, session, sub_cmd)
            results.append(result)
        return {"ok": True, "results": results}

    else:
        return {"ok": False, "error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# Agent command handler — called by AI chat to execute tools
# ---------------------------------------------------------------------------

async def agent_execute(session_id: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute an agent tool command on the session.
    Called from the chat router when the AI agent issues a tool call.
    """
    session = manager.get_session(session_id)
    if not session:
        session = manager.get_or_create_session(session_id)

    cmd = {"action": tool_name, **arguments}
    # Map tool names to websocket commands
    tool_to_action = {
        "load_track_to_deck": "load_track",
        "play_deck": "play",
        "pause_deck": "pause",
        "stop_deck": "stop",
        "set_crossfader": "set_crossfader",
        "set_volume": "set_volume",
        "set_effect": "set_effect",
        "get_deck_info": "get_state",
    }
    action = tool_to_action.get(tool_name, tool_name)
    cmd["action"] = action

    # Create a dummy websocket for the handler (agent doesn't need a real ws)
    result = await handle_command(None, session, cmd)
    return result


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time DJ control.

    Client sends JSON commands, server pushes state updates.
    Session ID identifies the DJ session (multiple clients can share one).
    """
    session = await manager.connect(ws, session_id)

    # Send initial state
    await ws.send_text(json.dumps({
        "type": "session_state",
        "data": session.to_dict(),
    }))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"ok": False, "error": "Invalid JSON"}))
                continue

            result = await handle_command(ws, session, cmd)
            await ws.send_text(json.dumps(result))

    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        log.error("[ws] Error: %s", e, exc_info=True)
        await manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Heartbeat — push deck state to all clients every 500ms
# ---------------------------------------------------------------------------

async def _deck_state_heartbeat():
    """Background task: push deck state to all clients every 2s for real-time sync.

    Only sends if state actually changed since last push to avoid flooding.
    """
    _last_state: dict[str, str] = {}  # session_id -> last serialized state hash
    while True:
        await asyncio.sleep(2)
        for session_id, session in manager._sessions.items():
            if session_id in manager._connections:
                import hashlib
                state_dict = session.to_dict()
                state_hash = hashlib.md5(json.dumps(state_dict, default=str).encode()).hexdigest()
                if _last_state.get(session_id) == state_hash:
                    continue  # state unchanged, skip
                _last_state[session_id] = state_hash
                await manager.broadcast_to_session(session_id, {
                    "type": "deck_state",
                    "data": state_dict,
                })


_heartbeat_task: asyncio.Task | None = None


def start_ws_heartbeat():
    global _heartbeat_task
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running() and (_heartbeat_task is None or _heartbeat_task.done()):
            _heartbeat_task = loop.create_task(_deck_state_heartbeat())
            log.info("[ws] Deck state heartbeat started")
    except RuntimeError:
        pass
