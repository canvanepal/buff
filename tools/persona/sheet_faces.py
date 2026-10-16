#!/usr/bin/env python3
"""Build assets/persona-faces.png: every state face, labelled, on one sheet.

The sheet is generated from the shipped assets (not from the mascot source), so
it can never drift from what the popup actually shows. Reuses the popup's own
PNG codec and resampler - no extra dependencies.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
from buff_popup import (FACES, FACE_ASSETS, BREATH, png_decode, png_encode,  # noqa: E402
                        resize)

# 5x7 glyphs for the labels. A font dependency would be silly for six words.
GLYPHS = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "11110", "10001", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "11110", "10000", "10000", "10000", "11111"),
    "F": ("11111", "10000", "11110", "10000", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    ">": ("10000", "01000", "00100", "00010", "00100", "01000", "10000"),
}

# state -> face, expanded for the sheet (the rows a user actually sees)
STATES = [
    ("connecting", "connecting"),
    ("working", "working"),
    ("busy", "working + sandbox busy"),
    ("ssh", "ssh"),
    ("reconnecting", "reconnecting"),
    ("error", "error"),
    ("sleepy", "idle > 60s"),
]

BG = (244, 244, 242)
INK = (60, 66, 74)
DIM = (120, 128, 138)
CARD = (255, 255, 255)

SPRITE = 150            # sprite drawn at this size on the sheet
CELL_W, CELL_H = 210, 250
PAD = 16
GLYPH_SCALE = 3


def draw_text(rgba, w, h, x, y, text, scale, rgb):
    """Blit 5x7 glyphs. Returns the x after the last glyph."""
    for ch in text.upper():
        rows = GLYPHS.get(ch)
        if rows is None:
            x += 6 * scale
            continue
        for gy, row in enumerate(rows):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                for sy in range(scale):
                    py = y + gy * scale + sy
                    if not (0 <= py < h):
                        continue
                    for sx in range(scale):
                        px = x + gx * scale + sx
                        if not (0 <= px < w):
                            continue
                        i = (py * w + px) * 4
                        rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3] = (
                            rgb[0], rgb[1], rgb[2], 255)
        x += 6 * scale
    return x


def fill(rgba, w, h, x0, y0, x1, y1, rgb, alpha=255):
    for y in range(max(0, y0), min(h, y1)):
        base = y * w
        for x in range(max(0, x0), min(w, x1)):
            i = (base + x) * 4
            if alpha == 255:
                rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3] = (
                    rgb[0], rgb[1], rgb[2], 255)
            else:
                a = alpha / 255.0
                rgba[i] = int(rgba[i] * (1 - a) + rgb[0] * a)
                rgba[i + 1] = int(rgba[i + 1] * (1 - a) + rgb[1] * a)
                rgba[i + 2] = int(rgba[i + 2] * (1 - a) + rgb[2] * a)
                rgba[i + 3] = 255


def rounded(rgba, w, h, x0, y0, x1, y1, r, rgb, alpha=255):
    fill(rgba, w, h, x0 + r, y0, x1 - r, y1, rgb, alpha)
    fill(rgba, w, h, x0, y0 + r, x1, y1 - r, rgb, alpha)
    for cx, cy, a0 in ((x0, y0, 180), (x1 - 2 * r, y0, 270),
                       (x0, y1 - 2 * r, 90), (x1 - 2 * r, y1 - 2 * r, 0)):
        for oy in range(2 * r):
            for ox in range(2 * r):
                dx, dy = ox - r + 0.5, oy - r + 0.5
                if dx * dx + dy * dy > r * r:
                    continue
                px, py = cx + ox, cy + oy
                if not (0 <= px < w and 0 <= py < h):
                    continue
                i = (py * w + px) * 4
                a = alpha / 255.0
                rgba[i] = int(rgba[i] * (1 - a) + rgb[0] * a)
                rgba[i + 1] = int(rgba[i + 1] * (1 - a) + rgb[1] * a)
                rgba[i + 2] = int(rgba[i + 2] * (1 - a) + rgb[2] * a)
                rgba[i + 3] = 255


def blit(rgba, w, h, sprite, sw, sh, ox, oy):
    for y in range(sh):
        py = oy + y
        if not (0 <= py < h):
            continue
        for x in range(sw):
            px = ox + x
            if not (0 <= px < w):
                continue
            si = (y * sw + x) * 4
            a = sprite[si + 3] / 255.0
            if a <= 0:
                continue
            di = (py * w + px) * 4
            for c in range(3):
                rgba[di + c] = int(rgba[di + c] * (1 - a) + sprite[si + c] * a)


def main():
    cols = 4
    rows = (len(STATES) + cols - 1) // cols
    w = cols * CELL_W + PAD * 2
    h = rows * CELL_H + PAD * 2
    canvas = bytearray()
    for _ in range(w * h):
        canvas += bytes((BG[0], BG[1], BG[2], 255))

    for n, (state, caption) in enumerate(STATES):
        face = FACES[state]
        path = os.path.join(REPO, "assets", FACE_ASSETS[face])
        sw, sh, src = png_decode(open(path, "rb").read())
        tw = SPRITE
        th = max(8, int(round(sh * SPRITE / float(sw))))
        _a, _b, small = resize(sw, sh, src, tw, th, box=True)

        col, row = n % cols, n // cols
        x0 = PAD + col * CELL_W
        y0 = PAD + row * CELL_H
        rounded(canvas, w, h, x0 + 4, y0 + 4, x0 + CELL_W - 4, y0 + CELL_H - 4,
                10, CARD)
        blit(canvas, w, h, small, tw, th,
             x0 + (CELL_W - tw) // 2, y0 + 26 + (SPRITE - th) // 2)
        label = caption.upper()
        tw_lbl = len(label) * 6 * GLYPH_SCALE
        draw_text(canvas, w, h, x0 + (CELL_W - tw_lbl) // 2,
                  y0 + CELL_H - 46, label, GLYPH_SCALE, INK)
        sub = "FACE: " + face.upper()
        tw_sub = len(sub) * 6 * 2
        draw_text(canvas, w, h, x0 + (CELL_W - tw_sub) // 2,
                  y0 + CELL_H - 24, sub, 2, DIM)

    out = os.path.join(REPO, "assets", "persona-faces.png")
    with open(out, "wb") as fh:
        fh.write(png_encode(w, h, canvas))
    print("wrote %s  %dx%d  %d bytes  (%d faces)"
          % (out, w, h, os.path.getsize(out), len(STATES)))
    print("breath frames per face:", len(BREATH))


if __name__ == "__main__":
    main()