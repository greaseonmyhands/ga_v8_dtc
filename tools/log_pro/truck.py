"""The built-in "GM truck (CAN display)" signal set: the HS broadcast signals and polled PIDs
the silverado-can-display firmware logs, with its HP Tuners channel mapping.

Values come out in the display's units (US: mph, psi, F, lb-ft); signals that have an HP
Tuners channel come out in that channel's unit and are logged under its id and name, so
VCM Scanner shows them as its own channels.
"""
from __future__ import annotations

import dbc
from truck_catalog_data import DATA

NAME = "GM truck (CAN display)"
SETTINGS_PATH = "builtin:truck"

GEAR = {13: "N", 14: "R", 15: "P"}
FUEL_LOOP = {1: "Open (not warm)", 2: "Closed", 4: "Open (load/decel)", 8: "Open (fault)",
             16: "Closed (fault)"}


# The display shows the BCM tire polls on its low-speed tire tiles, so the poll catalog has no
# label of its own for them. Factor 0.00906486 = 0.0625 kPa per bit in psi.
_FIXUPS = {f"bcmTire{w}": {"label": f"Tire Pressure {w}", "unit": "psi", "category": "Tires"}
           for w in ("LF", "RF", "LR", "RR")}
for _e in DATA["polled"]:
    if _e["key"] in _FIXUPS and _e["label"] == _e["key"]:
        _e.update(_FIXUPS[_e["key"]])


def _gear(v):
    v = int(round(v))
    return GEAR.get(v) or (f"Gear {v}" if 1 <= v <= 10 else None)


def _onoff(v):
    return "On" if round(v) else "Off"


ENUMS = {"gearText": _gear, "onOffText": _onoff,
         "fuelLoopText": lambda v: FUEL_LOOP.get(int(round(v)))}


def _unit(u: str) -> str:
    return {"F": "°F", "deg": "°", "lb-ft": "lb·ft", "L/hr": "L/h"}.get(u, u)


def _signal(e: dict, start=0, length=8, little=False, signed=False, factor=1.0, offset=0.0) -> dbc.Signal:
    """A dbc.Signal whose decode gives the display value, or the HP Tuners value when the
    signal has an HP Tuners channel."""
    scale, disp_off = e["scale"], e["disp_offset"]
    unit, name, hpt_id, hscale = _unit(e["unit"]), e["label"], 0, 1.0
    if "hpt" in e:
        h = e["hpt"]
        unit, name, hpt_id, hscale = h["unit"], h["name"], h["id"], h["scale"]
    f = factor * scale * hscale
    o = (offset * scale + disp_off) * hscale
    return dbc.Signal(e["key"], start, length, little, signed, f, o, None, None, unit,
                      label=name, hpt_id=hpt_id, enum_text=ENUMS.get(e.get("enum") or ""))


def build() -> dbc.Dbc:
    messages = {}
    for e in DATA["broadcast"]:
        m = messages.get(e["id"])
        if m is None:
            m = messages[e["id"]] = dbc.Message(e["id"], False, f"HS {e['id']:03X}", 8, "")
        s = _signal(e, e["start"], e["length"], e["little"], e["signed"], e["factor"], e["offset"])
        s.message = m
        m.signals.append(s)
    polled = []
    for e in DATA["polled"]:
        svc = "Mode 01 PID" if e["service"] == 0x01 else "$22 DID"
        ident = f"{e['ident']:02X}" if e["service"] == 0x01 else f"{e['ident']:04X}"
        m = dbc.Message(e["resp"], False, f"Polled: {svc} {ident} to {e['req']:03X}", 8, "")
        s = _signal(e, factor=e["factor"], offset=e["offset"], length=8 * e["nbytes"])
        s.poll = {k: e[k] for k in ("req", "resp", "service", "ident", "nbytes", "interval_ms")}
        s.message = m
        m.signals.append(s)
        polled.append(m)
    return dbc.Dbc(NAME, list(messages.values()) + polled)


def modules() -> dict:
    """Module name -> {'title', 'req', 'resp'} for the controls."""
    return {m["name"]: m for m in DATA["modules"]}


def controls() -> list:
    return list(DATA["controls"])


def label(key: str) -> str:
    """The display's label for a signal key (broadcast or polled), or the key itself."""
    for e in DATA["broadcast"] + DATA["polled"]:
        if e["key"] == key:
            return e["label"]
    return key


def broadcast_signal(key: str) -> dbc.Signal:
    """A built-in broadcast signal by its display key, decoding to display units (not HP
    Tuners units): used by the controls' safety checks."""
    e = next(x for x in DATA["broadcast"] if x["key"] == key)
    e = dict(e)
    e.pop("hpt", None)
    s = _signal(e, e["start"], e["length"], e["little"], e["signed"], e["factor"], e["offset"])
    s.message = dbc.Message(e["id"], False, "", 8, "")
    return s
