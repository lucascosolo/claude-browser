#!/usr/bin/env python3
"""Turn logo.png into a proper icon set.

    python3 packaging/make-icons.py [source.png]

Source art is usually a picture, not an icon: an opaque background, and the
subject floating in a wide margin. Both are fatal in a panel -- the background
becomes a coloured tile that ignores the user's theme, and the margin shrinks
the actual glyph to a fraction of its box. This script fixes both, then writes
the sizes an icon theme expects.

Three steps:

  1. Key out the background with a flood fill from the border that compares
     each pixel to its *neighbour*, not to the seed. That walks a smooth
     gradient (this logo sits on a soft radial glow) all the way to the
     subject, and stops at the hard outline edge, which a fixed
     distance-from-seed threshold cannot do.
  2. Crop to what survived, and re-square it, so the art fills its box.
  3. Scale down to each icon size.

Uses GdkPixbuf, which is already a dependency of the browser, so there is no
ImageMagick or Pillow requirement.
"""

import sys
from collections import deque
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "icons"

# Icon theme sizes. 16-48 are the ones that actually get used (menu, panel,
# task list); the large ones matter for the alt-tab switcher and about dialogs.
SIZES = [16, 22, 24, 32, 48, 64, 128, 256, 512]

# Max per-channel jump still considered "same region" while flood filling.
# Measured on the source: the background gradient moves 0-3 per pixel, while the
# step into the subject's outline is 31 and then 130. 6 clears the gradient with
# room to spare and blocks the edge.
#
# This alone is not enough. At a shallow tangent the anti-aliased edge spreads
# over enough pixels that its per-step deltas fall under any workable tolerance,
# and one such leak is unrecoverable: the subject's strokes are a single
# connected shape of uniform colour, so the fill enters at one weak point and
# consumes every stroke in the drawing. That is exactly what a tolerance of 14
# did here -- it erased the outline, the meridians and the tab border, leaving
# only the fills. Hence the luminance guard below.
TOLERANCE = 6

# How far a pixel's luminance may stray from the border-sampled range and still
# count as background. Calibrated from the image rather than hardcoded, so this
# survives a different logo. On this source the border is luminance 99-105,
# giving a band of 69-135: the warm glow (91) stays inside it, while the outline
# (57), the cream fill (240) and the tan panels (178) all fall outside.
LUMA_SLACK = 30

MARGIN = 0.04  # breathing room around the art, as a fraction of its longest side


def load_rgba(path):
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    stride = pixbuf.get_rowstride()
    raw = pixbuf.get_pixels()
    # Compact to a tight w*4 stride so index maths below stays simple.
    px = bytearray(w * h * 4)
    for y in range(h):
        src = y * stride
        dst = y * w * 4
        px[dst:dst + w * 4] = raw[src:src + w * 4]
    return px, w, h


def luma(px, i):
    base = i * 4
    return (px[base] * 299 + px[base + 1] * 587 + px[base + 2] * 114) // 1000


def key_out_background(px, w, h):
    """Flood fill inward from every border pixel. Returns an alpha bytearray.

    A pixel joins the background only if it is BOTH a small step from the
    neighbour that reached it (so smooth gradients are followed) AND within the
    luminance band the border establishes (so a leak through an anti-aliased
    edge cannot run away into the subject).
    """
    border = []
    for x in range(w):
        border.append(x)
        border.append((h - 1) * w + x)
    for y in range(h):
        border.append(y * w)
        border.append(y * w + w - 1)

    lumas = [luma(px, i) for i in border]
    lo, hi = min(lumas) - LUMA_SLACK, max(lumas) + LUMA_SLACK
    print("luma band     border %d-%d, accepting %d-%d"
          % (min(lumas), max(lumas), lo, hi))

    opaque = bytearray(b"\xff" * (w * h))
    queue = deque()
    for i in border:
        if opaque[i] and lo <= luma(px, i) <= hi:
            opaque[i] = 0
            queue.append(i)

    while queue:
        i = queue.popleft()
        base = i * 4
        r, g, b = px[base], px[base + 1], px[base + 2]
        y, x = divmod(i, w)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            j = ny * w + nx
            if not opaque[j]:
                continue
            nb = j * 4
            if (abs(px[nb] - r) <= TOLERANCE
                    and abs(px[nb + 1] - g) <= TOLERANCE
                    and abs(px[nb + 2] - b) <= TOLERANCE
                    and lo <= luma(px, j) <= hi):
                opaque[j] = 0
                queue.append(j)
    return opaque


def feather(opaque, w, h):
    """One box-blur pass over the mask, so the cut edge is not a hard staircase."""
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            total = 0
            for dy in (-1, 0, 1):
                yy = y + dy
                if not 0 <= yy < h:
                    continue
                row = yy * w
                for dx in (-1, 0, 1):
                    xx = x + dx
                    if 0 <= xx < w:
                        total += opaque[row + xx]
            out[y * w + x] = total // 9
    return out


def bounding_box(alpha, w, h, threshold=8):
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * w
        for x in range(w):
            if alpha[row + x] > threshold:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        raise SystemExit("nothing survived background removal -- is the source all one colour?")
    return x0, y0, x1, y1


def build_pixbuf(px, alpha, w, h):
    for i in range(w * h):
        px[i * 4 + 3] = alpha[i]
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(px)), GdkPixbuf.Colorspace.RGB, True, 8, w, h, w * 4
    )


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logo.png"
    if not source.exists():
        raise SystemExit("no such file: %s" % source)

    px, w, h = load_rgba(source)
    print("source        %s  %dx%d" % (source.name, w, h))

    alpha = feather(key_out_background(px, w, h), w, h)
    kept = sum(1 for a in alpha if a > 8)
    print("background    keyed out, %.1f%% of pixels kept" % (100.0 * kept / (w * h)))

    x0, y0, x1, y1 = bounding_box(alpha, w, h)
    art_w, art_h = x1 - x0 + 1, y1 - y0 + 1
    print("artwork bbox  %dx%d at (%d,%d)" % (art_w, art_h, x0, y0))

    full = build_pixbuf(px, alpha, w, h)

    # Square canvas around the art, plus a small margin, so every generated size
    # has identical proportions and the art is centred.
    side = int(max(art_w, art_h) * (1 + 2 * MARGIN))
    square = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, side, side)
    square.fill(0x00000000)
    full.copy_area(x0, y0, art_w, art_h, square,
                   (side - art_w) // 2, (side - art_h) // 2)
    print("squared to    %dx%d (art now fills %.0f%% of the box)"
          % (side, side, 100.0 * max(art_w, art_h) / side))

    OUT.mkdir(exist_ok=True)
    for size in SIZES:
        scaled = square.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
        path = OUT / ("claude-browser-%d.png" % size)
        scaled.savev(str(path), "png", [], [])
        print("  wrote %s" % path.relative_to(ROOT))

    # A large flat copy for the window icon and any docs use.
    square.scale_simple(512, 512, GdkPixbuf.InterpType.HYPER).savev(
        str(OUT / "claude-browser.png"), "png", [], [])
    print("  wrote %s" % (OUT / "claude-browser.png").relative_to(ROOT))


if __name__ == "__main__":
    main()
