"""Core of the CAN logger: frames, ID filter, log files, simulator, capture session and
the send-script language. No GUI and no hardware access in this file, so it is all
testable offline (see tests/test_j2534_can.py).
"""
from __future__ import annotations

import csv
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent / "can_logs"   # beside the exe
else:
    DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "j2534_can"

BUSES = ("LS", "HS")
CANDUMP_IFACE = {"LS": "ls0", "HS": "hs0"}
_HEX = re.compile(r"[0-9A-Fa-f]+\Z")


class BusError(Exception):
    """A device/bus problem with a message fit to show the user."""


class TxDisabled(BusError):
    pass


@dataclass(frozen=True)
class Frame:
    ts: float          # seconds since the capture started
    bus: str           # "LS" or "HS"
    arb_id: int
    ext: bool          # True = 29-bit ID
    data: bytes
    tx: bool = False   # True = sent by this tool

    @property
    def id_text(self) -> str:
        return f"{self.arb_id:08X}" if self.ext else f"{self.arb_id:03X}"

    @property
    def data_text(self) -> str:
        return self.data.hex(" ").upper()


# ----------------------------------------------------------------------- parsing
def parse_id(text: str) -> tuple[int, bool]:
    """'7E0' -> (0x7E0, False); '10242040' -> (0x10242040, True).
    Up to 3 hex digits is an 11-bit ID, 4 to 8 digits is a 29-bit ID."""
    t = text.strip()
    if t[:2].lower() == "0x":
        t = t[2:]
    if not t:
        raise ValueError("ID is empty")
    if not _HEX.match(t):
        raise ValueError(f"ID '{text.strip()}' is not hex")
    if len(t) > 8:
        raise ValueError("ID has more than 8 hex digits")
    v = int(t, 16)
    if len(t) <= 3:
        if v > 0x7FF:
            raise ValueError("11-bit ID is above 7FF (write 4 to 8 digits for a 29-bit ID)")
        return v, False
    if v > 0x1FFFFFFF:
        raise ValueError("29-bit ID is above 1FFFFFFF")
    return v, True


def _byte_tokens(text: str) -> list[str]:
    out = []
    for tok in text.replace(",", " ").split():
        if len(tok) % 2:
            raise ValueError(f"'{tok}' is not whole bytes (2 hex digits per byte)")
        out.extend(tok[i:i + 2] for i in range(0, len(tok), 2))
    return out


def parse_data(text: str) -> bytes:
    """'02 01 00' or '020100' -> bytes. 0 to 8 bytes."""
    toks = _byte_tokens(text)
    for t in toks:
        if not _HEX.match(t):
            raise ValueError(f"data byte '{t}' is not hex")
    if len(toks) > 8:
        raise ValueError(f"{len(toks)} data bytes; a CAN frame holds at most 8")
    return bytes(int(t, 16) for t in toks)


def parse_pattern(text: str) -> list:
    """Like parse_data but '??' (or 'XX') matches any byte. Returns ints and None."""
    toks = _byte_tokens(text)
    if len(toks) > 8:
        raise ValueError(f"{len(toks)} pattern bytes; a CAN frame holds at most 8")
    out = []
    for t in toks:
        if t in ("??", "XX", "xx"):
            out.append(None)
        elif _HEX.match(t):
            out.append(int(t, 16))
        else:
            raise ValueError(f"pattern byte '{t}' is not hex or ??")
    return out


def pattern_matches(pattern, data: bytes) -> bool:
    """True if data starts with the pattern (None = any byte)."""
    if len(data) < len(pattern):
        return False
    return all(p is None or p == b for p, b in zip(pattern, data))


