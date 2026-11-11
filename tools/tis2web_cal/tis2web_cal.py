#!/usr/bin/env python3
"""CAL PRO: calibration history for each module of a GM VIN, from the public TIS2Web
"SPS Info" page (https://tis2web.service.opel.com/tis2web/).

Personal, low-volume use. Drives a real, minimized Chrome/Edge window (Playwright, its own
persistent profile), one VIN at a time, clicking through the site's own wizard:
VIN -> controller -> function -> "Complete History". It does not get around any protection:
if a block page, CAPTCHA or login appears it stops and tells you.

    python tools/tis2web_cal/tis2web_cal.py <VIN> --list               # which controllers TIS has
    python tools/tis2web_cal/tis2web_cal.py <VIN> --modules P16,K20    # fetch only these
    python tools/tis2web_cal/tis2web_cal.py <VIN> [<VIN>...]           # fetch every controller
    python tools/tis2web_cal/tis2web_cal.py --from-html history.html   # parse a saved page
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "vin_rpo"))
import vin_rpo  # noqa: E402  (VIN validation, browser start-up, block detection)
from vin_rpo import BlockedError, NoResultError, validate_vin  # noqa: E402,F401

if getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent / "cal_pro_data"
    PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "cal_pro" / "profile"
else:
    DATA_DIR = HERE.parents[1] / "data" / "tis2web_cal"
    PROFILE_DIR = HERE / ".profile"

SITE_URL = "https://tis2web.service.opel.com/tis2web/"
PAUSE_BETWEEN_VINS_S = 5.0
STEP_TIMEOUT_S = 45


class SiteError(RuntimeError):
    """The site answered with one of its own error messages (e.g. E2769: Invalid VIN!)."""


# ----------------------------------------------------------------- tiny HTML tree

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs or {}), [], parent

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def text(self) -> str:
        out = []

        def walk(n):
            for c in n.children:
                if isinstance(c, str):
                    out.append(c)
                elif c.tag not in ("script", "style"):
                    out.append(" ")  # element boundaries separate words (cells, options)
                    walk(c)
                    out.append(" ")
        walk(self)
        return re.sub(r"\s+", " ", "".join(out).replace("\xa0", " ")).strip()

    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    def has_desc(self, tag) -> bool:
        return any(n.tag == tag for n in self.iter() if n is not self)


class _TreeBuilder(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("#root")

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in self.VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:  # ignore stray end tags
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_tree(html: str) -> Node:
    b = _TreeBuilder()
    b.feed(html)
    return b.root


# ------------------------------------------------------------------ page parsing

_ERR_RE = re.compile(r"^E\d{4}: \S")
_LABEL_RE = re.compile(r"\b(Module|Function|Controller):")


def classify_page(html: str) -> dict:
    """What step of the wizard a page is. Returns
    {kind: landing|controller|function|module|history|unknown, options: [...], selected,
     history_button: bool, error: "E2769: Invalid VIN!" | None, blocked: marker | None}"""
    root = parse_tree(html)
    text = root.text()
    title = next((n.text() for n in root.iter() if n.tag == "title"), "")
    sel = next((n for n in root.iter() if n.tag == "select"), None)
    opts = [(o.attrs.get("value", ""), o.text(), "selected" in o.attrs)
            for o in (sel.iter() if sel else []) if o.tag == "option"]
    buttons = [n.text() for n in root.iter() if n.tag == "button"]
    # Site errors sit in their own cell, e.g. <td style="color:red">E2769: Invalid VIN!</td>
    err = next((n.text() for n in root.iter() if n.tag == "td" and not n.has_desc("table")
                and _ERR_RE.match(n.text())), None)
    kind = "unknown"
    if "Calibration History for:" in text and any("parthistory" in n.classes() for n in root.iter()):
        kind = "history"
    elif sel is not None:
        lab = _LABEL_RE.search(text.split("Selected Options:")[0])
        kind = {"Module": "module", "Function": "function", "Controller": "controller"}.get(
            lab.group(1) if lab else "", "unknown")
    elif any("Get CAL ID" in b for b in buttons):
        kind = "landing"
    return {
        "kind": kind,
        "options": [t for v, t, s in opts if not t.lower().startswith("please select")],
        "selected": next((t for v, t, s in opts if s), None),
        "history_button": any("Complete History" in b for b in buttons),
        "error": err,
        "blocked": None if sel is not None or "Get CAL ID" in text
        else vin_rpo.looks_blocked(title, text),
    }


def _selected_options(root: Node) -> dict:
    out = {}
    for span in root.iter():
        if span.tag == "span" and "selectedoptionstable" in span.classes():
            for tr in span.iter():
                if tr.tag == "tr":
                    th = next((c for c in tr.children if isinstance(c, Node) and c.tag == "th"), None)
                    td = next((c for c in tr.children if isinstance(c, Node) and c.tag == "td"), None)
                    if th is not None and td is not None:
                        out[th.text().rstrip(":")] = td.text()
            break
    return out


def _part_table(table: Node) -> list[dict]:
    """Rows of a Part Number / CVN / [Bulletin #] / Description table, newest first."""
    keymap = {"part number": "part_number", "cvn": "cvn", "bulletin #": "bulletin",
              "description": "description"}
    cols, rows = [], []
    for tr in table.iter():
        if tr.tag != "tr":
            continue
        cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("th", "td")]
        if cells and all(c.tag == "th" for c in cells):
            cols = [keymap.get(c.text().lower(), c.text().lower()) for c in cells]
        elif cells and cols:
            row = {k: c.text() for k, c in zip(cols, cells)}
            for k in ("cvn", "bulletin"):
                if row.get(k) in ("N/A", "-", ""):
                    row[k] = None
            if row.get("part_number"):
                rows.append(row)
    return rows


