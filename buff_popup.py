#!/usr/bin/env python3
"""Fun "it's working" popup for buff - a floating PNG mascot.

Child process (spawned by start_popup):

    python buff_popup.py "<title>"

The persona is a real PNG sprite (assets/persona-*.png), not drawn in code:

  * compact  - just the sprite, floating and breathing, on a transparent
               window you can drag anywhere on screen
  * hover    - the card expands in place to show the details: status line,
               LOCAL AGENTS (buff/ttyd/ssh/websocat processes on this
               machine) and SANDBOX (tmux sessions inside the sandbox),
               each with a green "running" / grey "stopped" dot
  * leave    - it shrinks back to the bare sprite, keeping its position
  * mood     - the sprite *changes face* with the session: it leans in while
               connecting, beams when the sandbox is working, looks uneasy
               when the connection drops, is startled by a hard error and
               dozes off once nothing has happened for a while. See
               face_for() - that one function is the whole behaviour.

The faces are separate PNGs (see assets/NOTICE.md), so nothing is drawn in
code and a missing face just falls back to the shipped persona.png.

Everything is laid out in design units and multiplied by the display scale,
and the child claims DPI awareness on Windows first, so the PNG is rendered
1:1 instead of being resampled by the OS.

The child reads one line per message from stdin (utf-8):

    connecting | working | reconnecting | error | ssh -> set the status line
                                                         (and with it the face)
    agents {"local": [...], "remote": [...]}     -> replace the agent panels
    <anything else>                              -> raw status text

It closes itself when stdin reaches EOF (parent exited / called stop_popup),
so it can never outlive the session it is reporting on.

Parent side (imported by buff.py / ttyd_bridge.py):

    ph = start_popup("buff")          # best-effort; returns handle or None
    popup_state(ph, "working")        # update the status line
    popup_agents(ph, payload)         # update the agent panels
    watch = agent_watch(ph, remote_fn=...)   # background refresher
    stop_popup(ph)                    # close window + reap child
    stop_popup(ph, linger=True)       # or: leave the error face up for a
                                      # moment after a failed session

All helpers are best-effort: they never raise, and silently no-op when
tkinter/the display is unavailable (headless runs).
"""
import base64
import json
import os
import struct
import subprocess
import sys
import threading
import time
import zlib

# states the child renders with a little extra expression/mood
LINES = {
    "connecting": "connecting to sandbox…",
    "working": "it's working!",
    "reconnecting": "reconnecting…",
    "error": "session failed",
    "ssh": "ssh tunnel up",
}

# how many agent rows fit in each section
MAX_ROWS = 3

# ---- the faces -----------------------------------------------------------
# State -> expression. `curious` is the shipped sprite (assets/persona.png)
# and doubles as the plain "working" face.
FACES = {
    "connecting": "focused",
    "working": "curious",
    "busy": "beaming",
    "reconnecting": "uneasy",
    "error": "surprised",
    "sleepy": "sleepy",
    "ssh": "confident",
}

# expression -> file in assets/, all baked from Open Mascot Studio
FACE_ASSETS = {
    "curious": "persona.png",
    "focused": "persona-focused.png",
    "beaming": "persona-beaming.png",
    "uneasy": "persona-uneasy.png",
    "surprised": "persona-surprised.png",
    "sleepy": "persona-sleepy.png",
    "confident": "persona-confident.png",
}

IDLE_SEC = 60.0        # nothing changing for this long and it dozes off
ERROR_LINGER = 1.6     # hold the error face this long after the session dies
BUSY_LOCAL = 3         # local helpers that count as "busy" (the session
                       # reporting on itself is one, so it must be more)

# layout, in design units (multiplied by the display scale at runtime)
CARD_W, CARD_H = 440, 272
COMPACT = 92
SPRITE_CARD = 160                    # persona size inside the expanded card
CARD_SPRITE_CENTER = (86, 86)
# a breath cycle: rescales the sprite without ever touching the disk
BREATH = (0.955, 0.97, 0.985, 1.0, 1.012, 1.022, 1.012, 1.0, 0.985, 0.97)

ASSET = ("assets", "persona.png")


