"""Small DBC reader and signal decoder for LOG PRO. No dependencies.

Reads messages (BO_), signals (SG_), value tables (VAL_) and float markers (SIG_VALTYPE_).
Handles Intel and Motorola byte order, signed values, scale/offset and simple multiplexing.

GMLAN note: some GM low-speed DBCs write a 29-bit ID as the parameter ID only (priority
and sender bits zero, e.g. 0x0210000 for a frame seen on the bus as 0x10210040). Such
messages match any frame with the same parameter ID.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

GMLAN_PARAM_MASK = 0x03FFE000          # bits 13..25 of a 29-bit GMLAN arbitration ID

_BO = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\S+)")
_SG = re.compile(
    r"^\s*SG_\s+(\w+)\s*(M|m\d+M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
    r"\(\s*([^,\s]+)\s*,\s*([^)\s]+)\s*\)\s*\[\s*([^|\s]*)\s*\|\s*([^\]\s]*)\s*\]\s*\"([^\"]*)\"")
_VAL = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s+(.*);\s*$")
_VAL_ITEM = re.compile(r"(-?\d+)\s+\"([^\"]*)\"")
_VALTYPE = re.compile(r"^SIG_VALTYPE_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s*;")


class DbcError(Exception):
    pass


@dataclass
class Signal:
    name: str
    start: int
    length: int
    little: bool                 # True = Intel (@1), False = Motorola (@0)
    signed: bool
    factor: float
    offset: float
    minimum: float | None
    maximum: float | None
    unit: str
    mux: object = None           # None, "M" (the multiplexor) or an int (shown when mux == it)
    is_float: bool = False
    choices: dict = field(default_factory=dict)   # raw value -> text
    message: "Message" = None
    label: str = ""              # display name, when it differs from the DBC name
    hpt_id: int = 0              # HP Tuners channel id, when the signal has one
    poll: dict = None            # set for a polled PID: req, resp, service, ident, nbytes, interval_ms
    enum_text: object = None     # value -> text (built-in signals)

    @property
    def is_integer(self) -> bool:
        return (not self.is_float and float(self.factor).is_integer()
                and float(self.offset).is_integer())

    def raw(self, data: bytes):
        """Raw unsigned/signed integer (or float) from the frame data, or None if the
        frame is too short to hold this signal."""
        nbits = len(data) * 8
        if self.little:
            if self.start + self.length > nbits:
                return None
            v = (int.from_bytes(data, "little") >> self.start) & ((1 << self.length) - 1)
        else:
            pos = (self.start // 8) * 8 + (7 - self.start % 8)     # bits from the frame's MSB
            if pos + self.length > nbits:
                return None
            v = (int.from_bytes(data, "big") >> (nbits - pos - self.length)) & ((1 << self.length) - 1)
        if self.is_float:
            if self.length == 32:
                return struct.unpack(">f", v.to_bytes(4, "big"))[0]
            if self.length == 64:
                return struct.unpack(">d", v.to_bytes(8, "big"))[0]
        if self.signed and v >> (self.length - 1):
            v -= 1 << self.length
        return v

    def decode(self, data: bytes):
        """Physical value (raw * factor + offset), or None if the frame is too short."""
        v = self.raw(data)
        if v is None:
            return None
        if self.is_integer:
            return int(v) * int(self.factor) + int(self.offset)
        return v * self.factor + self.offset

    def text(self, value) -> str:
        """Value as shown on screen: number, plus the VAL_ label when there is one."""
        if value is None:
            return ""
        if self.enum_text:
            t = self.enum_text(value)
            if t:
                return f"{value:g}  ({t})"
        if isinstance(value, int):
            label = self.choices.get(value) if self.factor == 1 and self.offset == 0 else None
            return f"{value}  ({label})" if label else str(value)
        return f"{value:.6g}"

    @property
    def display_name(self) -> str:
        return self.label or self.name


@dataclass
class Message:
    frame_id: int                # without the DBC's extended-ID flag bit
    ext: bool
    name: str
    dlc: int
    sender: str
    signals: list = field(default_factory=list)

    @property
    def id_text(self) -> str:
        return f"{self.frame_id:08X}" if self.ext else f"{self.frame_id:03X}"

    @property
    def param_only(self) -> bool:
        """29-bit GMLAN ID written as the parameter ID alone (see module docstring)."""
        return self.ext and self.frame_id != 0 and (self.frame_id & ~GMLAN_PARAM_MASK) == 0

    def multiplexor(self):
        return next((s for s in self.signals if s.mux == "M"), None)

    def decode(self, data: bytes) -> dict:
        """{signal name: value} for every signal present in this frame."""
        out = {}
        mux = self.multiplexor()
        mux_val = mux.raw(data) if mux else None
        for s in self.signals:
            if isinstance(s.mux, int) and s.mux != mux_val:
                continue
            v = s.decode(data)
            if v is not None:
                out[s.name] = v
        return out


class Dbc:
    def __init__(self, name: str, messages: list):
        self.name = name
        self.messages = messages
        self._exact = {(m.ext, m.frame_id): m for m in messages}
        self._param = {m.frame_id: m for m in messages if m.param_only}

    @property
    def signal_count(self) -> int:
        return sum(len(m.signals) for m in self.messages)

    def lookup(self, arb_id: int, ext: bool):
        """The message for a frame's ID, or None."""
        m = self._exact.get((ext, arb_id))
        if m is None and ext and self._param:
            m = self._param.get(arb_id & GMLAN_PARAM_MASK)
        return m


