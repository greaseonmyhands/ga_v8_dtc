"""Polls diagnostic PIDs: OBD Mode 01 and GM ReadDataByIdentifier ($22), one request at a
time per module, single-frame replies only (1-4 data bytes), the way the CAN display's
PollEngine does it.

Pure logic: the caller sends the frames it returns and feeds it every received frame.
Nothing here transmits on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REPLY_TIMEOUT_S = 0.25         # the window drains frames every ~50 ms, so allow for that
BACKOFF_AFTER = 5          # failures in a row before a PID is rested
BACKOFF_S = 2.0
NRC_NOT_SUPPORTED = (0x11, 0x12, 0x31)


@dataclass
class PollItem:
    key: object
    req: int
    resp: int
    service: int               # 0x22 or 0x01
    ident: int
    nbytes: int
    factor: float
    offset: float
    interval_s: float
    next_due: float = 0.0
    fails: int = 0
    unsupported: str = ""      # set once the module says it does not support this PID
    replies: int = 0

    def request(self) -> bytes:
        if self.service == 0x22:
            body = bytes([3, 0x22, self.ident >> 8, self.ident & 0xFF])
        else:
            body = bytes([2, 0x01, self.ident & 0xFF])
        return body + bytes(8 - len(body))             # padded to 8, as GMW3110 asks

    def matches(self, data: bytes) -> bool:
        if self.service == 0x22:
            return data[1] == 0x62 and len(data) >= 4 and data[2] == self.ident >> 8 and data[3] == self.ident & 0xFF
        return data[1] == 0x41 and len(data) >= 3 and data[2] == self.ident & 0xFF

    def value(self, data: bytes):
        start = 4 if self.service == 0x22 else 3
        raw = data[start:start + self.nbytes]
        if len(raw) < self.nbytes or (data[0] & 0x0F) < start - 1 + self.nbytes:
            return None
        return int.from_bytes(raw, "big") * self.factor + self.offset


@dataclass
class _Pending:
    item: PollItem
    sent_at: float


class Poller:
    def __init__(self, items, speed: float = 1.0):
        self.items = list(items)
        self.speed = speed                  # interval multiplier
        self.pending: dict = {}             # req id -> _Pending
        self.requests = 0
        self.timeouts = 0

    def due(self, now: float) -> list:
        """(req id, frame data) to send now: at most one per module."""
        out = []
        for req, p in list(self.pending.items()):
            if now - p.sent_at > REPLY_TIMEOUT_S:
                self._fail(p.item, now)
                self.timeouts += 1
                del self.pending[req]
        for req in sorted({i.req for i in self.items}):
            if req in self.pending:
                continue
            cands = [i for i in self.items if i.req == req and not i.unsupported and i.next_due <= now]
            if not cands:
                continue
            item = min(cands, key=lambda i: i.next_due)
            self.pending[req] = _Pending(item, now)
            item.next_due = now + item.interval_s * self.speed
            self.requests += 1
            out.append((req, item.request()))
        return out

    def _fail(self, item, now):
        item.fails += 1
        if item.fails >= BACKOFF_AFTER:
            item.next_due = now + BACKOFF_S

    def on_frame(self, arb_id: int, data: bytes, now: float):
        """A received frame. Returns (key, value) for a reply to a pending request, else None."""
        if len(data) < 3:
            return None
        for req, p in list(self.pending.items()):
            item = p.item
            if arb_id != item.resp:
                continue
            if data[1] == 0x7F and data[2] == item.service:
                nrc = data[3] if len(data) > 3 else 0
                if nrc == 0x78:                       # response pending: keep waiting
                    p.sent_at = now
                    return None
                del self.pending[req]
                if nrc in NRC_NOT_SUPPORTED:
                    item.unsupported = f"not supported (NRC {nrc:02X})"
                else:
                    self._fail(item, now)
                return None
            if item.matches(data):
                del self.pending[req]
                v = item.value(data)
                if v is None:
                    self._fail(item, now)
                    return None
                item.fails = 0
                item.replies += 1
                return item.key, v
        return None
