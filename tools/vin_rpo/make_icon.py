"""Draws vin_rpo.ico (a gauge over the letters RPO). Needs Pillow: pip install pillow

    python tools/vin_rpo/make_icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "vin_rpo.ico"
S = 1024  # draw large, downsample for smooth edges


def font(size):
    for name in ("seguibl.ttf", "arialbd.ttf", "segoeuib.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 24, S - 24, S - 24), radius=190, fill="#12233a")

    # gauge: 240-degree sweep, opening at the bottom
    cx, cy, r = S // 2, 450, 330
    box = (cx - r, cy - r, cx + r, cy + r)
    d.arc(box, 150, 390, fill="#3b556f", width=58)
    d.arc(box, 150, 318, fill="#f5a623", width=58)          # filled part, up to the needle
    for i in range(9):                                       # tick marks
        a = math.radians(150 + i * 30)
        r1, r2 = r - 100, r - 48
        d.line((cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                cx + r2 * math.cos(a), cy + r2 * math.sin(a)), fill="#dfe7ef", width=16)
    a = math.radians(318)                                    # needle
    d.line((cx, cy, cx + (r - 120) * math.cos(a), cy + (r - 120) * math.sin(a)),
           fill="#ffffff", width=30)
    d.ellipse((cx - 46, cy - 46, cx + 46, cy + 46), fill="#ffffff")

    f = font(250)
    w = d.textlength("RPO", font=f)
    d.text(((S - w) / 2, 640), "RPO", font=f, fill="#ffffff")
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(
        OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)