class IdFilter:
    """Display filter. Space or comma separated:
        7E8            one ID
        7E0-7EF        a range
        !0C9           hide an ID (or range)
        LS / HS        only that bus
    Empty shows everything. IDs match by value, 11-bit and 29-bit alike."""

    def __init__(self, text: str = ""):
        self.buses, self.include, self.exclude = set(), [], []
        for tok in text.replace(",", " ").split():
            up = tok.upper()
            if up in BUSES:
                self.buses.add(up)
                continue
            neg = up.startswith("!")
            body = up[1:] if neg else up
            lo, sep, hi = body.partition("-")
            try:
                a = parse_id(lo)[0]
                b = parse_id(hi)[0] if sep else a
            except ValueError:
                raise ValueError(f"'{tok}' is not an ID, a range like 7E0-7EF, LS or HS") from None
            if b < a:
                raise ValueError(f"range '{tok}' is backwards")
            (self.exclude if neg else self.include).append((a, b))
        self.empty = not (self.buses or self.include or self.exclude)

    def match(self, f: Frame) -> bool:
        if self.empty:
            return True
        if self.buses and f.bus not in self.buses:
            return False
        i = f.arb_id
        if any(a <= i <= b for a, b in self.exclude):
            return False
        return not self.include or any(a <= i <= b for a, b in self.include)


# --------------------------------------------------------------------- log files
CSV_HEADER = ["time_s", "utc_time", "bus", "dir", "id", "id_bits", "dlc", "data"]


def csv_row(f: Frame, start_wall: float) -> list:
    utc = datetime.fromtimestamp(start_wall + f.ts, timezone.utc)
    return [f"{f.ts:.6f}", utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z", f.bus,
            "Tx" if f.tx else "Rx", f.id_text, 29 if f.ext else 11, len(f.data), f.data_text]


def candump_line(f: Frame, start_wall: float) -> str:
    """Linux can-utils 'candump -L' line: (epoch) iface ID#DATA. 3-digit ID = 11-bit,
    8-digit ID = 29-bit."""
    return f"({start_wall + f.ts:.6f}) {CANDUMP_IFACE[f.bus]} {f.id_text}#{f.data.hex().upper()}"


def parse_candump_line(line: str):
    """-> (epoch, iface, arb_id, ext, data) or None for a line that is not a frame."""
    m = re.match(r"\((\d+\.\d+)\)\s+(\S+)\s+([0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})#([0-9A-Fa-f]*)\s*\Z",
                 line.strip())
    if not m:
        return None
    return (float(m[1]), m[2], int(m[3], 16), len(m[3]) == 8, bytes.fromhex(m[4]))


class LogWriter:
    """Writes every frame to <stem>.csv and <stem>.log (candump format) as it arrives."""

    def __init__(self, folder: Path, start_wall: float | None = None, stem: str | None = None):
        self.start_wall = time.time() if start_wall is None else start_wall
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        if stem is None:
            stem = "can_" + datetime.fromtimestamp(self.start_wall).strftime("%Y%m%d_%H%M%S")
        self.csv_path = folder / f"{stem}.csv"
        self.log_path = folder / f"{stem}.log"
        self._csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._log_file = open(self.log_path, "w", newline="\n", encoding="ascii")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(CSV_HEADER)
        self.count = 0

    def write(self, f: Frame):
        self._csv.writerow(csv_row(f, self.start_wall))
        self._log_file.write(candump_line(f, self.start_wall) + "\n")
        self.count += 1

    def flush(self):
        self._csv_file.flush()
        self._log_file.flush()

    def close(self):
        for fh in (self._csv_file, self._log_file):
            try:
                fh.close()
            except OSError:
                pass


