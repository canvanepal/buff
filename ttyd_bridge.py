#!/usr/bin/env python3
"""
ttyd-bridge: pipe a ttyd (browser terminal) websocket into your local terminal.

Speaks the ttyd v1 websocket protocol (decoded from the ttyd client bundle):
  - Connect to <wsUrl> with subprotocol "tty"
  - On open, send a BINARY frame of raw JSON: {"AuthToken": <token>, "columns": C, "rows": R}
    (no opcode prefix -- the server detects it by the leading '{')
  - Server -> client binary frames, first byte = opcode:
      '0' (0x30) OUTPUT  : rest is PTY output (write to stdout)
      '1' (0x31) TITLE   : rest is UTF-8 window title
      '2' (0x32) PING    : empty payload = keepalive (echo back);
                           non-empty = server preferences blob (ignore)
  - Client -> server binary frames, first byte = opcode:
      '0' (0x30) INPUT   : keystrokes (VT sequences)
      '1' (0x31) RESIZE  : {"columns":C,"rows":R} JSON
      '2' (0x32) PING    : keepalive

Usable as a CLI:
  python ttyd_bridge.py wss://7681-<sandbox-id>.e2b.app/ws [--cols N] [--rows N] [--token TOKEN] [--tmux] [--no-reconnect]

Or imported by other tools (see buff.py):
  from ttyd_bridge import run_session
  await run_session(url, token="")

Exit the session with Ctrl+]  (Ctrl+C is forwarded to the remote shell).
"""

import argparse
import asyncio
import json
import re
import sys
import time
from collections import deque

try:
    import websockets
    import websockets.exceptions  # explicit: needed at import time for RECOVERABLE
except ImportError:
    websockets = None

# ttyd message opcodes
OUT_OUTPUT = b"0"
OUT_TITLE = b"1"
OUT_PING = b"2"
IN_INPUT = b"0"
IN_RESIZE = b"1"
IN_PING = b"2"

# msvcrt 2-byte key prefix -> VT sequence (Windows only)
WIN_KEYMAP = {
    b"H": b"\x1b[A", b"P": b"\x1b[B", b"K": b"\x1b[D", b"M": b"\x1b[C",  # arrows
    b"G": b"\x1b[H", b"O": b"\x1b[F",                                     # home/end
    b"I": b"\x1b[5~", b"Q": b"\x1b[6~",                                   # pgup/pgdn
    b"R": b"\x1b[2~", b"S": b"\x1b[3~",                                   # ins/del
    b"t": b"\x1b[15~", b"u": b"\x1b[17~", b"v": b"\x1b[18~", b"w": b"\x1b[19~",  # F5-F8
}


# ANSI parsing for prompt/TUI detection
ANSI_CSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
ANSI_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
PROMPT_TAIL = re.compile(rb"[#>$%] ?$")

# sequences that mean a full-screen app (vim/htop/...) is running: never predict
TUI_ON = (b"\x1b[?1049h", b"\x1b[?25l", b"\x1b[?1000h", b"\x1b[?1002h", b"\x1b[?1006h")
TUI_OFF = (b"\x1b[?1049l", b"\x1b[?25h")


