#!/usr/bin/env python3
"""Bake the popup's state faces from Open Mascot Studio.

For each expression we render the sprite with headless Chrome (transparent,
no stage fill, no ground shadow), then composite it over the *shipped*
`assets/persona.png`.

That composite is the trick: the glow behind the sprite is a blurred copy of
the blob silhouette, and only the expression changes between faces - the body
is identical - so reusing persona.png as the backdrop gives every new face a
pixel-identical halo with no guessing at blur radii or colours.

    python bake_faces.py                 # bake every face below
    python bake_faces.py --verify-only   # re-bake `curious`, diff vs persona.png

Requires Chrome. Setup (once), then run this script from this directory:

    curl -sL -o oms.tar.gz \
      https://codeload.github.com/0oooh/open-mascot-studio/tar.gz/refs/heads/main
    tar xzf oms.tar.gz          # -> open-mascot-studio-main

The vendored source is deliberately not committed (the assets it produced are,
with attribution in assets/NOTICE.md).
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
from buff_popup import png_decode, png_encode, resize  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 8799
PX = 1120                       # render the 560 viewBox at 2x, then downsample

# expression -> output asset name. `curious` is the shipped persona itself and
# is only ever regenerated for the --verify-only comparison.
FACES = {
    "focused": "persona-focused.png",
    "beaming": "persona-beaming.png",
    "uneasy": "persona-uneasy.png",
    "surprised": "persona-surprised.png",
    "sleepy": "persona-sleepy.png",
    "confident": "persona-confident.png",
}

SOLID = 250                     # alpha at/above this counts as solid sprite


def crop(w, h, rgba, x0, y0, x1, y1):
    out = bytearray((x1 - x0) * (y1 - y0) * 4)
    for y in range(y0, y1):
        src = (y * w + x0) * 4
        dst = ((y - y0) * (x1 - x0)) * 4
        out[dst:dst + (x1 - x0) * 4] = rgba[src:src + (x1 - x0) * 4]
    return (x1 - x0), (y1 - y0), out


def bbox(w, h, rgba, thresh=SOLID):
    xs, ys = [], []
    for y in range(h):
        row = y * w
        for x in range(w):
            if rgba[(row + x) * 4 + 3] >= thresh:
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ValueError("nothing opaque in the render")
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def composite_over(base, bw, bh, sprite, sw, sh, at, ox, oy):
    """Source-over `sprite` onto a copy of `base`, sprite top-left at (ox, oy)."""
    out = bytearray(base)
    for y in range(sh):
        by = oy + y
        if by < 0 or by >= bh:
            continue
        for x in range(sw):
            bx = ox + x
            if bx < 0 or bx >= bw:
                continue
            si = (y * sw + x) * 4
            sa = sprite[si + 3]
            if not sa:
                continue
            di = (by * bw + bx) * 4
            if sa == 255 and base[di + 3] == 255:
                out[di:di + 4] = sprite[si:si + 4]
                continue
            asa, aba = sa / 255.0, base[di + 3] / 255.0
            aoa = asa + aba * (1 - asa)
            if aoa <= 0:
                out[di:di + 4] = b"\0\0\0\0"
                continue
            for c in range(3):
                out[di + c] = int(round(
                    (sprite[si + c] * asa + base[di + c] * aba * (1 - asa)) / aoa))
            out[di + 3] = int(round(aoa * 255))
    return out


def shot(expr, out_png):
    url = ("http://127.0.0.1:%d/bake.html?expr=%s&px=%d"
           % (PORT, expr, PX))
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-sandbox", "--force-device-scale-factor=1",
           "--window-size=%d,%d" % (PX, PX),
           "--default-background-color=00000000",
           "--virtual-time-budget=6000",
           "--screenshot=" + out_png, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.exists(out_png) or os.path.getsize(out_png) == 0:
        raise RuntimeError("chrome produced no screenshot for %s\n%s\n%s"
                           % (expr, r.stdout[-2000:], r.stderr[-2000:]))


def bake(expr, base, bw, bh, tar, out_path):
    raw = os.path.join(HERE, "raw-%s.png" % expr)
    shot(expr, raw)
    with open(raw, "rb") as fh:
        rw, rh, rgba = png_decode(fh.read())
    x0, y0, x1, y1 = bbox(rw, rh, rgba)
    cw, ch, sprite = crop(rw, rh, rgba, x0, y0, x1, y1)
    tw, th, ox, oy = tar
    # Fit to the persona's width and keep the pose's own proportions, centred
    # vertically: some expressions squash or stretch the blob, and forcing
    # those into a fixed box would flatten the expression itself.
    sw = tw
    sh = max(8, int(round(ch * tw / float(cw))))
    _w, _h, small = resize(cw, ch, sprite, sw, sh, box=True)
    at = oy + (th - sh) // 2
    out = composite_over(base, bw, bh, small, sw, sh, tar, ox, at)
    with open(out_path, "wb") as fh:
        fh.write(png_encode(bw, bh, out))
    return dict(expr=expr, crop=(cw, ch), placed=(sw, sh, ox, at),
                squash=abs(sh - th) / float(th),
                size=os.path.getsize(out_path))


BODY = (18, 22, 29)            # #12161d, the blob colour


def detail_bbox(w, h, rgba):
    """Bbox of the eyes: solid pixels that are not the blob colour."""
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 4
            if rgba[i + 3] < SOLID:
                continue
            if max(abs(rgba[i] - BODY[0]), abs(rgba[i + 1] - BODY[1]),
                   abs(rgba[i + 2] - BODY[2])) > 24:
                xs.append(x)
                ys.append(y)
    return None if not xs else (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def compare(a_path, b_path):
    """Compare a re-bake against the shipped persona.

    A re-bake is re-centred on the persona's bbox, so it can sit a pixel off
    and a raw pixel diff would be dominated by that outline shift - it would
    say nothing about whether the *mascot* is the same. What does matter is
    that the interior matches and the eyes land in the same place.
    """
    with open(a_path, "rb") as fh:
        aw, ah, a = png_decode(fh.read())
    with open(b_path, "rb") as fh:
        bw, bh, b = png_decode(fh.read())
    if (aw, ah) != (bw, bh):
        return dict(dims=(aw, ah, bw, bh), ok=False)
    n = total = 0
    for i in range(0, len(a), 4):
        if a[i + 3] >= SOLID and b[i + 3] >= SOLID:
            total += max(abs(a[i] - b[i]), abs(a[i + 1] - b[i + 1]),
                         abs(a[i + 2] - b[i + 2]))
            n += 1
    da, db = detail_bbox(aw, ah, a), detail_bbox(bw, bh, b)
    drift = None if not (da and db) else max(abs(da[i] - db[i])
                                             for i in range(4))
    interior = total / float(n or 1)
    return dict(interior=round(interior, 3), eye_a=da, eye_b=db, eye_drift=drift,
                ok=interior < 1.0 and drift is not None and drift <= 2)


def serve():
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(PORT)],
                         cwd=HERE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/bake.html" % PORT,
                                   timeout=1).read(64)
            return p
        except Exception:
            time.sleep(0.25)
    p.kill()
    raise RuntimeError("local http server never came up")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated expressions")
    args = ap.parse_args()

    persona = os.path.join(REPO, "assets", "persona.png")
    with open(persona, "rb") as fh:
        bw, bh, base = png_decode(fh.read())
    x0, y0, x1, y1 = bbox(bw, bh, base)
    tar = (x1 - x0, y1 - y0, x0, y0)
    print("persona    %dx%d  sprite bbox %s = %dx%d" % (bw, bh, (x0, y0, x1, y1),
                                                        tar[0], tar[1]))
    only = set(args.only.split(",")) if args.only else None

    server = serve()
    try:
        # 1. always re-bake `curious` and diff it against the shipped persona.
        #    Same expression + same composite path => the result must be
        #    essentially the shipped file. That is the whole pipeline's proof.
        tmp = os.path.join(HERE, "verify-curious.png")
        info = bake("curious", base, bw, bh, tar, tmp)
        print("curious    %s" % info)
        d = compare(persona, tmp)
        print("           re-bake vs assets/persona.png: %s" % d)
        ok = d.get("ok", False)
        print("           PIPELINE %s" % ("FAITHFUL" if ok else "DIVERGED"))
        if args.verify_only:
            return 0 if ok else 1

        # 2. bake the actual faces
        for expr, name in sorted(FACES.items()):
            if only and expr not in only:
                continue
            out = os.path.join(REPO, "assets", name)
            info = bake(expr, base, bw, bh, tar, out)
            print("%-10s -> assets/%-24s %s" % (expr, name, info))
        return 0 if ok else 1
    finally:
        server.kill()


if __name__ == "__main__":
    sys.exit(main())
