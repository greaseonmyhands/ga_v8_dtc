"""The CAN PRO window: GM LS (single-wire) / HS CAN logger and script sender for J2534
devices. can_pro.pyw is the launcher; LOG PRO (tools/log_pro) builds on the same App class
through the hook methods near the end of it.

Receive only until "Enable transmit" is clicked. Frames are written to a .csv and a
candump .log while "Start logging" is on.
"""
from __future__ import annotations

import bisect
import os
import queue
import struct
import sys
import time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import canlog  # noqa: E402
import j2534  # noqa: E402

SIM_LABEL = "Simulator (no hardware)"
BUS_CHOICES = {
    "LS  single-wire 33.3k (pin 1)": ("LS",),
    "HS  500k (pins 6/14)": ("HS",),
    "LS + HS together": ("LS", "HS"),
}
MAX_ROWS = 2000        # rows kept on screen in the scrolling view
MAX_PER_TICK = 300     # rows added per screen update; the rest are in the log file only
RECENT = 5000          # frames remembered for re-filtering
TICK_MS = 50
DETECT_TIMEOUT_S = 5.0  # a driver that has not answered by then shows "no answer"
RESORT_S = 0.4         # while sorted by a column, re-sort live rows this often
COLS = ("time", "bus", "dir", "id", "bits", "dlc", "data", "count", "period")
HEAD = {"time": "Time (s)", "bus": "Bus", "dir": "Dir", "id": "ID", "bits": "Bits",
        "dlc": "DLC", "data": "Data", "count": "Count", "period": "Period (ms)"}
WIDTH = {"time": 96, "bus": 44, "dir": 40, "id": 92, "bits": 44, "dlc": 40, "data": 230,
         "count": 70, "period": 84}
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


