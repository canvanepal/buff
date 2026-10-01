#!/usr/bin/env python3
"""Non-interactive smoke test: connect to ttyd /ws, do handshake, read first frames."""
import asyncio, json, sys

import websockets

URL = sys.argv[1] if len(sys.argv) > 1 else "wss://7681-idtgli4wgl5e24mehup90.e2b.app/ws"

async def main():
    async with websockets.connect(URL, subprotocols=["tty"], max_size=None,
                                  ping_interval=None, compression=None,
                                  open_timeout=10, close_timeout=3) as ws:
        print("connected; negotiated subprotocol:", ws.subprotocol)
        auth = json.dumps({"AuthToken": "", "columns": 120, "rows": 30})
        await ws.send(auth.encode())  # raw JSON binary, no opcode prefix
        await ws.send(b'1{"columns":120,"rows":30}')
        for i in range(5):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=4)
            except asyncio.TimeoutError:
                print(f"[frame {i}] (timeout waiting for more)")
                break
            if isinstance(msg, str):
                print(f"[frame {i}] TEXT: {msg[:120]}")
            else:
                op, payload = msg[:1], msg[1:]
                names = {b"0": "OUTPUT", b"1": "TITLE", b"2": "PING"}
                print(f"[frame {i}] {names.get(op, op)} {len(payload)}B: {payload[:100]!r}")

try:
    asyncio.run(main())
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
    sys.exit(1)