def dpi_scale():
    """Display scale (1.0 at 96 dpi). Claims DPI awareness on Windows, so
    call it before creating the Tk root.

    Claiming awareness is best-effort and independent of *measuring* the DPI:
    a process that is already DPI aware (a host that set it, a second call)
    raises on the setter, and bailing out there would silently report 1.0 on a
    scaled display and hand back a blurry, upscaled sprite.
    """
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass          # already aware, or unsupported: still measurable
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(0.75, min(3.0, dpi / 96.0)) if dpi else 1.0
    except Exception:
        return 1.0


# --------------------------------------------------------- tiny png toolkit
def png_decode(data):
    """Decode an 8-bit RGB/RGBA PNG into (w, h, bytearray RGBA). No deps."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if ihdr is None:
        raise ValueError("no IHDR")
    w, h, depth, ctype, interlace = ihdr[0], ihdr[1], ihdr[2], ihdr[3], ihdr[6]
    if depth != 8 or ctype not in (2, 6) or interlace != 0:
        raise ValueError("unsupported png (depth=%s type=%s)" % (depth, ctype))
    nch = 3 if ctype == 2 else 4
    stride = w * nch
    raw = zlib.decompress(idat)
    prev = bytearray(stride)
    out = bytearray(w * h * 4)
    p, o = 0, 0
    for _ in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                x = line[i]
                if f == 1:
                    x += a
                elif f == 2:
                    x += b
                elif f == 3:
                    x += (a + b) >> 1
                else:  # Paeth
                    pp = a + b - c
                    pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                    x += a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = x & 0xFF
        if nch == 4:
            out[o:o + stride] = line
        else:
            for x in range(w):
                out[o + x * 4:o + x * 4 + 3] = line[x * 3:x * 3 + 3]
                out[o + x * 4 + 3] = 255
        o += w * 4
        prev = line
    return w, h, out


def _chunk(typ, body):
    return (struct.pack(">I", len(body)) + typ + body
            + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))


def png_encode(w, h, rgba):
    """Encode 8-bit RGBA to PNG bytes. No deps."""
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)  # filter: none
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def resize(src_w, src_h, rgba, out_w, out_h=None, box=True):
    """Resample RGBA. box=True averages (good for shrinking), else nearest."""
    out_h = out_h or out_w
    out = bytearray(out_w * out_h * 4)
    if box:
        for oy in range(out_h):
            y0 = oy * src_h // out_h
            y1 = max(y0 + 1, (oy + 1) * src_h // out_h)
            for ox in range(out_w):
                x0 = ox * src_w // out_w
                x1 = max(x0 + 1, (ox + 1) * src_w // out_w)
                r = g = b = a = n = 0
                for y in range(y0, y1):
                    base = (y * src_w + x0) * 4
                    for x in range(x1 - x0):
                        i = base + x * 4
                        r += rgba[i]
                        g += rgba[i + 1]
                        b += rgba[i + 2]
                        a += rgba[i + 3]
                        n += 1
                o = (oy * out_w + ox) * 4
                out[o] = r // n
                out[o + 1] = g // n
                out[o + 2] = b // n
                out[o + 3] = a // n
    else:
        for oy in range(out_h):
            sy = min(src_h - 1, oy * src_h // out_h)
            for ox in range(out_w):
                sx = min(src_w - 1, ox * src_w // out_w)
                out[(oy * out_w + ox) * 4:(oy * out_w + ox) * 4 + 4] = \
                    rgba[(sy * src_w + sx) * 4:(sy * src_w + sx) * 4 + 4]
    return out_w, out_h, out


def persona_path():
    """Path to the bundled default persona PNG (next to this module)."""
    from pathlib import Path
    return Path(__file__).parent.joinpath(*ASSET)


def face_path(face=None):
    """Path to a face PNG; unknown/missing faces fall back to persona.png."""
    from pathlib import Path
    name = FACE_ASSETS.get(face or "", FACE_ASSETS["curious"])
    return Path(__file__).parent.joinpath("assets", name)


def face_for(state, busy=False, idle=False):
    """The state -> face decision: all of the reactive behaviour, in one
    pure function (so it is the easy part to test and to argue with).

    Explicit states win, because they mean something precise and are
    momentary: connecting leans in, a hard failure is a startle, a drop is a
    worry. Only a plain working session gets refined by what the popup can
    observe for itself - the sandbox busy, or nothing happening for a while.
    """
    key = (state or "").strip().lower()
    if key == "error":
        return FACES["error"]
    if key == "reconnecting":
        return FACES["reconnecting"]
    if key == "connecting":
        return FACES["connecting"]
    if busy:
        return FACES["busy"]
    if idle:
        return FACES["sleepy"]
    if key == "ssh":
        return FACES["ssh"]
    return FACES["working"]


def is_busy(local, remote, threshold=BUSY_LOCAL):
    """Is something actually being worked on?

    Live sessions inside the sandbox are the real signal. On the local side
    the session reporting on itself is always there, so a low count must not
    read as busy - that would mask the connecting and ssh faces - and only a
    pile of concurrent helpers counts.
    """
    def running(items):
        n = 0
        for a in items or []:
            state = a.get("state", "running") if isinstance(a, dict) else \
                "running"
            if state != "stopped":
                n += 1
        return n

    return running(remote) > 0 or running(local) >= threshold


def sprite_pngs(size=SPRITE_CARD, path=None, factors=BREATH, face=None):
    """Build one face's breathing sprite as a list of PNG byte strings.

    Returns (frames, w, h). Returns (None, 0, 0) when the asset is missing or
    unreadable - the popup then renders without a sprite instead of breaking
    the session.
    """
    try:
        data = (path or face_path(face)).read_bytes()
        w, h, rgba = png_decode(data)
        base = resize(w, h, rgba, size)
        frames = []
        for f in factors:
            s = max(8, int(round(size * f)))
            if s == size:
                frames.append(png_encode(size, size, base[2]))
            else:
                sw, sh, sr = resize(size, size, base[2], s, box=False)
                frames.append(png_encode(sw, sh, sr))
        return frames, size, size
    except Exception:
        return None, 0, 0


# ---------------------------------------------------------------- child side
def _child(title):
    import math
    import queue
    import tkinter as tk

    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # BUFF_POPUP_DEBUG=1 echoes the window geometry so it can be verified
    dbg_on = bool(os.environ.get("BUFF_POPUP_DEBUG"))

    def dbg(msg):
        if dbg_on:
            try:
                sys.stderr.write("POPUP %s\n" % msg)
                sys.stderr.flush()
            except Exception:
                pass

    SCALE = dpi_scale()                 # must happen before Tk() exists

    def S(v):
        return int(round(v * SCALE))

    try:
        root = tk.Tk()
    except Exception:
        sys.exit(0)  # no display: vanish, never break the session

    # On Windows a magic canvas colour can be punched out entirely, which is
    # what lets the bare sprite float with no card behind it.
    MAGIC = "#010203"
    see_through = False
    if sys.platform == "win32" and not os.environ.get("BUFF_POPUP_OPAQUE"):
        try:
            root.attributes("-transparentcolor", MAGIC)
            see_through = True
        except Exception:
            see_through = False
    PANEL, INK, DIM, GLOW, GREEN, GREY = ("#0d1117", "#e6edf3", "#8b949e",
                                          "#58a6ff", "#3fb950", "#484f58")

    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.title("buff-persona")
    except Exception:
        pass
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    canvas = tk.Canvas(root, highlightthickness=0, bd=0,
                       bg=MAGIC if see_through else PANEL)
    canvas.pack(fill="both", expand=True)

    CARD_PX = (S(CARD_W), S(CARD_H))
    COMP_PX = S(COMPACT)
    CENTER_CARD = (S(CARD_SPRITE_CENTER[0]), S(CARD_SPRITE_CENTER[1]))
    CENTER_COMP = (COMP_PX // 2, COMP_PX // 2)

    # ---- the faces -------------------------------------------------------
    # One sprite pool per face, built lazily: a face costs a decode and a
    # resample, so the first paint builds the one it needs and a background
    # thread warms the rest - swapping a face must never stall the window.
    built = {face: None for face in set(FACES.values())}
    building = set()
    cur = {"face": None, "photos": [], "smalls": []}

    def build_face(face):
        """Decode + resample one face's breath frames (safe off-thread)."""
        if built.get(face) is None and face not in building:
            building.add(face)
            try:
                frames, _w, _h = sprite_pngs(size=S(SPRITE_CARD), face=face)
            except Exception:
                frames = None
            built[face] = frames          # None is cached too: don't retry
            building.discard(face)
        return built.get(face)

    def use_face(face):
        """Put `face` on screen. True when it is actually showing."""
        if face == cur["face"]:
            return True
        frames = built.get(face)
        if not frames:
            return False                  # not ready: keep the current face
        try:
            photos = [tk.PhotoImage(data=base64.b64encode(f).decode())
                      for f in frames]
            smalls = [p.subsample(2) for p in photos]
        except Exception:
            return False
        cur["face"], cur["photos"], cur["smalls"] = face, photos, smalls
        return True

    st = {"mode": "compact", "status": LINES["connecting"],
          "state": "connecting", "local": [], "remote": []}
    act = [time.monotonic()]      # last time something actually changed
    last_agents = [None]
    pos = [sw - COMP_PX - S(40), sh - COMP_PX - S(120)]
    ui = {}
    dragging = [False, 0, 0, 0, 0]
    hover_job = [None]

    start_face = face_for(st["state"])
    build_face(start_face)            # synchronously, for the very first paint
    use_face(start_face)

    def center_of(mode):
        return CENTER_CARD if mode == "card" else CENTER_COMP

    def rounded(x0, y0, x1, y1, r, fill, outline=None):
        for sx, sy, s in ((x0, y0, 90), (x1 - 2 * r, y0, 0),
                          (x0, y1 - 2 * r, 180), (x1 - 2 * r, y1 - 2 * r, 270)):
            canvas.create_arc(sx, sy, sx + 2 * r, sy + 2 * r, start=s,
                              extent=90, fill=fill, outline=fill)
        canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
        canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill)
        if outline:
            for sx, sy, s in ((x0, y0, 90), (x1 - 2 * r, y0, 0),
                              (x0, y1 - 2 * r, 180), (x1 - 2 * r, y1 - 2 * r, 270)):
                canvas.create_arc(sx, sy, sx + 2 * r, sy + 2 * r, start=s,
                                  extent=90, style="arc", outline=outline,
                                  width=max(1, S(2)))
            for a, b, c, d in ((x0 + r, y0, x1 - r, y0), (x0 + r, y1, x1 - r, y1),
                               (x0, y0 + r, x0, y1 - r), (x1, y0 + r, x1, y1 - r)):
                canvas.create_line(a, b, c, d, fill=outline, width=max(1, S(2)))

    def draw_rows(items, key):
        canvas.itemconfigure(ui[("head", key)], text=str(len(items)))
        for i, (dot, lbl) in enumerate(ui[("rows", key)]):
            if i >= len(items):
                canvas.itemconfigure(dot, state="hidden")
                canvas.itemconfigure(lbl, state="hidden")
                continue
            a = items[i]
            running = a.get("state", "running") != "stopped"
            canvas.itemconfigure(dot, state="normal",
                                 fill=GREEN if running else GREY)
            name = a.get("name") or "?"
            if not running:
                name += "  (stopped)"
            canvas.itemconfigure(lbl, state="normal",
                                 fill=INK if running else DIM,
                                 text=name if len(name) <= 24 else name[:23] + "…")

    def redraw():
        canvas.delete("all")
        ui.clear()
        card = st["mode"] == "card"
        w, h = CARD_PX if card else (COMP_PX, COMP_PX)
        if card:
            rounded(S(1), S(1), w - S(1), h - S(1), S(14), PANEL, GLOW)
            canvas.create_text(S(182), S(56), text=title, fill="#7ee787",
                               anchor="w", font=("Consolas", 15, "bold"))
            ui["status"] = canvas.create_text(S(182), S(86), text=st["status"],
                                              fill=DIM, anchor="w",
                                              font=("Segoe UI", 10))
            canvas.create_text(S(182), S(112), text="drag the sprite to move",
                               fill=GREY, anchor="w", font=("Segoe UI", 8))
            canvas.create_line(S(18), S(172), w - S(18), S(172), fill="#21262d")
            canvas.create_text(S(20), S(192), text="LOCAL AGENTS", fill=DIM,
                               anchor="w", font=("Consolas", 8, "bold"))
            canvas.create_text(S(230), S(192), text="SANDBOX", fill=DIM,
                               anchor="w", font=("Consolas", 8, "bold"))
            for key, x, right in (("local", 20, 200), ("remote", 230, CARD_W - 20)):
                rows = []
                for i in range(MAX_ROWS):
                    y = S(212 + i * 19)
                    rows.append((
                        canvas.create_oval(S(x), y - S(4), S(x) + S(8), y + S(4),
                                           fill=GREY, outline="", state="hidden"),
                        canvas.create_text(S(x + 14), y, text="", fill=INK,
                                           anchor="w", font=("Consolas", 9),
                                           state="hidden"),
                    ))
                ui[("rows", key)] = rows
                ui[("head", key)] = canvas.create_text(
                    S(right), S(192), text="0", fill=DIM, anchor="e",
                    font=("Consolas", 8, "bold"))
            draw_rows(st["local"], "local")
            draw_rows(st["remote"], "remote")
        elif not see_through:
            rounded(0, 0, COMP_PX, COMP_PX, S(16), PANEL, GLOW)

        cx, cy = center_of(st["mode"])
        if cur["photos"]:
            pool = cur["photos"] if card else cur["smalls"]
            ui["sprite"] = canvas.create_image(
                cx, cy, anchor="center", image=pool[last_frame[0] % len(pool)])
        else:  # no asset: a plain marker so the window is still draggable
            ui["sprite"] = canvas.create_text(cx, cy, text="📡",
                                              font=("Segoe UI Emoji",
                                                    26 if card else 18))
        canvas.tag_bind(ui["sprite"], "<ButtonPress-1>", on_press)
        canvas.tag_bind(ui["sprite"], "<B1-Motion>", on_drag)

    def apply_geometry(mode):
        cx, cy = center_of(st["mode"])
        gx, gy = pos[0] + cx, pos[1] + cy          # sprite centre on screen
        w, h = CARD_PX if mode == "card" else (COMP_PX, COMP_PX)
        nx, ny = center_of(mode)
        x = max(0, min(sw - w, gx - nx))
        y = max(0, min(sh - h, gy - ny))
        st["mode"] = mode
        pos[0], pos[1] = x, y
        canvas.configure(width=w, height=h)
        root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        dbg("geom %dx%d+%d+%d %s" % (w, h, x, y, mode))
        redraw()

    def expand():
        hover_job[0] = None
        if st["mode"] != "card":
            apply_geometry("card")

    def collapse():
        hover_job[0] = None
        if st["mode"] != "compact":
            apply_geometry("compact")

    def on_enter(_e):
        act[0] = time.monotonic()      # being poked at counts as activity
        if hover_job[0] is not None:
            try:
                root.after_cancel(hover_job[0])
            except Exception:
                pass
            hover_job[0] = None
        root.after(180, expand)

    def on_leave(_e):
        if dragging[0]:
            return
        try:
            hover_job[0] = root.after(320, collapse)
        except Exception:
            collapse()

    def on_press(e):
        act[0] = time.monotonic()
        dragging[0] = True
        dragging[1], dragging[2] = e.x_root, e.y_root
        dragging[3], dragging[4] = root.winfo_x(), root.winfo_y()

    def on_drag(e):
        if not dragging[0]:
            return
        x = dragging[3] + (e.x_root - dragging[1])
        y = dragging[4] + (e.y_root - dragging[2])
        pos[0], pos[1] = x, y
        root.geometry("+%d+%d" % (x, y))
        dbg("drag +%d+%d" % (x, y))

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", lambda e: dragging.__setitem__(0, False))
    canvas.bind("<Enter>", on_enter)
    canvas.bind("<Leave>", on_leave)

    # ---- state from the parent -----------------------------------------
    q: queue.Queue = queue.Queue()

    def reader():
        for line in sys.stdin:
            q.put(line.rstrip("\n"))
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    t = [0.0]
    last_frame = [0]

    def tick():
        try:
            while True:
                msg = q.get_nowait()
                if msg is None:
                    if st["state"] == "error":
                        # the session just died: don't vanish on the spot, hold
                        # the startled face up long enough to be seen. The
                        # parent is already gone, so the child closes itself.
                        root.after(int(ERROR_LINGER * 1000), root.destroy)
                    else:
                        root.destroy()
                    return
                if msg.startswith("agents "):
                    if msg != last_agents[0]:
                        last_agents[0] = msg
                        act[0] = time.monotonic()
                    try:
                        payload = json.loads(msg[7:])
                        st["local"] = payload.get("local") or []
                        st["remote"] = payload.get("remote") or []
                        if st["mode"] == "card":
                            redraw()
                    except Exception:
                        pass
                else:
                    key = msg.strip().lower()
                    if key != st["state"]:
                        act[0] = time.monotonic()
                    st["state"] = key
                    st["status"] = LINES.get(key, msg)
                    tgt = ui.get("status")
                    if tgt:
                        canvas.itemconfigure(tgt, text=st["status"])
        except queue.Empty:
            pass
        if not canvas.winfo_exists():
            return
        # the mood: what the parent told us, plus what we can see for
        # ourselves (the sandbox busy, or a long stretch of nothing)
        want = face_for(st["state"], busy=is_busy(st["local"], st["remote"]),
                        idle=(time.monotonic() - act[0]) > IDLE_SEC)
        if want != cur["face"]:
            build_face(want)              # no-op once warmed
            if use_face(want):
                dbg("face %s -> %s (state=%s busy=%s idle=%s)"
                    % (cur["face"], want, st["state"],
                       is_busy(st["local"], st["remote"]),
                       (time.monotonic() - act[0]) > IDLE_SEC))
                redraw()                  # the sprite item changes image pool
        t[0] += 0.05
        # breathe: cycle the pre-scaled frames; float: gentle vertical bob
        f = int(t[0] / 0.13) % len(BREATH)
        if f != last_frame[0]:
            last_frame[0] = f
            item = ui.get("sprite")
            pool = cur["photos"] if st["mode"] == "card" else cur["smalls"]
            if item and pool:
                canvas.itemconfigure(item, image=pool[f % len(pool)])
        sprite = ui.get("sprite")
        if sprite:
            cx, cy = center_of(st["mode"])
            canvas.coords(sprite, cx, cy + S(3) * math.sin(t[0] * 1.9))
        root.after(50, tick)

    pos[0] = max(0, min(sw - COMP_PX, pos[0]))
    pos[1] = max(0, min(sh - COMP_PX, pos[1]))
    canvas.configure(width=COMP_PX, height=COMP_PX)
    root.geometry("%dx%d+%d+%d" % (COMP_PX, COMP_PX, pos[0], pos[1]))
    dbg("scale %.2f geom %dx%d+%d+%d %s"
        % (SCALE, COMP_PX, COMP_PX, pos[0], pos[1], st["mode"]))
    redraw()

    def warm():
        """Build the faces not on screen yet, off the main thread, so a mood
        change later costs nothing."""
        for face in sorted(set(FACES.values())):
            if built.get(face) is None:
                build_face(face)

    threading.Thread(target=warm, daemon=True).start()
    root.after(50, tick)
    root.mainloop()


