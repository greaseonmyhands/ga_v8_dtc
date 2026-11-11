"""Small SAE J2534 04.04 binding (ctypes) for raw CAN logging and sending.

Only what the logger needs: list the installed pass-thru devices from the registry, open
one, connect a raw CAN channel (HS on pins 6/14) and/or a single-wire CAN channel (J2534-2
SW_CAN_PS on pin 1), set pass-all filters, read and write frames.

The vendor DLLs are 32-bit, so this must run in a 32-bit Python (or the 32-bit exe).
Nothing is ever transmitted by open(); frames go out only through write().
"""
from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import POINTER, Structure, byref, c_ubyte, c_ulong
from dataclasses import dataclass, field

from canlog import BusError, Frame

REG_KEY = r"SOFTWARE\PassThruSupport.04.04"  # opened in the 32-bit view (WOW6432Node)

# Protocol IDs (J2534-1 and J2534-2)
CAN = 0x05
SW_CAN_PS = 0x8008

# Connect flags / TxFlags / RxStatus
CAN_29BIT_ID = 0x0100
CAN_ID_BOTH = 0x0800
SW_CAN_HV_TX = 0x0400
TX_MSG_TYPE = 0x0001

# Filters
PASS_FILTER = 1

# Ioctl IDs and config parameters
SET_CONFIG = 0x02
READ_VBATT = 0x03
CLEAR_RX_BUFFER = 0x08
J1962_PINS = 0x8001

# Error codes
ERR_NOT_SUPPORTED = 0x01
ERR_INVALID_PROTOCOL_ID = 0x03
ERR_INVALID_FLAGS = 0x06
ERR_FAILED = 0x07
ERR_DEVICE_NOT_CONNECTED = 0x08
ERR_TIMEOUT = 0x09
ERR_DEVICE_IN_USE = 0x0E
ERR_BUFFER_EMPTY = 0x10
ERR_BUFFER_OVERFLOW = 0x12
ERR_PIN_INVALID = 0x13
ERR_CHANNEL_IN_USE = 0x14
ERR_INVALID_BAUDRATE = 0x19

ERROR_NAMES = {
    0x00: "STATUS_NOERROR", 0x01: "ERR_NOT_SUPPORTED", 0x02: "ERR_INVALID_CHANNEL_ID",
    0x03: "ERR_INVALID_PROTOCOL_ID", 0x04: "ERR_NULL_PARAMETER", 0x05: "ERR_INVALID_IOCTL_VALUE",
    0x06: "ERR_INVALID_FLAGS", 0x07: "ERR_FAILED", 0x08: "ERR_DEVICE_NOT_CONNECTED",
    0x09: "ERR_TIMEOUT", 0x0A: "ERR_INVALID_MSG", 0x0B: "ERR_INVALID_TIME_INTERVAL",
    0x0C: "ERR_EXCEEDED_LIMIT", 0x0D: "ERR_INVALID_MSG_ID", 0x0E: "ERR_DEVICE_IN_USE",
    0x0F: "ERR_INVALID_IOCTL_ID", 0x10: "ERR_BUFFER_EMPTY", 0x11: "ERR_BUFFER_FULL",
    0x12: "ERR_BUFFER_OVERFLOW", 0x13: "ERR_PIN_INVALID", 0x14: "ERR_CHANNEL_IN_USE",
    0x15: "ERR_MSG_PROTOCOL_ID", 0x16: "ERR_INVALID_FILTER_ID", 0x17: "ERR_NO_FLOW_CONTROL",
    0x18: "ERR_NOT_UNIQUE", 0x19: "ERR_INVALID_BAUDRATE", 0x1A: "ERR_INVALID_DEVICE_ID",
}

# bus name -> (protocol, baud rates to try, J1962 pins value or None, description)
BUS_SETUP = {
    "HS": (CAN, (500000,), None, "HS CAN 500 kbps, pins 6/14"),
    "LS": (SW_CAN_PS, (33333, 33300), 0x0100, "LS single-wire CAN 33.3 kbps, pin 1"),
}

READ_BATCH = 16


class PASSTHRU_MSG(Structure):
    _fields_ = [("ProtocolID", c_ulong), ("RxStatus", c_ulong), ("TxFlags", c_ulong),
                ("Timestamp", c_ulong), ("DataSize", c_ulong), ("ExtraDataIndex", c_ulong),
                ("Data", c_ubyte * 4128)]


class SCONFIG(Structure):
    _fields_ = [("Parameter", c_ulong), ("Value", c_ulong)]


class SCONFIG_LIST(Structure):
    _fields_ = [("NumOfParams", c_ulong), ("ConfigPtr", POINTER(SCONFIG))]


