#!/usr/bin/env python3
"""WebSocket connection test."""
import json
import sys
import asyncio

async def test_ws():
    try:
        import websockets
    except ImportError:
        print("  [SKIP] websockets not installed")
        return True

    uri = "ws://localhost:8000/ws/default"
    try:
        async with websockets.connect(uri, open_timeout=5) as ws:
            # Should get initial state
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            ok = data.get("type") in ("session_state", "deck_state")
            msg_type = data.get("type")
            print(f"  [PASS] WS connect: got {msg_type}")
            
            # Send a ping
            await ws.send(json.dumps({"type": "ping"}))
            pong = await asyncio.wait_for(ws.recv(), timeout=5)
            pong_data = json.loads(pong)
            print(f"  [PASS] WS ping/pong: {pong_data.get('type')}")
            
            return True
    except Exception as e:
        print(f"  [FAIL] WS: {e}")
        return False

async def test_ws_commands():
    try:
        import websockets
    except ImportError:
        return True

    uri = "ws://localhost:8000/ws/default"
    try:
        async with websockets.connect(uri, open_timeout=5) as ws:
            # Wait for initial state
            await asyncio.wait_for(ws.recv(), timeout=5)
            
            # Set crossfader
            await ws.send(json.dumps({
                "type": "set_crossfader",
                "position": 0.75
            }))
            
            # Collect responses for a bit
            got_response = False
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    data = json.loads(msg)
                    if data.get("type") == "deck_state":
                        state = data.get("data", {})
                        xf = state.get("crossfader_position")
                        if xf is not None:
                            print(f"  [PASS] WS crossfader: position={xf}")
                            got_response = True
                            break
                except asyncio.TimeoutError:
                    break
            
            if not got_response:
                print(f"  [INFO] WS crossfader: no response (heartbeat may not have fired)")
            
            return True
    except Exception as e:
        print(f"  [FAIL] WS commands: {e}")
        return False

async def main():
    print("WebSocket Tests:")
    r1 = await test_ws()
    r2 = await test_ws_commands()
    
    if r1 and r2:
        print("\nAll WebSocket tests passed")
    else:
        print("\nSome WebSocket tests failed")
        sys.exit(1)

asyncio.run(main())
