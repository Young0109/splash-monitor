#!/usr/bin/env python3
"""Generate AppIcon.icns for Splash Monitor — standard library only.

Draws the app icon (light rounded square, white card, blue trend line,
teal quota bar) at 1024x1024 with pure Python, then downscales with the
system `sips` and packages with `iconutil` (both ship with macOS).

Usage:  python3 tools/make_icon.py [--out assets/AppIcon.icns]
                                    [--save-master assets/icon_master.png]
"""
import argparse
import os
import struct
import subprocess
import tempfile
import zlib
import zlib as _z

W = H = 1024
BG = (238, 240, 244)
CARD = (255, 255, 255)
BLUE = (10, 132, 255)
TEAL = (51, 168, 196)
EDGE = (220, 222, 228)
LINE_PTS = [(208, 520), (300, 470), (360, 500), (450, 380), (520, 430),
            (600, 330), (690, 400), (760, 360), (816, 380)]


def png_bytes(w, h, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return (dx * dx + dy * dy) ** 0.5


def rrect(px, py, x0, y0, x1, y1, r):
    if not (x0 <= px <= x1 and y0 <= py <= y1):
        return False
    qx = max(x0 + r - px, px - (x1 - r), 0)
    qy = max(y0 + r - py, py - (y1 - r), 0)
    return qx * qx + qy * qy <= r * r


def render_master():
    px = bytearray(W * H * 4)
    for y in range(H):
        row = y * W * 4
        for x in range(W):
            i = row + x * 4
            col = None
            if rrect(x, y, 40, 40, 984, 984, 230):
                col = BG
                if rrect(x, y, 160, 240, 864, 784, 64):
                    col = CARD
                    for a in range(len(LINE_PTS) - 1):
                        ax, ay = LINE_PTS[a]
                        bx, by = LINE_PTS[a + 1]
                        if seg_dist(x, y, ax, ay, bx, by) <= 26:
                            col = BLUE
                            break
                    if col == CARD and rrect(x, y, 240, 660, 620, 712, 26):
                        col = TEAL
                    if col == CARD and 744 <= y <= 748 and 240 <= x <= 784:
                        col = EDGE
            if col:
                px[i:i + 4] = bytes(col) + b"\xff"
    return png_bytes(W, H, px)


ICONSET = {
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(here, "assets", "AppIcon.icns"))
    ap.add_argument("--save-master", default=None, help="also save the 1024px master PNG")
    a = ap.parse_args()

    png = render_master()
    if a.save_master:
        os.makedirs(os.path.dirname(a.save_master) or ".", exist_ok=True)
        open(a.save_master, "wb").write(png)
        print("master:", a.save_master)

    with tempfile.TemporaryDirectory() as td:
        iconset = os.path.join(td, "AppIcon.iconset")
        os.makedirs(iconset)
        master = os.path.join(td, "master.png")
        open(master, "wb").write(png)
        for name, s in ICONSET.items():
            subprocess.run(["sips", "-z", str(s), str(s), master,
                            "--out", os.path.join(iconset, name)],
                           check=True, capture_output=True)
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", a.out], check=True)
    print("icns:", a.out, os.path.getsize(a.out), "bytes")


if __name__ == "__main__":
    main()
