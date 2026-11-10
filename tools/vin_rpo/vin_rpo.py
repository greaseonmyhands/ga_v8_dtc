#!/usr/bin/env python3
"""Look up the factory RPO codes for a GM VIN via gmpartsgiant.com's free VIN decoder.

Personal, low-volume use. Drives a real, visible Chrome window (Playwright, persistent
profile), one VIN at a time, and reads what the decoder page itself loads. It does not
solve CAPTCHAs or try to get around bot protection: if the site shows a block page,
CAPTCHA or login wall, it stops and tells you so you can finish that lookup by hand.

    python tools/vin_rpo/vin_rpo.py <VIN> [<VIN>...] [--csv] [--refresh]
    python tools/vin_rpo/vin_rpo.py --from-html saved_page.html      (offline parse)
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "vin_rpo"
PROFILE_DIR = Path(__file__).resolve().parent / ".profile"

DECODER_URL = "https://www.gmpartsgiant.com/vin-decoder.html"
API_PATH = "/api/vehicle/mul/decode-vin-attributes"

# Rows of "Other Specifications" that are catalog metadata, not RPO codes.
NON_RPO_CATEGORIES = {"CATALOG", "MD", "MK", "YEAR_FROM", "YEAR_TO"}

_RPO_RE = re.compile(r"^[A-Z0-9]{3}$")
# One row can hold several codes: "1SZ-PACKAGE OPTION-;R9N-CONTROL - SALES ITEM NO. 89"
_MULTI_SPLIT_RE = re.compile(r";(?=\s*[A-Z0-9]{3}-)")
_BLOCK_MARKERS = (
    "captcha", "are you a human", "verify you are human", "access denied",
    "unusual traffic", "attention required", "just a moment", "request blocked",
)


class BlockedError(RuntimeError):
    """The site showed a CAPTCHA / block page / login wall. Stop; do not work around it."""


class NoResultError(RuntimeError):
    """The decoder answered but had no data for this VIN."""


# --------------------------------------------------------------------------- VIN

_TRANSLIT = {**{str(d): d for d in range(10)},
             **dict(zip("ABCDEFGH", range(1, 9))),
             **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9,
             **dict(zip("STUVWXYZ", range(2, 10)))}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check_digit(vin: str) -> str:
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS))
    r = total % 11
    return "X" if r == 10 else str(r)


def validate_vin(vin: str) -> str:
    """Return the normalised (upper-case) VIN, or raise ValueError saying what is wrong."""
    v = vin.strip().upper()
    if len(v) != 17:
        raise ValueError(f"{vin!r}: VIN must be 17 characters, got {len(v)}")
    bad = sorted({c for c in v if c not in _TRANSLIT})
    if bad:
        raise ValueError(f"{vin!r}: invalid character(s) {''.join(bad)} (I, O and Q are never used)")
    want = vin_check_digit(v)
    if v[8] != want:
        raise ValueError(f"{vin!r}: check digit is {v[8]!r}, expected {want!r} (typo?)")
    return v


# ------------------------------------------------------------------------ parsing

def split_spec_row(category: str, desc: str) -> list[dict]:
    """'WNT', 'AKO-WINDOW TYPE PRIVACY' -> [{'code': 'AKO', 'description': ..., 'category': 'WNT'}]"""
    out = []
    for piece in _MULTI_SPLIT_RE.split(desc or ""):
        piece = piece.strip()
        if not piece:
            continue
        code, sep, text = piece.partition("-")
        code = code.strip().upper()
        text = text.strip().rstrip("-").strip()
        # No description => site metadata ("MRKT_GME: OPM-", "CC: ZZ-"), not an RPO.
        if not sep or not text or not _RPO_RE.match(code):
            continue
        out.append({"code": code, "description": text, "category": category})
    return out


def _build(vin, vehicle, info_rows, major_rows, spec_rows) -> dict:
    catalog, codes = {}, []
    for name, desc in spec_rows:
        name = (name or "").strip()
        row = [] if name.upper() in NON_RPO_CATEGORIES else split_spec_row(name, desc)
        if row:
            codes.extend(row)
        else:
            catalog[name] = (desc or "").strip().rstrip("-")
    return {
        "vin": (vin or "").strip().upper() or None,
        "vehicle": (vehicle or "").strip() or None,
        "vehicle_information": {n.strip(): (d or "").strip() for n, d in info_rows},
        "major_attributes": {n.strip(): (d or "").strip() for n, d in major_rows},
        "catalog": catalog,
        "codes": codes,
    }


def parse_api_json(payload: dict, vin: str | None = None) -> dict:
    """Parse the body of the page's own POST /api/vehicle/mul/decode-vin-attributes response."""
    infos = ((payload or {}).get("data") or {}).get("vinInfos") or []
    if not infos:
        raise NoResultError(f"decoder returned no vehicle (message: {(payload or {}).get('message')!r})")
    info = infos[0]
    rows = lambda key: [(r.get("name") or "", r.get("desc") or "") for r in info.get(key) or []]
    if not vin:
        m = re.search(r"[A-HJ-NPR-Z0-9]{17}", info.get("redirectUrl") or "")
        vin = m.group(0) if m else None
    result = _build(vin, info.get("vehicleInfo"), rows("vehicleInformation"),
                    rows("majorAttribute"), rows("specification"))
    if not result["codes"]:
        raise NoResultError("decoder returned a vehicle but no option rows")
    return result


