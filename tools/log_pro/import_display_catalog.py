"""Regenerate truck_catalog_data.py from the CAN display firmware's own tables.

    python tools/log_pro/import_display_catalog.py [path to silverado-can-display]

Reads (never writes) these files in firmware/tab5_can_dashboard:
  BroadcastCatalog.h  HS broadcast signals: CAN id, start bit, length, byte order, scaling
  PolledCatalog.h     polled PIDs: module ids, service ($22 / Mode 01), identifier, scaling
  SignalCatalog.cpp   labels, categories, display units and the display-unit conversion
  HptCsv.cpp          which signals have an HP Tuners channel: id, name, unit, scale
  VehicleControl.cpp  the bidirectional controls ($AE) and which are confirmed on the truck
"""
from __future__ import annotations

import pprint
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = Path(r"C:\Users\Keegan\code\silverado-can-display")
OUT = HERE / "truck_catalog_data.py"

CONSTS = {"KPA_TO_PSI": 0.1450377, "C_TO_F_SCALE": 1.8, "NM_TO_LBFT": 0.7375621,
          "GPS_TO_LBH": 7.936641, "KMH_TO_MPH": 0.621371}


def num(tok: str) -> float:
    t = tok.strip().rstrip("fF")
    if t in CONSTS:
        return CONSTS[t]
    m = re.fullmatch(r"([\d.eE+-]+)f?\s*/\s*([\d.eE+-]+)f?", tok.strip())
    if m:
        return float(m[1]) / float(m[2])
    return float(t)


