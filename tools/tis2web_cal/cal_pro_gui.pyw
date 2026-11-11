"""CAL PRO window app: calibration history per module for a GM VIN, from TIS2Web.

Two steps: "Find modules" asks TIS which controllers it has for the VIN; tick the ones you
want and press "Get history" to fetch them one by one, with progress per module.

The window is a CalPanel (a ttk.Frame) so it can later sit as a tab inside RPO PRO.
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tis2web_cal as cal  # noqa: E402
from tis2web_cal import vin_rpo  # noqa: E402  (VIN check digit, browser window state)

NAVY, AMBER = "#12233a", "#e0900a"


class CalPanel(ttk.Frame):
    def __init__(self, master, data_dir: Path = cal.DATA_DIR):
        super().__init__(master)
        self.data_dir = Path(data_dir)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.stop_evt = threading.Event()
        self.rec: dict | None = None        # record on screen
        self.checks: dict[str, tk.BooleanVar] = {}
        self.row_state: dict[str, ttk.Label] = {}

        # ---------------------------------------------------------------- top row
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        tk.Label(top, text="CAL", font=("Segoe UI Black", 15), fg=NAVY, padx=0).pack(side="left")
        tk.Label(top, text="PRO", font=("Segoe UI Black", 10), fg=AMBER, padx=0).pack(
            side="left", padx=(3, 16), pady=(4, 0))
        ttk.Label(top, text="VIN:").pack(side="left")
        self.vin_var = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.vin_var, font=("Consolas", 12), width=22)
        self.entry.pack(side="left", fill="x", expand=True, padx=8)
        self.entry.bind("<Return>", lambda e: self.find())
        self.vin_var.trace_add("write", self.on_vin_edit)
        self.find_btn = ttk.Button(top, text="Find modules", command=self.find)
        self.find_btn.pack(side="left")
        self.get_btn = ttk.Button(top, text="Get history", command=self.get_history, state="disabled")
        self.get_btn.pack(side="left", padx=(8, 0))
        self.refresh_btn = ttk.Button(top, text="Refresh data", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=(8, 0))
        self.csv_btn = ttk.Button(top, text="Save as CSV", command=self.save_csv, state="disabled")
        self.csv_btn.pack(side="left", padx=(8, 0))

        self.vin_hint = tk.Label(self, text="0 / 17", font=("Segoe UI", 10), anchor="w", fg="#666666")
        self.vin_hint.pack(fill="x")

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)

        # ------------------------------------------------------------ saved VINs
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        ttk.Label(left, text="Saved VINs").pack(anchor="w")
        self.saved = tk.Listbox(left, font=("Consolas", 10), width=20, height=6, exportselection=False)
        self.saved.pack(fill="both", expand=True)
        self.saved.bind("<<ListboxSelect>>", self.on_saved)
        ttk.Button(left, text="Open data folder", command=self.open_folder).pack(fill="x", pady=8)
        body.add(left, weight=0)

        # ------------------------------------------------------- module checklist
        mid = ttk.Frame(body, padding=(0, 0, 8, 0))
        ttk.Label(mid, text="Modules TIS lists for this VIN").pack(anchor="w")
        brow = ttk.Frame(mid)
        brow.pack(fill="x", pady=(2, 4))
        ttk.Button(brow, text="All", width=6, command=lambda: self.tick(True)).pack(side="left")
        ttk.Button(brow, text="None", width=6, command=lambda: self.tick(False)).pack(side="left", padx=4)
        ttk.Button(brow, text="Not saved", width=10, command=self.tick_unsaved).pack(side="left")
        lf = ttk.Frame(mid, borderwidth=1, relief="solid")
        lf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(lf, width=330, height=120, highlightthickness=0, background="white")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.listf = tk.Frame(self.canvas, background="white")
        self.canvas.create_window((0, 0), window=self.listf, anchor="nw")
        self.listf.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.empty = tk.Label(self.listf, text="Enter a VIN and press Find modules.",
                              background="white", fg="#666666", padx=8, pady=8)
        self.empty.pack(anchor="w")
        self.bar = ttk.Progressbar(mid, mode="determinate")
        self.bar.pack(fill="x", pady=(8, 2))
        prow = ttk.Frame(mid)
        prow.pack(fill="x", pady=(0, 8))
        self.prog = ttk.Label(prow, text="")
        self.prog.pack(side="left")
        self.stop_btn = ttk.Button(prow, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        body.add(mid, weight=0)

        # ---------------------------------------------------------------- results
        right = ttk.Frame(body)
        self.head = ttk.Label(right, text="", font=("Segoe UI", 11, "bold"))
        self.head.pack(anchor="w")
        frow = ttk.Frame(right)
        frow.pack(fill="x", pady=6)
        ttk.Label(frow, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill())
        self.filter_entry = ttk.Entry(frow, textvariable=self.filter_var, width=24)
        self.filter_entry.pack(side="left", padx=6)
        ttk.Button(frow, text="Copy current parts", command=self.copy_current).pack(side="left")
        self.hist_var = tk.BooleanVar(value=True)
        self.hist_btn = ttk.Button(frow, text="Current only", width=13, command=self.toggle_hist)
        self.hist_btn.pack(side="left", padx=8)
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, pady=(0, 8))
        self.table = tk.Text(tf, height=6, width=40, wrap="word", cursor="arrow", borderwidth=1,
                             relief="solid", padx=8, pady=6, spacing1=1, spacing3=1,
                             tabs=("190p", "270p", "360p"), font=("Segoe UI", 10))
        t = self.table
        t.tag_configure("ctrl", font=("Segoe UI", 12, "bold"), foreground=NAVY, spacing1=10)
        t.tag_configure("ctrlsub", font=("Segoe UI", 8), foreground="#8a8a8a")
        t.tag_configure("module", font=("Segoe UI", 10, "bold"), lmargin1=8, lmargin2=8)
        t.tag_configure("part", font=("Consolas", 12, "bold"), foreground="#111111")
        t.tag_configure("old", font=("Segoe UI", 9), foreground="#7a7a7a", lmargin1=8, lmargin2=8)
        t.tag_configure("oldpart", font=("Consolas", 10), foreground="#7a7a7a")
        t.tag_configure("note", font=("Segoe UI", 9, "italic"), foreground="#8a5a00",
                        lmargin1=8, lmargin2=8)
        t.tag_configure("err", foreground="#9a2a2a", lmargin1=8)
        sb = ttk.Scrollbar(tf, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set, state="disabled")
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body.add(right, weight=1)

        self.status = tk.StringVar(value="Ready.")
        ttk.Separator(self, orient="horizontal").pack(fill="x")
        ttk.Label(self, textvariable=self.status, padding=(10, 6), anchor="w").pack(fill="x")

        t.bind("<Button-1>", lambda e: t.focus_set(), add="+")
        t.bind("<Control-a>", lambda e: (t.tag_add("sel", "1.0", "end-1c"), "break")[1])
        for w in (self.entry, self.filter_entry):
            w.bind("<Button-3>", self.entry_menu)
        t.bind("<Button-3>", self.table_menu)
        self.saved.bind("<Button-3>", self.saved_menu)

        self.after_idle(self._align_hint)
        self.reload_saved()
        self.entry.focus_set()
        self.after(100, self.pump)

    def _align_hint(self):
        self.update_idletasks()
        self.vin_hint.config(padx=max(0, self.entry.winfo_rootx() - self.winfo_rootx()))

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

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
        m = re.search(r"\b\d{8}\b", line)
        part = m.group(0) if m else ""
        return self.popup(event, [
            ("Copy", lambda: self.set_clip(t.get("sel.first", "sel.last")), sel),
            (f"Copy part {part}" if part else "Copy part number", lambda: self.set_clip(part), bool(part)),
            ("Copy this row", lambda: self.set_clip(re.sub(r"\t+", "  ", line).strip()), bool(line.strip())),
            None,
            ("Copy current parts", self.copy_current, bool(self.rec)),
            ("Select all", lambda: t.tag_add("sel", "1.0", "end-1c"), bool(self.rec)),
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
            vin = self.saved.get(sel[0])
            self.vin_var.set(vin)
            self.show(cal.load_record(vin, self.data_dir))

    def show(self, rec: dict | None, keep_ticks: bool = False):
        old = {c for c, v in self.checks.items() if v.get()} if keep_ticks else set()
        self.rec = rec
        for w in self.listf.winfo_children():
            w.destroy()
        self.checks, self.row_state = {}, {}
        if not rec or not rec.get("controllers_available"):
            tk.Label(self.listf, text="Enter a VIN and press Find modules.", background="white",
                     fg="#666666", padx=8, pady=8).pack(anchor="w")
        else:
            for c in rec["controllers_available"]:
                row = tk.Frame(self.listf, background="white")
                row.pack(fill="x", anchor="w")
                v = tk.BooleanVar(value=c in old)
                v.trace_add("write", lambda *a: self._update_buttons())
                tk.Checkbutton(row, text=c, variable=v, background="white", anchor="w",
                               activebackground="white", font=("Segoe UI", 9)).pack(side="left")
                st = tk.Label(row, text="", background="white", font=("Segoe UI", 8), fg="#7a7a7a")
                st.pack(side="left", padx=(4, 8))
                self.checks[c], self.row_state[c] = v, st
                self._set_row(c)
        self.csv_btn.config(state="normal" if rec and rec.get("controllers") else "disabled")
        if rec:
            n = sum(1 for c in rec["controllers"].values() if not c.get("error"))
            self.head.config(text=f"{rec['vin']}   ·   {n} of {len(rec['controllers_available'])} "
                                  f"modules saved   ·   list from {rec.get('controllers_listed') or '?'}")
        else:
            self.head.config(text="")
        self._update_buttons()
        self.fill()

    def _set_row(self, c, text=None, color="#7a7a7a"):
        st = self.row_state.get(c)
        if st is None:
            return
        if text is None:
            got = (self.rec or {}).get("controllers", {}).get(c)
            if got and got.get("error"):
                text, color = "failed - " + got["error"][:40], "#9a2a2a"
            elif got:
                text, color = f"saved {got.get('lookup_date', '')}", "#1a7f37"
            else:
                text = ""
        st.config(text=text, fg=color)

    def _update_buttons(self):
        any_ticked = any(v.get() for v in self.checks.values())
        self.get_btn.config(state="normal" if any_ticked and not self.busy else "disabled")

    def tick(self, on: bool):
        for v in self.checks.values():
            v.set(on)

    def tick_unsaved(self):
        done = (self.rec or {}).get("controllers", {})
        for c, v in self.checks.items():
            v.set(c not in done or bool(done[c].get("error")))

    def toggle_hist(self):
        self.hist_var.set(not self.hist_var.get())
        self.hist_btn.config(text="Current only" if self.hist_var.get() else "Show history")
        self.fill()

    def fill(self):
        t = self.table
        t.configure(state="normal")
        t.delete("1.0", "end")
        rec = self.rec
        if rec and rec.get("controllers"):
            needle = self.filter_var.get().strip().lower()
            hist = self.hist_var.get()
            order = [c for c in rec.get("controllers_available", []) if c in rec["controllers"]]
            order += [c for c in rec["controllers"] if c not in order]
            for cname in order:
                c = rec["controllers"][cname]
                mods = c.get("modules") or []
                if needle:
                    if needle in cname.lower():
                        pass
                    else:
                        mods = [m for m in mods if needle in m["module"].lower() or any(
                            needle in f"{r['part_number']} {r.get('cvn') or ''} {r.get('description', '')}".lower()
                            for opt in m["options"] for r in opt)]
                        if not mods and not (c.get("error") and needle in c["error"].lower()):
                            continue
                t.insert("end", cname, ("ctrl",))
                t.insert("end", f"   looked up {c.get('lookup_date', '')}\n", ("ctrlsub",))
                if c.get("error"):
                    t.insert("end", f"Failed: {c['error']}\n", ("err",))
                    continue
                for m in mods:
                    if m.get("note"):
                        t.insert("end", m["note"] + "\n", ("note",))
                    for oi, opt in enumerate(m["options"], 1):
                        label = m["module"] + (f"  (option {oi} of {len(m['options'])})"
                                               if len(m["options"]) > 1 else "")
                        cur = opt[0]
                        t.insert("end", label, ("module",))
                        t.insert("end", "\t")
                        t.insert("end", cur["part_number"], ("part",))
                        t.insert("end", f"\tCVN {cur.get('cvn') or '-'}\t{cur.get('description', '')}\n")
                        if hist:
                            for r in opt[1:]:
                                t.insert("end", "   earlier\t", ("old",))
                                t.insert("end", r["part_number"], ("oldpart",))
                                t.insert("end", f"\tCVN {r.get('cvn') or '-'}\t{r.get('description', '')}\n",
                                         ("old",))
        elif rec and rec.get("controllers_available"):
            t.insert("end", "Tick the modules you want on the left and press Get history.", ("note",))
        t.configure(state="disabled")

    def copy_current(self):
        if not self.rec:
            return
        lines = []
        for cname, c in self.rec["controllers"].items():
            for m in c.get("modules") or []:
                for opt in m["options"]:
                    lines.append(f"{cname}\t{m['module']}\t{opt[0]['part_number']}")
        self.set_clip("\n".join(lines))
        self.status.set(f"{len(lines)} current part numbers copied (controller, module, part).")

    def on_vin_edit(self, *_):
        raw = self.vin_var.get()
        up = raw.upper()
        if up != raw:
            pos = self.entry.index("insert")
            self.vin_var.set(up)  # does not re-fire this trace
            self.entry.icursor(pos)
        v = up.strip()
        if not v:
            return self.vin_hint.config(text="0 / 17", fg="#666666")
        bad = "".join(sorted({c for c in v if c not in vin_rpo._TRANSLIT}))
        if bad:
            text, ok = f"{len(v)} / 17  ✖ '{bad}' not allowed in a VIN", False
        elif len(v) < 17:
            text, ok = f"{len(v)} / 17  ✖ {17 - len(v)} more needed", False
        elif len(v) > 17:
            text, ok = f"{len(v)} / 17  ✖ {len(v) - 17} too many", False
        elif vin_rpo.vin_check_digit(v) != v[8]:
            text, ok = "17 / 17  ✖ check digit wrong - typo somewhere", False
        else:
            text, ok = "17 / 17  ✔", True
        self.vin_hint.config(text=text, fg="#1a7f37" if ok else "#000000")

    def open_folder(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.data_dir)  # Windows only

    def save_csv(self):
        if not self.rec:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / f"{self.rec['vin']}.csv"
        try:
            cal.write_csv(self.rec, path)
        except OSError as e:  # e.g. the CSV is open in Excel
            messagebox.showerror("Save as CSV", f"Could not write {path}\n\n{e}", parent=self)
            return
        self.status.set(f"Saved {path}")

    # ------------------------------------------------------------------- lookup
    def _vin(self) -> str | None:
        try:
            return cal.validate_vin(self.vin_var.get())
        except ValueError as e:
            messagebox.showerror("Invalid VIN", str(e), parent=self)
            return None

    def find(self, refresh=False):
        if self.busy:
            return
        vin = self._vin()
        if not vin:
            return
        rec = cal.load_record(vin, self.data_dir)
        if rec and rec.get("controllers_available") and not refresh:
            self.show(rec)
            self.status.set(f"{vin}: module list shown from the saved file (no lookup made). "
                            "Refresh data asks TIS again.")
            return
        self._run(vin, None, refresh)

    def get_history(self):
        if self.busy or not self.rec:
            return
        names = [c for c, v in self.checks.items() if v.get()]
        if names:
            self._run(self.rec["vin"], names, False)

    def refresh(self):
        if self.busy:
            return
        vin = self._vin()
        if not vin:
            return
        names = [c for c, v in self.checks.items() if v.get()] if self.rec and self.rec["vin"] == vin else []
        what = (f"the module list and {len(names)} ticked module(s)" if names else "the module list")
        if messagebox.askokcancel("Refresh data", f"Ask TIS again for {vin}: {what}, replacing "
                                  "what is saved?", parent=self):
            self._run(vin, names or None, True)

    def stop(self):
        self.stop_evt.set()
        self.stop_btn.config(state="disabled")
        self.status.set("Stopping after the current module...")

    def _set_busy(self, busy: bool):
        self.busy = busy
        for b in (self.find_btn, self.refresh_btn):
            b.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self._update_buttons()

    def _run(self, vin, names, refresh):
        self.stop_evt.clear()
        self._set_busy(True)
        self.bar.config(maximum=max(1, len(names or [])), value=0)
        self.prog.config(text="")
        for c in names or []:
            if refresh or c not in (self.rec or {}).get("controllers", {}) \
                    or self.rec["controllers"][c].get("error"):
                self._set_row(c, "queued", "#7a7a7a")
        threading.Thread(target=self.work, daemon=True, args=(vin, names, refresh)).start()

    def work(self, vin, names, refresh):
        """Runs off the UI thread. Talks to the UI only through self.q."""
        say = lambda *m: self.q.put(m)
        rec = (None if refresh and not names else cal.load_record(vin, self.data_dir)) \
            or cal.new_record(vin)
        if refresh and names:
            for c in names:
                rec["controllers"].pop(c, None)

        def progress(event, c, i, n, detail):
            if event == "listing":
                say("status", f"{vin}: asking TIS which modules it has...")
            elif event == "start":
                say("row", c, "working...", "#0b5cad")
                say("prog", i - 1, n, f"{i} of {n}: {c}")
            elif event in ("done", "error"):
                cal.save_record(rec, self.data_dir)  # keep each module as soon as it arrives
                say("row", c, None, None)
                say("prog", i, n, f"{i} of {n} done")
                say("show", rec)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return say("done", "Playwright is not installed.\n\nRun:\n  pip install playwright")
        try:
            with sync_playwright() as pw:
                ctx, page = cal.open_browser(pw, minimized=True)
                try:
                    try:
                        rec = cal.lookup(page, vin, names, rec, refresh=refresh, progress=progress,
                                         should_stop=self.stop_evt.is_set)
                    except cal.BlockedError as e:
                        vin_rpo.set_window_state(page, "normal")
                        cal.save_record(rec, self.data_dir) if rec.get("controllers_available") else None
                        gate = threading.Event()
                        say("blocked", gate,
                            f"Stopped at {vin}: {e}\n\nThe site is asking for something this tool "
                            "will not do (CAPTCHA / block page / login). Finish by hand in the "
                            "browser window that just came up.\n\nClick OK when you are done to "
                            "close that browser window.")
                        gate.wait()
                        return say("done", None)
                    except cal.SiteError as e:
                        return say("done", f"TIS says: {e}")
                    except cal.NoResultError as e:
                        return say("done", f"{vin}: {e}")
                finally:
                    ctx.close()
        except Exception as e:  # browser failed to start, window closed by hand, etc.
            return say("done", f"Lookup failed: {str(e).splitlines()[0]}")
        path = cal.save_record(rec, self.data_dir)
        say("show", rec)
        if names:
            failed = [c for c in names if rec["controllers"].get(c, {}).get("error")]
            msg = (f"{vin}: {len(names) - len(failed)} of {len(names)} modules saved to {path}"
                   + (f"; {len(failed)} failed" if failed else "")
                   + ("; stopped early" if self.stop_evt.is_set() else ""))
        else:
            msg = (f"{vin}: TIS lists {len(rec['controllers_available'])} modules. "
                   "Tick the ones you want and press Get history.")
        say("finished", msg)

    def pump(self):
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == "status":
                    self.status.set(a[0])
                elif kind == "row":
                    self._set_row(a[0], a[1], a[2] or "#7a7a7a")
                elif kind == "prog":
                    self.bar.config(maximum=max(1, a[1]), value=a[0])
                    self.prog.config(text=a[2])
                    self.status.set(a[2])
                elif kind == "show":
                    self.show(a[0], keep_ticks=True)
                    self.reload_saved()
                elif kind == "finished":
                    self._set_busy(False)
                    self.status.set(a[0])
                elif kind == "blocked":
                    self.status.set("Stopped: the site showed a block page.")
                    messagebox.showwarning("Stopped", a[1], parent=self)
                    a[0].set()
                elif kind == "done":
                    self._set_busy(False)
                    if a[0]:
                        self.status.set(a[0].splitlines()[0])
                        messagebox.showerror("CAL PRO", a[0], parent=self)
        except queue.Empty:
            pass
        self.after(100, self.pump)


class App(tk.Tk):
    def __init__(self, data_dir: Path = cal.DATA_DIR):
        super().__init__()
        self.title("CAL PRO")
        self.geometry("1280x720")
        self.minsize(980, 480)
        try:
            self.iconbitmap(str(HERE / "cal_pro.ico"))
        except tk.TclError:
            pass
        self.panel = CalPanel(self, data_dir)
        self.panel.pack(fill="both", expand=True)


def selftest() -> None:
    """--selftest: check the bundled Playwright can start a browser. Opens about:blank only."""
    out = cal.DATA_DIR / "selftest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx, page = cal.open_browser(pw, minimized=True)
            page.goto("about:blank")
            msg = "ok: browser started"
            ctx.close()
    except Exception as e:
        msg = f"FAILED: {e}"
    out.write_text(msg, encoding="utf-8")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        App().mainloop()