# --------------------------------------------------------------------- simulator
class SimBackend:
    """Stands in for a J2534 device: makes up periodic traffic on both buses and answers
    a request sent to HS 7E0 with a reply on 7E8 (so 'waitfor' can be tried). No hardware."""

    name = "Simulator"

    # (bus, id, 29-bit, period s)
    TRAFFIC = (
        ("LS", 0x10242040, True, 0.100), ("LS", 0x0C050040, True, 0.250),
        ("LS", 0x10ACA040, True, 0.500), ("LS", 0x621, False, 1.000),
        ("HS", 0x0C9, False, 0.0125), ("HS", 0x1E9, False, 0.025),
        ("HS", 0x3D1, False, 0.100), ("HS", 0x4C1, False, 0.500),
    )

    def __init__(self, traffic: bool = True):
        self.traffic = traffic
        self.buses = ()
        self.sent: list[tuple] = []        # (bus, arb_id, ext, data, hv) for tests
        self._pending: list[Frame] = []
        self._due = {}
        self._count = 0
        self.t0 = time.perf_counter()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def open(self, buses) -> list[str]:
        self.buses = tuple(buses)
        self.t0 = time.perf_counter()
        self._due = {i: t[3] for i, t in enumerate(self.TRAFFIC)} if self.traffic else {}
        return ["Simulator: made-up traffic, no hardware attached"]

    def read(self, bus: str) -> list[Frame]:
        now = self.now()
        out = []
        for i, due in self._due.items():
            b, arb, ext, period = self.TRAFFIC[i]
            if b != bus:
                continue
            while due <= now:
                self._count += 1
                c = self._count
                data = bytes([c & 0xFF, (c >> 8) & 0xFF, arb & 0xFF, 0x00, 0x55, 0xAA,
                              (c * 7) & 0xFF, 0xFF])[:8 if ext else 6]
                out.append(Frame(due, b, arb, ext, data))
                due += period
            self._due[i] = due
        keep = []
        for f in self._pending:
            (out if f.bus == bus and f.ts <= now else keep).append(f)
        self._pending = keep
        out.sort(key=lambda f: f.ts)
        return out

    def write(self, bus: str, arb_id: int, ext: bool, data: bytes, hv: bool = False):
        if bus not in self.buses:
            raise BusError(f"{bus} is not open")
        self.sent.append((bus, arb_id, ext, bytes(data), hv))
        if bus == "HS" and arb_id == 0x7E0 and not ext and len(data) >= 2:
            reply = bytes([0x06, (data[1] + 0x40) & 0xFF, data[2] if len(data) > 2 else 0,
                           0xBE, 0x1F, 0xA8, 0x13, 0x00])
            self._pending.append(Frame(self.now() + 0.005, "HS", 0x7E8, False, reply))

    def close(self):
        self.buses = ()