def c_unescape(s: str) -> str:
    """C string literal pieces ("\\xC2\\xB0" "F") -> text."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', s)
    raw = bytearray()
    for p in parts:
        i = 0
        while i < len(p):
            if p[i] == "\\" and p[i + 1] == "x":
                raw.append(int(p[i + 2:i + 4], 16))
                i += 4
            else:
                raw.extend(p[i].encode())
                i += 1
    return raw.decode("utf-8")


def rows(text: str, start_marker: str):
    body = text[text.index(start_marker):]
    body = body[:body.index("\n};")]
    for line in body.splitlines():
        line = line.split("//")[0].strip()
        if line.startswith("{\"") and line.endswith(("},", "}")):
            yield line.rstrip(",")[1:-1]


def split_args(s: str):
    out, depth, cur, q = [], 0, "", False
    for ch in s:
        if ch == '"':
            q = not q
        if not q and ch == "{":
            depth += 1
        if not q and ch == "}":
            depth -= 1
        if ch == "," and depth == 0 and not q:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def main(repo: Path):
    fw = repo / "firmware" / "tab5_can_dashboard"
    bc = (fw / "BroadcastCatalog.h").read_text("utf-8")
    pc = (fw / "PolledCatalog.h").read_text("utf-8")
    sc = (fw / "SignalCatalog.cpp").read_text("utf-8")
    hp = (fw / "HptCsv.cpp").read_text("utf-8")
    vc = (fw / "VehicleControl.cpp").read_text("utf-8")

    meta = {}
    for r in rows(sc, "kSignalCatalog[] ="):
        a = split_args(r)
        if len(a) < 11 or not a[1].startswith('"'):
            continue
        meta[a[0].strip('"')] = {
            "label": a[1].strip('"'), "unit": a[3].strip('"'), "category": a[4].strip('"'),
            "source": a[5].split("::")[-1], "scale": num(a[6]), "decimals": int(a[7]),
            "offset": num(a[11]) if len(a) > 11 else 0.0,
            "enum": a[12] if len(a) > 12 else None}

    hpt = {}
    for r in rows(hp, "kHptChannels[] ="):
        a = split_args(r)
        hpt[a[0].strip('"')] = {"id": int(a[1]), "name": c_unescape(a[2]), "unit": c_unescape(a[3]),
                                "scale": num(a[4])}

    broadcast = []
    for r in rows(bc, "kBroadcastCatalog[] ="):
        a = split_args(r)
        broadcast.append({"key": a[0].strip('"'), "id": int(a[1], 16), "start": int(a[2]),
                          "length": int(a[3]), "little": a[4] == "true", "signed": a[5] == "true",
                          "factor": num(a[6]), "offset": num(a[7])})

    polled = []
    for r in rows(pc, "kPolledCatalog[] ="):
        a = split_args(r)
        polled.append({"key": a[0].strip('"'), "req": int(a[1], 16), "resp": int(a[2], 16),
                       "service": 0x22 if "ReadDataByIdentifier" in a[3] else 0x01,
                       "ident": int(a[4], 16), "nbytes": int(a[5]), "factor": num(a[6]),
                       "offset": num(a[7]), "interval_ms": int(a[8])})

    modules = {}
    for m in re.finditer(r'\{"(\w+)", k(\w+)RequestId, k\w+ResponseId, "([^"]+)"\}', vc + "\n" +
                         (fw / "VehicleControl.h").read_text("utf-8")):
        modules.setdefault(m[1], m[3])
    ids = dict(re.findall(r"k(Ecm|Bcm|Tcm)(Request|Response)Id = (0x[0-9A-Fa-f]+)",
                          (fw / "VehicleControl.h").read_text("utf-8")) and
               [((g[0] + g[1]), int(g[2], 16)) for g in re.findall(
                   r"k(Ecm|Bcm|Tcm)(Request|Response)Id = (0x[0-9A-Fa-f]+)",
                   (fw / "VehicleControl.h").read_text("utf-8"))])
    module_list = [{"name": n.upper(), "title": modules.get(n.upper(), n.upper()),
                    "req": ids[n + "Request"], "resp": ids[n + "Response"]}
                   for n in ("Ecm", "Bcm", "Tcm")]

    controls = []
    for r in rows(vc, "kControls[] ="):
        a = split_args(r)
        b = [int(x, 16) for x in a[3].strip("{}").split(",")]
        mod = a[9].strip() if len(a) > 9 else "kModuleEcm"
        controls.append({
            "group": a[0].strip('"'), "label": a[1].strip('"'), "cpid": int(a[2], 16),
            "bytes": b[:int(a[4])], "max_run_s": int(a[5]),
            "watch": None if a[6] == "nullptr" else a[6].strip('"'),
            "confirmed": len(a) > 7 and a[7] == "true",
            "engine_test": len(a) > 8 and a[8] == "true",
            "module": {"kModuleEcm": "ECM", "kModuleBcm": "BCM", "kModuleTcm": "TCM"}.get(mod, "ECM"),
            "hold_to_run": len(a) > 10 and a[10] == "true"})

    def entry(key, extra):
        m = meta.get(key, {})
        h = hpt.get(key)
        e = {"key": key, "label": m.get("label", key), "category": m.get("category", ""),
             "unit": m.get("unit", ""), "scale": m.get("scale", 1.0), "disp_offset": m.get("offset", 0.0)}
        if h:
            e["hpt"] = h
        e.update(extra)
        return e

    data = {
        "broadcast": [entry(b["key"], b) for b in broadcast],
        "polled": [entry(p["key"], p) for p in polled],
        "modules": module_list,
        "controls": controls,
    }
    head = ('"""Generated by import_display_catalog.py from the silverado-can-display firmware.\n'
            'Do not edit by hand; rerun the importer instead."""\n\n')
    OUT.write_text(head + "DATA = " + pprint.pformat(data, width=110, sort_dicts=False) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"wrote {OUT.name}: {len(broadcast)} broadcast, {len(polled)} polled "
          f"({sum(1 for k in [*map(lambda x: x['key'], broadcast), *map(lambda x: x['key'], polled)] if k in hpt)} "
          f"with an HP Tuners channel), {len(controls)} controls "
          f"({sum(c['confirmed'] for c in controls)} confirmed)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPO)
