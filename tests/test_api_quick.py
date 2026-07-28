#!/usr/bin/env python3
"""Quick API smoke test."""
import httpx
import json
import sys

base = "http://localhost:8000"

def test(name, fn):
    try:
        ok, msg = fn()
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {msg}")
        return ok
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return False

results = []

def health():
    r = httpx.get(f"{base}/health/live", timeout=5)
    return r.status_code == 200 and r.json().get("status") == "ok", f"{r.status_code}"

results.append(test("health/live", health))

def library():
    r = httpx.get(f"{base}/library", timeout=5)
    data = r.json()
    songs = data["stats"]["total_songs"]
    return r.status_code == 200, f"songs={songs}"

results.append(test("library", library))

def library_names():
    r = httpx.get(f"{base}/library/names", timeout=5)
    return r.status_code == 200, f"count={len(r.json())}"

results.append(test("library/names", library_names))

def jobs():
    r = httpx.get(f"{base}/jobs", timeout=5)
    return r.status_code == 200, f"jobs={len(r.json())}"

results.append(test("jobs", jobs))

def chat_stream():
    """Test that the chat SSE endpoint responds."""
    with httpx.stream("POST", f"{base}/chat",
                       json={"messages": [
                           {"role": "system", "content": "Reply with exactly: OK"},
                           {"role": "user", "content": "Test"}
                       ]},
                       timeout=30) as r:
        chunks = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                data = line[6:]
                if data and data != ": ping":
                    try:
                        parsed = json.loads(data)
                        if parsed.get("type") == "chunk":
                            chunks.append(parsed["data"]["content"])
                    except:
                        pass
        full = "".join(chunks)
        return len(full) > 0, f"got {len(full)} chars: {full[:50]}"

results.append(test("chat/stream", chat_stream))

passed = sum(results)
total = len(results)
print(f"\n{'='*40}")
print(f"Results: {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