# --------------------------------------------------------- agent discovery
def _iter_procs():
    """Yield (pid, name, commandline) for local processes. Best-effort."""
    if sys.platform == "win32":
        ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
              "CommandLine | ConvertTo-Json -Compress")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        out = (out or "").strip()
        if not out:
            return
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        for p in data:
            yield int(p.get("ProcessId") or 0), str(p.get("Name") or ""), \
                str(p.get("CommandLine") or "")
    else:
        out = subprocess.run(["ps", "-eo", "pid=,comm=,args="],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, comm, args = parts
            try:
                yield int(pid), comm, args
            except ValueError:
                continue


def _classify(name, cmd):
    """Return a friendly label if this process is a buff agent, else None."""
    n, low = (name or "").lower(), (cmd or "").lower()
    if "buff_popup" in low:
        return None  # never list the popup itself
    if n.startswith("python") and ("buff" in low or "ttyd_bridge" in low):
        if "ttyd_bridge" in low:
            return "ttyd bridge"
        return "buff session"
    if n.startswith("ssh") and ("e2b.app" in low or "buff" in low):
        return "ssh bridge"
    if "websocat" in n and "e2b.app" in low:
        return "websocat tunnel"
    return None


def local_agents(limit=MAX_ROWS):
    """Locally running buff-related agents: [{'name','pid','state'}]. Never raises."""
    try:
        found, seen = [], set()
        for pid, name, cmd in _iter_procs():
            label = _classify(name, cmd)
            if not label or label in seen:
                continue
            seen.add(label)
            found.append({"name": label, "pid": pid, "state": "running"})
        return found[:limit]
    except Exception:
        return []


def parse_tmux(text):
    """Parse 'S:<session>' lines from a tmux listing. Never raises."""
    names, seen = [], set()
    try:
        for line in (text or "").splitlines():
            line = line.strip().rstrip("\r")
            if line.startswith("S:"):
                name = line[2:].strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    except Exception:
        pass
    return names


def build_payload(local, remote, seen):
    """Merge current + vanished agents into the popup payload.

    `seen` maps name -> group for agents observed on the previous tick; any
    agent that was running and is now gone is reported once as 'stopped'.
    """
    payload = {"local": [], "remote": []}
    current = {}
    for group, items in (("local", local or []), ("remote", remote or [])):
        for a in items:
            name = a.get("name") if isinstance(a, dict) else str(a)
            if not name:
                continue
            name = str(name)
            current[name] = group
            row = dict(a) if isinstance(a, dict) else {"name": name}
            row["name"], row["state"] = name, "running"
            payload[group].append(row)
    for name, group in sorted((seen or {}).items()):
        if name not in current and group in payload:
            payload[group].append({"name": name, "state": "stopped"})
    return payload


# --------------------------------------------------------------- parent side
_WRITE_LOCK = threading.Lock()


def _spawn(title):
    from pathlib import Path
    here = Path(__file__).with_name("buff_popup.py")
    if not here.exists():
        return None
    kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                  errors="replace")
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen([sys.executable, str(here), title], **kwargs)
    except Exception:
        return None


