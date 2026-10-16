#!/usr/bin/env python3
"""Live check: is the popup window *actually* showing the face it should?

Counting changed pixels does not work here - the sprite floats and breathes, so
two frames of the same face already differ by thousands of pixels (measured:
~2500 of ~15000). Instead this builds the reference frames for every face
exactly the way the child builds them, captures the real window, and asks which
face the live pixels are closest to. The answer has to be the expected face.

No dependencies: the screen grab is GDI BitBlt through ctypes.

    python demo_faces.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    user32.SetProcessDPIAware()

user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                         ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

SRCCOPY = 0x00CC0020
CHILD = os.path.join(REPO, "buff_popup.py")

import buff_popup                                            # noqa: E402

GEOM_RE = re.compile(
    r"POPUP scale ([\d.]+) geom (\d+)x(\d+)\+(-?\d+)\+(-?\d+)")


def configure(scale):
    """Pin every measurement to the scale the *child* reported.

    The child owns the window, so its scale is the authority. Measuring it
    again in this process is not the same number: a process whose DPI
    awareness is already claimed can read back 96 dpi on a 125% display, which
    silently builds reference frames that do not line up with the real window
    (and reports a false failure). So the geometry always comes from the
    child's own debug line.
    """
    scale = max(0.5, min(4.0, float(scale)))
    card = int(round(buff_popup.SPRITE_CARD * scale))
    compact = int(round(buff_popup.COMPACT * scale))
    globals().update(SCALE=scale, COMPACT=compact, CARD_SPRITE=card,
                     BOB=int(round(3 * scale)),
                     OFF=(compact - card // 2) // 2, SPRITE=card // 2)


def attach(proc, timeout=30.0):
    """Read the child's geometry line, configure, and return (l,t,r,b).

    Returns None if the child never reports (it crashed, or debug is off).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            return None
        match = GEOM_RE.search(line)
        if not match:
            continue
        scale, w, h, x, y = match.groups()
        configure(scale)
        x, y, w, h = int(x), int(y), int(w), int(h)
        return x, y, x + w, y + h
    return None


configure(buff_popup.dpi_scale())   # a sane default for the offline tooling


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def capture(x0, y0, x1, y1):
    """(w, h, BGRA bytes) for a screen rectangle, top-down rows."""
    w, h = x1 - x0, y1 - y0
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, screen, x0, y0, SRCCOPY)
    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    return w, h, buf.raw


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def find_window(pid):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def subsample2(w, h, rgba):
    """Exactly what Tk's PhotoImage.subsample(2) does: take every 2nd pixel."""
    ow, oh = w // 2, h // 2
    out = bytearray(ow * oh * 4)
    for y in range(oh):
        for x in range(ow):
            s = ((y * 2) * w + x * 2) * 4
            d = (y * ow + x) * 4
            out[d:d + 4] = rgba[s:s + 4]
    return ow, oh, out


def reference_frames():
    """{face: [(rgba, w, h), ...]} built the same way the child builds them.

    Call this only after attach() (or configure()): the frames are sized for
    the scale the child is actually rendering at.
    """
    refs = {}
    for face in sorted(set(buff_popup.FACES.values())):
        frames, _w, _h = buff_popup.sprite_pngs(size=CARD_SPRITE, face=face)
        if not frames:
            continue
        built = []
        for f in frames:
            fw, fh, rgba = buff_popup.png_decode(f)
            ow, oh, small = subsample2(fw, fh, rgba)
            built.append((small, ow, oh))
        refs[face] = built
    return refs


def score(live, lw, lh, ref, rw, rh, ox, oy):
    """Mean colour distance over the reference's opaque pixels.

    Comparing only where the sprite is opaque keeps the transparent window
    (which shows whatever desktop is behind it) out of the answer.
    """
    total, n = 0, 0
    for y in range(0, rh, 2):
        ly = oy + y
        if not (0 <= ly < lh):
            continue
        for x in range(0, rw, 2):
            lx = ox + x
            if not (0 <= lx < lw):
                continue
            ri = (y * rw + x) * 4
            if ref[ri + 3] < 200:
                continue
            li = (ly * lw + lx) * 4
            total += (abs(live[li] - ref[ri]) + abs(live[li + 1] - ref[ri + 1])
                      + abs(live[li + 2] - ref[ri + 2]))
            n += 1
    return (total / float(n)) if n else 1e9


def classify(live, lw, lh, refs):
    """Which face is on screen? Returns (face, best, second, margin, scores)."""
    scores = {}
    for face, frames in refs.items():
        best = 1e9
        for rgba, rw, rh in frames:
            for dy in range(-BOB - 2, BOB + 3):
                s = score(live, lw, lh, rgba, rw, rh, OFF, OFF + dy)
                if s < best:
                    best = s
        scores[face] = best
    order = sorted(scores.items(), key=lambda kv: kv[1])
    top = order[0]
    second = order[1] if len(order) > 1 else ("", 1e9)
    return top[0], top[1], second[0], second[1] - top[1], scores