class FastEcho:
    """Mosh-style predictive local echo.

    At a quiet shell prompt, printable keys are rendered instantly and their
    remote echo is swallowed on arrival. Any divergence drops the prediction
    queue and falls back to remote echo. Full-screen apps (TUI) disable
    prediction completely.
    """

    ARM_QUIET = 0.12   # seconds of output silence before re-arming prediction
    TTL = 1.5          # expected-echo entries expire after this long
    PASTE = 8          # bursts larger than this are treated as paste

    def __init__(self, fast=True):
        self.fast = fast
        self.out = sys.stdout.buffer   # local render target (tests may override)
        self.suppress = deque()   # entries: [remaining_bytes, deadline]
        self.tail = b""           # recent output for prompt detection
        self.last_output = 0.0
        self.hold = False         # disarmed until the next output chunk
        self.tui = False

    # ---------- output side ----------
    def on_output(self, data):
        if not self.fast:
            return data
        self.last_output = time.monotonic()
        self.hold = False
        if any(s in data for s in TUI_ON):
            self.tui = True
        elif any(s in data for s in TUI_OFF):
            self.tui = False
        self.tail = (self.tail + data)[-192:]
        if self.suppress:
            data = self._swallow(data)
        return data

    def _swallow(self, data):
        i = 0
        while i < len(data) and self.suppress:
            exp, deadline = self.suppress[0]
            if time.monotonic() > deadline:
                self.suppress.popleft()
                continue
            lim = min(len(exp), len(data) - i)
            n = 0
            while n < lim and data[i + n] == exp[n]:
                n += 1
            if n == len(exp):          # entry fully matched: skip its echo
                i += n
                self.suppress.popleft()
            elif n > 0 and i + n == len(data):   # partial at chunk boundary
                self.suppress[0] = [exp[n:], deadline]
                i += n
                break
            elif n == 0:               # divergence: fall back to remote echo
                self.suppress.clear()
                break
            else:                      # matched then diverged: consume, drop rest
                self.suppress.clear()
                i += n
                break
        return data[i:]

    # ---------- input side ----------
    def at_prompt(self):
        if self.tui or self.hold:
            return False
        if time.monotonic() - self.last_output < self.ARM_QUIET:
            return False
        plain = ANSI_OSC.sub(b"", ANSI_CSI.sub(b"", self.tail))
        return bool(PROMPT_TAIL.search(plain))

    def key(self, data):
        """Render predictions for this keystroke burst; always returns data
        unchanged so the exact same bytes still go to the remote PTY."""
        if not self.fast or len(data) > self.PASTE:
            if len(data) > self.PASTE:
                self.hold = True
            return data
        if not self.at_prompt():
            return data

        render = b""
        now = time.monotonic()
        i, n = 0, len(data)
        while i < n:
            b0 = data[i]
            if b0 == 0x7F:                       # backspace
                render += b"\b \b"
                self.suppress.append([b"\b \b", now + self.TTL])
                i += 1
            elif b0 in (0x0D, 0x0A):             # enter
                render += b"\r\n"
                self.suppress.append([b"\r\n", now + self.TTL])
                self.hold = True
                break
            elif b0 < 0x20 or b0 == 0x1B:        # other control / escape: disarm
                break
            else:                                # printable (utf-8 aware)
                j = i + 1
                while j < n and (data[j] & 0xC0) == 0x80:
                    j += 1
                render += data[i:j]
                self.suppress.append([data[i:j], now + self.TTL])
                i = j
        if render:
            self.out.write(render)
            self.out.flush()
        return data


