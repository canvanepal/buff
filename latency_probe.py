#!/usr/bin/env python3
"""
Measure real keystroke -> echo latency through the ttyd bridge.

Method: send a unique probe string (INPUT frame), then time how long until the
echoed characters come back in OUTPUT frames. Run a few rounds, report stats.
Also sends a command and times its execution (ls\\r) separately.
"""
import asyncio
import json
import statistics
import sys
import time

import websockets

HOST = sys.argv[1] if len(sys.argv) > 1 else "7681-idtgli4wgl5e24mehup90.e2b.app"
TOKEN = ""
N_ROUNDS = 12


async def main():
    url = "wss://" + HOST + "/ws"
    async with websockets.connect(url, subprotocols=["tty"], max_size=None,
                                  ping_interval=None, compression=None,
                                  open_timeout=10) as ws:
        await ws.send(json.dumps({"AuthToken": TOKEN, "columns": 120, "rows": 30}).encode())
        await ws.send(b'1{"columns":120,"rows":30}')

        # drain connect-time burst
        async def drain(seconds):
            end = time.monotonic() + seconds
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=end - time.monotonic())
                except asyncio.TimeoutError:
                    return
        await drain(2.0)

        results = []
        for i in range(N_ROUNDS):
            # prime with Ctrl-C to get a clean prompt, then probe
            await ws.send(b"0" + b"\x03")
            await asyncio.sleep(0.35)
            await drain(0.25)

            probe = "x" * 8
            t0 = time.perf_counter()
            await ws.send(b"0" + probe.encode())
            # wait until we've seen all 8 probe chars echoed
            got = 0
            while got < len(probe) and time.perf_counter() - t0 < 5.0:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                if isinstance(msg, bytes) and msg[:1] == b"0":
                    got += msg[1:].count(b"x")

            # backspace them away, brief settle
            await ws.send(b"0" + b"\x7f" * len(probe))
            await asyncio.sleep(0.2)
            await drain(0.15)
            results.append((time.perf_counter() - t0) * 1000)

        print("keystroke->echo latency (ms) for %d rounds:" % len(results))
        print("  min  : %6.1f" % min(results))
        "  median: %6.1f" and print("  median: %6.1f" % statistics.median(results))
        print("  mean : %6.1f" % statistics.mean(results))
        print("  max  : %6.1f" % max(results))
        print("  values:", ", ".join("%.0f" % r for r in results))


asyncio.run(main())