def classify_settled(grabs, lw, lh, refs, min_margin=5.0):
    """Classify a burst of grabs by majority vote.

    A single grab can land mid face-swap or between two breath frames, which
    reads as a blend. Taking the *best-scoring* frame would be wrong - that
    just favours whichever face the animation phase happens to render most
    cleanly. So each grab votes for the face it matches best, frames that are
    too close to call abstain, and the majority wins.
    """
    votes, details = {}, {}
    for live in grabs:
        got, bs, _second, margin, scores = classify(live, lw, lh, refs)
        if got not in details or bs < details[got][0]:
            details[got] = (bs, margin, scores)
        if margin >= min_margin:
            votes[got] = votes.get(got, 0) + 1
    if not votes:
        if not details:
            return None, 1e9, "", 0.0, {}
        got = min(details.items(), key=lambda kv: kv[1][0])[0]
        bs, margin, scores = details[got]
        return got, bs, "", margin, scores
    winner = max(votes.items(), key=lambda kv: kv[1])[0]
    bs, margin, scores = details[winner]
    return winner, bs, "", margin, scores


def sanity(refs):
    """The classifier must identify its own reference frames."""
    # sanity: the classifier must identify its own reference frames
    print("\n-- offline sanity: classify each face's own frames")
    ok_offline = True
    for face, frames in sorted(refs.items()):
        rgba, rw, rh = frames[3]
        # synthesise a "live" grab of this face inside a compact window
        live = bytearray(b"\x20\x20\x20\x00" * (COMPACT * COMPACT))
        for y in range(rh):
            for x in range(rw):
                s = (y * rw + x) * 4
                if rgba[s + 3] < 200:
                    continue
                d = ((OFF + y) * COMPACT + OFF + x) * 4
                live[d:d + 4] = rgba[s:s + 4]
        got, bs, bs2, margin, _ = classify(bytes(live), COMPACT, COMPACT, refs)
        good = got == face
        ok_offline &= good
        print("  %-3s %-10s -> %-10s (best %.1f, next %s %.1f, margin %.1f)"
              % ("OK" if good else "!!", face, got, bs, bs2, bs + margin,
                 margin))
    print("offline classifier:", "SOUND" if ok_offline else "BROKEN")
    return ok_offline


def main():
    env = dict(os.environ, BUFF_POPUP_DEBUG="1")
    proc = subprocess.Popen([sys.executable, CHILD, "buff faces"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=env)
    try:
        reported = attach(proc)
        if not reported:
            print("NO GEOMETRY: the child never reported one")
            print(proc.stderr.read()[-2000:])
            return 1
        refs = reference_frames()
        print("reference frames: %d faces x %d frames, sprite %dx%d in a "
              "%dx%d window (scale %.2f, read from the child)"
              % (len(refs), len(next(iter(refs.values()))), SPRITE, SPRITE,
                 COMPACT, COMPACT, SCALE))
        ok_offline = sanity(refs)

        hwnd = None
        for _ in range(80):
            hwnd = find_window(proc.pid)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            print("NO WINDOW: the child never came up")
            print(proc.stderr.read()[-2000:])
            return 1
        time.sleep(1.0)
        rect = window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        print("\nwindow rect %s  %dx%d" % (rect, w, h))
        if (w, h) != (COMPACT, COMPACT):
            print("UNEXPECTED WINDOW SIZE (expected %dx%d)" % (COMPACT, COMPACT))
            return 1
        if rect != reported:
            print("WINDOW IS NOT WHERE THE CHILD SAID IT IS (child %s)"
                  % (reported,))
            return 1

        def send(line):
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

        def grabs(count=5, gap=0.28):
            """A short burst of captures over ~1.4s."""
            out = []
            time.sleep(0.7)      # let the face swap and settle
            for i in range(count):
                out.append(capture(rect[0], rect[1], rect[2], rect[3])[2])
                if i < count - 1:
                    time.sleep(gap)
            return out

        time.sleep(3.0)          # let the background face warmer finish

        # The agents panel is refreshed by a watcher in the real app, so a
        # step that is not busy has to clear it - otherwise the busy flag from
        # an earlier step sticks and legitimately overrides the state.
        CLEAR = '{"local": [], "remote": []}'
        steps = [
            ("connecting", "connecting", "focused", CLEAR),
            ("working", "working", "curious", CLEAR),
            ("busy", "working", "beaming",
             '{"local": [{"name": "buff session", "state": "running"}],'
             ' "remote": [{"name": "buff", "state": "running"}]}'),
            ("ssh", "ssh", "confident", CLEAR),
            ("reconnecting", "reconnecting", "uneasy", CLEAR),
            ("error", "error", "surprised", CLEAR),
            ("back to working", "working", "curious", CLEAR),
        ]

        print("\n%-3s %-16s %-12s %-12s %8s  %s"
              % ("", "state", "expected", "detected", "margin", "scores"))
        bad = []
        for label, msg, want, agents in steps:
            if agents:
                send("agents " + agents)
                time.sleep(0.3)
            send(msg)
            got, bs, second, margin, scores = classify_settled(
                grabs(), w, h, refs)
            good = got == want and margin > 5.0
            if not good:
                bad.append((label, want, got, round(margin, 1)))
            detail = " ".join("%s=%.0f" % (k[:3], v)
                              for k, v in sorted(scores.items(),
                                                 key=lambda kv: kv[1]))
            print("%-3s %-16s %-12s %-12s %8.1f  %s"
                  % ("OK" if good else "!!", label, want, got, margin, detail))

        print()
        if bad:
            print("MISCLASSIFIED:", bad)
            return 1
        print("all %d states showed the right face (margin > 5.0)" % len(steps))
        return 0 if ok_offline else 1
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())