def enable_raw_mode():
    """Put the local console in raw mode. Returns (restore_fn, msvcrt_getch_or_None)."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        h = ctypes.windll.kernel32.GetStdHandle
        STDIN, STDOUT = -10, -11

        def get_mode(handle):
            mode = ctypes.c_uint32()
            ctypes.windll.kernel32.GetConsoleMode(h(handle), ctypes.byref(mode))
            return mode.value

        # ENABLE_VIRTUAL_TERMINAL_INPUT=0x200 ; clear processed/line/echo input (0x7)
        old_in = get_mode(STDIN)
        ctypes.windll.kernel32.SetConsoleMode(h(STDIN), (old_in | 0x200) & ~0x7)

        # ENABLE_VIRTUAL_TERMINAL_PROCESSING=0x4 for ANSI output
        old_out = get_mode(STDOUT)
        ctypes.windll.kernel32.SetConsoleMode(h(STDOUT), old_out | 0x4)

        def restore():
            ctypes.windll.kernel32.SetConsoleMode(h(STDIN), old_in)
            ctypes.windll.kernel32.SetConsoleMode(h(STDOUT), old_out)

        return restore, msvcrt.getch
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        tty.setraw(fd)
        return (lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)), None


def read_key_win(getch):
    """Read one keypress on Windows, mapping msvcrt prefixes to VT sequences."""
    b = getch()
    if b in (b"\x00", b"\xe0"):
        b2 = getch()
        return WIN_KEYMAP.get(b2, b"")
    return b


async def pump_output(ws, sess):
    """Server -> local stdout. Ends when ws closes."""
    out = sys.stdout.buffer
    async for msg in ws:
        if isinstance(msg, str):
            continue  # JSON control frames (preferences etc.) -- ignored
        if not msg:
            continue
        op, payload = msg[:1], msg[1:]
        if op == OUT_OUTPUT:
            disp = sess.on_output(payload)
            if disp:
                out.write(disp)
                out.flush()
        elif op == OUT_TITLE:
            sys.stdout.write(f"\x1b]2;{payload.decode('utf-8', 'replace')}\x07")
            sys.stdout.flush()
        elif op == OUT_PING:
            if not payload:  # empty ping => reply; non-empty is preferences, ignore
                await ws.send(OUT_PING)


async def pump_input(ws, q, cols, rows, token, sess, tmux=False):
    """Handshake + keystroke queue -> server. Returns when user hits Ctrl+].

    The keystroke queue is owned by run_session (not this coroutine) so the
    same queue - and the raw-mode reader feeding it - survives reconnects.
    """
    # handshake: auth JSON as raw binary bytes (server detects the leading '{')
    auth = json.dumps({"AuthToken": token, "columns": cols, "rows": rows})
    await ws.send(auth.encode())
    await ws.send(IN_RESIZE + json.dumps({"columns": cols, "rows": rows}).encode())
    if tmux:
        # Enter (or re-attach to) a persistent tmux session so work inside it
        # survives a dropped connection: this line runs again in every fresh
        # shell after a reconnect and `tmux -A` reattaches to the same
        # session. Falls back to a plain shell (with a note) if tmux is absent.
        await ws.send(IN_INPUT + b"command -v tmux >/dev/null 2>&1 "
                                 b"&& exec tmux new -A -s buff "
                                 b"|| echo 'buff: tmux not available - plain shell'\r")
    while True:
        data = await q.get()
        if data == b"\x1d":  # Ctrl+] = exit
            return
        if sess.fast:
            sess.key(data)  # renders local predictions; sends data unchanged
        await ws.send(IN_INPUT + data)


def start_reader(q):
    """Put the local console in raw mode and feed keystrokes into q.

    Returns the restore function that puts the console back to normal.
    The reader runs on a daemon thread so it dies with the process.
    """
    import threading

    if sys.platform == "win32":
        import msvcrt
        import time
        restore, getch = enable_raw_mode()

        def reader():
            # poll so the daemon thread never blocks interpreter exit
            while True:
                if msvcrt.kbhit():
                    q.put_nowait(read_key_win(getch))
                else:
                    time.sleep(0.02)
    else:
        import os
        restore, _ = enable_raw_mode()

        def reader():
            while True:
                try:
                    q.put_nowait(os.read(0, 1024))
                except OSError:
                    q.put_nowait(b"")
                    return

    threading.Thread(target=reader, daemon=True).start()
    return restore


async def backoff_wait(q, delay):
    """Sleep `delay` seconds; return True if the user hit Ctrl+] meanwhile.

    Keys typed during the outage are dropped: they were meant for a shell
    that no longer exists.
    """
    deadline = time.monotonic() + delay
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            data = await asyncio.wait_for(q.get(), timeout=min(remaining, 0.25))
        except asyncio.TimeoutError:
            continue
        if data == b"\x1d":
            return True


WS_PATH = "/ws"

RECOVERABLE = (
    websockets.exceptions.WebSocketException,  # ConnectionClosed, InvalidStatus, ...
    OSError,           # connection refused / DNS / edge unreachable
    TimeoutError,      # open_timeout / ping_timeout (asyncio.TimeoutError is an alias)
)

# What the edge's refusal usually means, measured against freebuff sandboxes:
#   - the access token trimmed off the URL       -> 404
#   - nothing listening on that port inside the VM -> 502
# The 502 case is per-port, not per-sandbox: observed with sandbox 1, whose
# ttyd on 7681 was down while its sshd/websocat bridge on 8081 answered fine.
HANDSHAKE_HINTS = {
    404: "sandbox rejected the URL - the host is missing its /<access-token> "
         "path; copy the whole URL from freebuff.com",
    502: "nothing is answering on that port: the sandbox's ttyd is not "
         "running. Open the terminal on freebuff.com once to start it (the "
         "sandbox itself may still be up)",
    503: "sandbox is starting up or restarting",
}


def http_status(exc):
    """HTTP status behind a failed websocket handshake, or None."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code is None and exc is not None:
        match = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
        code = int(match.group(1)) if match else None
    return code


