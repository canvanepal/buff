#!/usr/bin/env python3
"""
Decisive test: is the ~620ms latency from freebuff's shell wrapper or from the
E2B edge chain? Measures keystroke->echo latency, then `exec bash` (replacing
the wrapper in this PTY session) and measures again.
"""
import asyncio
import json
import statistics
import time

import websockets

HOST = "7681-idtgli4wgl5e24mehup90.e2b.app"
TOKEN = ""
N = 8


async def measure_echo(ws, probe_char="x"):
    results = []
    for _ in range(N):
        await ws.send(b"0" + b"\x03")   # Ctrl-C -> fresh prompt
        await asyncio.sleep(0.3)
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.2)
        except asyncio.TimeoutError:
            pass

        probe = probe_char * 8
        t0 = time.perf_counter()
        await ws.send(b"0" + probe.encode())
        got = 0
        while got < len(probe):
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, bytes) and msg[:1] == b"0":
                got += msg[1:].count(probe_char.encode())
        results.append((time.perf_counter() - t0) * 1000)

        await ws.send(b"0" + b"\x7f" * len(probe))
        await asyncio.sleep(0.2)
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.2)
        except asyncio.TimeoutError:
            pass
    return results


def report(label, r):
    print("%-22s min %6.1f  med %6.1f  max %6.1f ms" % (
        label, min(r), statistics.median(r), max(r)))


async def main():
    url = "wss://" + HOST + "/ws"
    async with websockets.connect(url, subprotocols=["tty"], max_size=None,
                                  ping_interval=None, compression=None,
                                  open_timeout=10) as ws:
        await ws.send(json.dumps({"AuthToken": TOKEN, "columns": 120, "rows": 30}).encode())
        await ws.send(b'1{"columns":120,"rows":30}')
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=1.5)
        except asyncio.TimeoutError:
            pass

        r1 = await measure_echo(ws, "x")
        report("wrapped shell:", r1)

        # replace the wrapper with plain bash in this PTY session
        await ws.send(b"0" + b"exec bash\n")
        await asyncio.sleep(1.0)
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

        r2 = await measure_echo(ws, "y")
        report("plain bash:", r2)
        print()
        d1, d2 = statistics.median(r1), statistics.median(r2)
        print("wrapper overhead: ~%.0f ms | edge+network floor: ~%.0f ms" % (d1 - d2, d2))


asyncio.run(main())
