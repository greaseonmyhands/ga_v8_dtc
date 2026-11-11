"""Window app for vin_rpo. Double-click this file (runs with pythonw, no console).

Same rules as the command line tool: one lookup at a time, a pause between VINs, cached
results are reused, and it stops if the site shows a CAPTCHA / block page.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vin_rpo  # noqa: E402

PAUSE_S = 5.0


class App(tk.Tk):
    def __init__(self, data_dir: Path = vin_rpo.DATA_DIR):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.title("GM VIN → RPO lookup")
        self.geometry("980x640")
        self.minsize(720, 420)
        try:  # window/taskbar icon; bundled next to the code (or unpacked by the exe)
            self.iconbitmap(str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
                                / "vin_rpo.ico"))
        except tk.TclError:
            pass
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.current = None

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="VIN(s):").pack(side="left")
        self.vin_var = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.vin_var, font=("Consolas", 12))
        self.entry.pack(side="left", fill="x", expand=True, padx=8)
        self.entry.bind("<Return>", lambda e: self.start())
        self.vin_var.trace_add("write", self.on_vin_edit)
        self.btn = ttk.Button(top, text="Look up", command=self.start)
        self.btn.pack(side="left")
        # Refresh: look the VIN up again even though a saved result exists.
        self.refresh_btn = ttk.Button(top, text="Refresh data", command=lambda: self.start(refresh=True))
        self.refresh_btn.pack(side="left", padx=(8, 0))
        self.csv_btn = ttk.Button(top, text="Save as CSV", command=self.save_csv, state="disabled")
        self.csv_btn.pack(side="left", padx=(8, 0))

        self.vin_hint = tk.Label(self, text="0 / 17", font=("Segoe UI", 10), anchor="w",
                                 fg="#666666", padx=62)
        self.vin_hint.pack(fill="x")

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)

        left = ttk.Frame(body)
        ttk.Label(left, text="Saved VINs").pack(anchor="w")
        self.saved = tk.Listbox(left, font=("Consolas", 10), width=20, exportselection=False)
        self.saved.pack(fill="both", expand=True)
        self.saved.bind("<<ListboxSelect>>", self.on_saved)
        ttk.Button(left, text="Open data folder", command=self.open_folder).pack(fill="x", pady=(6, 0))
        body.add(left, weight=0)

        right = ttk.Frame(body)
        self.head = ttk.Label(right, text="Enter a 17-character GM VIN and press Enter.",
                              wraplength=700, justify="left")
        self.head.pack(anchor="w", padx=(10, 0))
        # Engine / transmission: the site gives these only as text (no RPO rows), so make
        # them stand out.
        self.power = ttk.Label(right, text="", wraplength=700, justify="left",
                               font=("Segoe UI", 11, "bold"))
        self.power.pack(anchor="w", padx=(10, 0), pady=(4, 2))
        self.sub = ttk.Label(right, text="", wraplength=700, justify="left")
        self.sub.pack(anchor="w", padx=(10, 0))
        frow = ttk.Frame(right)
        frow.pack(fill="x", padx=(10, 0), pady=6)
        ttk.Label(frow, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill())
        self.filter_entry = ttk.Entry(frow, textvariable=self.filter_var, width=28)
        self.filter_entry.pack(side="left", padx=6)
        ttk.Button(frow, text="Copy codes", command=self.copy_codes).pack(side="left")
        self.cluster_var = tk.BooleanVar(value=False)
        # Same look as the other buttons; the label says what a click will do.
        self.cluster_btn = ttk.Button(frow, text="Cluster RPOs only", width=18,
                                      command=self.toggle_cluster)
        self.cluster_btn.pack(side="left", padx=8)
        self.count = ttk.Label(frow, text="")
        self.count.pack(side="left")
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, padx=(10, 0))
        self.table = tk.Text(tf, wrap="word", cursor="arrow", borderwidth=1, relief="solid",
                             padx=8, pady=6, spacing1=2, spacing3=2, tabs=("64p",),
                             font=("Segoe UI", 10))
        self.table.tag_configure("row", lmargin2="64p")
        self.table.tag_configure("code", font=("Consolas", 13, "bold"), foreground="#111111")
        self.table.tag_configure("cluster", foreground="#0b5cad")
        self.table.tag_configure("cat", font=("Segoe UI", 8), foreground="#9a9a9a")
        self.table.tag_configure("note", font=("Segoe UI", 9, "italic"), foreground="#8a5a00",
                                 lmargin2=0)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set, state="disabled")
        self.table.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body.add(right, weight=1)

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, padding=(10, 6), anchor="w").pack(fill="x")

        self.table.bind("<Button-1>", lambda e: self.table.focus_set(), add="+")
        self.table.bind("<Control-a>", lambda e: (self.table.tag_add("sel", "1.0", "end-1c"), "break")[1])
        for w in (self.entry, self.filter_entry):
            w.bind("<Button-3>", self.entry_menu)
        self.table.bind("<Button-3>", self.table_menu)
        self.saved.bind("<Button-3>", self.saved_menu)

        self.reload_saved()
        self.entry.focus_set()
        self.after(100, self.pump)

    # -------------------------------------------------------------- right-click
    def popup(self, event, items):
        m = tk.Menu(self, tearoff=0)
        for item in items:
            if item is None:
                m.add_separator()
            else:
                label, cmd, enabled = item
                m.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()
        return "break"

    def clip_has_text(self) -> bool:
        try:
            return bool(self.clipboard_get())
        except tk.TclError:
            return False

    def entry_menu(self, event):
        w = event.widget
        w.focus_set()
        sel = w.selection_present()
        return self.popup(event, [
            ("Cut", lambda: w.event_generate("<<Cut>>"), sel),
            ("Copy", lambda: w.event_generate("<<Copy>>"), sel),
            ("Paste", lambda: w.event_generate("<<Paste>>"), self.clip_has_text()),
            None,
            ("Select all", lambda: w.selection_range(0, "end"), bool(w.get())),
        ])

    def table_menu(self, event):
        t = self.table
        t.focus_set()
        sel = bool(t.tag_ranges("sel"))
        line = t.get(f"@{event.x},{event.y} linestart", f"@{event.x},{event.y} lineend")
        code = line.split("\t")[0] if "\t" in line else ""
        return self.popup(event, [
            ("Copy", lambda: self.set_clip(t.get("sel.first", "sel.last")), sel),
            (f"Copy code {code}" if code else "Copy code", lambda: self.set_clip(code), bool(code)),
            ("Copy this row", lambda: self.set_clip(line.replace("\t", "  ")), bool(code)),
            None,
            ("Copy all shown codes", self.copy_codes, bool(self.current)),
            ("Select all", lambda: t.tag_add("sel", "1.0", "end-1c"), bool(self.current)),
        ])

    def saved_menu(self, event):
        i = self.saved.nearest(event.y)
        if i < 0 or not self.saved.size():
            return "break"
        vin = self.saved.get(i)
        return self.popup(event, [(f"Copy {vin}", lambda: self.set_clip(vin), True)])

    def set_clip(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("Copied to the clipboard.")

    # ------------------------------------------------------------------ display
    def reload_saved(self):
        self.saved.delete(0, "end")
        if self.data_dir.is_dir():
            for p in sorted(self.data_dir.glob("*.json")):
                self.saved.insert("end", p.stem)

    def on_saved(self, _evt=None):
        sel = self.saved.curselection()
        if sel and not self.busy:
            self.show(json.loads((self.data_dir / f"{self.saved.get(sel[0])}.json").read_text("utf-8")))

    def show(self, r: dict):
        self.current = r
        self.csv_btn.config(state="normal")
        attrs = dict(r.get("major_attributes") or {})
        engine = attrs.pop("Engine", None) or "not listed"
        trans = attrs.pop("Transmission", None) or "not listed"
        self.head.config(text=f"{r['vin']}   {r.get('vehicle') or ''}")
        self.power.config(text=f"Engine:  {engine}\nTransmission:  {trans}")
        rest = [f"{k}: {v}" for k, v in attrs.items()]
        rest.append(f"{len(r['codes'])} RPO codes, looked up {r.get('lookup_date')}")
        self.sub.config(text="   ·   ".join(rest))
        self.fill()

    def toggle_cluster(self):
        on = not self.cluster_var.get()
        self.cluster_var.set(on)
        self.cluster_btn.config(text="Show all RPOs" if on else "Cluster RPOs only")
        self.fill()

    def visible_codes(self):
        if not self.current:
            return []
        needle = self.filter_var.get().strip().lower()
        only = self.cluster_var.get()
        return [c for c in self.current["codes"]
                if (not only or c["code"] in vin_rpo.CLUSTER_RPOS)
                and (not needle or needle in f"{c['code']} {c['category']} {c['description']}".lower())]

    def fill(self):
        t = self.table
        t.configure(state="normal")
        t.delete("1.0", "end")
        if self.current:
            only = self.cluster_var.get()
            rows = self.visible_codes()
            n_cluster = len(vin_rpo.cluster_codes(self.current))
            self.count.config(text=f"{len(rows)} shown · {n_cluster} cluster-relevant "
                                   f"of {len(self.current['codes'])}")
            if only:
                attrs = self.current.get("major_attributes") or {}
                t.insert("end", "Engine and transmission RPOs (LV3, MYC, ...) are not in this "
                                "site's option list, only as text: "
                                f"{attrs.get('Engine', '?')} / {attrs.get('Transmission', '?')}. "
                                "Check those codes yourself.\n", "note")
                if not rows:
                    t.insert("end", "None of the cluster-relevant RPOs are in this VIN's list.\n", "note")
            for c in rows:
                tags = ("code", "cluster", "row") if c["code"] in vin_rpo.CLUSTER_RPOS else ("code", "row")
                t.insert("end", c["code"], tags)
                t.insert("end", "\t" + c["description"], ("row",))
                t.insert("end", "   " + c["category"].lower() + "\n", ("cat", "row"))
        else:
            self.count.config(text="")
        t.configure(state="disabled")

    def on_vin_edit(self, *_):
        raw = self.vin_var.get()
        up = raw.upper()
        if up != raw:
            pos = self.entry.index("insert")
            self.vin_var.set(up)  # does not re-fire this trace
            self.entry.icursor(pos)
        toks = up.replace(",", " ").split()
        if not toks:
            return self.vin_hint.config(text="0 / 17", fg="#666666")
        parts, ok = [], True
        for v in toks:
            bad = "".join(sorted({c for c in v if c not in vin_rpo._TRANSLIT}))
            if bad:
                parts.append(f"{len(v)} / 17  ✖ '{bad}' not allowed in a VIN"); ok = False
            elif len(v) < 17:
                parts.append(f"{len(v)} / 17  ✖ {17 - len(v)} more needed"); ok = False
            elif len(v) > 17:
                parts.append(f"{len(v)} / 17  ✖ {len(v) - 17} too many"); ok = False
            elif vin_rpo.vin_check_digit(v) != v[8]:
                parts.append("17 / 17  ✖ check digit wrong - typo somewhere"); ok = False
            else:
                parts.append("17 / 17  ✔")
        self.vin_hint.config(text="     ".join(parts), fg="#1a7f37" if ok else "#000000")

    def copy_codes(self):
        if self.current:
            self.clipboard_clear()
            codes = [c["code"] for c in self.visible_codes()]
            self.clipboard_append(" ".join(codes))
            self.status.set(f"{len(codes)} RPO codes copied to the clipboard.")

    def open_folder(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.data_dir)  # Windows only

    # ------------------------------------------------------------------- lookup
    def save_csv(self):
        if not self.current:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / f"{self.current['vin']}.csv"
        try:
            vin_rpo.write_csv(self.current, path)
        except OSError as e:  # e.g. the CSV is open in Excel
            messagebox.showerror("Save as CSV", f"Could not write {path}\n\n{e}", parent=self)
            return
        self.status.set(f"Saved {path}")

    def start(self, refresh=False):
        if self.busy:
            return
        raw = self.vin_var.get().replace(",", " ").split()
        if not raw and refresh and self.current:
            raw = [self.current["vin"]]  # Refresh with an empty box redoes the VIN on screen
        if not raw:
            return
        if refresh and not messagebox.askokcancel(
                "Refresh data", "Look up " + ", ".join(v.upper() for v in raw) + " again on the "
                "site and replace the saved result?", parent=self):
            return
        try:
            vins = list(dict.fromkeys(vin_rpo.validate_vin(v) for v in raw))
        except ValueError as e:
            messagebox.showerror("Invalid VIN", str(e), parent=self)
            return
        self.busy = True
        self.btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        threading.Thread(target=self.work, daemon=True, args=(vins, refresh, False)).start()

    def work(self, vins, refresh, want_csv, minimized=True):
        # The browser always runs minimized here; it is restored if the site needs you.
        """Runs off the UI thread. Talks to the UI only through self.q."""
        say = lambda *m: self.q.put(m)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        todo = []
        for vin in vins:
            path = self.data_dir / f"{vin}.json"
            if path.exists() and not refresh:
                r = json.loads(path.read_text("utf-8"))
                if want_csv:
                    vin_rpo.write_csv(r, path.with_suffix(".csv"))
                say("result", r, f"{vin}: shown from saved file (no lookup made).")
            else:
                todo.append(vin)
        if not todo:
            return say("done", None)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return say("done", "Playwright is not installed.\n\nRun:\n  pip install playwright\n"
                               "  playwright install chrome")
        try:
            with sync_playwright() as pw:
                ctx, page = vin_rpo.open_browser(pw, minimized=minimized)
                try:
                    for i, vin in enumerate(todo):
                        if i:
                            say("status", f"Pausing {PAUSE_S:.0f} s before {vin}...")
                            time.sleep(PAUSE_S)
                        say("status", f"Looking up {vin} in the browser...")
                        try:
                            r = vin_rpo.lookup_live(page, vin)
                        except vin_rpo.BlockedError as e:
                            vin_rpo.set_window_state(page, "normal")
                            gate = threading.Event()
                            say("blocked", gate,
                                f"Stopped at {vin}: {e}\n\nThe site is asking for something this "
                                "tool will not do (CAPTCHA / block page / login). Finish this "
                                "lookup by hand in the browser window that just came up.\n\n"
                                f"Not looked up: {' '.join(todo[i:])}\n\n"
                                "Click OK when you are done to close that browser window.")
                            gate.wait()
                            return say("done", None)
                        except vin_rpo.NoResultError as e:
                            say("status", f"{vin}: no result - {e}")
                            continue
                        r = vin_rpo.finish(r, vin, f"{vin_rpo.DECODER_URL}?vin={vin}")
                        path = self.data_dir / f"{vin}.json"
                        path.write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
                        if want_csv:
                            vin_rpo.write_csv(r, path.with_suffix(".csv"))
                        say("result", r, f"{vin}: {len(r['codes'])} codes saved to {path}")
                finally:
                    ctx.close()
        except Exception as e:  # browser failed to start, window closed by hand, etc.
            return say("done", f"Lookup failed: {e}")
        say("done", None)

    def pump(self):
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == "status":
                    self.status.set(a[0])
                elif kind == "result":
                    self.show(a[0])
                    self.status.set(a[1])
                    self.reload_saved()
                elif kind == "blocked":
                    self.status.set("Stopped: the site showed a block page.")
                    messagebox.showwarning("Stopped", a[1], parent=self)
                    a[0].set()
                elif kind == "done":
                    self.busy = False
                    self.btn.config(state="normal")
                    self.refresh_btn.config(state="normal")
                    if a[0]:
                        self.status.set(a[0].splitlines()[0])
                        messagebox.showerror("VIN lookup", a[0], parent=self)
        except queue.Empty:
            pass
        self.after(100, self.pump)


def selftest() -> None:
    """--selftest: check the bundled Playwright can start a browser. Opens about:blank only."""
    out = vin_rpo.DATA_DIR / "selftest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx, page = vin_rpo.open_browser(pw, minimized=True)
            page.goto("about:blank")
            msg = f"ok: browser started ({ctx.browser.version if ctx.browser else 'persistent context'})"
            ctx.close()
    except Exception as e:
        msg = f"FAILED: {e}"
    out.write_text(msg, encoding="utf-8")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        App().mainloop()
