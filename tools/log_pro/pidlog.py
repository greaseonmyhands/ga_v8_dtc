"""PID log for LOG PRO: one column per chosen PID, one row per sample interval, written in
the layout HP Tuners VCM Scanner uses for its own "Export Log File" CSV:

    HP Tuners CSV Log File
    Version: 1.0

    [Log Information]
    Creation Time: 10/5/2026 10:32:37 PM
    Notes:

    [Channel Information]
    0,<channel id>,<channel id>,...
    Offset,<name>,<name>,...
    s,<unit>,<unit>,...

    [Channel Data]
    0.000,<value>,<value>,...

VCM Scanner's own files carry HP Tuners' internal channel numbers on the channel-id line.
DBC signals have no such number, so they get made-up ids from FIRST_CHANNEL_ID upward.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FIRST_CHANNEL_ID = 900001
STYLES = ("hpt", "plain")


@dataclass
class Column:
    key: object          # whatever the caller uses to identify the PID
    name: str
    unit: str = ""


def clean(text: str) -> str:
    """A name or unit safe for one CSV cell."""
    return " ".join(str(text).replace(",", " ").replace('"', "").split())


def unique_names(names) -> list:
    """Make column names unique by adding ' (2)', ' (3)' ..."""
    seen, out = {}, []
    for n in names:
        n = clean(n) or "PID"
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out


def fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10g}"
    return clean(value)


def creation_time(now: datetime) -> str:
    """'10/5/2026 10:32:37 PM' (no zero padding on month, day or hour)."""
    hour = now.hour % 12 or 12
    return (f"{now.month}/{now.day}/{now.year} {hour}:{now.minute:02d}:{now.second:02d} "
            f"{'AM' if now.hour < 12 else 'PM'}")


class PidLog:
    """Samples the latest value of each column every interval_s and writes a row.

    Time is the capture clock (frame timestamps), not the PC clock: call advance(ts) with
    the time of each frame before applying it with update(), and now and then with the
    current capture time so rows keep coming when the bus is quiet."""

    MAX_CATCH_UP = 100000        # rows written in one advance() at most

    def __init__(self, folder, columns, interval_s: float, start_ts: float, style: str = "hpt",
                 stem: str | None = None, notes: str = "", now: datetime | None = None):
        if style not in STYLES:
            raise ValueError(f"unknown PID log style {style!r}")
        self.columns = list(columns)
        self.interval = max(0.001, float(interval_s))
        self.start_ts = float(start_ts)
        self.style = style
        self.rows = 0
        self.closed = False
        self._latest = {c.key: None for c in self.columns}
        self._n = 0                      # index of the next sample
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        now = now or datetime.now()
        if stem is None:
            base = stem = "pids_" + now.strftime("%Y%m%d_%H%M%S")
            n = 1
            while (folder / f"{stem}.csv").exists():
                n += 1
                stem = f"{base}_{n}"
        self.path = folder / f"{stem}.csv"
        names = unique_names(c.name for c in self.columns)
        units = [clean(c.unit) for c in self.columns]
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        w = self._line
        if style == "hpt":
            w("HP Tuners CSV Log File")
            w("Version: 1.0")
            w("")
            w("[Log Information]")
            w(f"Creation Time: {creation_time(now)}")
            w(f"Notes: {clean(notes)}")
            w("")
            w("[Channel Information]")
            w(",".join(["0"] + [str(FIRST_CHANNEL_ID + i) for i in range(len(names))]))
            w(",".join(["Offset"] + names))
            w(",".join(["s"] + units))
            w("")
            w("[Channel Data]")
        else:
            w(",".join(["Time (s)"] + [f"{n} ({u})" if u else n for n, u in zip(names, units)]))

    def _line(self, text: str):
        self._fh.write(text + "\r\n")

    @property
    def next_ts(self) -> float:
        """Capture time of the next row to be written."""
        return self.start_ts + self._n * self.interval

    def update(self, key, value):
        if key in self._latest:
            self._latest[key] = value

    def advance(self, ts: float):
        """Write every row whose sample time is at or before ts."""
        if self.closed:
            return
        done = 0
        while self.next_ts <= ts and done < self.MAX_CATCH_UP:
            offset = self._n * self.interval
            self._line(",".join([f"{offset:.3f}"] + [fmt(self._latest[c.key]) for c in self.columns]))
            self._n += 1
            self.rows += 1
            done += 1

    def flush(self):
        if not self.closed:
            self._fh.flush()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self._fh.close()
            except OSError:
                pass
