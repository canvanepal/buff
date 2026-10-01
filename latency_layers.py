#!/usr/bin/env python3
"""
Isolate where terminal latency comes from.

Measures three round-trip times:
  1. RFC6455 protocol ping  (handled by the server's websocket stack, no app code)
  2. ttyd PING opcode       (handled by the ttyd app itself)
  3. PTY echo               (full keystroke -> shell echo path)
"""
import asyncio
import json
import statistics
import time

import websockets

HOST = "7681-idtgli4wgl5e24mehup90.e2b.app"
TOKEN = ""
N = 10


async def timed(ws, send_fn, wait_fn):
    t0 = time.perf_counter()
    await send_fn()
    await wait_fn()
    return (time.perf_counter() - t0) * 1000


async def main():
    url = "wss://" + HOST + "/ws"
    async with websockets.connect(url, subprotocols=["tty"], max_size=None,
                                  ping_interval=None, compression=None,
                                  open_timeout=10) as ws:
        await ws.send(json.dumps({"AuthToken": TOKEN, "columns": 120, "rows": 30}).encode())
        await ws.send(b'1{"columns":120,"rows":30}')
        # drain connect burst
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=1.5)
        except asyncio.TimeoutError:
            pass

        # 1. RFC6455 ping
        rtt_ws = []
        for _ in range(N):
            async def do_send():
                pong = ws.ping()
                await pong  # waits for pong

            t0 = time.perf_counter()
            await do_send()
            rtt_ws.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.1)

        # 2. ttyd PING opcode (empty payload binary b"2")
        rtt_ttyd = []
        for _ in range(N):
            got = asyncio.Event()

            async def wait_ping():
                async for msg in ws:
                    if isinstance(msg, bytes) and msg[:1] == b"2" and len(msg) == 1:
                        got.set()
                        return

            t0 = time.perf_counter()
            await ws.send(b"2")
            try:
                await asyncio.wait_for(wait_ping(), timeout=5)
                rtt_ttyd.append((time.perf_counter() - t0) * 1000)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.1)

        print("RFC6455 ping  (ws stack):  min %6.1f  med %6.1f  max %6.1f ms  n=%d" % (
            min(rtt_ws), statistics.median(rtt_ws), max(rtt_ws), len(rtt_ws)))
        if rtt_ttyd:
            print("ttyd PING op  (ttyd app):  min %6.1f  med %6.1f  max %6.1f ms  n=%d" % (
                min(rtt_ttyd), statistics.median(rtt_ttyd), max(rtt_ttyd), len(rtt_ttyd)))
        else:
            print("ttyd PING op  (ttyd app):  no echo observed (server does not reply to client pings)")
        print("(PTY echo measured earlier: ~620 ms) -- for reference")


asyncio.run(main())
