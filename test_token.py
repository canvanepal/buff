#!/usr/bin/env python3
"""Tests for sandbox access-token routing and the popup's remote watcher.

A freebuff URL carries its access token as a path segment
(`7681-x.e2b.app/<token>`). Measured against a live sandbox: that segment is the
*route* the E2B edge matches on - the ttyd AuthToken is not checked at all
(empty, correct and wrong all connected once the path was right, while the same
host with the path trimmed answered 404/502). So dropping the segment is the
failure mode these tests pin down.

  1. split_token / with_token: parse, and never override a token that is there
  2. resolve_target keeps the path from a pasted URL (it used to split it off)
  3. fetch_token: override skips the network; repeated failures warn once
  4. _remote_agents_fn: the watcher's host keeps the token, and it reuses the
     session's AuthToken instead of asking /token on every poll
  5. agent_watch: a remote_fn that keeps failing backs off (no 4s hammering)
  6. http_status: 404/502 handshake refusals are recognised for the hint text
"""
import asyncio
import io
import sys
import time

sys.path.insert(0, ".")
import urllib.request

import buff
from buff import fetch_token, resolve_target, split_token, with_token

import ttyd_bridge
from ttyd_bridge import _remote_agents_fn, http_status

import buff_popup


def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return bool(cond)


# synthetic, 64 hex characters like a real one - never the live value
TOKEN = "0f" * 32


def test_split_and_with_token():
    ok = True
    ok &= t("bare host has no token", split_token("7681-a.e2b.app")
            == ("7681-a.e2b.app", ""))
    ok &= t("path segment is the token",
            split_token("7681-a.e2b.app/" + TOKEN)
            == ("7681-a.e2b.app", TOKEN))
    ok &= t("trailing slash / stray spaces tolerated",
            split_token(" 7681-a.e2b.app/" + TOKEN + "/ ")
            == ("7681-a.e2b.app", TOKEN))
    ok &= t("--token routes a bare host",
            with_token("7681-a.e2b.app", TOKEN)
            == "7681-a.e2b.app/" + TOKEN)
    ok &= t("a token already in the URL is not overridden",
            with_token("7681-a.e2b.app/" + TOKEN, "other")
            == "7681-a.e2b.app/" + TOKEN)
    ok &= t("no --token changes nothing",
            with_token("7681-a.e2b.app", None) == "7681-a.e2b.app")
    return ok


def test_resolve_target_keeps_token():
    ok = True
    for arg in ("https://7681-a.e2b.app/" + TOKEN,
                "wss://7681-a.e2b.app/" + TOKEN,
                "7681-a.e2b.app/" + TOKEN):
        name, host = resolve_target([arg])
        ok &= t("resolve_target keeps the token: " + arg.split("://")[-1][:18],
                host == "7681-a.e2b.app/" + TOKEN)
        ok &= t("  and names the sandbox by bare host (no token printed)",
                name == "7681-a.e2b.app")
    name, host = resolve_target(["7681-a.e2b.app/"])
    ok &= t("trailing slash on a tokenless URL is dropped",
            host == "7681-a.e2b.app")
    return ok


def test_fetch_token_override_and_warn_once():
    ok = True
    calls = []

    def boom(url, timeout=None):
        calls.append(url)
        raise OSError("HTTP Error 502: Bad Gateway")

    real = urllib.request.urlopen
    urllib.request.urlopen = boom
    saved_cache = dict(buff._token_cache)
    saved_warned = set(buff._token_warned)
    buff._token_cache.clear()
    buff._token_warned.clear()
    real_stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        got = asyncio.run(fetch_token("7681-a.e2b.app", override="FROM-FLAG"))
        ok &= t("override token is returned without any network call",
                got == "FROM-FLAG" and not calls)

        first = asyncio.run(fetch_token("7681-b.e2b.app"))
        second = asyncio.run(fetch_token("7681-b.e2b.app"))
        ok &= t("a failed /token yields an empty token (not an exception)",
                first == "" and second == "")
        ok &= t("the failure is cached: one request, not one per poll",
                len(calls) == 1)
        warnings = sys.stderr.getvalue().count("could not fetch token")
        ok &= t("the warning is printed exactly once per host", warnings == 1)

        # a second, different host still gets its own single warning
        asyncio.run(fetch_token("7681-c.e2b.app"))
        asyncio.run(fetch_token("7681-c.e2b.app"))
        ok &= t("a different host warns once too",
                sys.stderr.getvalue().count("could not fetch token") == 2
                and len(calls) == 2)
    finally:
        sys.stderr = real_stderr
        urllib.request.urlopen = real
        buff._token_cache.clear()
        buff._token_cache.update(saved_cache)
        buff._token_warned.clear()
        buff._token_warned.update(saved_warned)
    return ok