def _remote_agents_fn(url, token=None):
    """Sync callable (for the popup watcher) listing the sandbox's tmux sessions.

    The host keeps its path: a freebuff sandbox may need the URL's access token
    as a route prefix, and urlparse().hostname alone would drop it (every poll
    then answering 404/502).
    """
    try:
        from urllib.parse import urlparse
        parts = urlparse(url)
        host = parts.netloc or ""
        path = (parts.path or "").rstrip("/")
        if path.endswith(WS_PATH):
            path = path[:-len(WS_PATH)]
        host += path
    except Exception:
        host = ""
    if not host.split("/")[0].endswith(".e2b.app"):
        return None      # the path (the access token) is what follows the host

    def _fn():
        try:
            import asyncio
            import buff  # lazy: keeps standalone ttyd_bridge usable
            # reuse the session's AuthToken: the watcher must not re-ask /token
            # every few seconds (that is what flooded the terminal with
            # 'could not fetch token: HTTP Error 502')
            names = asyncio.run(buff.remote_agents(host, token=token))
            return [{"name": n, "state": "running"} for n in names]
        except Exception:
            return None

    return _fn


async def run_session(url, token="", cols=None, rows=None, fast=False,
                      tmux=False, reconnect=True, input_queue=None,
                      popup=False):
    """Run an interactive bridge session.

    fast=True enables predictive local echo. tmux=True keeps work alive
    across drops by running the shell inside `tmux new -A -s buff` (every
    reconnect re-attaches to that same session). reconnect=True (default)
    automatically redials after a dropped or refused connection with capped
    backoff; Ctrl+] during the wait quits. A clean remote close (code 1000 -
    the user typed exit) ends the session instead of reconnecting. The
    input_queue parameter lets tests inject keys instead of driving the real
    console. popup=True opens the fun animated status buddy (best-effort:
    its own process, closes itself when this session ends).
    """
    if cols is None or rows is None:
        import shutil
        size = shutil.get_terminal_size((120, 30))
        cols = cols or size.columns
        rows = rows or size.lines

    if input_queue is None:
        q: asyncio.Queue = asyncio.Queue()
        restore = start_reader(q)
    else:
        q, restore = input_queue, None

    # fun status popup: own process, never blocks or breaks the session
    ph = watch = None
    if popup:
        try:
            from buff_popup import start_popup, popup_state
            ph = start_popup("buff 📡")
            popup_state(ph, "connecting")
        except Exception:
            ph = None
    if ph is not None:
        try:
            from buff_popup import agent_watch
            watch = agent_watch(ph, remote_fn=_remote_agents_fn(url, token))
        except Exception:
            watch = None

    attempt = 0
    failed = False        # did we give up, or did the user leave?
    try:
        while True:
            sess = FastEcho(fast=fast)  # fresh prediction state per connection
            t_connect = time.monotonic()
            drop = exc = None
            try:
                async with websockets.connect(
                    url,
                    subprotocols=["tty"],
                    max_size=None,
                    # The E2B edge kills idle websockets after ~60s (abnormal
                    # close 1006) and ttyd sends no keepalives of its own, so
                    # client-side pings are the only thing keeping the session
                    # (and its shell) alive while the user pauses mid-work.
                    # Verified on the wire: no ping -> dead at 63s,
                    # ping every 15s -> survives 240s+ idle and stays writable.
                    ping_interval=15,
                    ping_timeout=15,
                    compression=None,
                    open_timeout=15,
                ) as ws:
                    if ph is not None:
                        try:
                            popup_state(ph, "working")
                        except Exception:
                            pass
                    out_task = asyncio.create_task(pump_output(ws, sess))
                    in_task = asyncio.create_task(
                        pump_input(ws, q, cols, rows, token, sess, tmux=tmux))
                    try:
                        done, _pending = await asyncio.wait(
                            [out_task, in_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # user pressed Ctrl+]: input side returned cleanly
                        if (in_task in done and not in_task.cancelled()
                                and in_task.exception() is None):
                            return
                        exc = next((t.exception() for t in done
                                   if not t.cancelled()
                                   and t.exception() is not None), None)
                        if exc is not None and not isinstance(exc, RECOVERABLE):
                            failed = True
                            raise exc  # a real bug - never mask it with a retry
                        if ws.close_code == 1000:
                            return  # remote shell exited cleanly (user typed exit)
                        drop = ("closed with code %s" % ws.close_code
                                if ws.close_code is not None else repr(exc))
                    finally:
                        for t in (out_task, in_task):
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(out_task, in_task,
                                             return_exceptions=True)
            except RECOVERABLE as e:
                exc = e
                drop = "%s: %s" % (type(e).__name__, e)

            if not reconnect:
                if exc is not None:
                    failed = True
                    raise exc
                return
            if time.monotonic() - t_connect >= 30:
                attempt = 0  # last connection was healthy - restart backoff
            attempt += 1
            delay = min(2 ** (attempt - 1), 15)  # 1s, 2s, 4s, 8s, 15s, ...
            status = http_status(exc)
            if status in (404, 502, 503):
                # The handshake itself was refused: this is not a dropped link,
                # it is the edge saying the sandbox is not there. Say which, and
                # do not hammer it every second while it boots.
                delay = max(delay, min(10 * attempt, 30))
            if ph is not None:
                try:
                    popup_state(ph, "reconnecting")
                except Exception:
                    pass
            hint = HANDSHAKE_HINTS.get(status)
            if hint and attempt == 1:
                sys.stderr.write("* %s (HTTP %d, Ctrl+] to quit)\r\n"
                                 % (hint, status))
            sys.stderr.write("* connection lost (%s) - reconnecting in %ds "
                             "(attempt %d, Ctrl+] to quit)\r\n"
                             % (drop, delay, attempt))
            sys.stderr.flush()
            if await backoff_wait(q, delay):
                return
    finally:
        if restore is not None:
            restore()
        if watch is not None:
            try:
                from buff_popup import stop_watch
                stop_watch(watch)
            except Exception:
                pass
        if ph is not None:
            try:
                from buff_popup import popup_state, stop_popup
                if failed:
                    # it did not just end, it broke: say so, and let the child
                    # hold that face up for a moment after we are gone
                    popup_state(ph, "error")
                stop_popup(ph, linger=failed)
            except Exception:
                pass


def main():
    if websockets is None:
        sys.exit("Missing dependency: pip install websockets")
    p = argparse.ArgumentParser(description="Bridge a ttyd websocket to your terminal")
    p.add_argument("url", help="ttyd websocket URL, e.g. wss://7681-<id>.e2b.app/ws")
    p.add_argument("--token", default="", help="auth token from /token endpoint (default empty)")
    p.add_argument("--cols", type=int, default=None)
    p.add_argument("--rows", type=int, default=None)
    p.add_argument("--fast", action="store_true",
                   help="mosh-style predictive local echo (helps on high-latency links)")
    p.add_argument("--tmux", action="store_true",
                   help="run the shell inside tmux so work survives reconnects")
    p.add_argument("--no-reconnect", action="store_true",
                   help="exit when the connection drops instead of redialing")
    p.add_argument("--no-popup", action="store_true",
                   help="don't show the animated status popup")
    args = p.parse_args()

    try:
        asyncio.run(run_session(args.url, token=args.token, cols=args.cols,
                                rows=args.rows, fast=args.fast,
                                tmux=args.tmux,
                                reconnect=not args.no_reconnect,
                                popup=not args.no_popup))
    except (KeyboardInterrupt, websockets.exceptions.ConnectionClosed):
        print("\n* session closed", file=sys.stderr)


if __name__ == "__main__":
    main()
