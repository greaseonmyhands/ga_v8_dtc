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
        self.btn = ttk.Button(top, text="Look up", command=self.start)
        self.btn.pack(side="left")
        self.refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Refresh (ignore saved)", variable=self.refresh_var).pack(
            side="left", padx=(10, 0))
        self.csv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Also save CSV", variable=self.csv_var).pack(side="left", padx=(10, 0))

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
        frow = ttk.Frame(right)
        frow.pack(fill="x", padx=(10, 0), pady=6)
        ttk.Label(frow, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill())
        ttk.Entry(frow, textvariable=self.filter_var, width=28).pack(side="left", padx=6)
        ttk.Button(frow, text="Copy codes", command=self.copy_codes).pack(side="left")
        cols = ("code", "category", "description")
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, padx=(10, 0))
        self.tree = ttk.Treeview(tf, columns=cols, show="headings")
        for c, w in zip(cols, (70, 90, 560)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, stretch=(c == "description"), anchor="w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body.add(right, weight=1)

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, padding=(10, 6), anchor="w").pack(fill="x")

        self.reload_saved()
        self.entry.focus_set()
        self.after(100, self.pump)

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
        attrs = "\n".join(f"{k}: {v}" for k, v in (r.get("major_attributes") or {}).items())
        self.head.config(text=f"{r['vin']}   {r.get('vehicle') or ''}\n{attrs}\n"
                              f"{len(r['codes'])} RPO codes, looked up {r.get('lookup_date')}")
        self.fill()

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        if not self.current:
            return
        needle = self.filter_var.get().strip().lower()
        for c in self.current["codes"]:
            if needle and needle not in f"{c['code']} {c['category']} {c['description']}".lower():
                continue
            self.tree.insert("", "end", values=(c["code"], c["category"], c["description"]))

    def copy_codes(self):
        if self.current:
            self.clipboard_clear()
            self.clipboard_append(" ".join(c["code"] for c in self.current["codes"]))
            self.status.set("RPO codes copied to the clipboard.")

    def open_folder(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.data_dir)  # Windows only

    # ------------------------------------------------------------------- lookup
    def start(self):
        if self.busy:
            return
        raw = self.vin_var.get().replace(",", " ").split()
        if not raw:
            return
        try:
            vins = list(dict.fromkeys(vin_rpo.validate_vin(v) for v in raw))
        except ValueError as e:
            messagebox.showerror("Invalid VIN", str(e), parent=self)
            return
        self.busy = True
        self.btn.config(state="disabled")
        threading.Thread(target=self.work, daemon=True,
                         args=(vins, self.refresh_var.get(), self.csv_var.get())).start()

    def work(self, vins, refresh, want_csv):
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
                ctx, page = vin_rpo.open_browser(pw)
                try:
                    for i, vin in enumerate(todo):
                        if i:
                            say("status", f"Pausing {PAUSE_S:.0f} s before {vin}...")
                            time.sleep(PAUSE_S)
                        say("status", f"Looking up {vin} in Chrome...")
                        try:
                            r = vin_rpo.lookup_live(page, vin)
                        except vin_rpo.BlockedError as e:
                            gate = threading.Event()
                            say("blocked", gate,
                                f"Stopped at {vin}: {e}\n\nThe site is asking for something this "
                                "tool will not do (CAPTCHA / block page / login). Finish this "
                                "lookup by hand in the Chrome window that is open.\n\n"
                                f"Not looked up: {' '.join(todo[i:])}\n\n"
                                "Click OK when you are done to close that Chrome window.")
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
                    if a[0]:
                        self.status.set(a[0].splitlines()[0])
                        messagebox.showerror("VIN lookup", a[0], parent=self)
        except queue.Empty:
            pass
        self.after(100, self.pump)


if __name__ == "__main__":
    App().mainloop()