def test_remote_agents_fn_keeps_token():
    ok = True
    seen = {}

    class FakeBuff:
        @staticmethod
        async def remote_agents(host, token=None):
            seen["host"], seen["token"] = host, token
            return ["work"]

    real_buff = sys.modules.get("buff")
    sys.modules["buff"] = FakeBuff
    try:
        fn = _remote_agents_fn("wss://7681-x.e2b.app/" + TOKEN + "/ws",
                               token="AUTH")
        ok &= t("a non-e2b host returns no watcher",
                _remote_agents_fn("wss://localhost:9999/ws") is None)
        if fn is None:
            return False
        out = fn()
        ok &= t("the watcher host keeps the access token in its path",
                seen.get("host") == "7681-x.e2b.app/" + TOKEN)
        ok &= t("the watcher reuses the session AuthToken (no /token per poll)",
                seen.get("token") == "AUTH")
        ok &= t("agents come back as running panels",
                out == [{"name": "work", "state": "running"}])
    finally:
        if real_buff is not None:
            sys.modules["buff"] = real_buff
        else:
            sys.modules.pop("buff", None)
    return ok


def test_watch_backs_off():
    """A dead sandbox must not be retried every interval forever."""
    ok = True
    dead = {"n": 0}

    def failing():
        dead["n"] += 1
        return None

    h = buff_popup.agent_watch({"id": None}, remote_fn=failing, interval=0.05,
                               max_interval=0.8, local_fn=lambda: [])
    time.sleep(1.0)
    dead_n = dead["n"]
    h["stop"].set()

    healthy = {"n": 0}

    def working():
        healthy["n"] += 1
        return []

    h2 = buff_popup.agent_watch({"id": None}, remote_fn=working, interval=0.05,
                                max_interval=0.8, local_fn=lambda: [])
    time.sleep(1.0)
    live_n = healthy["n"]
    h2["stop"].set()

    ok &= t("a failing watcher polls far less than a healthy one (%d vs %d)"
            % (dead_n, live_n), dead_n * 2 < live_n)
    ok &= t("the failing watcher did keep trying (%d calls)" % dead_n,
            dead_n >= 3)
    return ok


def test_unreachable_is_explained():
    """An unreachable sandbox must raise SandboxUnreachable, not leak a traceback.

    Regression: `buff ssh 1` used to end in a raw
    websockets.exceptions.InvalidStatus traceback.
    """
    ok = True
    ok &= t("SandboxUnreachable is a RuntimeError you can catch",
            issubclass(buff.SandboxUnreachable, RuntimeError))
    ok &= t("no status -> plain 'could not reach' wording",
            "could not reach the sandbox"
            in buff.SandboxUnreachable("h.e2b.app", None, "refused").friendly())
    ok &= t("404 -> missing access token wording",
            "access-token" in buff.SandboxUnreachable("h.e2b.app", 404).friendly())
    ok &= t("502 -> says the port is not answering, not that the box is gone",
            "answering on that port"
            in buff.SandboxUnreachable("h.e2b.app", 502).friendly())

    # a refused connection to a closed local port must come back as ours
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        asyncio.run(buff.connect_sandbox("127.0.0.1:%d" % port))
        ok &= t("connect_sandbox raises on a refused connection", False)
    except buff.SandboxUnreachable as e:
        ok &= t("connect_sandbox raises SandboxUnreachable", True)
        ok &= t("  with a readable one-liner (%s)" % e.friendly()[:38],
                bool(e.friendly()))
    except Exception as e:                                     # noqa: BLE001
        ok &= t("connect_sandbox raised %s instead" % type(e).__name__, False)
    return ok


def test_http_status():
    ok = True

    class Resp:
        status_code = 502

    class Err(Exception):
        response = Resp()

    ok &= t("status read off the handshake response", http_status(Err()) == 502)
    ok &= t("status read out of the message text",
            http_status(ValueError("server rejected WebSocket connection: "
                                   "HTTP 502")) == 502)
    ok &= t("a plain drop has no status", http_status(None) is None)
    ok &= t("a drop message with no status has none",
            http_status(OSError("connection reset")) is None)
    ok &= t("404 is a missing access token, 502 a sleeping sandbox",
            404 in ttyd_bridge.HANDSHAKE_HINTS
            and 502 in ttyd_bridge.HANDSHAKE_HINTS)
    return ok


def main():
    ok = True
    for fn in (test_split_and_with_token, test_resolve_target_keeps_token,
               test_fetch_token_override_and_warn_once,
               test_remote_agents_fn_keeps_token, test_watch_backs_off,
               test_unreachable_is_explained, test_http_status):
        print("--", fn.__name__)
        ok &= fn()
        print()
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