class _DecoderHTML(HTMLParser):
    """Pulls <li><b>name</b> <span>desc</span></li> rows out of each div.decoded-detail."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set]] = []
        self.sections: list[dict] = []
        self.page_text: list[str] = []
        self.title = ""
        self._sec = None
        self._sec_depth = None
        self._row = None
        self._field = None  # "title" | "name" | "desc" | "pagetitle"

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        classes = set((dict(attrs).get("class") or "").split())
        self.stack.append((tag, classes))
        if tag == "title":
            self._field = "pagetitle"
        elif tag == "div" and "decoded-detail" in classes:
            self._sec = {"title": "", "rows": []}
            self._sec_depth = len(self.stack)
            self.sections.append(self._sec)
        elif self._sec is not None:
            if "decoded-detail-title" in classes:
                self._field = "title"
            elif tag == "li":
                self._row = ["", ""]
            elif self._row is not None and tag == "b":
                self._field = "name"
            elif self._row is not None and tag == "span":
                self._field = "desc"

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break
        if tag in ("b", "span", "title") or (tag == "div" and self._field == "title"):
            self._field = None
        if tag == "li" and self._row is not None and self._sec is not None:
            if self._row[0].strip():
                self._sec["rows"].append((self._row[0].strip(), self._row[1].strip()))
            self._row = None
        if self._sec is not None and len(self.stack) < self._sec_depth:
            self._sec = self._sec_depth = None

    def handle_data(self, data):
        if self._field == "pagetitle":
            self.title += data
        elif self._field == "title" and self._sec is not None:
            self._sec["title"] += data
        elif self._field == "name" and self._row is not None:
            self._row[0] += data
        elif self._field == "desc" and self._row is not None:
            self._row[1] += data
        if not any(t in ("script", "style") for t, _ in self.stack):
            self.page_text.append(data)


def looks_blocked(title: str, text: str) -> str | None:
    hay = f"{title}\n{text[:5000]}".lower()
    for m in _BLOCK_MARKERS:
        if m in hay:
            return m
    return None


def parse_html(html: str, vin: str | None = None) -> dict:
    """Parse a saved copy of the rendered results page (vin-decoder.html?vin=...)."""
    p = _DecoderHTML()
    p.feed(html)
    text = " ".join(p.page_text)
    by_title = {s["title"].strip().lower(): s["rows"] for s in p.sections}
    spec = by_title.get("other specifications", [])
    if not spec:
        marker = looks_blocked(p.title, text)
        if marker:
            raise BlockedError(f"page looks like a block/CAPTCHA page (matched {marker!r})")
        raise NoResultError("no 'Other Specifications' rows in the page")
    if not vin:
        m = re.search(r"Your VIN:\s*([A-HJ-NPR-Z0-9]{17})", text, re.I)
        vin = m.group(1) if m else None
    info = by_title.get("vehicle information", [])
    vehicle = dict(info).get("Vehicle")
    result = _build(vin, vehicle, info, by_title.get("major attribute", []), spec)
    if not result["codes"]:
        raise NoResultError("results section present but no RPO rows parsed")
    return result


# --------------------------------------------------------------------------- live

def lookup_live(page, vin: str, timeout_s: float = 45) -> dict:
    """Load the decoder page for one VIN in an already-open Playwright page.

    Passively reads the page's own API response; falls back to the rendered DOM.
    Never calls the API directly.
    """
    url = f"{DECODER_URL}?vin={vin}"
    captured = {}

    def on_response(resp):
        if API_PATH in resp.url and "json" not in captured:
            captured["status"] = resp.status
            try:
                captured["json"] = resp.json()
            except Exception:
                captured["json"] = None

    page.on("response", on_response)
    try:
        nav = page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        if nav is not None and nav.status in (401, 403, 407, 429, 503):
            raise BlockedError(f"decoder page returned HTTP {nav.status}")
        if "vin-decoder" not in page.url:
            raise BlockedError(f"redirected away from the decoder to {page.url}")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if captured.get("status") in (401, 403, 407, 429, 503):
                raise BlockedError(f"decoder API returned HTTP {captured['status']}")
            if captured.get("json") is not None:
                break
            if page.locator(".vin-decoder-sr .decoded-detail li").count() > 0:
                break
            marker = looks_blocked(page.title(), page.inner_text("body")[:5000])
            if marker:
                raise BlockedError(f"page looks like a block/CAPTCHA page (matched {marker!r})")
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("response", on_response)

    if captured.get("json") is not None:
        result = parse_api_json(captured["json"], vin)
        result["source"] = "api-response"
    else:
        result = parse_html(page.content(), vin)  # raises BlockedError / NoResultError
        result["source"] = "dom"
    return result


def open_browser(playwright):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome", headless=False, no_viewport=True)
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


# ---------------------------------------------------------------------------- CLI

def finish(result: dict, vin: str, source_url: str) -> dict:
    ordered = {"vin": vin, "lookup_date": _dt.date.today().isoformat(),
               "source_url": source_url, "source": result.get("source")}
    ordered.update({k: v for k, v in result.items() if k not in ordered})
    ordered["vin"] = vin
    return ordered


def print_result(r: dict, out=None) -> None:
    out = out or sys.stdout
    print(f"\n{r['vin']}  {r.get('vehicle') or ''}", file=out)
    for k, v in (r.get("major_attributes") or {}).items():
        print(f"  {k}: {v}", file=out)
    print(f"  {len(r['codes'])} RPO codes (looked up {r.get('lookup_date')}):", file=out)
    for c in r["codes"]:
        print(f"  {c['code']}  {c['description']}", file=out)


def write_csv(r: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vin", "code", "category", "description"])
        for c in r["codes"]:
            w.writerow([r["vin"], c["code"], c["category"], c["description"]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("vins", nargs="*", metavar="VIN")
    ap.add_argument("--csv", action="store_true", help="also write data/vin_rpo/<VIN>.csv")
    ap.add_argument("--refresh", action="store_true", help="look up again even if cached")
    ap.add_argument("--delay", type=float, default=5.0,
                    help="seconds to pause between live lookups (default 5, minimum 3)")
    ap.add_argument("--out-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--from-html", type=Path, metavar="FILE",
                    help="parse a saved results page instead of opening the browser")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    if args.from_html:
        try:
            r = parse_html(args.from_html.read_text(encoding="utf-8", errors="replace"),
                           args.vins[0].upper() if args.vins else None)
        except (BlockedError, NoResultError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        r = finish(r, r["vin"] or "UNKNOWN", str(args.from_html))
        r["source"] = "html-file"
        print_result(r)
        return 0

    if not args.vins:
        ap.error("give at least one VIN")
    try:
        vins = list(dict.fromkeys(validate_vin(v) for v in args.vins))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    todo, failed = [], []
    for vin in vins:
        path = args.out_dir / f"{vin}.json"
        if path.exists() and not args.refresh:
            r = json.loads(path.read_text(encoding="utf-8"))
            print(f"[cached] {path}")
            print_result(r)
            if args.csv:
                write_csv(r, path.with_suffix(".csv"))
        else:
            todo.append(vin)
    if not todo:
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: Playwright is not installed. Run:\n"
              "  pip install playwright\n  playwright install chrome", file=sys.stderr)
        return 2

    delay = max(args.delay, 3.0)
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        try:
            for i, vin in enumerate(todo):
                if i:
                    time.sleep(delay)
                try:
                    r = lookup_live(page, vin)
                except BlockedError as e:
                    left = todo[i:]
                    print(f"\nSTOPPED at {vin}: {e}\n"
                          "The site is asking for something this tool will not do (CAPTCHA / "
                          "block page / login).\nFinish this lookup by hand in the open Chrome "
                          f"window: {DECODER_URL}?vin={vin}\n"
                          f"Not looked up: {' '.join(left)}", file=sys.stderr)
                    if sys.stdin.isatty():
                        input("Press Enter to close the browser... ")
                    return 3
                except NoResultError as e:
                    print(f"\n{vin}: no result - {e}", file=sys.stderr)
                    failed.append(vin)
                    continue
                r = finish(r, vin, f"{DECODER_URL}?vin={vin}")
                path = args.out_dir / f"{vin}.json"
                path.write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
                print_result(r)
                print(f"  -> {path}")
                if args.csv:
                    write_csv(r, path.with_suffix(".csv"))
        finally:
            ctx.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