# ----------------------------------------------------------------------- session
class Session:
    """Owns one open backend and a worker thread. Every device call happens on that
    thread. Receive only until tx_enabled is set True by an explicit user action."""

    RING = 4096

    def __init__(self, backend, buses, on_frames=None, log_factory=None, on_event=None):
        self.backend = backend
        self.buses = tuple(buses)
        self.on_frames = on_frames          # called on the worker thread with a list of frames
        self.on_event = on_event            # ("opened", notes) / ("closed", error or None)
        self.log_factory = log_factory      # () -> LogWriter, called once the device is open
        self.log = None
        self.tx_enabled = False
        self.rx_count = 0
        self.tx_count = 0
        self.notes: list[str] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._txq: queue.Queue = queue.Queue()
        self._cond = threading.Condition()
        self._seq = 0
        self._ring: deque = deque(maxlen=self.RING)
        self._thread = threading.Thread(target=self._run, daemon=True, name="can-session")

    # ------------------------------------------------------------------ control
    def start(self):
        self._thread.start()

    def wait_open(self, timeout: float = 10.0):
        """Block until the device is open. Raises BusError if it failed."""
        if not self._opened.wait(timeout):
            raise BusError("Timed out opening the device")
        if self.error:
            raise BusError(self.error)

    def stop(self, wait: float | None = None):
        self._stop.set()
        if wait and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(wait)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    @property
    def seq(self) -> int:
        return self._seq

    def _emit(self, kind, arg):
        if self.on_event:
            try:
                self.on_event(kind, arg)
            except Exception:
                pass

    # ------------------------------------------------------------------- worker
    def _run(self):
        err = None
        try:
            try:
                self.notes = list(self.backend.open(self.buses))
                if self.log_factory:
                    self.log = self.log_factory()
            except Exception as e:
                self.error = str(e) if isinstance(e, BusError) else f"{type(e).__name__}: {e}"
                err = self.error
                return
            finally:
                self._opened.set()
            self._emit("opened", self.notes)
            last_flush = time.monotonic()
            while not self._stop.is_set():
                got = []
                for bus in self.buses:
                    got.extend(self.backend.read(bus))
                self.rx_count += len(got)
                while True:
                    try:
                        req = self._txq.get_nowait()
                    except queue.Empty:
                        break
                    bus, arb_id, ext, data, hv, box = req
                    try:
                        self.backend.write(bus, arb_id, ext, data, hv)
                        got.append(Frame(self.backend.now(), bus, arb_id, ext, data, tx=True))
                        self.tx_count += 1
                    except Exception as e:
                        box["error"] = str(e)
                    box["done"].set()
                if got:
                    self._dispatch(got)
                else:
                    time.sleep(0.002)
                if self.log and time.monotonic() - last_flush > 1.0:
                    self.log.flush()
                    last_flush = time.monotonic()
        except Exception as e:
            err = str(e) if isinstance(e, BusError) else f"{type(e).__name__}: {e}"
            self.error = err
        finally:
            self._stop.set()
            try:
                self.backend.close()
            except Exception:
                pass
            if self.log:
                self.log.close()
            while True:   # release anyone still waiting on a send
                try:
                    box = self._txq.get_nowait()[5]
                except queue.Empty:
                    break
                box["error"] = "capture stopped"
                box["done"].set()
            with self._cond:
                self._cond.notify_all()
            self._emit("closed", err)

    def _dispatch(self, frames):
        if self.log:
            for f in frames:
                self.log.write(f)
        with self._cond:
            for f in frames:
                self._seq += 1
                self._ring.append((self._seq, f))
            self._cond.notify_all()
        if self.on_frames:
            self.on_frames(frames)

    # --------------------------------------------------------------- send / wait
    def send(self, bus: str, arb_id: int, ext: bool, data: bytes, hv: bool = False,
             timeout: float = 2.0):
        """Queue one frame and wait until the device accepted it. Raises on any problem."""
        if not self.tx_enabled:
            raise TxDisabled("Transmit is off. Enable transmit first.")
        if bus not in self.buses:
            raise BusError(f"The {bus} bus is not open in this capture.")
        if not self.running:
            raise BusError("The capture is not running.")
        box = {"done": threading.Event(), "error": None}
        self._txq.put((bus, arb_id, ext, bytes(data), hv, box))
        if not box["done"].wait(timeout):
            raise BusError("Timed out waiting for the device to send the frame.")
        if box["error"]:
            raise BusError(f"Send failed: {box['error']}")

    def wait_for(self, pred, timeout: float, since_seq: int = 0, stop: threading.Event | None = None):
        """First received (not sent) frame after since_seq for which pred(frame) is true,
        or None on timeout / stop."""
        deadline = time.monotonic() + timeout
        seen = since_seq
        with self._cond:
            while True:
                for s, f in self._ring:
                    if s > seen:
                        seen = s
                        if not f.tx and pred(f):
                            return f
                left = deadline - time.monotonic()
                if left <= 0 or self._stop.is_set() or (stop is not None and stop.is_set()):
                    return None
                self._cond.wait(min(left, 0.05))


# ------------------------------------------------------------------------ script
class ScriptError(Exception):
    def __init__(self, line: int, msg: str):
        self.line, self.msg = line, msg
        super().__init__(f"line {line}: {msg}" if line else msg)


@dataclass
class Step:
    kind: str                 # send / wait / waitfor / repeat
    line: int
    bus: str | None = None
    arb_id: int = 0
    ext: bool = False
    data: bytes = b""
    hv: bool = False
    ms: int = 0               # wait time, or waitfor timeout
    pattern: tuple = ()
    go_on: bool = False       # waitfor: carry on after a timeout
    count: int | None = None  # repeat: None = forever
    body: tuple = ()

    def text(self) -> str:
        if self.kind == "send":
            d = self.data.hex(" ").upper() or "(no data)"
            idt = f"{self.arb_id:08X}" if self.ext else f"{self.arb_id:03X}"
            return f"SEND {self.bus} {idt}  {d}" + ("  [high-voltage wake-up]" if self.hv else "")
        if self.kind == "wait":
            return f"wait {self.ms} ms"
        if self.kind == "waitfor":
            idt = f"{self.arb_id:08X}" if self.ext else f"{self.arb_id:03X}"
            pat = " ".join("??" if p is None else f"{p:02X}" for p in self.pattern)
            return (f"wait for {self.bus} {idt}" + (f" starting {pat}" if pat else "")
                    + f", up to {self.ms} ms" + (" (continue on timeout)" if self.go_on else ""))
        return "repeat forever" if self.count is None else f"repeat {self.count} times"


