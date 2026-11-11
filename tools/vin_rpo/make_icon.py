"""Draws vin_rpo.ico: "RPO" over "PRO" (the same three letters, re-ordered) on a navy tile.

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
    f = font(350)
    rows = (("RPO", WHITE), ("PRO", AMBER))
    boxes = [d.textbbox((0, 0), t, font=f) for t, _ in rows]
    cap = max(b[3] - b[1] for b in boxes)
    gap = 80
    y = (S - (2 * cap + gap)) / 2
    for (text, colour), b in zip(rows, boxes):
        d.text(((S - (b[2] - b[0])) / 2 - b[0], y - b[1]), text, font=f, fill=colour)
        y += cap + gap
    return img


if __name__ == "__main__":
    draw().resize((256, 256), Image.LANCZOS).save(
        OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)
