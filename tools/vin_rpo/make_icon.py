"""Draws vin_rpo.ico for RPO PRO: big "RPO", an amber rule, and a smaller letter-spaced "PRO"
spanning the same width, on a navy tile.

Needs Pillow (pip install pillow). Uses Segoe UI Black, which ships with Windows.

    python tools/vin_rpo/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "vin_rpo.ico"
S = 1024  # draw large, downsample for smooth edges
NAVY, WHITE, AMBER = "#12233a", "#ffffff", "#f5a623"


def font(size):
    # Segoe UI Black: heavy, with a properly round O (a narrow O reads as a zero).
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

    big, small = font(400), font(180)
    b = d.textbbox((0, 0), "RPO", font=big)
    w, h_big = b[2] - b[0], b[3] - b[1]
    letters = [d.textbbox((0, 0), c, font=small) for c in "PRO"]
    h_small = max(l[3] for l in letters) - min(l[1] for l in letters)
    gap, rule = 58, 22
    top = (S - (h_big + gap + rule + gap + h_small)) / 2      # centre the whole stack
    left = (S - w) / 2

    d.text((left - b[0], top - b[1]), "RPO", font=big, fill=WHITE)
    y = top + h_big + gap
    d.rectangle((left, y, left + w, y + rule), fill=AMBER)
    y += rule + gap - min(l[1] for l in letters)
    ink = sum(l[2] - l[0] for l in letters)
    step = (w - ink) / 2                                        # spread P R O across the width
    x = left
    for c, l in zip("PRO", letters):
        d.text((x - l[0], y), c, font=small, fill=AMBER)
        x += (l[2] - l[0]) + step
    return img


if __name__ == "__main__":
    draw().resize((256, 256), Image.LANCZOS).save(
        OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)
