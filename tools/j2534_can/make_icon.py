"""Draws can_pro.ico for CAN PRO: big "CAN", an amber rule, and a smaller "PRO" centred
beneath it, on a navy tile (same family as the RPO PRO icon).

Needs Pillow (pip install pillow). Uses Segoe UI Black, which ships with Windows.

    python tools/j2534_can/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "can_pro.ico"
S = 1024  # draw large, downsample for smooth edges
NAVY, WHITE, AMBER = "#12233a", "#ffffff", "#f5a623"


def font(size):
    for name in ("seguibl.ttf", "ariblk.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 24, S - 24, S - 24), radius=190, fill=NAVY)

    big, small = font(400), font(215)
    b = d.textbbox((0, 0), "CAN", font=big)
    w, h_big = b[2] - b[0], b[3] - b[1]
    p = d.textbbox((0, 0), "PRO", font=small)
    h_small = p[3] - p[1]
    gap, rule = 58, 22
    top = (S - (h_big + gap + rule + gap + h_small)) / 2
    left = (S - w) / 2

    d.text((left - b[0], top - b[1]), "CAN", font=big, fill=WHITE)
    y = top + h_big + gap
    d.rectangle((left, y, left + w, y + rule), fill=AMBER)
    y += rule + gap
    d.text(((S - (p[2] - p[0])) / 2 - p[0], y - p[1]), "PRO", font=small, fill=AMBER)
    return img


if __name__ == "__main__":
    draw().resize((256, 256), Image.LANCZOS).save(
        OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)
