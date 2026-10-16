#!/usr/bin/env python3
"""Tests for buff_popup and its wiring into run_ssh / run_session.

1. real child: spawns, accepts a state line, exits promptly on EOF (stop)
2. stop_popup is safe on None / twice / after death; popup_state never raises
3. run_ssh(popup=True) spawns the buddy, reports 'ssh', reports
   'reconnecting' on a 255 drop, and stops it when done; a bridge that fails
   for good (no-reconnect) reports 'error' and leaves the face up
4. run_session(popup=True) reports connecting -> working and closes the
   buddy when the session ends (mock ttyd server, clean close 1000)
5. state -> face: the decision table, the busy/idle rules, and one distinct
   PNG per face
"""
import asyncio
import json
import socket
import sys
import time

sys.path.insert(0, ".")
import buff
import buff_popup
import ttyd_bridge
from ttyd_bridge import run_session


def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def wait_gone(handle, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.poll() is not None:
            return True
        time.sleep(0.05)
    return False


ok = True

# 1. real child lifecycle
h = buff_popup.start_popup("popup test")
ok &= t("child spawned", h is not None)
if h is not None:
    # settle first: a child that crashes during Tk() startup (bad geometry,
    # missing display) must be caught here, not masked by a too-early poll
    time.sleep(0.5)
    ok &= t("child still alive 0.5s after spawn (window up)",
            h.poll() is None)
    buff_popup.popup_state(h, "working")   # must not raise
    ok &= t("state line delivered", True)
    buff_popup.popup_agents(h, {"local": [{"name": "buff session", "state": "running"}],
                                "remote": [{"name": "buff", "state": "running"},
                                           {"name": "old", "state": "stopped"}]})
    time.sleep(0.4)
    ok &= t("child survives an agents payload (still rendering)",
            h.poll() is None)
    buff_popup.stop_popup(h)
    ok &= t("child exits on stop_popup (window closes)", wait_gone(h))
    ok &= t("child exits cleanly (rc=0)", h.returncode == 0)
else:
    ok = False  # spawn failed (recorded FAIL above)

# 2. safety of the helpers
try:
    buff_popup.stop_popup(None)
    buff_popup.stop_popup(h)          # twice / already dead
    buff_popup.popup_state(h, "late")  # dead handle
    buff_popup.popup_state(None, "x")
    ok &= t("helpers never raise (None/dead/double stop)", True)
except Exception as e:
    ok &= t("helpers never raise (None/dead/double stop): %r" % e, False)


# 3. run_ssh wiring (patched helpers -> no real window in this test)
events = []


def spy_start(title="buff"):
    events.append(("start", title))
    return "FAKEHANDLE"


def spy_state(h, state):
    events.append(("state", state))


def spy_stop(h, linger=False):
    events.append(("stop", h, linger))


real = (buff_popup.start_popup, buff_popup.popup_state, buff_popup.stop_popup)
buff_popup.start_popup, buff_popup.popup_state, buff_popup.stop_popup = (
    spy_start, spy_state, spy_stop)

rc = buff.run_ssh(["ssh", "root@sid"], reconnect=True, popup=True,
                  call=lambda a: 0, sleep=lambda d: None)
ok &= t("run_ssh popup: returns rc 0", rc == 0)
ok &= t("run_ssh popup: start/ssh-state/stop sequence",
        events == [("start", "buff ssh"), ("state", "ssh"),
                   ("stop", "FAKEHANDLE", False)])

events.clear()
rc = buff.run_ssh(["ssh", "root@sid"], reconnect=True, popup=True,
                  call=lambda a: 255 if len(events) < 5 else 0,
                  sleep=lambda d: None)
ok &= t("run_ssh popup: reconnect reports 'reconnecting'",
        ("state", "reconnecting") in events)
ok &= t("run_ssh popup: stops after recovery, no error face",
        events and events[-1] == ("stop", "FAKEHANDLE", False)
        and ("state", "error") not in events)

# a bridge that is dead for good (--no-reconnect) is a failure, not a goodbye:
# the popup is told, and is left up (linger) so the face is actually seen
events.clear()
rc = buff.run_ssh(["ssh", "root@sid"], reconnect=False, popup=True,
                  call=lambda a: 255, sleep=lambda d: None)
ok &= t("run_ssh popup: dead bridge returns 255", rc == 255)
ok &= t("run_ssh popup: failed session reports 'error'",
        ("state", "error") in events)
ok &= t("run_ssh popup: failed session leaves the face up (linger=True)",
        events and events[-1] == ("stop", "FAKEHANDLE", True))

# Ctrl+C during backoff is the user leaving, so it must NOT look like a crash
events.clear()
rc = buff.run_ssh(["ssh", "root@sid"], reconnect=True, popup=True,
                  call=lambda a: 255,
                  sleep=lambda d: (_ for _ in ()).throw(KeyboardInterrupt()))
ok &= t("run_ssh popup: Ctrl+C returns 130 and reports no error",
        rc == 130 and ("state", "error") not in events
        and events[-1] == ("stop", "FAKEHANDLE", False))

(buff_popup.start_popup, buff_popup.popup_state,
 buff_popup.stop_popup) = real


# 4. run_session wiring against a mock ttyd server
async def session_popup_test():
    ev = []
    orig = (buff_popup.start_popup, buff_popup.popup_state,
            buff_popup.stop_popup)
    buff_popup.start_popup = lambda title="buff": (ev.append(("start", title)),
                                                   "H")[1]
    buff_popup.popup_state = lambda h, s: ev.append(("state", s))
    buff_popup.stop_popup = lambda h, linger=False: ev.append(("stop", h, linger))
    try:
        from websockets.asyncio.server import serve

        async def handler(ws):
            for _ in range(2):  # auth + resize, then clean close
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except Exception:
                    break
            await ws.close(code=1000)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            await run_session("ws://127.0.0.1:%d" % port,
                              input_queue=asyncio.Queue(), popup=True)
    finally:
        (buff_popup.start_popup, buff_popup.popup_state,
         buff_popup.stop_popup) = orig
    return ev

ev = asyncio.run(session_popup_test())
ok &= t("run_session popup: started with title", ("start", "buff 📡") in ev)
ok &= t("run_session popup: connecting -> working",
        ("state", "connecting") in ev and ("state", "working") in ev)
ok &= t("run_session popup: stopped at session end",
        bool(ev) and ev[-1][0] == "stop")


# 4b. a session that cannot reconnect is a failure, and says so
async def session_error_test():
    ev = []
    orig = (buff_popup.start_popup, buff_popup.popup_state,
            buff_popup.stop_popup)
    buff_popup.start_popup = lambda title="buff": (ev.append(("start", title)),
                                                   "H")[1]
    buff_popup.popup_state = lambda h, s: ev.append(("state", s))
    buff_popup.stop_popup = lambda h, linger=False: ev.append(("stop", h,
                                                              linger))
    try:
        try:
            # nothing is listening on port 1: refused, and --no-reconnect
            await run_session("ws://127.0.0.1:1", input_queue=asyncio.Queue(),
                              popup=True, reconnect=False)
        except Exception:
            pass
    finally:
        (buff_popup.start_popup, buff_popup.popup_state,
         buff_popup.stop_popup) = orig
    return ev

ev = asyncio.run(session_error_test())
ok &= t("run_session popup: dead sandbox reports 'error'",
        ("state", "error") in ev)
ok &= t("run_session popup: dead sandbox leaves the face up (linger=True)",
        bool(ev) and ev[-1] == ("stop", "H", True))


# 5. CLI flag propagation: default ON, --no-popup turns it off
async def cli_flag_test():
    caps = []
    orig_run_ssh = buff.run_ssh
    orig_fetch, orig_load, orig_save = (buff.fetch_token, buff.load_config,
                                        buff.save_config)
    import ttyd_bridge
    orig_rs = ttyd_bridge.run_session

    def fake_run_ssh(argv, **kw):        # run_ssh is sync (real one too)
        caps.append(("ssh", kw.get("popup")))
        return 0

    async def fake_fetch(host):
        return "TOK"

    async def fake_run_session(url, **kw):
        caps.append(("session", kw.get("popup")))

    cfg = {"sandboxes": {"mybox": "7681-x.e2b.app"}, "last": "mybox"}
    buff.run_ssh = fake_run_ssh
    buff.fetch_token = fake_fetch
    buff.load_config = lambda: {k: (dict(v) if isinstance(v, dict) else v)
                                for k, v in cfg.items()}
    buff.save_config = lambda c: None
    ttyd_bridge.run_session = fake_run_session
    try:
        for argv, expect in ((["buff"], True),
                             (["buff", "--no-popup"], False)):
            sys.argv = argv
            try:
                await buff.main_async()
            except SystemExit:
                pass
        # --no-setup keeps this test offline (no sshd provisioning probe)
        for argv, expect in ((["buff", "ssh", "mybox", "--no-setup"], True),
                             (["buff", "ssh", "mybox", "--no-setup",
                               "--no-popup"], False)):
            sys.argv = argv
            try:
                await buff.main_async()
            except SystemExit:
                pass
    finally:
        buff.run_ssh = orig_run_ssh
        buff.fetch_token, buff.load_config, buff.save_config = (orig_fetch,
                                                                 orig_load,
                                                                 orig_save)
        ttyd_bridge.run_session = orig_rs
        sys.argv = ["test_popup.py"]
    return caps

caps = asyncio.run(cli_flag_test())
ok &= t("CLI: default session popup ON", ("session", True) in caps)
ok &= t("CLI: --no-popup session OFF", ("session", False) in caps)
ok &= t("CLI: default ssh popup ON", ("ssh", True) in caps)
ok &= t("CLI: --no-popup ssh OFF", ("ssh", False) in caps)


# 6. agent discovery + watch
ok &= t("parse_tmux keeps only S: session names",
        buff_popup.parse_tmux("echo abc | base64 -d | bash; echo deadbeef_0\r\n"
                              "S:buff\r\nS:worker\r\nS:buff\r\ndeadbeef_0\r\n")
        == ["buff", "worker"])

orig_it = buff_popup._iter_procs
buff_popup._iter_procs = lambda: iter([
    (1, "python.exe", "python buff.py"),
    (2, "python.exe", "python ttyd_bridge.py wss://x"),
    (3, "ssh.exe", "ssh -o ProxyCommand=w root@7681-x.e2b.app"),
    (4, "python.exe", "python buff_popup.py buff"),
    (5, "chrome.exe", "chrome --type=gpu"),
])
labels = [a["name"] for a in buff_popup.local_agents()]
buff_popup._iter_procs = orig_it
ok &= t("local_agents classifies buff/ttyd/ssh and hides the popup",
        labels == ["buff session", "ttyd bridge", "ssh bridge"])

ok &= t("_remote_agents_fn disabled for non-e2b hosts",
        ttyd_bridge._remote_agents_fn("ws://127.0.0.1:9/ws") is None)
ok &= t("_remote_agents_fn enabled for e2b hosts",
        callable(ttyd_bridge._remote_agents_fn("wss://7681-x.e2b.app/ws")))

sent = []
orig_send = buff_popup._send
buff_popup._send = lambda h, ln: sent.append(ln)
buff_popup.popup_agents("H", {"local": [{"name": "x", "state": "running"}],
                              "remote": []})
buff_popup._send = orig_send
ok &= t("popup_agents emits an 'agents <json>' line",
        bool(sent) and sent[0].startswith("agents ")
        and json.loads(sent[0][7:])["local"][0]["name"] == "x")

# agent_watch: running -> stopped transition
pushes = []
orig_pa = buff_popup.popup_agents
buff_popup.popup_agents = lambda h, p: pushes.append(p)
remote = [{"name": "buff", "state": "running"}]
w = buff_popup.agent_watch("H", remote_fn=lambda: list(remote), interval=0.15,
                           local_fn=lambda: [{"name": "buff session", "pid": 1}])
time.sleep(0.35)
remote.clear()               # the sandbox session went away
time.sleep(0.55)
buff_popup.stop_watch(w)
buff_popup.popup_agents = orig_pa
ok &= t("watch pushed a running local agent",
        any(p["local"] and p["local"][0]["state"] == "running" for p in pushes))
ok &= t("watch reported a vanished agent as stopped",
        any(a["name"] == "buff" and a["state"] == "stopped"
            for p in pushes for a in p["remote"]))
ok &= t("stopped agents drop out on the next tick",
        not any(a["name"] == "buff"
                for a in pushes[-1]["remote"]))


# 7. the PNG persona: codec, resampling, breathing frames
px = bytearray()
for y in range(4):
    for x in range(4):
        px += bytes((x * 60, y * 60, 128, 255 if x < 2 else 0))
enc = buff_popup.png_encode(4, 4, px)
w, h, back = buff_popup.png_decode(enc)
ok &= t("png codec round-trips (size + pixels)",
        (w, h) == (4, 4) and bytes(back) == bytes(px))

src = bytearray()
for y in range(8):
    for x in range(8):
        src += bytes((250, 250, 250, 255))
rw, rh, red = buff_popup.resize(8, 8, src, 4, box=True)
mid = (1 * 4 + 1) * 4
ok &= t("resize(box) shrinks to the requested size",
        (rw, rh) == (4, 4) and len(red) == 4 * 4 * 4)
nw, nh, nred = buff_popup.resize(4, 4, red, 8, box=False)
ok &= t("resize(nearest) enlarges to the requested size",
        (nw, nh) == (8, 8) and len(nred) == 8 * 8 * 4)

ok &= t("dpi_scale returns a sane float",
        isinstance(buff_popup.dpi_scale(), float)
        and 0.75 <= buff_popup.dpi_scale() <= 3.0)

ok &= t("persona asset ships with the module",
        buff_popup.persona_path().exists())
frames, fw, fh = buff_popup.sprite_pngs()
ok &= t("sprite frames built from the PNG asset",
        bool(frames) and len(frames) == len(buff_popup.BREATH)
        and (fw, fh) == (buff_popup.SPRITE_CARD, buff_popup.SPRITE_CARD))
if frames:
    import struct as _s
    dims = [_s.unpack(">II", f[16:24]) for f in frames]
    ok &= t("every breath frame decodes to the expected square",
            all(d[0] == d[1] for d in dims)
            and dims[0][0] < dims[len(dims) // 2][0])
    aw, ah, argb = buff_popup.png_decode(frames[0])
    ok &= t("sprite is transparent at the corners, opaque in the middle",
            argb[3] == 0 and argb[((ah // 2) * aw + aw // 2) * 4 + 3] > 200)
ok &= t("missing asset degrades gracefully (no sprite, no crash)",
        buff_popup.sprite_pngs(path=buff_popup.persona_path().with_suffix(
            ".nope"))[0] is None)


# 8. the faces: state -> sprite, and the mood rules
ok &= t("every face has a baked PNG asset",
        all(buff_popup.face_path(f).exists()
            for f in set(buff_popup.FACES.values())))
ok &= t("an unknown face falls back to the default persona",
        buff_popup.face_path("no-such-face") == buff_popup.persona_path())

TABLE = (
    (("connecting", False, False), "focused"),
    (("working", False, False), "curious"),      # the shipped sprite
    (("working", True, False), "beaming"),
    (("working", False, True), "sleepy"),
    (("working", True, True), "beaming"),        # busy beats dozing
    (("reconnecting", True, True), "uneasy"),    # a drop beats everything
    (("error", True, True), "surprised"),
    (("ssh", False, False), "confident"),
    (("ssh", True, False), "beaming"),
    (("connecting", True, False), "focused"),    # no busy face while dialling
    (("mystery status", False, False), "curious"),
)
wrong = [(c[0], buff_popup.face_for(*c[0]), c[1]) for c in TABLE
         if buff_popup.face_for(*c[0]) != c[1]]
for args, got, want in wrong:
    print("      face_for%r -> %s (want %s)" % (args, got, want))
ok &= t("face_for: all %d state combinations" % len(TABLE), not wrong)

ok &= t("is_busy: the session reporting on itself is not busy",
        not buff_popup.is_busy([{"name": "buff session"}], []))
ok &= t("is_busy: an ssh bridge pair is not busy",
        not buff_popup.is_busy([{"name": "ssh bridge"},
                                {"name": "websocat tunnel"}], []))
ok &= t("is_busy: a live session in the sandbox is busy",
        buff_popup.is_busy([], [{"name": "buff", "state": "running"}]))
ok &= t("is_busy: a stopped agent is not work",
        not buff_popup.is_busy([], [{"name": "buff", "state": "stopped"}]))
ok &= t("is_busy: a pile of local helpers is busy",
        buff_popup.is_busy([{"name": "a"}, {"name": "b"}, {"name": "c"}],
                           []))

base_frames, _w, _h = buff_popup.sprite_pngs()
base_px = buff_popup.png_decode(base_frames[0])[2]
for face in sorted(set(buff_popup.FACES.values())):
    ff, fw, fh = buff_popup.sprite_pngs(face=face)
    ok &= t("sprite frames built for the '%s' face" % face,
            bool(ff) and len(ff) == len(buff_popup.BREATH)
            and (fw, fh) == (buff_popup.SPRITE_CARD, buff_popup.SPRITE_CARD))
    if ff and face != "curious":
        ok &= t("face '%s' really is a different sprite" % face,
                buff_popup.png_decode(ff[0])[2] != base_px)

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