def start_popup(title="buff"):
    """Open the mascot popup. Returns a handle, or None if unavailable."""
    try:
        return _spawn(title)
    except Exception:
        return None


def _send(handle, line):
    try:
        if handle is not None and handle.stdin is not None:
            with _WRITE_LOCK:
                handle.stdin.write(line + "\n")
                handle.stdin.flush()
    except Exception:
        pass


def popup_state(handle, state):
    """Push a state line ('connecting'/'working'/'reconnecting'/… or text)."""
    _send(handle, str(state))


def popup_agents(handle, payload):
    """Push an agent payload: {'local': [...], 'remote': [...]}."""
    try:
        _send(handle, "agents " + json.dumps(payload))
    except Exception:
        pass


def agent_watch(handle, remote_fn=None, interval=4.0, local_fn=None,
                max_interval=60.0):
    """Refresh the agent panels in the background. Returns a stop handle.

    remote_fn is an optional *sync* callable returning a list of
    remote agents (or None); it is called from the watcher thread.

    A remote_fn that keeps failing (dead or unreachable sandbox) backs off
    from `interval` to `max_interval`: retrying a websocket session every 4s
    forever is what makes a bad connection feel like it is flapping, and each
    failed attempt is a wasted round trip.
    """
    stop = threading.Event()
    seen = {}
    wait = interval

    def loop():
        nonlocal wait
        while True:
            ok = True
            try:
                remote = remote_fn() if remote_fn is not None else None
                if remote_fn is not None and remote is None:
                    ok = False
                payload = build_payload((local_fn or local_agents)(), remote,
                                        seen)
                popup_agents(handle, payload)
                seen.clear()
                for group in ("local", "remote"):
                    for a in payload[group]:
                        if a.get("state") == "running":
                            seen[a["name"]] = group
            except Exception:
                ok = False
            wait = interval if ok else min(wait * 2, max_interval)
            if stop.wait(wait):
                return

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return {"stop": stop, "thread": th}


def stop_watch(watch):
    """Stop an agent_watch refresher. Never raises."""
    try:
        if watch is not None:
            watch["stop"].set()
    except Exception:
        pass


def stop_popup(handle, linger=False):
    """Close the window (EOF) and reap the child. Never raises.

    linger=True closes stdin and returns without waiting: the child holds the
    error face on screen for ERROR_LINGER and then closes itself, so a failed
    session is actually seen without the CLI hanging around to watch it.
    """
    try:
        if handle is None:
            return
        if handle.stdin is not None:
            try:
                handle.stdin.close()
            except Exception:
                pass
        if linger:
            return
        try:
            handle.wait(timeout=2)
        except Exception:
            handle.kill()
    except Exception:
        pass


if __name__ == "__main__":
    _child(sys.argv[1] if len(sys.argv) > 1 else "buff")