@dataclass
class Script:
    steps: tuple
    buses: tuple              # buses the script sends on or waits on
    sends: int | None         # total frames it will send; None = runs until stopped
    wait_ms: int | None       # total of the fixed waits; None = runs until stopped

    def preview(self) -> list[str]:
        """Plain lines saying what the script will do, in order."""
        out = []

        def walk(steps, depth):
            for s in steps:
                out.append("    " * depth + s.text())
                if s.kind == "repeat":
                    walk(s.body, depth + 1)
        walk(self.steps, 0)
        return out

    def summary(self) -> str:
        if self.sends is None:
            return (f"Sends on {', '.join(self.buses) or 'no bus'}; repeats forever until you "
                    "press Stop script.")
        return (f"Will send {self.sends} frame{'s' if self.sends != 1 else ''} on "
                f"{', '.join(self.buses) or 'no bus'}; fixed waits total {self.wait_ms} ms.")


def parse_script(text: str) -> Script:
    """Parse the script language (see README). Raises ScriptError with a line number."""
    root: list[Step] = []
    stack: list[tuple[Step, list]] = []   # open repeat blocks
    cur = root
    default_bus = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        cmd, args = toks[0].lower(), toks[1:]
        try:
            if cmd == "bus":
                if len(args) != 1 or args[0].upper() not in BUSES:
                    raise ValueError("write 'bus LS' or 'bus HS'")
                default_bus = args[0].upper()
            elif cmd == "send":
                bus = default_bus
                if args and args[0].upper() in BUSES:
                    bus, args = args[0].upper(), args[1:]
                if bus is None:
                    raise ValueError("no bus: write 'send LS ...' / 'send HS ...', or a "
                                     "'bus LS' line first")
                if not args:
                    raise ValueError("send needs an ID")
                hv = args[-1].lower() == "hv"
                if hv:
                    args = args[:-1]
                    if bus != "LS":
                        raise ValueError("'hv' (high-voltage wake-up) is only for the LS bus")
                arb_id, ext = parse_id(args[0])
                cur.append(Step("send", n, bus=bus, arb_id=arb_id, ext=ext,
                                data=parse_data(" ".join(args[1:])), hv=hv))
            elif cmd == "wait":
                if len(args) != 1 or not args[0].isdigit():
                    raise ValueError("write 'wait <milliseconds>', e.g. wait 100")
                cur.append(Step("wait", n, ms=int(args[0])))
            elif cmd == "waitfor":
                bus = default_bus
                if args and args[0].upper() in BUSES:
                    bus, args = args[0].upper(), args[1:]
                if bus is None:
                    raise ValueError("no bus: write 'waitfor HS ...' or a 'bus HS' line first")
                go_on = bool(args) and args[-1].lower() == "continue"
                if go_on:
                    args = args[:-1]
                ms = 1000
                if len(args) >= 2 and args[-2].lower() == "timeout":
                    if not args[-1].isdigit():
                        raise ValueError("timeout needs milliseconds, e.g. timeout 500")
                    ms, args = int(args[-1]), args[:-2]
                if not args:
                    raise ValueError("waitfor needs an ID")
                arb_id, ext = parse_id(args[0])
                cur.append(Step("waitfor", n, bus=bus, arb_id=arb_id, ext=ext, ms=ms,
                                pattern=tuple(parse_pattern(" ".join(args[1:]))), go_on=go_on))
            elif cmd == "repeat":
                if len(args) != 1 or not (args[0].lower() == "forever" or args[0].isdigit()):
                    raise ValueError("write 'repeat <count>' or 'repeat forever'")
                count = None if args[0].lower() == "forever" else int(args[0])
                if count == 0:
                    raise ValueError("repeat count must be at least 1")
                step = Step("repeat", n, count=count)
                cur.append(step)
                stack.append((step, cur))
                cur = []
            elif cmd == "end":
                if args:
                    raise ValueError("'end' takes nothing after it")
                if not stack:
                    raise ValueError("'end' without a 'repeat'")
                step, parent = stack.pop()
                if not cur:
                    raise ValueError(f"the repeat on line {step.line} is empty")
                step.body = tuple(cur)
                if step.count is None and not _has_wait(step.body):
                    raise ValueError(f"'repeat forever' on line {step.line} has no wait inside; "
                                     "it would flood the bus")
                cur = parent
            else:
                raise ValueError(f"unknown command '{toks[0]}' (send, wait, waitfor, repeat, "
                                 "end, bus)")
        except ValueError as e:
            raise ScriptError(n, str(e)) from None
    if stack:
        raise ScriptError(stack[-1][0].line, "this 'repeat' has no matching 'end'")
    if not root:
        raise ScriptError(0, "the script has no commands")
    buses = []
    sends, wait_ms = _totals(root, buses)
    return Script(tuple(root), tuple(b for b in BUSES if b in buses), sends, wait_ms)


