"""Measure ttyd websocket frame traffic: ping intervals, idle gaps, close code.

Runs locally, connects to the sandbox ttyd endpoint, sends NO input, and logs
every frame with a timestamp for the observation window. Reports:
  - whether the server sends opcode-2 keepalive pings, and at what interval
  - the longest silent gap
  - how/when the connection closes (code + reason)
"""
import asyncio
import json
import sys
import time

import websockets

HOST = sys.argv[1] if len(sys.argv) > 1 else "7681-iec7mdhobz4a9geme96o9.e2b.app"
WINDOW = int(sys.argv[2]) if len(sys.argv) > 2 else 180  # seconds to watch
# third arg: client ping interval (0 = disabled, same as buff's current behavior)
PING = int(sys.argv[3]) if len(sys.argv) > 3 else 0


async def main():
    import urllib.request
    try:
        token = json.loads(urllib.request.urlopen(
            "https://" + HOST + "/token", timeout=10).read().decode()).get("token", "")
    except Exception as e:
        token = ""
        print("token fetch failed:", e)

    url = "wss://" + HOST + "/ws"
    t0 = time.monotonic()
    frames = []

    def log(kind, detail=""):
        now = time.monotonic() - t0
        frames.append((now, kind, detail))
        print("%8.1fs  %-10s %s" % (now, kind, detail), flush=True)

    try:
        async with websockets.connect(url, subprotocols=["tty"], max_size=None,
                                      ping_interval=(PING or None),
                                      ping_timeout=(PING or None) or None,
                                      compression=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"AuthToken": token, "columns": 80, "rows": 24}).encode())
            log("open", "connected + authed")
            deadline = time.monotonic() + WINDOW
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(),
                                                 timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    log("timeout", "window over")
                    break
                if isinstance(msg, str):
                    log("json-frame", msg[:60])
                    continue
                op = msg[:1]
                name = {b"0": "output", b"1": "title", b"2": "ping"}.get(op, repr(op))
                detail = ""
                if op == b"0":
                    detail = repr(msg[1:80])
                elif op == b"2":
                    detail = "(empty=keepalive)" if not msg[1:] else "(prefs)"
                # only log pings/outputs; suppress flood
                if name != "output" or len(msg) > 1:
                    log(name, detail)
            # after window: send input to test if the connection is still writable
            try:
                await ws.send(b"0echo STILL_ALIVE_$?\r")
                log("input", "sent test command after idle window")
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                log("post-idle", repr(msg[1:100]))
            except Exception as e:
                log("post-idle", "FAILED: " + type(e).__name__ + " " + str(e))
    except websockets.exceptions.ConnectionClosed as e:
        log("CLOSED", "code=%s reason=%r" % (e.code, e.reason))
    except Exception as e:
        log("ERROR", type(e).__name__ + " " + str(e))

    # analysis
    print("\n=== analysis ===")
    print("client ping_interval: %ss" % (PING or "disabled"))
    pings = [f for f in frames if f[1] == "ping" and "(empty" in f[2]]
    if len(pings) >= 2:
        gaps = [round(pings[i + 1][0] - pings[i][0], 1) for i in range(len(pings) - 1)]
        print("server keepalive pings: %d, intervals: %s" % (len(pings), gaps))
    else:
        print("server keepalive pings: %d  <-- if 0, nothing keeps the idle ws alive"
              % len(pings))
    maxgap = 0.0
    for i in range(1, len(frames)):
        maxgap = max(maxgap, frames[i][0] - frames[i - 1][0])
    print("longest silent gap: %.1fs" % maxgap)
    print("last event: %.1fs %s %s" % (frames[-1][0], frames[-1][1], frames[-1][2]))


asyncio.run(main())
