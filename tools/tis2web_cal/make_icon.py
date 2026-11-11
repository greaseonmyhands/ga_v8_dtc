"""Draws cal_pro.ico for CAL PRO, in the same style as RPO PRO's icon ("CAL" over "PRO").

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

if __name__ == "__main__":
    rpo_icon.draw("CAL").resize((256, 256), Image.LANCZOS).save(
        OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)
