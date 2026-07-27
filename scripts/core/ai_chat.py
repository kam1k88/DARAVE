"""
scripts/core/ai_chat.py — AI Chat engine for DJ assistant.

Connects to a local Ollama instance for conversational AI with tool-calling
capability.  The chat endpoint streams responses via SSE; this module handles
the Ollama API interaction, tool definitions, and the agent loop.

Usage (called from the chat router, never directly):
    from scripts.core.ai_chat import AIChatEngine
    engine = AIChatEngine()
    async for chunk in engine.chat_stream(messages):
        ...
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions — Ollama function-calling format
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_library",
            "description": "List all songs in the library with their metadata (BPM, key, energy, stems status). Returns a summary and full list.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_track_info",
            "description": "Get detailed metadata for a specific track: BPM, key, camelot, energy, genre, duration, stems, structure sections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact song name in the library",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_compatibility",
            "description": "Check harmonic and tempo compatibility between two tracks. Returns compatibility scores (overall, bpm, key, energy) and a recommendation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "song_a": {"type": "string", "description": "First song name"},
                    "song_b": {"type": "string", "description": "Second song name"},
                },
                "required": ["song_a", "song_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_similar",
            "description": "Find tracks similar to a given song using 35-dim embedding similarity. Returns ranked list with similarity scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Reference song name"},
                    "k": {
                        "type": "integer",
                        "description": "Number of results (default 5)",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_track_structure",
            "description": "Get the structural analysis of a track: sections (intro, verse, chorus, drop, break, build, outro), phrase boundaries, energy curve.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Song name"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_effects",
            "description": "List all available DJ transition effects with descriptions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_techniques",
            "description": "List all DJ transition techniques with difficulty, BPM range, energy delta, and when to use.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_transition",
            "description": "Get an AI recommendation for the best transition technique between two tracks, considering BPM, key, energy, and structure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "song_a": {"type": "string", "description": "Outgoing track"},
                    "song_b": {"type": "string", "description": "Incoming track"},
                },
                "required": ["song_a", "song_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize_setlist",
            "description": "Optimize a setlist order for given tracks using harmonic compatibility, BPM continuity, and energy arc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tracks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of song names to optimize",
                    },
                    "arc": {
                        "type": "string",
                        "enum": ["ramp_up", "mountain", "wave", "ramp_down"],
                        "description": "Energy arc shape (default: mountain)",
                    },
                },
                "required": ["tracks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_remix",
            "description": "Launch a server-side remix job between two tracks. Returns a job_id for tracking progress. Requires user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "song_a": {"type": "string", "description": "First song"},
                    "song_b": {"type": "string", "description": "Second song"},
                    "transition_bars": {
                        "type": "integer",
                        "description": "Bars for the transition (8, 16, or 32)",
                    },
                },
                "required": ["song_a", "song_b"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution — thin wrappers around existing backend modules
# ---------------------------------------------------------------------------

def _tool_list_library() -> str:
    try:
        from scripts.core.library import LibraryManager
        mgr = LibraryManager()
        songs = mgr.list_songs()
        if not songs:
            return "Library is empty."
        summary = f"Library contains {len(songs)} songs.\n\n"
        lines = []
        for s in songs[:100]:  # cap at 100 for context window
            meta = _load_meta(s.name)
            bpm = meta.get("bpm", "—")
            key = meta.get("key", "—")
            camelot = meta.get("camelot", "—")
            energy = meta.get("energy_mean", "—")
            stems = "yes" if s.stems_available else "no"
            lines.append(f"- {s.name} | BPM: {bpm} | Key: {key} | Camelot: {camelot} | Energy: {energy} | Stems: {stems}")
        return summary + "\n".join(lines)
    except Exception as e:
        return f"Error listing library: {e}"


def _tool_get_track_info(name: str) -> str:
    try:
        from scripts.core.library import LibraryManager
        mgr = LibraryManager()
        songs = mgr.list_songs()
        song = next((s for s in songs if s.name == name), None)
        if not song:
            # Fuzzy match
            lower_name = name.lower()
            song = next((s for s in songs if lower_name in s.name.lower()), None)
        if not song:
            return f"Track '{name}' not found in library."
        meta = _load_meta(song.name)
        info = {
            "name": song.name,
            "has_full_wav": song.has_full_wav,
            "stems": song.stems_available,
            "source": song.source,
        }
        info.update(meta)
        return json.dumps(info, indent=2, default=str)
    except Exception as e:
        return f"Error getting track info: {e}"


def _tool_check_compatibility(song_a: str, song_b: str) -> str:
    try:
        from scripts.core.dj_analysis import analyze_structure
        from scripts.core.transition_intel import analyze_transition_pair
        struct_a = analyze_structure(song_a)
        struct_b = analyze_structure(song_b)
        rec = analyze_transition_pair(struct_a, struct_b)
        result = {
            "song_a": song_a,
            "song_b": song_b,
            "technique": rec.technique,
            "effect": rec.effect,
            "transition_bars": rec.transition_bars,
            "crossfade_type": rec.crossfade_type,
            "confidence": round(rec.confidence, 2),
            "reason": rec.reason,
            "bpm_a": rec.bpm_a,
            "bpm_b": rec.bpm_b,
            "camelot_a": rec.camelot_a,
            "camelot_b": rec.camelot_b,
            "energy_a": round(rec.energy_a, 2),
            "energy_b": round(rec.energy_b, 2),
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error checking compatibility: {e}"


def _tool_find_similar(name: str, k: int = 5) -> str:
    try:
        from scripts.core.music_index import MusicIndex
        idx = MusicIndex()
        results = idx.find_similar(name, k=k)
        if not results:
            return f"No similar tracks found for '{name}'."
        lines = [f"Similar tracks to '{name}':"]
        for r in results:
            score = round(r.get("score", 0), 3)
            bpm = r.get("bpm", "—")
            key = r.get("key", "—")
            lines.append(f"- {r['name']} (score: {score}, BPM: {bpm}, Key: {key})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error finding similar tracks: {e}"


def _tool_get_track_structure(name: str) -> str:
    try:
        from scripts.core.dj_analysis import analyze_structure
        struct = analyze_structure(name)
        sections = []
        for sec in struct.sections:
            sections.append({
                "type": sec.label,
                "start_bar": sec.start_bar,
                "end_bar": sec.end_bar,
                "start_time": round(sec.start_time, 1),
                "end_time": round(sec.end_time, 1),
            })
        result = {
            "name": struct.name,
            "bpm": struct.bpm,
            "key": struct.key,
            "camelot": struct.camelot,
            "total_bars": struct.total_bars,
            "duration": round(struct.duration, 1) if struct.duration else None,
            "sections": sections,
        }
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"Error analyzing structure: {e}"


def _tool_list_effects() -> str:
    try:
        from scripts.core.dj_effects import EFFECTS
        lines = ["Available DJ effects:"]
        for name, desc in EFFECTS.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing effects: {e}"


def _tool_list_techniques() -> str:
    try:
        from scripts.core.dj_techniques import TECHNIQUES
        lines = ["Available DJ transition techniques:"]
        for t in TECHNIQUES[:30]:  # cap for context
            lines.append(
                f"- [{t.id}] {t.name} ({t.category}, difficulty {t.difficulty}/5): "
                f"{t.description[:80]}... BPM range: {t.bpm_range[0]}-{t.bpm_range[1]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing techniques: {e}"


def _tool_recommend_transition(song_a: str, song_b: str) -> str:
    try:
        from scripts.core.dj_analysis import analyze_structure
        from scripts.core.transition_intel import analyze_transition_pair
        struct_a = analyze_structure(song_a)
        struct_b = analyze_structure(song_b)
        rec = analyze_transition_pair(struct_a, struct_b)
        result = {
            "recommendation": {
                "technique": rec.technique,
                "effect": rec.effect,
                "transition_bars": rec.transition_bars,
                "crossfade_type": rec.crossfade_type,
                "bridge_beat": rec.bridge_beat,
                "eq_strategy": rec.eq_strategy,
                "confidence": round(rec.confidence, 2),
                "reason": rec.reason,
            },
            "tracks": {
                "a": {"name": song_a, "bpm": rec.bpm_a, "camelot": rec.camelot_a, "energy": round(rec.energy_a, 2)},
                "b": {"name": song_b, "bpm": rec.bpm_b, "camelot": rec.camelot_b, "energy": round(rec.energy_b, 2)},
            },
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error recommending transition: {e}"


def _tool_optimize_setlist(tracks: List[str], arc: str = "mountain") -> str:
    try:
        from scripts.core.library import LibraryManager
        from scripts.core.setlist_planner import EnergyArc, SetlistPlanner, TrackNode
        mgr = LibraryManager()
        songs = mgr.list_songs()
        track_nodes = []
        for name in tracks:
            song = next((s for s in songs if s.name == name), None)
            if not song:
                continue
            meta = _load_meta(name)
            track_nodes.append(TrackNode(
                name=name,
                bpm=meta.get("bpm", 120.0),
                key=meta.get("key", "C"),
                camelot=meta.get("camelot", "1A"),
                energy_mean=meta.get("energy_mean", 0.5),
                energy_std=meta.get("energy_std", 0.1),
                duration_sec=meta.get("duration", 240.0),
            ))
        if not track_nodes:
            return "No valid tracks found for setlist optimization."
        arc_enum = EnergyArc(arc) if arc in [e.value for e in EnergyArc] else EnergyArc.MOUNTAIN
        planner = SetlistPlanner()
        result = planner.optimize(track_nodes, arc=arc_enum)
        lines = [f"Optimized setlist ({arc} arc):"]
        for i, item in enumerate(result, 1):
            lines.append(f"{i}. {item.get('name', item.get('track_name', '?'))}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error optimizing setlist: {e}"


def _tool_create_remix(song_a: str, song_b: str, transition_bars: int = 16) -> str:
    try:
        from scripts.api.jobs import submit_job
        job_id = submit_job(
            job_type="dj_remix",
            meta={"song_a": song_a, "song_b": song_b, "transition_bars": transition_bars},
            task_module="scripts.api.task_modules.remix_tasks",
            task_function="run_dj_remix",
            kwargs={
                "song_a": song_a,
                "song_b": song_b,
                "transition_bars": transition_bars,
            },
        )
        return json.dumps({
            "status": "remix_job_created",
            "job_id": job_id,
            "message": f"Remix job launched: {song_a} → {song_b} ({transition_bars} bars). Track progress in the Jobs tab.",
        })
    except Exception as e:
        return f"Error creating remix: {e}"


def _load_meta(song_name: str) -> Dict[str, Any]:
    """Load meta.json from a song directory."""
    try:
        from scripts.core.paths import song_dir
        meta_path = song_dir(song_name) / "meta.json"
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_DISPATCH: Dict[str, Callable[..., str]] = {
    "list_library": lambda **kw: _tool_list_library(),
    "get_track_info": lambda **kw: _tool_get_track_info(kw.get("name", "")),
    "check_compatibility": lambda **kw: _tool_check_compatibility(kw.get("song_a", ""), kw.get("song_b", "")),
    "find_similar": lambda **kw: _tool_find_similar(kw.get("name", ""), kw.get("k", 5)),
    "get_track_structure": lambda **kw: _tool_get_track_structure(kw.get("name", "")),
    "list_effects": lambda **kw: _tool_list_effects(),
    "list_techniques": lambda **kw: _tool_list_techniques(),
    "recommend_transition": lambda **kw: _tool_recommend_transition(kw.get("song_a", ""), kw.get("song_b", "")),
    "optimize_setlist": lambda **kw: _tool_optimize_setlist(kw.get("tracks", []), kw.get("arc", "mountain")),
    "create_remix": lambda **kw: _tool_create_remix(kw.get("song_a", ""), kw.get("song_b", ""), kw.get("transition_bars", 16)),
}


def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    """Execute a tool by name with the given arguments. Returns result string."""
    handler = TOOL_DISPATCH.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return handler(**arguments)
    except Exception as e:
        log.error("[chat] Tool %s failed: %s", name, e, exc_info=True)
        return f"Tool '{name}' execution error: {e}"


# ---------------------------------------------------------------------------
# AI Chat Engine
# ---------------------------------------------------------------------------

class AIChatEngine:
    """
    Ollama-backed chat engine with tool-calling agent loop.

    Streams responses as SSE events:
      - {"type": "chunk", "data": {"content": "..."}}
      - {"type": "tool_call", "data": {"id": "...", "name": "...", "arguments": {...}}}
      - {"type": "tool_result", "data": {"id": "...", "name": "...", "result": "..."}}
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
            self._max_rounds = getattr(ollama_cfg, "max_tool_rounds", 5) if ollama_cfg else 5
            self._system_prompt = (getattr(ollama_cfg, "system_prompt", "") if ollama_cfg else "") or self._default_system_prompt()
        except Exception:
            self._host = "http://localhost:11501"
            self._model = model or "llama3.1:8b"
            self._timeout = 120
            self._max_rounds = 5
            self._system_prompt = self._default_system_prompt()

    @staticmethod
    def _default_system_prompt() -> str:
        return (
            "You are a DJ assistant for AI RemixMate — a real-time DJ engine. "
            "You help with mixing, track selection, transition techniques, compatibility, "
            "remix settings, and setlist planning. "
            "Always use tools to get real data from the user's library before answering. "
            "Be concise and practical — DJs need actionable advice, not essays."
        )

    def _build_messages(self, user_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build the message list with system prompt prepended."""
        messages = [{"role": "system", "content": self._system_prompt}]
        for m in user_messages:
            entry: Dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
                entry["role"] = "tool"
            messages.append(entry)
        return messages

    def _call_ollama_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Synchronous Ollama /api/chat call."""
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        url = f"{self._host}/api/chat"
        resp = requests.post(url, json=payload, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
    ) -> AsyncIterator[str]:
        """
        Async generator that yields SSE-formatted JSON lines.

        Implements the agent loop:
          1. Send messages to Ollama
          2. If response contains tool_calls → execute tools, append results, goto 1
          3. If response is plain text → stream it back
          4. Cap at max_tool_rounds to prevent infinite loops
        """
        all_messages = self._build_messages(messages)
        round_count = 0

        while round_count < self._max_rounds:
            round_count += 1

            try:
                response = self._call_ollama_chat(all_messages, tools=TOOLS)
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
            tool_calls = msg.get("tool_calls")

            # If there are tool calls, execute them
            if tool_calls:
                # Emit tool call events
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    tool_args = func.get("arguments", {})
                    tool_id = str(uuid.uuid4())[:8]

                    yield json.dumps({
                        "type": "tool_call",
                        "data": {"id": tool_id, "name": tool_name, "arguments": tool_args},
                    })

                    # Execute the tool
                    result = execute_tool(tool_name, tool_args)

                    yield json.dumps({
                        "type": "tool_result",
                        "data": {"id": tool_id, "name": tool_name, "result": result[:2000]},  # cap result size
                    })

                    # Add assistant message with tool call, then tool result
                    all_messages.append({
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [{"type": "function", "function": func}],
                    })
                    all_messages.append({
                        "role": "tool",
                        "content": result[:2000],
                        "tool_call_id": tool_id,
                    })

                # Continue the loop to get the final response
                continue

            # No tool calls — this is the final text response
            if content:
                yield json.dumps({"type": "chunk", "data": {"content": content}})

            model_name = response.get("model", self._model)
            yield json.dumps({"type": "done", "data": {"model": model_name}})
            return

        # Exceeded max rounds
        yield json.dumps({"type": "chunk", "data": {"content": "\n\n[Agent loop limit reached — rephrase your question or be more specific.]"}})
        yield json.dumps({"type": "done", "data": {"model": self._model}})
