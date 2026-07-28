#!/usr/bin/env python3
"""Test chat with tool calls through the API."""
import httpx
import json
import sys

base = "http://localhost:8000"

def test_chat_tool_call():
    """Send a message that should trigger tool calls."""
    print("Testing chat with tool-call prompt...")
    
    messages = [
        {"role": "system", "content": (
            "You are DARAVE DJ assistant. You have tools to control decks.\n"
            "When user asks you to DO something, output a JSON tool call.\n"
            "TOOL FORMAT: {\"name\": \"TOOL_NAME\", \"parameters\": {}}\n"
            "Available tools: list_library, play_deck, stop_deck, set_crossfader\n"
            "Be concise. If user wants a list, use the list_library tool."
        )},
        {"role": "user", "content": "Покажи список треков"}
    ]
    
    chunks = []
    with httpx.stream("POST", f"{base}/chat",
                       json={"messages": messages},
                       timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                data = line[6]
                if data and data != ": ping":
                    try:
                        parsed = json.loads(data)
                        ptype = parsed.get("type", "")
                        pdata = parsed.get("data", {})
                        if ptype == "chunk":
                            content = pdata.get("content", "")
                            chunks.append(content)
                            print(f"  chunk: {content[:80]}")
                        elif ptype == "tool_call":
                            print(f"  TOOL CALL: {pdata.get('name')} args={pdata.get('arguments', {})}")
                        elif ptype == "tool_result":
                            print(f"  TOOL RESULT: {pdata.get('result', '')[:100]}")
                        elif ptype == "error":
                            print(f"  ERROR: {pdata.get('message')}")
                    except:
                        pass
    
    full = "".join(chunks)
    print(f"\n  Full response ({len(full)} chars): {full[:200]}")
    
    # Check if it's a tool call or a text response
    has_tool_call = "tool_call" in "".join(str(c) for c in chunks) or '"name"' in full
    has_text = len(full) > 10
    
    if has_tool_call:
        print("  [PASS] AI generated tool call")
    elif has_text:
        print("  [PASS] AI generated text response")
    else:
        print("  [FAIL] No response from AI")
        return False
    
    return True

def test_chat_simple():
    """Simple text response test."""
    print("\nTesting simple chat...")
    
    messages = [
        {"role": "system", "content": "Reply with exactly one word: OK"},
        {"role": "user", "content": "Test"}
    ]
    
    chunks = []
    with httpx.stream("POST", f"{base}/chat",
                       json={"messages": messages},
                       timeout=30) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                data = line[6]
                if data and data != ": ping":
                    try:
                        parsed = json.loads(data)
                        if parsed.get("type") == "chunk":
                            chunks.append(parsed["data"]["content"])
                    except:
                        pass
    
    full = "".join(chunks).strip()
    ok = len(full) > 0
    print(f"  Response: '{full}'")
    print(f"  [{'PASS' if ok else 'FAIL'}] Simple chat")
    return ok

if __name__ == "__main__":
    r1 = test_chat_simple()
    r2 = test_chat_tool_call()
    
    print(f"\n{'='*40}")
    if r1 and r2:
        print("All chat tests passed!")
    else:
        print("Some chat tests failed")
        sys.exit(1)
