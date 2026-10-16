"""Live end-to-end: run_session(tmux=True) against the real box.

Connects, verifies the tmux exec line created a 'buff' session on the
sandbox, then quits via Ctrl+] and confirms clean return.
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")
from buff import run_remote
from ttyd_bridge import run_session

HOST = "7681-iec7mdhobz4a9geme96o9.e2b.app"
URL = "wss://" + HOST + "/ws"


async def main():
    import buff
    token = await buff.fetch_token(HOST)
    q = asyncio.Queue()
    t0 = time.monotonic()
    task = asyncio.create_task(
        run_session(URL, token=token, cols=100, rows=30,
                    tmux=True, input_queue=q))

    # give it time to connect + exec tmux
    await asyncio.sleep(4)
    rc, out = await run_remote(HOST,
                               "tmux ls 2>&1; echo TMUXRC_$?", timeout=30)
    alive = "buff" in out and "TMUXRC_0" in out
    print("LIVE tmux session on box:", "YES" if alive else "NO -> " + repr(out))

    # quit with Ctrl+]
    q.put_nowait(b"\x1d")
    try:
        await asyncio.wait_for(task, timeout=5)
        print("LIVE clean quit via Ctrl+]: YES (%.1fs)" % (time.monotonic() - t0))
        quit_ok = True
    except asyncio.TimeoutError:
        task.cancel()
        print("LIVE clean quit via Ctrl+]: NO (timeout)")
        quit_ok = False

    # after client quit, the tmux session should still be there (detached)
    rc, out = await run_remote(HOST, "tmux ls 2>&1; echo TMUXRC_$?", timeout=30)
    survives = "buff" in out
    print("LIVE tmux survives client quit (work preserved):",
          "YES" if survives else "NO -> " + repr(out))

    # cleanup: kill the test session
    rc, out = await run_remote(HOST,
                               "tmux kill-session -t buff 2>/dev/null; "
                               "echo CLEAN_$?", timeout=30)
    print("LIVE cleanup:", "done" if "CLEAN_0" in out else "check: " + repr(out))

    ok = alive and quit_ok and survives
    print()
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