class J2534Error(BusError):
    def __init__(self, func: str, code: int, detail: str = ""):
        self.func, self.code, self.detail = func, code, detail
        name = ERROR_NAMES.get(code, f"error 0x{code:02X}")
        super().__init__(f"{func}: {name}" + (f" ({detail})" if detail else ""))


@dataclass
class DeviceInfo:
    key: str                      # registry key name, e.g. "Autel Intelligent Tech Inc. - V200"
    name: str
    vendor: str
    dll: str
    protocols: set = field(default_factory=set)   # registry flags set to non-zero

    @property
    def label(self) -> str:
        return self.key


def list_devices() -> list[DeviceInfo]:
    """Installed J2534 04.04 devices, from HKLM\\SOFTWARE\\WOW6432Node\\PassThruSupport.04.04."""
    try:
        import winreg
    except ImportError:  # not Windows
        return []
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_KEY, 0,
                              winreg.KEY_READ | winreg.KEY_WOW64_32KEY)
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    vals, j = {}, 0
                    while True:
                        try:
                            n, v, _t = winreg.EnumValue(k, j)
                        except OSError:
                            break
                        vals[n] = v
                        j += 1
            except OSError:
                continue
            dll = str(vals.get("FunctionLibrary") or "")
            if not dll:
                continue
            protos = {n for n, v in vals.items() if isinstance(v, int) and v
                      and n not in ("DeviceID", "VersionNumber", "PDUAPISupported", "EnableLogging")}
            out.append(DeviceInfo(sub, str(vals.get("Name") or sub), str(vals.get("Vendor") or ""),
                                  dll, protos))
    return sorted(out, key=lambda d: d.key.lower())


def _load(path: str):
    if not os.path.isfile(path):
        raise BusError(f"The driver file for this device is missing:\n{path}")
    last = None
    for mode in (None, 0x8):  # default search, then LOAD_WITH_ALTERED_SEARCH_PATH
        try:
            return ctypes.WinDLL(path) if mode is None else ctypes.WinDLL(path, winmode=mode)
        except OSError as e:
            last = e
            if getattr(e, "winerror", None) == 193:
                break
    if getattr(last, "winerror", None) == 193 and struct.calcsize("P") == 8:
        raise BusError("This J2534 driver is 32-bit and cannot be loaded by a 64-bit program. "
                       "Use the 32-bit exe, or run with 32-bit Python (py -3.12-32).")
    raise BusError(f"Could not load the driver {path}: {last}")