def parse_history_html(html: str) -> dict:
    """Parse the "Complete History" page for one controller.

    -> {vin, controller, function, modules: [{module, note, options: [[row, ...], ...]}]}
    Each option is one valid calibration chain, newest part first. Usually one option per
    module; where TIS lists several (message M4455) the note says how to choose."""
    root = parse_tree(html)
    sel = _selected_options(root)
    modules, note, cur = [], None, None
    for n in root.iter():
        if n.tag == "td" and not n.has_desc("table"):
            t = n.text()
            if t.startswith("Calibration History for:"):
                b = next((x for x in n.iter() if x.tag == "b"), None)
                cur = {"module": (b.text() if b else t.split(":", 1)[1]).strip(),
                       "note": note, "options": []}
                modules.append(cur)
                note = None
            elif re.match(r"^[A-Z]\d{4}:", t):
                note = t
        elif n.tag == "span" and "parthistory" in n.classes() and cur is not None:
            table = next((x for x in n.iter() if x.tag == "table"), None)
            rows = _part_table(table) if table is not None else []
            if rows:
                cur["options"].append(rows)
    if not modules:
        raise NoResultError("no 'Calibration History for:' sections in the page")
    return {"vin": sel.get("VIN"), "controller": sel.get("Controller"),
            "function": sel.get("Function"), "modules": modules}


def parse_module_page(html: str) -> dict:
    """Fallback when a controller has no "Complete History" button: the module page shows
    the history of the selected module only."""
    root = parse_tree(html)
    info = classify_page(html)
    sel = _selected_options(root)
    table = next((t for t in root.iter() if t.tag == "table" and not t.has_desc("table")
                  and "Part Number" in t.text() and "CVN" in t.text()), None)
    rows = _part_table(table) if table is not None else []
    if not rows:
        raise NoResultError("no calibration table on the module page")
    return {"vin": sel.get("VIN"), "controller": sel.get("Controller"),
            "function": sel.get("Function"),
            "modules": [{"module": info["selected"] or "?", "note": "Only the module shown by "
                         "default; this controller had no Complete History page.",
                         "options": [rows]}]}


# ------------------------------------------------------------------ live browser

