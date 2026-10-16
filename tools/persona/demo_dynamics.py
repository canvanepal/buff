#!/usr/bin/env python3
"""Live check for the two behaviours the unit tests cannot reach.

The unit tests prove `face_for()` decides correctly; they cannot prove the
*child* acts on it over time. This drives the real child and watches the real
window:

  A. idle    - left alone for longer than IDLE_SEC, it dozes off on its own
  B. linger  - after a hard error the session's stdin closes, and the window
               must stay up briefly (so the startled face is actually seen)
               and then close itself, without the CLI waiting around

IDLE_SEC is shortened by monkey-patching the module in the child's own
interpreter (`python -c "import buff_popup; buff_popup.IDLE_SEC=6; ..."`), so
the code path under test is the real one - nothing is stubbed.

    python demo_dynamics.py
"""
import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import demo_faces as faces                                  # noqa: E402
from demo_faces import (attach, capture, classify_settled,   # noqa: E402
                        find_window, window_rect)

import buff_popup                                           # noqa: E402

CHILD = os.path.join(REPO, "buff_popup.py")
IDLE_TEST = 6.0


def spawn(src=None, title="buff dynamics"):
    """Start the real child, optionally with a patched module constant."""
    if src is None:
        argv = [sys.executable, CHILD, title]
    else:
        argv = [sys.executable, "-c", src]
    env = dict(os.environ, PYTHONPATH=REPO, BUFF_POPUP_DEBUG="1")
    return subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            env=env)


def wait_window(proc, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find_window(proc.pid)
        if hwnd:
            return hwnd
        time.sleep(0.2)
    return None


def burst(rect, count=5, gap=0.28):
    out = []
    for i in range(count):
        out.append(capture(rect[0], rect[1], rect[2], rect[3])[2])
        if i < count - 1:
            time.sleep(gap)
    return out


def main():
    results = []

    # ---- A. idle -> sleepy ------------------------------------------------
    print("=== A. idle: left alone past IDLE_SEC (patched to %.0fs) ==="
          % IDLE_TEST)
    src = ("import buff_popup as b; b.IDLE_SEC = %r; b._child('idle test')"
           % IDLE_TEST)
    proc = spawn(src)
    try:
        reported = attach(proc)
        if not reported:
            print("NO GEOMETRY"); print(proc.stderr.read()[-1500:]); return 1
        refs = faces.reference_frames()
        hwnd = wait_window(proc)
        if not hwnd:
            print("NO WINDOW"); print(proc.stderr.read()[-1500:]); return 1
        rect = window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if (w, h) != (faces.COMPACT, faces.COMPACT):
            print("UNEXPECTED WINDOW SIZE %dx%d" % (w, h)); return 1
        proc.stdin.write("working\n")
        proc.stdin.flush()
        time.sleep(1.0)
        got, bs, second, margin, _ = classify_settled(burst(rect), w, h, refs)
        print("  awake (t+1s)   expected curious  -> %-10s margin %.1f"
              % (got, margin))
        awake_ok = got == "curious" and margin > 5.0
        results.append(("awake before the idle timeout", awake_ok))

        # no further messages: the child must doze off on its own
        print("  waiting %.0fs with no input..." % (IDLE_TEST + 3.0))
        time.sleep(IDLE_TEST + 3.0)
        got, bs, second, margin, _ = classify_settled(burst(rect), w, h, refs)
        print("  idle (t+%ds)    expected sleepy   -> %-10s margin %.1f"
              % (int(IDLE_TEST + 4), got, margin))
        results.append(("dozes off after IDLE_SEC", got == "sleepy"
                        and margin > 5.0))

        # "activity" means something the popup can see *change*. Re-sending the
        # same state is not a change, so it must stay asleep (otherwise a
        # parent that repeats itself would keep it permanently awake).
        proc.stdin.write("working\n")
        proc.stdin.flush()
        got, bs, second, margin, _ = classify_settled(burst(rect), w, h, refs)
        print("  same state again -> %-10s margin %.1f (should stay asleep)"
              % (got, margin))
        results.append(("a repeated identical state is not activity",
                        got == "sleepy"))

        # ...but a real state change does wake it
        proc.stdin.write("connecting\n")
        proc.stdin.flush()
        got, bs, second, margin, _ = classify_settled(burst(rect), w, h, refs)
        print("  state change   expected focused  -> %-10s margin %.1f"
              % (got, margin))
        results.append(("a state change wakes it", got == "focused"))
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # ---- B. error linger --------------------------------------------------
    print("\n=== B. linger: 'error' then EOF (the session died) ===")
    proc = spawn()
    try:
        reported = attach(proc)
        if not reported:
            print("NO GEOMETRY"); print(proc.stderr.read()[-1500:]); return 1
        hwnd = wait_window(proc)
        if not hwnd:
            print("NO WINDOW"); print(proc.stderr.read()[-1500:]); return 1
        rect = window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        # same scale as section A, so A's reference frames still apply
        if (w, h) != (faces.COMPACT, faces.COMPACT):
            print("UNEXPECTED WINDOW SIZE %dx%d (A used %dx%d)"
                  % (w, h, faces.COMPACT, faces.COMPACT))
            return 1
        proc.stdin.write("error\n")
        proc.stdin.flush()
        time.sleep(1.0)
        got, bs, second, margin, _ = classify_settled(burst(rect), w, h, refs)
        print("  on error       expected surprised -> %-10s margin %.1f"
              % (got, margin))
        results.append(("shows the startled face", got == "surprised"
                        and margin > 5.0))

        # the CLI goes away here (stop_popup(linger=True) just closes stdin)
        proc.stdin.close()
        t0 = time.monotonic()
        time.sleep(0.6)
        alive_early = find_window(proc.pid) is not None
        print("  +0.6s after EOF: window up   = %s (linger is %.1fs)"
              % (alive_early, buff_popup.ERROR_LINGER))
        results.append(("window held up after the session died", alive_early))

        deadline = t0 + buff_popup.ERROR_LINGER + 4.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        gone_at = time.monotonic() - t0
        gone = find_window(proc.pid) is None and proc.poll() is not None
        print("  +%.1fs: window gone = %s, child rc=%s"
              % (gone_at, gone, proc.poll()))
        results.append(("window closes itself afterwards", gone))
        results.append(("child exits 0 on its own", proc.poll() == 0))
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # ---- verdict ----------------------------------------------------------
    print()
    bad = [n for n, ok in results if not ok]
    for name, ok in results:
        print("%-3s %s" % ("OK" if ok else "!!", name))
    print()
    print("DYNAMICS: %s" % ("ALL PASS" if not bad else "FAILED " + str(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())