class J2534Backend:
    """One open pass-thru device with up to two raw CAN channels (LS and/or HS)."""

    def __init__(self, info: DeviceInfo):
        self.info = info
        self.name = info.label
        self.dll = None
        self.device_id = None
        self.channels: dict[str, tuple[int, int]] = {}   # bus -> (channel id, protocol)
        self.overflows = 0
        self.t0 = time.perf_counter()
        self._buf = (PASSTHRU_MSG * READ_BATCH)()
        self._ts_last = None      # last raw device timestamp (32-bit microseconds)
        self._ts_wraps = 0
        self._ts_offset = None    # seconds to add to device time to get time since open

    def now(self) -> float:
        return time.perf_counter() - self.t0

    # ------------------------------------------------------------------ helpers
    def _last_error(self) -> str:
        try:
            buf = ctypes.create_string_buffer(256)
            self.dll.PassThruGetLastError(buf)
            return buf.value.decode("ascii", "replace").strip()
        except Exception:
            return ""

    def _call(self, func: str, *args, ok=()):
        rc = getattr(self.dll, func)(*args)
        if rc != 0 and rc not in ok:
            raise J2534Error(func, rc, self._last_error())
        return rc

    def _set_config(self, channel: int, param: int, value: int):
        cfg = SCONFIG(param, value)
        lst = SCONFIG_LIST(1, ctypes.pointer(cfg))
        self._call("PassThruIoctl", c_ulong(channel), c_ulong(SET_CONFIG), byref(lst), None)

    # --------------------------------------------------------------------- open
    def open(self, buses) -> list[str]:
        """Open the device and connect the buses. Receive only: nothing is sent.
        Returns notes for the user. Raises BusError with a plain message on failure."""
        notes = []
        self.dll = _load(self.info.dll)
        for fn in ("PassThruOpen", "PassThruClose", "PassThruConnect", "PassThruDisconnect",
                   "PassThruReadMsgs", "PassThruWriteMsgs", "PassThruStartMsgFilter",
                   "PassThruIoctl"):
            try:
                getattr(self.dll, fn).restype = ctypes.c_long
            except AttributeError:
                raise BusError(f"{self.info.dll} is not a J2534 04.04 driver ({fn} is missing).")
        dev = c_ulong(0)
        try:
            self._call("PassThruOpen", None, byref(dev))
        except J2534Error as e:
            if e.code in (ERR_DEVICE_NOT_CONNECTED, ERR_FAILED, ERR_TIMEOUT):
                raise BusError(f"Could not open {self.info.name}. Is it plugged in (USB) and "
                               f"powered?\n\n{e}") from None
            if e.code == ERR_DEVICE_IN_USE:
                raise BusError(f"{self.info.name} is in use by another program. Close it and "
                               f"try again.\n\n{e}") from None
            raise
        self.device_id = dev.value
        self.t0 = time.perf_counter()
        try:
            for bus in buses:
                notes.extend(self._connect(bus))
            volts = self.read_vbatt()
            if volts is not None:
                notes.append(f"OBD pin 16 voltage {volts:.1f} V")
        except Exception:
            self.close()
            raise
        return notes

    def _connect(self, bus: str) -> list[str]:
        protocol, bauds, pins, desc = BUS_SETUP[bus]
        notes = []
        fallback_flags = CAN_29BIT_ID if bus == "LS" else 0
        chan = c_ulong(0)
        flags, err = CAN_ID_BOTH, None
        connected = False
        for flags in (CAN_ID_BOTH, fallback_flags):
            for baud in bauds:
                try:
                    self._call("PassThruConnect", c_ulong(self.device_id), c_ulong(protocol),
                               c_ulong(flags), c_ulong(baud), byref(chan))
                    connected = True
                    break
                except J2534Error as e:
                    err = e
                    if e.code == ERR_INVALID_BAUDRATE:
                        continue
                    break
            if connected:
                break
        if not connected:
            if bus == "LS" and err.code in (ERR_NOT_SUPPORTED, ERR_INVALID_PROTOCOL_ID):
                raise BusError(f"{self.info.name} does not support single-wire CAN (J2534-2 "
                               f"SW_CAN_PS), so it cannot log the LS bus.\n\n{err}") from None
            if err.code == ERR_CHANNEL_IN_USE:
                raise BusError(f"{self.info.name} would not open {bus} alongside the other bus "
                               f"(channel in use). Log one bus at a time.\n\n{err}") from None
            raise BusError(f"Could not connect {desc} on {self.info.name}.\n\n{err}") from None
        cid = chan.value
        self.channels[bus] = (cid, protocol)
        if flags != CAN_ID_BOTH:
            only = "29-bit" if flags else "11-bit"
            notes.append(f"{bus}: device refused 11+29-bit mode, logging {only} IDs only")
        if pins is not None:
            try:
                self._set_config(cid, J1962_PINS, pins)
            except J2534Error as e:
                raise BusError(f"{self.info.name} would not route single-wire CAN to OBD pin 1 "
                               f"(J1962_PINS).\n\n{e}") from None
        # Pass-all filters: mask 0 / pattern 0 matches every ID. One per ID width.
        made = 0
        widths = (0, CAN_29BIT_ID) if flags == CAN_ID_BOTH else (flags,)
        ferr = None
        for txflags in widths:
            mask, pat = PASSTHRU_MSG(), PASSTHRU_MSG()
            for m in (mask, pat):
                m.ProtocolID, m.TxFlags, m.DataSize = protocol, txflags, 4
            fid = c_ulong(0)
            try:
                self._call("PassThruStartMsgFilter", c_ulong(cid), c_ulong(PASS_FILTER),
                           byref(mask), byref(pat), None, byref(fid))
                made += 1
            except J2534Error as e:
                ferr = e
        if not made:
            raise BusError(f"Could not set a pass-all filter on {bus}.\n\n{ferr}")
        if made < len(widths):
            notes.append(f"{bus}: only one pass-all filter accepted ({ferr}); "
                         "some ID widths may be missing")
        try:
            self._call("PassThruIoctl", c_ulong(cid), c_ulong(CLEAR_RX_BUFFER), None, None)
        except J2534Error:
            pass
        notes.append(f"{bus}: connected ({desc})")
        return notes

    def read_vbatt(self):
        try:
            mv = c_ulong(0)
            self._call("PassThruIoctl", c_ulong(self.device_id), c_ulong(READ_VBATT), None,
                       byref(mv))
            return mv.value / 1000.0
        except Exception:
            return None

    # --------------------------------------------------------------- read/write
    def _time(self, raw: int) -> float:
        if self._ts_last is not None and raw < self._ts_last and self._ts_last - raw > 0x80000000:
            self._ts_wraps += 1          # 32-bit microsecond counter rolled over (~71 min)
        self._ts_last = raw
        dev_s = (raw + (self._ts_wraps << 32)) / 1e6
        if self._ts_offset is None:
            self._ts_offset = self.now() - dev_s
        return max(0.0, dev_s + self._ts_offset)

    def read(self, bus: str) -> list[Frame]:
        cid, _protocol = self.channels[bus]
        n = c_ulong(READ_BATCH)
        rc = self._call("PassThruReadMsgs", c_ulong(cid), self._buf, byref(n), c_ulong(0),
                        ok=(ERR_BUFFER_EMPTY, ERR_TIMEOUT, ERR_BUFFER_OVERFLOW))
        if rc == ERR_BUFFER_OVERFLOW:
            self.overflows += 1
        elif rc == ERR_BUFFER_EMPTY:
            return []
        out = []
        for i in range(min(n.value, READ_BATCH)):
            m = self._buf[i]
            if m.DataSize < 4 or m.DataSize > 12 or m.RxStatus & TX_MSG_TYPE:
                continue
            raw = bytes(m.Data[:m.DataSize])
            out.append(Frame(self._time(m.Timestamp), bus, int.from_bytes(raw[:4], "big"),
                             bool(m.RxStatus & CAN_29BIT_ID), raw[4:]))
        return out

    def write(self, bus: str, arb_id: int, ext: bool, data: bytes, hv: bool = False):
        cid, protocol = self.channels[bus]
        m = PASSTHRU_MSG()
        m.ProtocolID = protocol
        m.TxFlags = (CAN_29BIT_ID if ext else 0) | (SW_CAN_HV_TX if hv and bus == "LS" else 0)
        raw = arb_id.to_bytes(4, "big") + bytes(data)
        m.DataSize = len(raw)
        ctypes.memmove(m.Data, raw, len(raw))
        n = c_ulong(1)
        self._call("PassThruWriteMsgs", c_ulong(cid), byref(m), byref(n), c_ulong(100))

    def close(self):
        if self.dll is None:
            return
        for cid, _p in self.channels.values():
            try:
                self.dll.PassThruDisconnect(c_ulong(cid))
            except Exception:
                pass
        self.channels.clear()
        if self.device_id is not None:
            try:
                self.dll.PassThruClose(c_ulong(self.device_id))
            except Exception:
                pass
            self.device_id = None