class Tis2Web:
    """Steps the site's wizard in an open Playwright page. One VIN at a time."""

    def __init__(self, page, vin: str):
        self.page, self.vin = page, vin

    def _state(self) -> dict:
        info = classify_page(self.page.content())
        if info["blocked"]:
            raise BlockedError(f"page looks like a block/CAPTCHA page (matched {info['blocked']!r})")
        return info

    def _wait_ready(self):
        # The site ignores a second click within 5 s of the last one (its own double-submit
        # guard). Waiting it out also keeps our pace gentle.
        try:
            self.page.wait_for_function("() => !window.flag", timeout=10_000)
        except Exception:
            pass

    def _click(self, label: str):
        self._wait_ready()
        btn = self.page.locator("button", has_text=label).first
        with self.page.expect_navigation(timeout=STEP_TIMEOUT_S * 1000) as nav:
            btn.click()
        resp = nav.value
        if resp is not None and resp.status in (401, 403, 407, 429, 503):
            raise BlockedError(f"site returned HTTP {resp.status}")
        return self._state()

    def _choose(self, text: str, label: str = "Next >"):
        self.page.select_option("select", label=text)
        return self._click(label)

    def start(self) -> list[str]:
        """Load the start page, submit the VIN; returns the controllers TIS lists."""
        resp = self.page.goto(SITE_URL, wait_until="domcontentloaded",
                              timeout=STEP_TIMEOUT_S * 1000)
        if resp is not None and resp.status in (401, 403, 407, 429, 503):
            raise BlockedError(f"site returned HTTP {resp.status}")
        info = self._state()
        if info["kind"] != "landing":
            raise NoResultError(f"unexpected start page ({info['kind']})")
        self.page.locator("input[type=text]").first.fill(self.vin)
        info = self._click("Get CAL ID")
        if info["error"]:
            raise SiteError(info["error"])
        if info["kind"] != "controller" or not info["options"]:
            raise NoResultError("the site listed no controllers for this VIN")
        return info["options"]

    def _back_to_controllers(self):
        for _ in range(3):
            info = self._state()
            if info["kind"] == "controller":
                return
            self._click("< Back")
        raise NoResultError("could not get back to the controller list")

    def fetch(self, controller: str) -> dict:
        """From the controller list: history for one controller, then back to the list."""
        info = self._choose(controller)
        if info["error"]:
            raise SiteError(info["error"])
        if info["kind"] != "function":
            raise NoResultError(f"expected the function step, got {info['kind']}")
        result = {"functions": info["options"], "modules": []}
        for i, fn in enumerate(info["options"]):
            if i:
                self._state()  # on the function page again after Back
            minfo = self._choose(fn)
            if minfo["kind"] != "module":
                raise NoResultError(f"expected the module step for {fn!r}, got {minfo['kind']}")
            if minfo["history_button"]:
                hist = parse_history_html(self._open_history())
            else:
                hist = parse_module_page(self.page.content())
            if hist.get("vin") and hist["vin"].upper() != self.vin:
                raise NoResultError(f"history page is for {hist['vin']}, not {self.vin}")
            for m in hist["modules"]:
                m["function"] = fn
            result["modules"].extend(hist["modules"])
            self._click("< Back")  # -> function page
        self._back_to_controllers()
        return result

    def _open_history(self) -> str:
        # "Complete History" posts the form into a window named "historycomplete". Give it
        # a frame of that name in this page so it lands here instead of a new window.
        self.page.evaluate("""() => { if (!document.querySelector('iframe[name=historycomplete]')) {
            const f = document.createElement('iframe'); f.name = 'historycomplete';
            f.style.cssText = 'width:100%;height:300px'; document.body.appendChild(f); } }""")
        self._wait_ready()
        self.page.locator("button", has_text="Complete History").first.click()
        deadline = time.time() + STEP_TIMEOUT_S
        while time.time() < deadline:
            fr = self.page.frame(name="historycomplete")
            if fr is not None:
                try:
                    html = fr.content()
                except Exception:
                    html = ""
                if "Calibration History for:" in html and "</form>" in html:
                    return html
                if html and vin_rpo.looks_blocked("", html):
                    raise BlockedError("the history page looks like a block/CAPTCHA page")
            self.page.wait_for_timeout(400)
        raise NoResultError("the Complete History page did not load")


def open_browser(playwright, minimized=True):
    return vin_rpo.open_browser(playwright, minimized=minimized, profile_dir=PROFILE_DIR)


def match_controllers(available: list[str], wanted: list[str] | None) -> list[str]:
    """--modules P16,K20,"cluster" -> matching names (code prefix or any word, case-free)."""
    if not wanted:
        return list(available)
    out = []
    for w in wanted:
        w = w.strip().lower()
        hits = [c for c in available if c.lower().split()[0] == w] or \
               [c for c in available if w in c.lower()]
        if not hits:
            raise ValueError(f"no controller matches {w!r}; available: {', '.join(available)}")
        out += [h for h in hits if h not in out]
    return out


