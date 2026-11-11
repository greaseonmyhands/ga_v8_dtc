"""CAN PRO: GM LS (single-wire) / HS CAN logger and script sender for J2534 devices.

Double-click to run (needs 32-bit Python for real devices; the simulator works anywhere).
The window itself lives in canpro_ui.py.

    can_pro.pyw --probe V200 --bus LS --seconds 5     receive-only console check, no window
    can_pro.pyw --selftest                            run the simulator briefly, write a report
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canpro_ui import *  # noqa: E402,F401,F403  (App, SIM_LABEL, COLS ... for tools that load this file)
from canpro_ui import App, main  # noqa: E402

if __name__ == "__main__":
    main(App)