def _has_wait(steps) -> bool:
    return any(s.kind == "wait" and s.ms > 0 or s.kind == "waitfor"
               or (s.kind == "repeat" and _has_wait(s.body)) for s in steps)


def _totals(steps, buses):
    sends, wait_ms = 0, 0
    for s in steps:
        if s.kind in ("send", "waitfor") and s.bus not in buses:
            buses.append(s.bus)
        if s.kind == "send":
            if sends is not None:
                sends += 1
        elif s.kind == "wait":
            if wait_ms is not None:
                wait_ms += s.ms
        elif s.kind == "repeat":
            a, b = _totals(s.body, buses)
            if s.count is None or a is None:
                sends, wait_ms = None, None
            elif sends is not None:
                sends += a * s.count
                wait_ms += b * s.count
    return sends, wait_ms


class _Stopped(Exception):
    pass


class ScriptRunner:
    """Runs a parsed Script against a Session on its own thread.
    on_event(kind, text): kind is 'step', 'done', 'stopped' or 'error'."""

    def __init__(self, session: Session, script: Script, on_event=None):
        self.session, self.script = session, script
        self.on_event = on_event or (lambda kind, text: None)
        self.sent = 0
        self.result: tuple | None = None    # (kind, text) once finished
        self._stop = threading.Event()
        self._mark = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="can-script")

    def start(self):
        missing = [b for b in self.script.buses if b not in self.session.buses]
        if missing:
            raise BusError(f"The script uses the {' and '.join(missing)} bus, which is not open "
                           "in this capture.")
        if not self.session.tx_enabled:
            raise TxDisabled("Transmit is off. Enable transmit first.")
        self._mark = self.session.seq
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _n(self) -> str:
        return f"{self.sent} frame{'' if self.sent == 1 else 's'}"

    def _finish(self, kind, text):
        self.result = (kind, text)
        self.on_event(kind, text)

    def _run(self):
        try:
            self._steps(self.script.steps)
        except _Stopped:
            return self._finish("stopped", f"Script stopped after {self._n()} sent.")
        except ScriptError as e:
            return self._finish("error", f"Script stopped at {e} ({self._n()} sent).")
        except BusError as e:
            return self._finish("error", f"Script stopped: {e} ({self._n()} sent).")
        self._finish("done", f"Script finished: {self._n()} sent.")

    def _steps(self, steps):
        for s in steps:
            if self._stop.is_set() or not self.session.running:
                raise _Stopped()
            if s.kind == "send":
                self._mark = self.session.seq   # a waitfor sees replies from here on
                self.session.send(s.bus, s.arb_id, s.ext, s.data, s.hv)
                self.sent += 1
                self.on_event("step", f"line {s.line}: {s.text()}")
            elif s.kind == "wait":
                if self._stop.wait(s.ms / 1000.0):
                    raise _Stopped()
            elif s.kind == "waitfor":
                self.on_event("step", f"line {s.line}: {s.text()}")
                hit = self.session.wait_for(
                    lambda f, s=s: (f.bus == s.bus and f.arb_id == s.arb_id and f.ext == s.ext
                                    and pattern_matches(s.pattern, f.data)),
                    s.ms / 1000.0, self._mark, self._stop)
                if self._stop.is_set():
                    raise _Stopped()
                if hit is None and not s.go_on:
                    raise ScriptError(s.line, f"no matching frame within {s.ms} ms")
            elif s.kind == "repeat":
                i = 0
                while s.count is None or i < s.count:
                    self._steps(s.body)
                    i += 1