def lookup(page, vin: str, controllers: list[str] | None = None, record: dict | None = None,
           refresh: bool = False, progress=None, should_stop=None) -> dict:
    """List the VIN's controllers (always) and fetch the wanted ones into `record`.

    controllers=None means none (list only); ["*"] means all. Controllers already in the
    record are skipped unless refresh. progress(event, controller, i, n, detail) is called
    as each one starts / finishes / fails (the record is updated in place before "done",
    so a caller can save it there). should_stop() is checked between controllers.
    Stops at a block page (BlockedError)."""
    say = progress or (lambda *a: None)
    rec = record or new_record(vin)
    tis = Tis2Web(page, vin)
    say("listing", None, 0, 0, "")
    rec["controllers_available"] = tis.start()
    rec["controllers_listed"] = _today()
    if not controllers:
        return rec
    names = rec["controllers_available"] if controllers == ["*"] else controllers
    todo = [c for c in names if refresh or c not in rec["controllers"]
            or rec["controllers"][c].get("error")]
    for i, c in enumerate(todo, 1):
        if should_stop and should_stop():
            say("stopped", c, i, len(todo), "")
            break
        say("start", c, i, len(todo), "")
        if c not in rec["controllers_available"]:
            rec["controllers"][c] = {"lookup_date": _today(), "error": "not listed by TIS"}
            say("error", c, i, len(todo), "not listed by TIS for this VIN")
            continue
        try:
            got = tis.fetch(c)
        except BlockedError:
            raise
        except Exception as e:  # one controller failing should not lose the rest
            rec["controllers"][c] = {"lookup_date": _today(), "error": str(e).splitlines()[0]}
            say("error", c, i, len(todo), str(e).splitlines()[0])
            try:  # start the wizard over so the next controller begins from a clean page
                tis.start()
            except BlockedError:
                raise
            except Exception:
                pass
            continue
        rec["controllers"][c] = {"lookup_date": _today(), **got}
        say("done", c, i, len(todo), f"{len(got['modules'])} modules")
    return rec


# ------------------------------------------------------------------- records / IO

def _today() -> str:
    return _dt.date.today().isoformat()


def new_record(vin: str) -> dict:
    return {"vin": vin, "source_url": SITE_URL, "lookup_date": _today(),
            "controllers_available": [], "controllers_listed": None, "controllers": {}}


def load_record(vin: str, data_dir: Path = DATA_DIR) -> dict | None:
    p = Path(data_dir) / f"{vin}.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def save_record(rec: dict, data_dir: Path = DATA_DIR) -> Path:
    rec["lookup_date"] = _today()
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    path = p / f"{rec['vin']}.json"
    path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return path


def csv_rows(rec: dict):
    yield ["vin", "controller", "function", "module", "option", "position", "current",
           "part_number", "cvn", "bulletin", "description", "lookup_date"]
    for cname, c in rec["controllers"].items():
        for m in c.get("modules") or []:
            for oi, opt in enumerate(m["options"], 1):
                for pi, row in enumerate(opt):
                    yield [rec["vin"], cname, m.get("function", ""), m["module"], oi, pi,
                           "yes" if pi == 0 else "", row["part_number"], row.get("cvn") or "",
                           row.get("bulletin") or "", row.get("description", ""),
                           c.get("lookup_date", "")]


