"""Draws CAL PRO's icon in the same style as RPO PRO's ("CAL" over "PRO"):
cal_pro.ico (exe, window and taskbar) and icons/cal_pro_<size>.png.

Small sizes (64 px and under: the taskbar shows 48 px at 200 % scaling, shrunk from 64) get
their own simpler drawing: just a big "CAL" over a thick amber bar. Shrinking the full
"CAL / PRO" stack to 24-48 px turns the little "PRO" into grey mush, which is what made
the taskbar icon look grainy. Every size is drawn on its own pixel grid, so nothing in
the .ico is a stretched copy of another size.

Needs Pillow (pip install pillow).

    python tools/tis2web_cal/make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "vin_rpo"))
import make_icon as rpo_icon  # noqa: E402

OUT = HERE / "cal_pro.ico"
PNG_DIR = HERE / "icons"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SMALL_MAX = 64


def draw_small(size: int) -> Image.Image:
    """Bold "CAL" and an amber bar, drawn 8x larger then reduced to exactly `size`."""
    S = size * 8
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = S * 0.02
    d.rounded_rectangle((pad, pad, S - pad, S - pad), radius=S * 0.18, fill=rpo_icon.NAVY)

    # largest "CAL" that leaves a margin each side
    target_w = S * 0.84
    fs = int(S * 0.6)
    while True:
        f = rpo_icon.font(fs)
        b = d.textbbox((0, 0), "CAL", font=f)
        if b[2] - b[0] <= target_w or fs < 8:
            break
        fs -= max(1, fs // 40)
    w, h = b[2] - b[0], b[3] - b[1]
    bar = max(S * 0.11, 8 * 1.5)          # at least ~1.5 real pixels thick
    gap = S * 0.07
    top = (S - (h + gap + bar)) / 2
    left = (S - w) / 2
    d.text((left - b[0], top - b[1]), "CAL", font=f, fill=rpo_icon.WHITE)
    y = top + h + gap
    # snap the bar to whole output pixels so it stays crisp after reduction
    y0 = round(y / 8) * 8
    y1 = y0 + max(8, round(bar / 8) * 8)
    d.rectangle((round(left / 8) * 8, y0, round((left + w) / 8) * 8, y1), fill=rpo_icon.AMBER)
    return img.resize((size, size), Image.LANCZOS)


def frame(size: int) -> Image.Image:
    if size <= SMALL_MAX:
        return draw_small(size)
    return rpo_icon.draw("CAL").resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    frames = [frame(s) for s in SIZES]
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    PNG_DIR.mkdir(exist_ok=True)
    for old in PNG_DIR.glob("cal_pro_*.png"):
        old.unlink()
    for s, im in zip(SIZES, frames):
        im.save(PNG_DIR / f"cal_pro_{s}.png")
    print("wrote", OUT, "and", PNG_DIR)
