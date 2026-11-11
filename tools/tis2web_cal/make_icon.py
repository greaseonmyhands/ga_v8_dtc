"""Draws CAL PRO's icon in the same style as RPO PRO's ("CAL" over "PRO"):
cal_pro.ico (exe / shortcut) and icons/cal_pro_<size>.png (window and taskbar, so Windows
has a sharp image at every display scaling instead of stretching a small one).

Needs Pillow (pip install pillow).

    python tools/tis2web_cal/make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "vin_rpo"))
import make_icon as rpo_icon  # noqa: E402

OUT = HERE / "cal_pro.ico"
PNG_DIR = HERE / "icons"
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256)

if __name__ == "__main__":
    big = rpo_icon.draw("CAL")  # drawn at 1024 px; every size is downsampled from that
    big.resize((256, 256), Image.LANCZOS).save(
        OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    PNG_DIR.mkdir(exist_ok=True)
    for s in PNG_SIZES:
        big.resize((s, s), Image.LANCZOS).save(PNG_DIR / f"cal_pro_{s}.png")
    print("wrote", OUT, "and", PNG_DIR)