def write_csv(rec: dict, path: Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(csv_rows(rec))


def print_record(rec: dict, out=None, only: list[str] | None = None) -> None:
    out = out or sys.stdout
    print(f"\n{rec['vin']}  ({len(rec['controllers_available'])} controllers listed by TIS)",
          file=out)
    for cname, c in rec["controllers"].items():
        if only and cname not in only:
            continue
        print(f"\n== {cname}  (looked up {c.get('lookup_date')})", file=out)
        if c.get("error"):
            print(f"   ERROR: {c['error']}", file=out)
            continue
        for m in c["modules"]:
            if m.get("note"):
                print(f"   note: {m['note']}", file=out)
            for oi, opt in enumerate(m["options"], 1):
                tag = f" (option {oi} of {len(m['options'])})" if len(m["options"]) > 1 else ""
                cur = opt[0]
                print(f"   {m['module']}{tag}", file=out)
                print(f"      {cur['part_number']}  CVN {cur.get('cvn') or '-':8}  "
                      f"{cur.get('description', '')}   <- current", file=out)
                for row in opt[1:]:
                    print(f"      {row['part_number']}  CVN {row.get('cvn') or '-':8}  "
                          f"{row.get('description', '')}", file=out)


# ----------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("vins", nargs="*", metavar="VIN")
    ap.add_argument("--list", action="store_true",
                    help="only list the controllers TIS has for the VIN")
    ap.add_argument("--modules", metavar="LIST",
                    help="comma-separated controllers to fetch, by code (P16) or name word "
                         "(cluster); default: all")
    ap.add_argument("--refresh", action="store_true",
                    help="fetch again even if already saved")
    ap.add_argument("--csv", action="store_true", help="also write <VIN>.csv")
    ap.add_argument("--out-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--show-browser", action="store_true", help="do not minimize the browser")
    ap.add_argument("--from-html", type=Path, metavar="FILE",
                    help="parse a saved Complete History page instead of opening the browser")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    if args.from_html:
        try:
            h = parse_history_html(args.from_html.read_text(encoding="utf-8", errors="replace"))
        except NoResultError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rec = new_record(h["vin"] or "UNKNOWN")
        rec["controllers"][h["controller"] or "?"] = {
            "lookup_date": _today(), "functions": [h["function"]], "modules": h["modules"]}
        print_record(rec)
        return 0

    if not args.vins:
        ap.error("give at least one VIN")
    try:
        vins = list(dict.fromkeys(validate_vin(v) for v in args.vins))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    wanted = [w for w in (args.modules or "").split(",") if w.strip()]

    # Cache: answer from saved files where possible, without opening the browser.
    todo = []
    for vin in vins:
        rec = load_record(vin, args.out_dir)
        if rec and not args.refresh:
            if args.list and rec["controllers_available"]:
                print(f"[saved] {vin}: " + "; ".join(rec["controllers_available"]))
                continue
            if not args.list and rec["controllers_available"]:
                try:
                    names = match_controllers(rec["controllers_available"], wanted)
                except ValueError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 2
                if all(n in rec["controllers"] and not rec["controllers"][n].get("error")
                       for n in names):
                    print(f"[saved] {args.out_dir / (vin + '.json')}")
                    print_record(rec, only=names)
                    if args.csv:
                        write_csv(rec, args.out_dir / f"{vin}.csv")
                    continue
        todo.append(vin)
    if not todo:
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: Playwright is not installed. Run:\n  pip install playwright",
              file=sys.stderr)
        return 2

    def progress(event, c, i, n, detail):
        if event == "listing":
            print("  listing controllers...", file=sys.stderr)
        elif event == "start":
            print(f"  [{i}/{n}] {c} ...", file=sys.stderr, flush=True)
        elif event == "done":
            print(f"  [{i}/{n}] {c}: {detail}", file=sys.stderr)
        elif event == "error":
            print(f"  [{i}/{n}] {c}: FAILED - {detail}", file=sys.stderr)

    failed = 0
    with sync_playwright() as pw:
        ctx, page = open_browser(pw, minimized=not args.show_browser)
        try:
            for k, vin in enumerate(todo):
                if k:
                    time.sleep(PAUSE_BETWEEN_VINS_S)
                print(f"\n{vin}:", file=sys.stderr)
                rec = (None if args.refresh else load_record(vin, args.out_dir)) or new_record(vin)
                try:
                    rec = lookup(page, vin, None, rec, progress=progress)
                    if args.list:
                        save_record(rec, args.out_dir)
                        print(f"{vin}: " + "; ".join(rec["controllers_available"]))
                        continue
                    names = match_controllers(rec["controllers_available"], wanted)
                    rec = lookup(page, vin, names, rec, refresh=args.refresh, progress=progress)
                except BlockedError as e:
                    vin_rpo.set_window_state(page, "normal")
                    print(f"\nSTOPPED at {vin}: {e}\nFinish by hand in the open browser window: "
                          f"{SITE_URL}", file=sys.stderr)
                    if sys.stdin.isatty():
                        input("Press Enter to close the browser... ")
                    return 3
                except (SiteError, NoResultError, ValueError) as e:
                    print(f"{vin}: {e}", file=sys.stderr)
                    failed += 1
                    continue
                path = save_record(rec, args.out_dir)
                print_record(rec, only=names)
                print(f"  -> {path}")
                if args.csv:
                    write_csv(rec, path.with_suffix(".csv"))
                failed += any(rec["controllers"][n].get("error") for n in names)
        finally:
            ctx.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
