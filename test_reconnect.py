#!/usr/bin/env python3
"""Tests for run_session auto-reconnect, --tmux and Ctrl+] handling.

Spins up a local mock ttyd websocket server and drives run_session against it
with an injected input queue (no raw mode, no real keyboard). Scenarios:

  1. abnormal server close (1011) -> client reconnects, handshake + tmux exec
     line are re-sent, then a clean close (1000) ends the session
  2. clean close (1000) on the first connection -> session ends, NO reconnect
  3. nothing listening -> client retries until the server appears
  4. Ctrl+] pressed during backoff -> session returns before the backoff ends
"""
import asyncio
import socket
import sys
import time

sys.path.insert(0, ".")
from websockets.asyncio.server import serve

from ttyd_bridge import run_session


def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_reconnect_after_drop():
    """Abnormal close (1011) must reconnect; clean close (1000) must stop."""
    conns, frames = [], []

    async def handler(ws):
        conns.append([])
        mine = conns[-1]
        try:
            n = 3  # tmux=True -> auth + resize + exec line
            for _ in range(n):
                mine.append(await asyncio.wait_for(ws.recv(), timeout=5))
        except Exception:
            return
        if len(conns) == 1:
            await ws.close(code=1011, reason="simulated edge kill")
        else:
            await ws.close(code=1000)

    ok = True
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        q = asyncio.Queue()
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                run_session("ws://127.0.0.1:%d" % port, input_queue=q,
                            tmux=True),
                timeout=10)
            ok &= t("session returns after drop -> reconnect -> clean close",
                    True)
        except asyncio.TimeoutError:
            ok &= t("session returns after drop -> reconnect -> clean close",
                    False)
        elapsed = time.monotonic() - t0

        ok &= t("server saw 2 connections (1 dropped, 1 reconnect)",
                len(conns) == 2)
        if len(conns) == 2:
            # both connections must have re-done the full handshake
            def frames_ok(fs):
                return (len(fs) == 3
                        and fs[0][:1] == b"{"
                        and fs[1][:1] == b"1"
                        and fs[2][:1] == b"0"
                        and b"tmux new -A -s buff" in fs[2])
            ok &= t("connection 1 handshake: auth+resize+tmux exec",
                    frames_ok(conns[0]))
            ok &= t("connection 2 handshake: auth+resize+tmux exec (re-sent)",
                    frames_ok(conns[1]))
        # first backoff is 1s, so a reconnect proves the delay happened
        ok &= t("reconnect was delayed by backoff (~1s), got %.1fs" % elapsed,
                0.8 <= elapsed <= 8)
    return ok


async def test_clean_close_no_reconnect():
    """close(1000) = user typed exit: session ends, must NOT redial."""
    conns = []

    async def handler(ws):
        conns.append(ws)
        try:
            for _ in range(2):
                await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            return
        await ws.close(code=1000)

    ok = True
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        q = asyncio.Queue()
        try:
            await asyncio.wait_for(
                run_session("ws://127.0.0.1:%d" % port, input_queue=q),
                timeout=4)
            ok &= t("clean close ends the session promptly", True)
        except asyncio.TimeoutError:
            ok &= t("clean close ends the session promptly", False)
        await asyncio.sleep(1.5)  # longer than the first backoff
        ok &= t("no reconnect after clean close (1 connection total)",
                len(conns) == 1)
    return ok


async def test_retries_until_server_up():
    """Connection refused -> backoff -> server appears -> connects."""
    port = free_port()
    conns = []

    async def handler(ws):
        conns.append(ws)
        try:
            for _ in range(2):
                await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            return
        await ws.close(code=1000)

    ok = True
    q = asyncio.Queue()
    task = asyncio.create_task(
        run_session("ws://127.0.0.1:%d" % port, input_queue=q))
    await asyncio.sleep(0.3)  # first attempt refused; client is in backoff
    async with serve(handler, "127.0.0.1", port) as server:
        try:
            await asyncio.wait_for(task, timeout=10)
            ok &= t("client connects once the server comes up", True)
        except asyncio.TimeoutError:
            task.cancel()
            ok &= t("client connects once the server comes up", False)
    await asyncio.sleep(0.3)
    ok &= t("connected exactly once after retry", len(conns) == 1)
    return ok


async def test_quit_during_backoff():
    """Ctrl+] during the backoff wait returns immediately, not after it."""
    port = free_port()  # nothing ever listens here

    class Sniff:
        """Captures the session's stderr so we know when backoff starts."""
        def __init__(self):
            self.data = ""
        def write(self, s):
            self.data += s
        def flush(self):
            pass

    sniff = Sniff()
    real_stderr = sys.stderr
    sys.stderr = sniff
    q = asyncio.Queue()
    ok = True
    task = asyncio.create_task(
        run_session("ws://127.0.0.1:%d" % port, input_queue=q))
    try:
        # wait until the first connect attempt has failed and backoff began
        # (on Windows a refused connect can take ~2s here, so keypress must
        # be timed from the backoff, not from task start)
        t0 = time.monotonic()
        while "connection lost" not in sniff.data and time.monotonic() - t0 < 5:
            await asyncio.sleep(0.05)
        entered_backoff = "connection lost" in sniff.data
        q.put_nowait(b"\x1d")  # Ctrl+]
        tk = time.monotonic()
        try:
            await asyncio.wait_for(task, timeout=0.5)
            ok &= t("Ctrl+] during backoff returns before the backoff expires",
                    entered_backoff)
            ok &= t("returned %.2fs after the keypress" %
                    (time.monotonic() - tk), time.monotonic() - tk < 0.5)
        except asyncio.TimeoutError:
            task.cancel()
            ok &= t("Ctrl+] during backoff returns before the backoff expires",
                    False)
    finally:
        sys.stderr = real_stderr
    return ok


async def main():
    ok = True
    ok &= await test_reconnect_after_drop()
    ok &= await test_clean_close_no_reconnect()
    ok &= await test_retries_until_server_up()
    ok &= await test_quit_during_backoff()
    print()
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
