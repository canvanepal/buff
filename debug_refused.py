"""Instrument run_session's refused-connect path: when does each stage run?"""
import asyncio
import socket
import sys
import time

sys.path.insert(0, ".")
import ttyd_bridge

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()

T0 = time.monotonic()


def log(msg):
    print("%6.2fs  %s" % (time.monotonic() - T0, msg), flush=True)


orig_backoff = ttyd_bridge.backoff_wait


async def spy_backoff(q, delay):
    log("backoff_wait(delay=%.1f) enter" % delay)
    try:
        r = await orig_backoff(q, delay)
        log("backoff_wait exit -> %r" % r)
        return r
    except BaseException as e:
        log("backoff_wait raised %r" % e)
        raise

ttyd_bridge.backoff_wait = spy_backoff


async def main():
    q = asyncio.Queue()
    task = asyncio.create_task(ttyd_bridge.run_session(
        "ws://127.0.0.1:%d" % port, input_queue=q))
    await asyncio.sleep(0.3)
    log("putting Ctrl+]")
    q.put_nowait(b"\x1d")
    deadline = time.monotonic() + 3.0
    while not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if task.done():
        log("task done, exception=%r" % task.exception())
    else:
        log("STILL RUNNING after 3s - cancelling")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

asyncio.run(main())