class App(tk.Tk):
    TITLE = "CAN PRO"
    MARK = ("CAN", "PRO")              # wordmark, top left
    ICON = HERE / "can_pro.ico"
    TOOLS_OPEN = True        # Send frame / Script section shown when the window opens
    SIZE = (1040, 700)

    def __init__(self, data_dir: Path | None = None, detect: bool = True):
        super().__init__()
        self.data_dir = Path(data_dir or canlog.DATA_DIR)
        self.title(self.TITLE)
        k = self.winfo_fpixels("1i") / 96.0   # the exe is DPI-aware: scale pixel sizes
        self.geometry(f"{int(self.SIZE[0] * k)}x{int(self.SIZE[1] * k)}")
        self.minsize(int(820 * k), int(520 * k))
        self.set_icon()
        self.q: queue.Queue = queue.Queue()
        self.session: canlog.Session | None = None
        self.runner: canlog.ScriptRunner | None = None
        self.script: canlog.Script | None = None
        self.devices: list = []
        self.paused = False
        self.overwrite = False
        self.filter = canlog.IdFilter("")
        self.recent: deque = deque(maxlen=RECENT)
        self.by_id: dict = {}          # (bus, ext, id, tx) -> [frame, count, period_s]
        self.sorted_keys: list = []    # keys currently shown in the overwrite view
        self.rows: deque = deque()     # item ids in the scrolling view
        self.skipped = 0
        self.log_on = False            # "Start logging" pressed; off until the user asks
        self.saved_logs: list = []     # files written during the current capture
        self.sort_col = None           # column the table is sorted by; None = arrival / ID order
        self.sort_rev = False
        self.sort_due = False          # rows changed since the last sort
        self.sort_at = 0.0

        # ---------------------------------------------------------------- top
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        tk.Label(top, text=self.MARK[0], font=("Segoe UI Black", 15), fg="#12233a", padx=0).pack(
            side="left")
        tk.Label(top, text=self.MARK[1], font=("Segoe UI Black", 10), fg="#e0900a", padx=0).pack(
            side="left", padx=(3, 16), pady=(4, 0))
        ttk.Label(top, text="Device:").pack(side="left")
        self.dev_var = tk.StringVar()      # text shown in the list (name + attached state)
        self.dev_label = ""                # the chosen device's real name
        self.dev_picked = False            # True once the user chose a device by hand
        self.dev_names: dict = {}          # list text -> device name
        self.detect: dict = {}             # device name -> checking / detected / ...
        self.detect_gen = 0
        self.auto_detect = detect
        # Right-hand controls are packed first so a narrow window never hides Start.
        self.start_btn = ttk.Button(top, text="Start", width=8, command=self.toggle_capture)
        self.start_btn.pack(side="right", padx=(6, 0))
        self.bus_var = tk.StringVar(value=next(iter(BUS_CHOICES)))
        self.bus_box = ttk.Combobox(top, textvariable=self.bus_var, state="readonly", width=28,
                                    values=list(BUS_CHOICES))
        self.bus_box.pack(side="right", padx=6)
        ttk.Label(top, text="Bus:").pack(side="right", padx=(12, 0))
        self.rescan_btn = ttk.Button(top, text="Rescan", width=8, command=self.load_devices)
        self.rescan_btn.pack(side="right")
        self.dev_box = ttk.Combobox(top, textvariable=self.dev_var, state="readonly", width=20)
        self.dev_box.pack(side="left", padx=(6, 4), fill="x", expand=True)
        self.dev_box.bind("<<ComboboxSelected>>", self.on_device_pick)

        row = ttk.Frame(self, padding=(10, 2, 10, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(row, textvariable=self.filter_var, width=30, font=("Consolas", 10))
        self.filter_entry.pack(side="left", padx=6)
        self.filter_var.trace_add("write", self.on_filter_edit)
        self.pause_btn = ttk.Button(row, text="Pause", width=8, command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        self.view_btn = ttk.Button(row, text="Overwrite view", width=15, command=self.toggle_view)
        self.view_btn.pack(side="left", padx=6)
        ttk.Button(row, text="Clear", width=7, command=self.clear).pack(side="left")
        self.log_btn = ttk.Button(row, text="Start logging", width=13, command=self.toggle_log)
        self.log_btn.pack(side="left", padx=(6, 0))
        self.log_options(row)
        ttk.Button(row, text="Open log folder", command=self.open_folder).pack(side="left", padx=6)
        hint_row = ttk.Frame(self, padding=(10, 2, 10, 0))
        hint_row.pack(fill="x")
        self.count = ttk.Label(hint_row, text="")       # right-hand side, so a wide toolbar never hides it
        self.count.pack(side="right")
        self.filter_hint = tk.Label(hint_row, text="", font=("Segoe UI", 9), anchor="w", fg="#000000")
        self.filter_hint.pack(side="left", fill="x", expand=True)

        # ------------------------------------------------- status (packed first so it stays)
        self.status = tk.StringVar(value="Ready. Receive only until you enable transmit.")
        ttk.Label(self, textvariable=self.status, padding=(10, 6), anchor="w").pack(
            side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")

        # ------------------------------------------------------------ bottom tabs
        # Enable transmit and the show/hide switch stay visible; the tabs fold away.
        self.tools_box = ttk.Frame(self, padding=(10, 4, 10, 6))
        self.tools_box.pack(side="bottom", fill="x")
        bar = ttk.Frame(self.tools_box)
        bar.pack(fill="x")
        self.tx_btn = ttk.Button(bar, text="Enable transmit", width=18, command=self.toggle_tx,
                                 state="disabled")
        self.tx_btn.pack(side="left")
        self.tools_btn = ttk.Button(bar, text="", width=26, command=self.toggle_tools)
        self.tools_btn.pack(side="left", padx=8)
        self.tabs = ttk.Notebook(self.tools_box)

        send = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(send, text="Send frame")
        line = ttk.Frame(send)
        line.pack(fill="x")
        ttk.Label(line, text="Bus:").pack(side="left")
        self.send_bus = tk.StringVar(value="LS")
        self.send_bus_box = ttk.Combobox(line, textvariable=self.send_bus, state="readonly",
                                         width=4, values=("LS", "HS"))
        self.send_bus_box.pack(side="left", padx=6)
        ttk.Label(line, text="ID:").pack(side="left")
        self.id_var = tk.StringVar()
        self.id_entry = ttk.Entry(line, textvariable=self.id_var, width=10, font=("Consolas", 11))
        self.id_entry.pack(side="left", padx=6)
        ttk.Label(line, text="Data:").pack(side="left")
        self.data_var = tk.StringVar()
        self.data_entry = ttk.Entry(line, textvariable=self.data_var, width=26, font=("Consolas", 11))
        self.data_entry.pack(side="left", padx=6, fill="x", expand=True)
        self.send_btn = ttk.Button(line, text="Send", width=8, command=self.send_one, state="disabled")
        self.send_btn.pack(side="left")
        self.send_hint = tk.Label(send, text="", font=("Segoe UI", 9), anchor="w", fg="#000000")
        self.send_hint.pack(fill="x", pady=(6, 0))
        for var, ent in ((self.id_var, self.id_entry), (self.data_var, self.data_entry)):
            var.trace_add("write", lambda *a, v=var, e=ent: self.on_hex_edit(v, e))
            ent.bind("<Return>", lambda e: self.send_one())

        scr = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(scr, text="Script")
        line = ttk.Frame(scr)
        line.pack(fill="x")
        ttk.Button(line, text="Load script...", command=self.load_script).pack(side="left")
        self.run_btn = ttk.Button(line, text="Start script", width=12, command=self.start_script,
                                  state="disabled")
        self.run_btn.pack(side="left", padx=6)
        self.stop_script_btn = ttk.Button(line, text="Stop script", width=12,
                                          command=self.stop_script, state="disabled")
        self.stop_script_btn.pack(side="left")
        self.script_label = ttk.Label(line, text="No script loaded.")
        self.script_label.pack(side="left", padx=10)
        pf = ttk.Frame(scr)
        pf.pack(fill="both", expand=True, pady=(6, 0))
        self.preview = tk.Text(pf, height=6, width=40, wrap="none", font=("Consolas", 10),
                               borderwidth=1, relief="solid", padx=6, pady=4, cursor="arrow")
        psb = ttk.Scrollbar(pf, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=psb.set, state="disabled")
        self.preview.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        self.show_tools(self.TOOLS_OPEN)

        # ------------------------------------------------------------ frame table
        self.ui_scale = k
        tf = self.make_table_frame()
        style = ttk.Style(self)
        self.mono = tkfont.Font(family="Consolas", size=10)   # keep a reference or Tk drops it
        style.configure("Can.Treeview", font=self.mono,
                        rowheight=self.mono.metrics("linespace") + 4)
        self.tree = ttk.Treeview(tf, columns=COLS, show="headings", height=6, style="Can.Treeview",
                                 selectmode="extended")
        for c in COLS:
            self.tree.heading(c, text=HEAD[c], anchor="w", command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=int(WIDTH[c] * k), anchor="w", stretch=False)
        self.tree.tag_configure("tx", foreground="#0b5cad")
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.set_columns()

        for w in (self.filter_entry, self.id_entry, self.data_entry):
            w.bind("<Button-3>", self.entry_menu)
        self.tree.bind("<Button-3>", self.tree_menu)
        self.tree.bind("<Control-c>", lambda e: self.copy_rows())
        self.preview.bind("<Button-3>", self.preview_menu)
        self.preview.bind("<Button-1>", lambda e: self.preview.focus_set(), add="+")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load_devices()
        self.on_hex_edit(self.id_var, self.id_entry)
        self.after(TICK_MS, self.pump)

    # ------------------------------------------------------------- right-click
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

    def set_clip(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("Copied to the clipboard.")

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

    def row_text(self, iid) -> str:
        v = self.tree.item(iid, "values")
        return "  ".join(str(x) for x in v if str(x) != "")

    def copy_rows(self):
        sel = self.tree.selection()
        if sel:
            self.set_clip("\n".join(self.row_text(i) for i in sel))
        return "break"

    def tree_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        sel = self.tree.selection()
        v = self.tree.item(iid, "values") if iid else None
        fid, data, bus = (v[3], v[6], v[1]) if v else ("", "", "")

        def to_send():
            self.send_bus.set(bus)
            self.id_var.set(fid)
            self.data_var.set(data)
            self.show_tools(True)
            self.tabs.select(0)
        return self.popup(event, [
            ("Copy row" + ("s" if len(sel) > 1 else ""), self.copy_rows, bool(sel)),
            (f"Copy ID {fid}" if fid else "Copy ID", lambda: self.set_clip(fid), bool(fid)),
            ("Copy data", lambda: self.set_clip(data), bool(data)),
            None,
            (f"Show only ID {fid}" if fid else "Show only this ID",
             lambda: self.filter_var.set(fid), bool(fid)),
            (f"Hide ID {fid}" if fid else "Hide this ID",
             lambda: self.filter_var.set((self.filter_var.get() + " !" + fid).strip()), bool(fid)),
            ("Put in the Send frame box", to_send, bool(fid)),
            None,
            ("Select all", lambda: self.tree.selection_set(self.tree.get_children()),
             bool(self.tree.get_children())),
        ])

    def preview_menu(self, event):
        t = self.preview
        t.focus_set()
        sel = bool(t.tag_ranges("sel"))
        return self.popup(event, [
            ("Copy", lambda: self.set_clip(t.get("sel.first", "sel.last")), sel),
            ("Copy all", lambda: self.set_clip(t.get("1.0", "end-1c")), bool(t.get("1.0", "end-1c"))),
            ("Select all", lambda: t.tag_add("sel", "1.0", "end-1c"), True),
        ])

    # ------------------------------------------------------------------ devices
    def load_devices(self):
        """List the installed devices, then check in the background which are attached."""
        self.devices = j2534.list_devices()
        labels = [d.label for d in self.devices]
        if self.dev_label not in labels + [SIM_LABEL]:
            self.dev_label = next((n for n in labels if "V200" in n), (labels + [SIM_LABEL])[0])
            self.dev_picked = False
        can_check = self.auto_detect and struct.calcsize("P") == 4 and not self.session
        self.detect = ({d.label: "not checked" if j2534.detect_skipped(d.dll) else "checking"
                        for d in self.devices} if can_check else {})
        self.detect_gen += 1
        self.show_devices()
        n = len(self.devices)
        note = "" if struct.calcsize("P") == 4 else "  (64-bit Python: only the simulator will open)"
        self.status.set(f"{n} J2534 device{'s' if n != 1 else ''} installed."
                        + (" Checking which are attached..." if can_check and n else note))
        if can_check:
            gen, q = self.detect_gen, self.q
            pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="can-detect")
            for d in self.devices:
                if self.detect[d.label] != "checking":
                    continue
                pool.submit(lambda d=d: q.put(("detect", gen, (
                    d.label, j2534.run_detect(detect_command(d.dll), DETECT_TIMEOUT_S)))))
            pool.shutdown(wait=False)

    def device_text(self, label: str) -> str:
        state = self.detect.get(label)
        if state in ("detected", "in use"):
            return f"● {label}   ({state})"
        return f"{label}   ({state}{'...' if state == 'checking' else ''})" if state else label

    def show_devices(self):
        """Fill the Device list: attached devices first, each with its state."""
        rank = {"detected": 0, "in use": 1}
        labels = sorted((d.label for d in self.devices),
                        key=lambda n: (rank.get(self.detect.get(n), 2), n.lower()))
        self.dev_names = {self.device_text(n): n for n in labels}
        self.dev_names[SIM_LABEL] = SIM_LABEL
        self.dev_box.config(values=list(self.dev_names))
        self.dev_var.set(next(t for t, n in self.dev_names.items() if n == self.dev_label))

    def on_device_pick(self, _evt=None):
        self.dev_label = self.dev_names.get(self.dev_var.get(), self.dev_label)
        self.dev_picked = True
        self.dev_box.selection_clear()

    def on_detect(self, label: str, state: str):
        self.detect[label] = state
        found = [n for n, st in self.detect.items() if st == "detected"]
        if (state == "detected" and not self.dev_picked and not self.session
                and self.detect.get(self.dev_label) != "detected"):
            self.dev_label = label        # nothing chosen by hand yet: pick the attached one
        self.show_devices()
        if "checking" not in self.detect.values():
            short = ", ".join(d.name for d in self.devices if d.label in found)
            if not self.session:
                self.status.set(f"{len(self.devices)} J2534 devices installed. "
                                + (f"Detected: {short}." if found else "None detected."))

    # ------------------------------------------------------------------ capture
    def toggle_capture(self):
        if self.session:
            self.start_btn.config(state="disabled")
            self.status.set("Stopping...")
            self.stop_script()
            self.session.stop()
            return
        name = self.dev_names.get(self.dev_var.get(), self.dev_var.get())
        buses = BUS_CHOICES[self.bus_var.get()]
        if name == SIM_LABEL:
            backend = canlog.SimBackend()
        else:
            info = next((d for d in self.devices if d.label == name), None)
            if info is None:
                return messagebox.showerror(self.TITLE, "Pick a device first.", parent=self)
            backend = j2534.J2534Backend(info)
        self.clear()
        q = self.q
        self.session = s = canlog.Session(
            backend, buses,
            on_frames=lambda frames: q.put(("frames", s, frames)),
            on_event=lambda kind, arg: q.put((kind, s, arg)),
            log_factory=((lambda: canlog.LogWriter(self.data_dir))
                         if self.log_on and self.log_raw() else None))
        self.saved_logs = []
        self.start_btn.config(state="disabled")
        for w in (self.dev_box, self.bus_box, self.rescan_btn):
            w.config(state="disabled")
        self.status.set(f"Opening {name} (receive only)...")
        s.start()

    def capture_ended(self, err):
        s, self.session = self.session, None
        self.runner = None
        self.start_btn.config(text="Start", state="normal")
        self.rescan_btn.config(state="normal")
        for w in (self.dev_box, self.bus_box):
            w.config(state="readonly")
        self.tx_btn.config(text="Enable transmit", state="disabled")
        self.send_btn.config(state="disabled")
        self.stop_script_btn.config(state="disabled")
        self.update_script_buttons()
        if err:
            self.extra_log_stop()
            self.status.set("Stopped: " + err.splitlines()[0])
            messagebox.showerror(self.TITLE, err, parent=self)
        elif s:
            if s.log:                      # still logging when the capture ended
                self.note_saved(s.log)
            extra = self.extra_log_stop()
            if extra:
                self.saved_logs.append(extra)
            self.status.set(f"Stopped. {s.rx_count} received, {s.tx_count} sent. "
                            + ("Saved " + ", ".join(self.saved_logs) + "." if self.saved_logs
                               else "Nothing was logged."))
        else:
            self.status.set("Stopped.")

    # ------------------------------------------------------------------ logging
    def note_saved(self, log):
        self.saved_logs.append(f"{log.csv_path.name} + .log ({log.count} frames)")

    def toggle_log(self):
        """Start logging / Stop logging. Off by default; each start makes a new file pair.
        Pressed before a capture, it arms logging so the capture logs from its first frame."""
        s = self.session
        if self.log_on:
            self.log_on = False
            self.log_btn.config(text="Start logging")
            log = s.stop_log() if s else None
            saved = []
            if log:
                self.note_saved(log)
                saved.append(f"{log.csv_path.name} and {log.log_path.name} ({log.count} frames)")
            extra = self.extra_log_stop()
            if extra:
                self.saved_logs.append(extra)
                saved.append(extra)
            self.status.set("Logging stopped. Saved " + ", ".join(saved) + "." if saved
                            else "Logging is off.")
            return
        try:
            self.extra_log_check()
        except ValueError as e:
            self.status.set(str(e))
            messagebox.showinfo("Start logging", str(e), parent=self)
            return
        if s and s.running:
            names = []
            try:
                if self.log_raw():
                    log = s.start_log(self.data_dir)
                    names.append(f"{log.csv_path.name} + .log")
                extra = self.extra_log_start(s)
                if extra:
                    names.append(extra)
            except OSError as e:
                s.stop_log()
                self.extra_log_stop()
                messagebox.showerror("Start logging",
                                     f"Could not create the log files in {self.data_dir}: {e}",
                                     parent=self)
                return
            self.status.set("Logging to " + " and ".join(names) + " from now on.")
        else:
            self.status.set("Logging is on: the next capture will be saved from its first frame.")
        self.log_on = True
        self.log_btn.config(text="Stop logging")

    # ------------------------------------------------- hooks for apps built on this one
    def make_table_frame(self):
        """The frame the CAN frame table is packed into."""
        tf = ttk.Frame(self)
        tf.pack(fill="both", expand=True, padx=10)
        return tf

    def frames_hook(self, frames):
        """Called on the UI thread with every batch of frames, in time order."""

    def tick_hook(self):
        """Called on every screen update (about 20 times a second)."""

    def capture_started_hook(self, session):
        """Called once the device is open."""

    def log_raw(self) -> bool:
        """Should Start logging save the raw CAN frames?"""
        return True

    def extra_log_check(self):
        """Raise ValueError(message) if logging cannot start as currently set up."""

    def extra_log_start(self, session):
        """Start any additional log. Returns its description for the status line, or None."""
        return None

    def extra_log_stop(self):
        """Stop any additional log. Returns 'name (N rows)' if one was written, else None."""
        return None

    def log_options(self, row):
        """Hook: extra logging controls placed right after the Start logging button."""

    def set_icon(self):
        """Window and taskbar icon. The .ico alone gives Windows a small image that looks
        blurry on the taskbar, so the PNG sizes made by make_icon.py are handed over too."""
        try:
            self.iconbitmap(default=str(self.ICON))
        except tk.TclError:
            pass
        imgs = []
        for size in (256, 64, 48, 32, 16):
            png = self.ICON.with_name(f"{self.ICON.stem}_{size}.png")
            if png.exists():
                try:
                    imgs.append(tk.PhotoImage(file=str(png)))
                except tk.TclError:
                    pass
        if imgs:
            self._icon_imgs = imgs                  # keep references or Tk drops them
            self.iconphoto(True, *imgs)

    def toggle_tools(self):
        self.show_tools(not self.tools_open)

    def show_tools(self, show: bool):
        self.tools_open = show
        if show:
            self.tabs.pack(fill="x", pady=(6, 0))
            self.tools_btn.config(text="▾ Hide Send frame / Script")
        else:
            self.tabs.pack_forget()
            self.tools_btn.config(text="▸ Send frame / Script")

    def extra_count_text(self) -> str:
        """Extra text for the counter beside the buttons."""
        return ""

    # ----------------------------------------------------------------- transmit
    def toggle_tx(self):
        s = self.session
        if not s:
            return
        if s.tx_enabled:
            self.stop_script()
            s.tx_enabled = False
            self.tx_btn.config(text="Enable transmit")
            self.status.set("Transmit is off. Receive only.")
        else:
            if not messagebox.askokcancel(
                    "Enable transmit",
                    "Sending frames puts traffic on the vehicle bus and modules will act on it.\n\n"
                    "Only frames you send with the Send button or a script you start will go "
                    "out. Enable transmit for this capture?", parent=self):
                return
            s.tx_enabled = True
            self.tx_btn.config(text="Disable transmit")
            self.status.set("Transmit is ON for this capture.")
        self.send_btn.config(state="normal" if s.tx_enabled else "disabled")
        self.update_script_buttons()

    def on_hex_edit(self, var, entry):
        raw = var.get()
        up = raw.upper()
        if up != raw:
            pos = entry.index("insert")
            var.set(up)
            entry.icursor(pos)
        self.send_hint.config(text=self.send_check()[1])

    def send_check(self):
        """-> ((id, ext, data) or None, hint text)"""
        idt, dt = self.id_var.get().strip(), self.data_var.get().strip()
        if not idt:
            return None, "ID: up to 3 hex digits = 11-bit, 4 to 8 digits = 29-bit.   Data: 0 to 8 hex bytes."
        try:
            arb_id, ext = canlog.parse_id(idt)
        except ValueError as e:
            return None, f"✖ {e}"
        try:
            data = canlog.parse_data(dt)
        except ValueError as e:
            return None, f"✖ {e}"
        idtxt = f"{arb_id:08X}" if ext else f"{arb_id:03X}"
        return (arb_id, ext, data), (f"{'29' if ext else '11'}-bit ID {idtxt}, {len(data)} data "
                                     f"byte{'s' if len(data) != 1 else ''}")

    def send_one(self):
        s = self.session
        if not s or not s.tx_enabled:
            self.status.set("Not sent: start a capture and click Enable transmit first.")
            return
        ok, hint = self.send_check()
        if not ok:
            self.status.set("Not sent: " + hint.lstrip("✖ "))
            return
        arb_id, ext, data = ok
        bus = self.send_bus.get()
        try:
            s.send(bus, arb_id, ext, data)
        except canlog.BusError as e:
            self.status.set(str(e))
            return
        idtxt = f"{arb_id:08X}" if ext else f"{arb_id:03X}"
        self.status.set(f"Sent {bus} {idtxt}  {data.hex(' ').upper()}")

    # ------------------------------------------------------------------- script
    def set_preview(self, text: str):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("end", text)
        self.preview.configure(state="disabled")

    def load_script(self):
        start = HERE / "examples"
        path = filedialog.askopenfilename(
            parent=self, title="Load script",
            initialdir=str(start if start.is_dir() else Path.cwd()),
            filetypes=[("CAN scripts", "*.canscript *.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text("utf-8-sig")
            self.script = canlog.parse_script(text)
        except (OSError, UnicodeDecodeError, canlog.ScriptError) as e:
            self.script = None
            self.script_label.config(text=f"{Path(path).name}: not loaded")
            self.set_preview(f"✖ {Path(path).name}: {e}")
            self.status.set(f"Script not loaded: {e}")
            self.update_script_buttons()
            return
        self.script_label.config(text=Path(path).name)
        self.set_preview(self.script.summary() + "\nNothing is sent until you press Start script.\n\n"
                         + "\n".join(self.script.preview()))
        self.status.set(f"Loaded {Path(path).name}. Check the preview, then press Start script.")
        self.update_script_buttons()

    def update_script_buttons(self):
        s = self.session
        ready = bool(self.script and s and s.tx_enabled and not (self.runner and self.runner.running))
        self.run_btn.config(state="normal" if ready else "disabled")

    def start_script(self):
        s = self.session
        if not (self.script and s):
            return
        q = self.q
        runner = canlog.ScriptRunner(s, self.script,
                                     on_event=lambda kind, text: q.put(("script", runner, (kind, text))))
        try:
            runner.start()
        except canlog.BusError as e:
            self.status.set(str(e))
            messagebox.showerror("Script", str(e), parent=self)
            return
        self.runner = runner
        self.run_btn.config(state="disabled")
        self.stop_script_btn.config(state="normal")
        self.status.set("Script running...")

    def stop_script(self):
        if self.runner and self.runner.running:
            self.runner.stop()

    # ------------------------------------------------------------------ display
    def set_columns(self):
        self.tree.configure(displaycolumns=COLS if self.overwrite else COLS[:7])

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="Resume" if self.paused else "Pause")
        if not self.paused:
            self.rebuild()
        elif self.session:
            self.status.set("Display paused." + (" The log file is still recording every frame."
                                                  if self.log_on else ""))

    def toggle_view(self):
        self.overwrite = not self.overwrite
        self.view_btn.config(text="Scrolling view" if self.overwrite else "Overwrite view")
        self.set_columns()
        self.rebuild()

    def on_filter_edit(self, *_):
        raw = self.filter_var.get()
        up = raw.upper()
        if up != raw:
            pos = self.filter_entry.index("insert")
            self.filter_var.set(up)  # does not re-fire this trace
            self.filter_entry.icursor(pos)
        try:
            self.filter = canlog.IdFilter(up)
            self.filter_hint.config(text="")
        except ValueError as e:
            self.filter_hint.config(text=f"✖ {e}   (examples: 7E8   7E0-7EF   !0C9   LS)")
            return
        self.rebuild()

    def clear(self):
        self.recent.clear()
        self.by_id.clear()
        self.skipped = 0
        self.rebuild()

    @staticmethod
    def values(f, count="", period=""):
        return (f"{f.ts:.4f}", f.bus, "Tx" if f.tx else "Rx", f.id_text, 29 if f.ext else 11,
                len(f.data), f.data_text, count, period)

    @staticmethod
    def key(f):
        return (f.bus, f.ext, f.arb_id, f.tx)

    # --------------------------------------------------------------------- sort
    def sort_by(self, col):
        """Heading click: ascending, then descending, then back to the normal order."""
        if self.sort_col != col:
            self.sort_col, self.sort_rev = col, False
        elif not self.sort_rev:
            self.sort_rev = True
        else:
            self.sort_col, self.sort_rev = None, False
        for c in COLS:
            mark = "" if c != self.sort_col else ("  ▼" if self.sort_rev else "  ▲")
            self.tree.heading(c, text=HEAD[c] + mark)
        if self.sort_col is None:
            self.rebuild()
            self.status.set("Sort off: " + ("ordered by bus and ID." if self.overwrite
                                            else "newest frames at the bottom."))
        else:
            self.apply_sort()
            self.status.set(f"Sorted by {HEAD[col]}, {'descending' if self.sort_rev else 'ascending'}. "
                            "Click the heading again to change.")

    @staticmethod
    def sort_value(col, text):
        """Sort key for one cell. Numbers sort as numbers, IDs as hex; blanks go first."""
        text = str(text)
        try:
            if col in ("time", "period"):
                return (1, float(text))
            if col == "id":
                return (1, int(text, 16))
            if col in ("bits", "dlc", "count"):
                return (1, int(text))
        except ValueError:
            return (0, 0)
        return (1, text)

    def apply_sort(self):
        self.sort_due = False
        self.sort_at = time.monotonic()
        col = self.sort_col
        if col is None:
            return
        i = COLS.index(col)
        tree = self.tree
        now = list(tree.get_children())
        # Python's sort is stable, so rows with equal cells keep their current order.
        want = sorted(now, key=lambda iid: self.sort_value(col, tree.item(iid, "values")[i]),
                      reverse=self.sort_rev)
        if want != now:
            for pos, iid in enumerate(want):
                tree.move(iid, "", pos)

    def rebuild(self):
        """Redraw the table from memory (after a filter/view change, Clear or Resume)."""
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.sorted_keys = []
        if self.overwrite:
            for k in sorted(self.by_id):
                self.put_overwrite(k)
        else:
            shown = [f for f in self.recent if self.filter.match(f)][-MAX_ROWS:]
            for f in shown:
                self.rows.append(self.tree.insert("", "end", values=self.values(f),
                                                  tags=("tx",) if f.tx else ()))
            if self.rows and self.sort_col is None:
                self.tree.see(self.rows[-1])
        self.apply_sort()
        self.update_count()

    def put_overwrite(self, k):
        f, n, period = self.by_id[k]
        if not self.filter.match(f):
            return
        vals = self.values(f, n, f"{period * 1000:.1f}" if period else "")
        iid = "|".join(str(x) for x in k)
        if self.tree.exists(iid):
            self.tree.item(iid, values=vals)
        else:
            pos = bisect.bisect(self.sorted_keys, k)
            self.sorted_keys.insert(pos, k)
            self.tree.insert("", pos if self.sort_col is None else "end", iid=iid, values=vals,
                             tags=("tx",) if f.tx else ())

    def add_frames(self, frames):
        self.frames_hook(frames)
        dirty = {}
        for f in frames:
            self.recent.append(f)
            k = self.key(f)
            e = self.by_id.get(k)
            if e is None:
                self.by_id[k] = [f, 1, 0.0]
            else:
                e[2] = f.ts - e[0].ts
                e[0] = f
                e[1] += 1
            dirty[k] = True
        if self.paused:
            return
        if self.overwrite:
            for k in dirty:
                self.put_overwrite(k)
            self.sort_due = True
            return
        shown = [f for f in frames if self.filter.match(f)]
        if len(shown) > MAX_PER_TICK:
            self.skipped += len(shown) - MAX_PER_TICK
            shown = shown[-MAX_PER_TICK:]
        if not shown:
            return
        follow = self.sort_col is None and self.tree.yview()[1] >= 0.999
        self.sort_due = True
        for f in shown:
            self.rows.append(self.tree.insert("", "end", values=self.values(f),
                                              tags=("tx",) if f.tx else ()))
        extra = len(self.rows) - MAX_ROWS
        if extra > 0:
            self.tree.delete(*[self.rows.popleft() for _ in range(extra)])
        if follow:
            self.tree.see(self.rows[-1])

    def update_count(self):
        s = self.session
        if not s:
            self.count.config(text="")
            return
        t = f"{s.rx_count} received · {s.tx_count} sent · {len(self.by_id)} IDs"
        log = s.log
        extra = self.extra_count_text()
        if log and not log.closed:
            t += f" · logging ({log.count} frames saved)"
        elif not extra:
            t += " · not logging"
        t += extra
        if self.skipped and not self.overwrite:
            t += f" · {self.skipped} not drawn"
        self.count.config(text=t)

    def open_folder(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.data_dir)  # Windows only

    # --------------------------------------------------------------------- pump
    def pump(self):
        frames = []
        try:
            while True:
                kind, src, arg = self.q.get_nowait()
                if kind == "detect":
                    if src == self.detect_gen:
                        self.on_detect(*arg)
                    continue
                if kind == "script":
                    if src is not self.runner:
                        continue
                    what, text = arg
                    self.status.set(text)
                    if what != "step":
                        self.stop_script_btn.config(state="disabled")
                        self.update_script_buttons()
                    continue
                if src is not self.session:
                    continue
                if kind == "frames":
                    frames.extend(arg)
                elif kind == "opened":
                    s = self.session
                    self.start_btn.config(text="Stop", state="normal")
                    self.tx_btn.config(state="normal")
                    if self.send_bus.get() not in s.buses:
                        self.send_bus.set(s.buses[0])
                    self.send_bus_box.config(values=s.buses)
                    names = [f"{s.log.csv_path.name} + .log"] if s.log else []
                    if self.log_on:
                        try:
                            extra = self.extra_log_start(s)
                            if extra:
                                names.append(extra)
                        except OSError as e:
                            names.append(f"(could not create the extra log: {e})")
                    log = ("  Logging to " + " and ".join(names) if names
                           else "  Not logging: press Start logging to save frames.")
                    self.status.set("Capturing, receive only. " + "; ".join(arg) + "." + log)
                    self.capture_started_hook(s)
                elif kind == "closed":
                    if frames:
                        self.add_frames(frames)
                        frames = []
                    self.capture_ended(arg)
        except queue.Empty:
            pass
        if frames:
            self.add_frames(frames)
        if self.sort_due and self.sort_col and time.monotonic() - self.sort_at >= RESORT_S:
            self.apply_sort()
        self.tick_hook()
        if self.session:
            self.update_count()
        self.after(TICK_MS, self.pump)

    def on_close(self):
        if self.session:
            self.session.stop(wait=2.0)
        self.destroy()


def detect_command(dll: str) -> list:
    """Command line of the helper process that checks one driver (see j2534.detect_child)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--detect", dll]
    return [sys.executable, str(Path(__file__).resolve().parent / "can_pro.pyw"), "--detect", dll]


# ------------------------------------------------------------------ command line
def probe(argv) -> int:
    """Receive-only check of a real device from a console. Sends nothing."""
    import argparse
    ap = argparse.ArgumentParser(prog="can_pro --probe")
    ap.add_argument("--probe", metavar="NAME", required=True, help="part of the device name, or 'sim'")
    ap.add_argument("--bus", default="LS", choices=["LS", "HS", "BOTH"])
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--show", metavar="FILTER", help="print frames matching this ID filter")
    ap.add_argument("--script", type=Path, metavar="FILE",
                    help="TRANSMITS: run this script after --seconds of listening")
    a = ap.parse_args(argv)
    show = canlog.IdFilter(a.show) if a.show else None
    script = canlog.parse_script(a.script.read_text("utf-8-sig")) if a.script else None
    buses = ("LS", "HS") if a.bus == "BOTH" else (a.bus,)
    if a.probe.lower() == "sim":
        backend = canlog.SimBackend()
    else:
        hits = [d for d in j2534.list_devices() if a.probe.lower() in d.label.lower()]
        if len(hits) != 1:
            print(f"'{a.probe}' matches {len(hits)} devices:", *[d.label for d in hits], sep="\n  ")
            return 2
        backend = j2534.J2534Backend(hits[0])
    seen = {}

    def on_frames(frames):
        for f in frames:
            seen[(f.bus, f.id_text)] = seen.get((f.bus, f.id_text), 0) + 1
            if f.tx or (show and show.match(f)):
                print(f"  {f.ts:9.4f} {f.bus} {'Tx' if f.tx else 'Rx'} {f.id_text:>8}  {f.data_text}")
    s = canlog.Session(backend, buses, on_frames=on_frames,
                       log_factory=lambda: canlog.LogWriter(canlog.DATA_DIR))
    s.start()
    try:
        s.wait_open(20)
    except canlog.BusError as e:
        print("FAILED:", e)
        return 1
    print("open:", "; ".join(s.notes))
    time.sleep(a.seconds)
    if script:
        print(script.summary(), *script.preview(), sep="\n    ")
        s.tx_enabled = True
        runner = canlog.ScriptRunner(s, script)
        try:
            runner.start()
            runner.join()
            print(runner.result[1])
        except canlog.BusError as e:
            print("script not run:", e)
        time.sleep(0.5)
    s.stop(wait=5)
    print(f"{s.rx_count} frames received, {s.tx_count} sent, {len(seen)} IDs"
          + (f", error: {s.error}" if s.error else ""))
    if not (show or script):
        for (bus, idt), n in sorted(seen.items()):
            print(f"  {bus} {idt:>8}  x{n}")
    if s.log:
        print("saved", s.log.csv_path, "and", s.log.log_path.name)
    return 0


def selftest() -> None:
    """--selftest: prove the built exe can list devices and capture from the simulator."""
    out = canlog.DATA_DIR / "selftest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        devs = j2534.list_devices()
        s = canlog.Session(canlog.SimBackend(), ("LS", "HS"))
        s.start()
        s.wait_open(5)
        time.sleep(1.0)
        s.stop(wait=3)
        msg = (f"ok: {struct.calcsize('P') * 8}-bit, {len(devs)} J2534 devices listed, "
               f"simulator gave {s.rx_count} frames\n" + "\n".join(d.label for d in devs))
    except Exception as e:
        msg = f"FAILED: {type(e).__name__}: {e}"
    out.write_text(msg, encoding="utf-8")


def main(app_cls=App):
    """Entry point shared by the launchers (can_pro.pyw, log_pro.pyw)."""
    if "--detect" in sys.argv:
        os._exit(j2534.detect_child(sys.argv[sys.argv.index("--detect") + 1]))
    elif "--selftest" in sys.argv:
        selftest()
    elif "--probe" in sys.argv:
        sys.exit(probe(sys.argv[1:]))
    else:
        app_cls().mainloop()


if __name__ == "__main__":
    main()