def _num(text: str):
    try:
        return float(text)
    except ValueError:
        return None


def parse(text: str, name: str = "dbc") -> Dbc:
    messages, by_raw_id, cur = [], {}, None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        head = line.lstrip()[:13]
        if head.startswith("BO_ "):
            m = _BO.match(line.lstrip())
            if not m:
                continue
            raw_id = int(m[1])
            if raw_id == 0xC0000000:              # VECTOR__INDEPENDENT_SIG_MSG placeholder
                cur = None
                continue
            cur = Message(raw_id & 0x1FFFFFFF, bool(raw_id & 0x80000000), m[2], int(m[3]), m[4])
            messages.append(cur)
            by_raw_id[raw_id] = cur
        elif head.startswith("SG_ ") and cur is not None:
            m = _SG.match(line)
            if not m:
                continue
            mux = m[2]
            if mux and mux != "M":
                mux = int(re.sub(r"\D", "", mux))
            factor, offset = _num(m[7]), _num(m[8])
            if factor is None or offset is None or int(m[4]) == 0:
                continue
            sig = Signal(m[1], int(m[3]), int(m[4]), m[5] == "1", m[6] == "-", factor, offset,
                         _num(m[9]), _num(m[10]), m[11], mux or None, message=cur)
            cur.signals.append(sig)
        elif head.startswith("VAL_ "):
            while not line.rstrip().endswith(";") and i < len(lines):   # value tables can wrap
                line += " " + lines[i]
                i += 1
            m = _VAL.match(line.strip())
            if m and int(m[1]) in by_raw_id:
                sig = next((s for s in by_raw_id[int(m[1])].signals if s.name == m[2]), None)
                if sig:
                    sig.choices = {int(a): b for a, b in _VAL_ITEM.findall(m[3])}
        elif head.startswith("SIG_VALTYPE_"):
            m = _VALTYPE.match(line.strip())
            if m and int(m[1]) in by_raw_id and m[3] in ("1", "2"):
                sig = next((s for s in by_raw_id[int(m[1])].signals if s.name == m[2]), None)
                if sig:
                    sig.is_float = True
    if not messages:
        raise DbcError(f"{name}: no messages (BO_ lines) found; is this a DBC file?")
    return Dbc(name, messages)


def load(path) -> Dbc:
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    return parse(text, path.name)


def guess_bus(filename: str) -> str:
    """'LS', 'HS' or 'Any' from a DBC file name."""
    n = re.sub(r"[^a-z0-9]+", " ", filename.lower())
    words = n.split()
    if "ls" in words or "lowspeed" in n.replace(" ", "") or "low speed" in n or "swcan" in n.replace(" ", ""):
        return "LS"
    if "hs" in words or "highspeed" in n.replace(" ", "") or "high speed" in n or "powertrain" in n:
        return "HS"
    return "Any"
