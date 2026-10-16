"""Probe: what close code does ttyd send when the shell exits cleanly?

Sends `exit` to the remote shell and reports the websocket close code.
Determines whether reconnect must treat close 1000 (clean) as user intent
to end, versus reconnecting.
"""
import asyncio
import json
import sys
import time

import websockets

HOST = sys.argv[1] if len(sys.argv) > 1 else "7681-iec7mdhobz4a9geme96o9.e2b.app"


async def main():
    import urllib.request
    try:
        token = json.loads(urllib.request.urlopen(
            "https://" + HOST + "/token", timeout=10).read().decode()).get("token", "")
    except Exception:
        token = ""

    t0 = time.monotonic()
    try:
        async with websockets.connect("wss://" + HOST + "/ws", subprotocols=["tty"],
                                      max_size=None, ping_interval=15, ping_timeout=15,
                                      compression=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"AuthToken": token, "columns": 80,
                                      "rows": 24}).encode())
            await ws.send(b'1{"columns":80,"rows":24}')
            # wait for prompt so exit lands on a live shell
            await asyncio.sleep(1.5)
            await ws.send(b"0exit\r")
            try:
                async for msg in ws:
                    if isinstance(msg, bytes) and msg[:1] == b"0":
                        sys.stdout.write(repr(msg[1:120]) + "\n")
            except websockets.exceptions.ConnectionClosed as e:
                print("\niteration raised: code=%s reason=%r (%.1fs)"
                      % (e.code, e.reason, time.monotonic() - t0))
                return
            print("\niteration ended normally (%.1fs)" % (time.monotonic() - t0))
            print("close_code after exit:", ws.close_code,
                  "close_reason:", repr(ws.close_reason))
    except websockets.exceptions.ConnectionClosed as e:
        print("connect block raised: code=%s reason=%r (%.1fs)"
              % (e.code, e.reason, time.monotonic() - t0))


asyncio.run(main())