# ---------------------------------------------------------------- presence check
# J2534 has no "is the device plugged in?" call, so the check is PassThruOpen followed by
# PassThruClose. Each driver is checked in its own short-lived helper process because some
# drivers stall for a long time (or could crash) when nothing is attached. The helper
# reports through its exit code: DETECT_BASE + the PassThruOpen result.
DETECT_BASE = 100
DETECT_NO_DRIVER = 0xFE
DETECT_WATCHDOG_S = 8.0
DETECT_STATES = ("detected", "in use", "not detected", "no answer", "not checked")
# Drivers left out of the check: the Bosch MDI / MDI 2 driver pops up its own window and
# takes over 20 s to give up when nothing is attached.
DETECT_SKIP_DLLS = frozenset({"bvtx4j32.dll"})


def detect_skipped(dll: str) -> bool:
    return os.path.basename(dll).lower() in DETECT_SKIP_DLLS


def detect_child(dll: str) -> int:
    """Runs in the helper process. Opens and closes the device; connects no channel and
    sends nothing. Returns the process exit code."""
    import threading
    dog = threading.Timer(DETECT_WATCHDOG_S, lambda: os._exit(DETECT_BASE + ERR_TIMEOUT))
    dog.daemon = True
    dog.start()                      # never outlive the app if the driver hangs
    try:
        lib = _load(dll)
        lib.PassThruOpen.restype = ctypes.c_long
        dev = c_ulong(0)
        rc = lib.PassThruOpen(None, byref(dev))
        if rc == 0:
            lib.PassThruClose(dev)
    except (BusError, AttributeError, OSError):
        return DETECT_BASE + DETECT_NO_DRIVER
    return DETECT_BASE + (rc if 0 <= rc < DETECT_NO_DRIVER else ERR_FAILED)


def classify_detect(exit_code) -> str:
    """Helper exit code (None = it had to be stopped) -> one of DETECT_STATES."""
    if exit_code is None:
        return "no answer"
    rc = exit_code - DETECT_BASE
    if rc == 0:
        return "detected"
    if rc == ERR_DEVICE_IN_USE:
        return "in use"
    if rc == ERR_TIMEOUT:
        return "no answer"
    return "not detected"


def run_detect(cmd, timeout: float = 5.0) -> str:
    """Run one helper command line and classify the result."""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        return "not detected"
    try:
        return classify_detect(p.wait(timeout))
    except subprocess.TimeoutExpired:
        try:   # the one-file exe is a parent and a child process: stop the whole tree
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=flags, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()
        return "no answer"
