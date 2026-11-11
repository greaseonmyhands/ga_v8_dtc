"""LOG PRO: CAN PRO's LS / HS CAN logger plus a PID side (DBC signals, live values, and a
PID log laid out like an HP Tuners VCM Scanner CSV export).

Double-click to run (needs 32-bit Python for real devices; the simulator works anywhere).
The window lives in logpro_ui.py and builds on tools/j2534_can/canpro_ui.py.

    log_pro.pyw --probe V200 --bus LS --seconds 5     receive-only console check, no window
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from logpro_ui import *  # noqa: E402,F401,F403  (for tools that load this file)
from logpro_ui import LogProApp, main  # noqa: E402

if __name__ == "__main__":
    main(LogProApp)
