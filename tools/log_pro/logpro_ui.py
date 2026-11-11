"""The LOG PRO window: CAN PRO's capture window plus a PID side.

The PID side loads DBC files, lets you pick signals ("PIDs") from them, shows their live
values decoded from the frames on the bus, and logs them, alone or together with the raw
CAN frames, in a CSV laid out like an HP Tuners VCM Scanner export.

Listen-only: decoding PIDs from broadcast frames transmits nothing.
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "j2534_can"))
import canlog  # noqa: E402
import dbc as dbcmod  # noqa: E402
import pidlog  # noqa: E402
from canpro_ui import App, main  # noqa: E402,F401

if getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent / "log_pro_logs"     # beside the exe
else:
    DATA_DIR = HERE.parents[1] / "data" / "log_pro"

LOG_MODES = ("CAN frames + PIDs", "PIDs only", "CAN frames only")
INTERVALS_MS = ("10", "20", "50", "100", "250", "500", "1000")
BUS_SCOPES = ("Auto", "LS", "HS", "Any")
MAX_AVAILABLE_ROWS = 2000
VALUE_REFRESH_TICKS = 4        # live values redraw every 4 screen updates (5 times a second)
IDLE_HOLD_S = 0.3              # PID rows are written this far behind the capture clock
A_COLS = ("signal", "message", "id", "bus", "unit")
A_HEAD = {"signal": "Signal", "message": "Message", "id": "ID", "bus": "Bus", "unit": "Unit"}
A_WIDTH = {"signal": 190, "message": 200, "id": 78, "bus": 40, "unit": 60}
S_COLS = ("pid", "value", "unit", "bus", "id", "updates")
S_HEAD = {"pid": "PID", "value": "Value", "unit": "Unit", "bus": "Bus", "id": "ID",
          "updates": "Updates"}
S_WIDTH = {"pid": 190, "value": 150, "unit": 60, "bus": 40, "id": 78, "updates": 70}


@dataclass
class LoadedDbc:
    path: str
    bus: str                   # "LS", "HS" or "Any": which bus its messages are matched on
    dbc: object


@dataclass
class Pid:
    """One chosen signal."""
    bus: str
    signal: object

    @property
    def message(self):
        return self.signal.message

    @property
    def key(self):
        m = self.message
        return (self.bus, m.ext, m.frame_id, self.signal.name)


class LogProApp(App):
    TITLE = "LOG PRO"
    MARK = ("LOG", "PRO")
    ICON = RES / "log_pro.ico"
    SIZE = (1180, 780)

    def __init__(self, data_dir: Path | None = None, detect: bool = True):
        super().__init__(data_dir or DATA_DIR, detect)
        self.restore_settings()

    # ------------------------------------------------------------------- layout
    def make_table_frame(self):
        """Called by the base window while it builds: put the CAN frame table and the PID
        side on two tabs."""
        self.dbcs: list[LoadedDbc] = []
        self.available: list[Pid] = []     # every signal of every loaded DBC
        self.shown: list[Pid] = []         # the ones listed after search / seen filter
        self.pids: list[Pid] = []          # chosen
        self.latest: dict = {}             # pid key -> [value, updates]
        self.watch: dict = {}              # (ext, frame id) -> [Pid]
        self.watch_param: dict = {}        # GMLAN parameter id -> [Pid]
        self.pid_log: pidlog.PidLog | None = None
        self.seen_only = False
        self.ticks = 0
        self.settings_ready = False
        k = self.ui_scale

        self.main_tabs = ttk.Notebook(self)
        self.main_tabs.pack(fill="both", expand=True, padx=10)
        can_tab = ttk.Frame(self.main_tabs, padding=(0, 6, 0, 0))
        self.main_tabs.add(can_tab, text="CAN bus")
        pid_tab = ttk.Frame(self.main_tabs, padding=8)
        self.main_tabs.add(pid_tab, text="PIDs")

        # --- row 1: DBC files
        row = ttk.Frame(pid_tab)
        row.pack(fill="x")
        ttk.Button(row, text="Load DBC...", command=self.load_dbc_dialog).pack(side="left")
        ttk.Label(row, text="for bus:").pack(side="left", padx=(10, 0))
        self.scope_var = tk.StringVar(value="Auto")
        ttk.Combobox(row, textvariable=self.scope_var, state="readonly", width=5,
                     values=BUS_SCOPES).pack(side="left", padx=6)
        ttk.Button(row, text="Clear DBCs", command=self.clear_dbcs).pack(side="left")
        self.dbc_label = ttk.Label(row, text="No DBC loaded. Load one to list its signals.")
        self.dbc_label.pack(side="left", padx=10)

        # --- row 2: logging choice
        row = ttk.Frame(pid_tab)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Start logging saves:").pack(side="left")
        self.mode_var = tk.StringVar(value=LOG_MODES[0])
        self.mode_box = ttk.Combobox(row, textvariable=self.mode_var, state="readonly", width=19,
                                     values=LOG_MODES)
        self.mode_box.pack(side="left", padx=6)
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self.save_settings())
        ttk.Label(row, text="PID row every").pack(side="left", padx=(10, 0))
        self.interval_var = tk.StringVar(value="100")
        self.interval_box = ttk.Combobox(row, textvariable=self.interval_var, state="readonly",
                                         width=5, values=INTERVALS_MS)
        self.interval_box.pack(side="left", padx=6)
        self.interval_box.bind("<<ComboboxSelected>>", lambda e: self.save_settings())
        ttk.Label(row, text="ms").pack(side="left")
        self.pid_hint = tk.Label(row, text="", font=("Segoe UI", 9), fg="#000000", anchor="w")
        self.pid_hint.pack(side="left", padx=12)

        body = ttk.Panedwindow(pid_tab, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(8, 0))

        # --- left: signals in the loaded DBCs
        left = ttk.Frame(body, padding=(0, 0, 6, 0))
        top = ttk.Frame(left)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var, width=18)
        self.search_entry.pack(side="left", padx=6, fill="x", expand=True)
        self.search_var.trace_add("write", lambda *a: self.fill_available())
        self.seen_btn = ttk.Button(top, text="Seen on bus only", width=17, command=self.toggle_seen)
        self.seen_btn.pack(side="left")
        self.add_btn = ttk.Button(top, text="Add  ▶", width=8, command=self.add_selected)
        self.add_btn.pack(side="left", padx=(6, 0))
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True, pady=(6, 0))
        self.avail_tree = ttk.Treeview(lf, columns=A_COLS, show="headings", height=6,
                                       style="Can.Treeview", selectmode="extended")
        for c in A_COLS:
            self.avail_tree.heading(c, text=A_HEAD[c], anchor="w")
            self.avail_tree.column(c, width=int(A_WIDTH[c] * k), anchor="w", stretch=(c == "message"))
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.avail_tree.yview)
        self.avail_tree.configure(yscrollcommand=sb.set)
        self.avail_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.avail_count = ttk.Label(left, text="")
        self.avail_count.pack(anchor="w", pady=(4, 0))
        body.add(left, weight=1)

        # --- right: chosen PIDs with live values
        right = ttk.Frame(body, padding=(6, 0, 0, 0))
        top = ttk.Frame(right)
        top.pack(fill="x")
        ttk.Label(top, text="PIDs to show and log:").pack(side="left")
        ttk.Button(top, text="Clear list", width=10, command=self.clear_pids).pack(side="right")
        ttk.Button(top, text="Remove", width=9, command=self.remove_selected).pack(side="right", padx=6)
        rf = ttk.Frame(right)
        rf.pack(fill="both", expand=True, pady=(6, 0))
        self.pid_tree = ttk.Treeview(rf, columns=S_COLS, show="headings", height=6,
                                     style="Can.Treeview", selectmode="extended")
        for c in S_COLS:
            self.pid_tree.heading(c, text=S_HEAD[c], anchor="w")
            self.pid_tree.column(c, width=int(S_WIDTH[c] * k), anchor="w", stretch=(c == "value"))
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.pid_tree.yview)
        self.pid_tree.configure(yscrollcommand=sb.set)
        self.pid_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.pid_count = ttk.Label(right, text="")
        self.pid_count.pack(anchor="w", pady=(4, 0))
        body.add(right, weight=1)

        self.avail_tree.bind("<Double-1>", lambda e: self.add_selected())
        self.avail_tree.bind("<Return>", lambda e: self.add_selected())
        self.avail_tree.bind("<Button-3>", self.avail_menu)
        self.pid_tree.bind("<Delete>", lambda e: self.remove_selected())
        self.pid_tree.bind("<Button-3>", self.pid_menu)
        self.pid_tree.bind("<Control-c>", lambda e: self.copy_pid_rows())
        self.search_entry.bind("<Button-3>", self.entry_menu)
        self.update_pid_labels()
        return can_tab

    # ---------------------------------------------------------------- DBC files
    def load_dbc_dialog(self):
        start = RES / "examples"
        paths = filedialog.askopenfilenames(
            parent=self, title="Load DBC",
            initialdir=str(start if start.is_dir() and not self.dbcs else Path.cwd()),
            filetypes=[("DBC files", "*.dbc"), ("All files", "*.*")])
        for p in paths:
            self.load_dbc(p, self.scope_var.get())

    def load_dbc(self, path, scope: str = "Auto", quiet: bool = False) -> bool:
        path = str(Path(path))
        try:
            d = dbcmod.load(path)
        except (OSError, dbcmod.DbcError) as e:
            if not quiet:
                self.status.set(f"DBC not loaded: {e}")
                messagebox.showerror("Load DBC", f"Could not load {Path(path).name}:\n\n{e}", parent=self)
            return False
        bus = dbcmod.guess_bus(d.name) if scope == "Auto" else scope
        self.dbcs = [x for x in self.dbcs if x.path != path]          # reloading replaces it
        self.dbcs.append(LoadedDbc(path, bus, d))
        self.rebuild_available()
        if not quiet:
            where = {"LS": "the LS bus", "HS": "the HS bus", "Any": "either bus"}[bus]
            self.status.set(f"Loaded {d.name}: {len(d.messages)} messages, {d.signal_count} "
                            f"signals, matched on {where}.")
        self.save_settings()
        return True

    def clear_dbcs(self):
        if self.pid_log:
            return self.status.set("Stop logging before changing the DBC files.")
        self.dbcs = []
        self.pids = []
        self.rebuild_available()
        self.rebuild_watch()
        self.fill_pids()
        self.status.set("DBC files and the PID list cleared.")
        self.save_settings()

    def rebuild_available(self):
        self.available = [Pid(x.bus, s) for x in self.dbcs for m in x.dbc.messages for s in m.signals]
        self.available.sort(key=lambda p: (p.signal.name.lower(), p.message.name.lower()))
        self.fill_available()
        self.update_pid_labels()

    def seen_sets(self):
        exact, param = set(), set()
        for (bus, ext, arb_id, tx) in self.by_id:
            if not tx:
                exact.add((bus, ext, arb_id))
                if ext:
                    param.add((bus, arb_id & dbcmod.GMLAN_PARAM_MASK))
        return exact, param

    @staticmethod
    def is_seen(p: Pid, exact, param) -> bool:
        m = p.message
        buses = ("LS", "HS") if p.bus == "Any" else (p.bus,)
        if m.param_only:
            return any((b, m.frame_id) in param for b in buses)
        return any((b, m.ext, m.frame_id) in exact for b in buses)

    def fill_available(self):
        t = self.avail_tree
        t.delete(*t.get_children())
        needle = self.search_var.get().strip().lower()
        words = needle.split()
        exact, param = self.seen_sets() if self.seen_only else (None, None)
        self.shown = []
        for p in self.available:
            if words:
                hay = f"{p.signal.name} {p.message.name} {p.message.id_text} {p.signal.unit}".lower()
                if not all(w in hay for w in words):
                    continue
            if self.seen_only and not self.is_seen(p, exact, param):
                continue
            self.shown.append(p)
        for i, p in enumerate(self.shown[:MAX_AVAILABLE_ROWS]):
            m = p.message
            t.insert("", "end", iid=str(i),
                     values=(p.signal.name, m.name, m.id_text, p.bus, p.signal.unit))
        n, total = len(self.shown), len(self.available)
        text = f"{n} of {total} signals" if (words or self.seen_only) else f"{total} signals"
        if n > MAX_AVAILABLE_ROWS:
            text += f"; first {MAX_AVAILABLE_ROWS} listed, type in Search to narrow"
        if self.seen_only and not self.by_id:
            text += "; nothing seen yet, start a capture"
        self.avail_count.config(text=text if total else "")

    def toggle_seen(self):
        self.seen_only = not self.seen_only
        self.seen_btn.config(text="Show all signals" if self.seen_only else "Seen on bus only")
        self.fill_available()

    # ----------------------------------------------------------------- PID list
    def add_selected(self):
        picks = [self.shown[int(i)] for i in self.avail_tree.selection()]
        if picks:
            self.add_pids(picks)
        return "break"

    def add_pids(self, picks, quiet: bool = False):
        if self.pid_log:
            return self.status.set("Stop logging before changing the PID list (its columns are fixed).")
        have = {p.key for p in self.pids}
        new = [p for p in picks if p.key not in have and not have.add(p.key)]
        self.pids.extend(new)
        self.rebuild_watch()
        self.fill_pids()
        if not quiet:
            self.status.set(f"Added {len(new)} PID{'s' if len(new) != 1 else ''}."
                            if new else "Already in the list.")
        self.save_settings()

    def remove_selected(self):
        if self.pid_log:
            return self.status.set("Stop logging before changing the PID list (its columns are fixed).")
        gone = {int(i) for i in self.pid_tree.selection()}
        if gone:
            self.pids = [p for i, p in enumerate(self.pids) if i not in gone]
            self.rebuild_watch()
            self.fill_pids()
            self.save_settings()
        return "break"

    def clear_pids(self):
        if self.pid_log:
            return self.status.set("Stop logging before changing the PID list (its columns are fixed).")
        self.pids = []
        self.rebuild_watch()
        self.fill_pids()
        self.save_settings()

    def rebuild_watch(self):
        self.watch, self.watch_param = {}, {}
        for p in self.pids:
            m = p.message
            if m.param_only:
                self.watch_param.setdefault(m.frame_id, []).append(p)
            else:
                self.watch.setdefault((m.ext, m.frame_id), []).append(p)
        keep = {p.key for p in self.pids}
        self.latest = {k: v for k, v in self.latest.items() if k in keep}

    def pid_values(self, p: Pid):
        value, updates = self.latest.get(p.key, (None, 0))
        m = p.message
        return (p.signal.name, p.signal.text(value) if updates else "", p.signal.unit, p.bus,
                m.id_text, updates or "")

    def fill_pids(self):
        t = self.pid_tree
        t.delete(*t.get_children())
        for i, p in enumerate(self.pids):
            t.insert("", "end", iid=str(i), values=self.pid_values(p))
        self.update_pid_labels()

    def refresh_values(self):
        t = self.pid_tree
        for i, p in enumerate(self.pids):
            if t.exists(str(i)):
                t.item(str(i), values=self.pid_values(p))

    def update_pid_labels(self):
        if self.dbcs:
            self.dbc_label.config(text="   ".join(
                f"{x.dbc.name} → {x.bus} ({x.dbc.signal_count} signals)" for x in self.dbcs))
        else:
            self.dbc_label.config(text="No DBC loaded. Load one to list its signals.")
        n = len(self.pids)
        self.pid_count.config(text=f"{n} PID{'s' if n != 1 else ''} chosen" if n else
                              "None chosen. Double-click a signal on the left to add it.")
        self.main_tabs.tab(1, text=f"PIDs ({n})" if n else "PIDs")

    # -------------------------------------------------------------- right-click
    def avail_menu(self, event):
        iid = self.avail_tree.identify_row(event.y)
        if iid and iid not in self.avail_tree.selection():
            self.avail_tree.selection_set(iid)
        sel = self.avail_tree.selection()
        p = self.shown[int(iid)] if iid else None
        return self.popup(event, [
            (f"Add {len(sel)} to the PID list" if len(sel) > 1 else "Add to the PID list",
             self.add_selected, bool(sel)),
            ("Add every signal of this message",
             lambda: self.add_pids([Pid(p.bus, s) for s in p.message.signals]), bool(p)),
            None,
            ("Copy signal name", lambda: self.set_clip(p.signal.name), bool(p)),
            (f"Show ID {p.message.id_text} on the CAN bus tab" if p else "Show this ID on the CAN bus tab",
             lambda: (self.filter_var.set(p.message.id_text), self.main_tabs.select(0)),
             bool(p) and not p.message.param_only),
        ])

    def copy_pid_rows(self):
        sel = self.pid_tree.selection()
        if sel:
            self.set_clip("\n".join("  ".join(str(v) for v in self.pid_tree.item(i, "values") if str(v))
                                    for i in sel))
        return "break"

    def pid_menu(self, event):
        iid = self.pid_tree.identify_row(event.y)
        if iid and iid not in self.pid_tree.selection():
            self.pid_tree.selection_set(iid)
        sel = self.pid_tree.selection()
        return self.popup(event, [
            ("Copy row" + ("s" if len(sel) > 1 else ""), self.copy_pid_rows, bool(sel)),
            ("Remove from the list", self.remove_selected, bool(sel)),
            None,
            ("Select all", lambda: self.pid_tree.selection_set(self.pid_tree.get_children()),
             bool(self.pids)),
        ])

    # ------------------------------------------------------------------ decoding
    def frames_hook(self, frames):
        watch, watch_param, latest, log = self.watch, self.watch_param, self.latest, self.pid_log
        if not (watch or watch_param or log):
            return
        mask = dbcmod.GMLAN_PARAM_MASK
        for f in frames:
            if log and f.ts >= log.next_ts:
                log.advance(f.ts)                 # rows up to here use the values before this frame
            if f.tx:
                continue
            hits = watch.get((f.ext, f.arb_id))
            if f.ext and watch_param:
                more = watch_param.get(f.arb_id & mask)
                if more:
                    hits = (hits or []) + more
            if not hits:
                continue
            for p in hits:
                if p.bus != "Any" and p.bus != f.bus:
                    continue
                s = p.signal
                if isinstance(s.mux, int):
                    mux = s.message.multiplexor()
                    if mux is None or mux.raw(f.data) != s.mux:
                        continue
                v = s.decode(f.data)
                if v is None:
                    continue
                e = latest.get(p.key)
                if e is None:
                    latest[p.key] = [v, 1]
                else:
                    e[0] = v
                    e[1] += 1
                if log:
                    log.update(p.key, v)

    def tick_hook(self):
        self.ticks += 1
        s, log = self.session, self.pid_log
        if log and s:
            log.advance(s.backend.now() - IDLE_HOLD_S)     # keep rows coming on a quiet bus
            if self.ticks % 20 == 0:
                log.flush()
        if self.ticks % VALUE_REFRESH_TICKS == 0 and self.pids and not self.paused:
            self.refresh_values()
        if self.seen_only and self.ticks % 40 == 0 and s:
            keep = self.avail_tree.selection()
            self.fill_available()
            self.avail_tree.selection_set([i for i in keep if self.avail_tree.exists(i)])

    def capture_started_hook(self, session):
        for e in self.latest.values():
            e[1] = 0
        self.latest = {}
        self.fill_pids()

    def clear(self):
        super().clear()
        if hasattr(self, "latest"):
            self.latest = {}
            self.fill_pids()

    # ------------------------------------------------------------------- logging
    def log_raw(self) -> bool:
        return self.mode_var.get() != "PIDs only"

    def log_pids(self) -> bool:
        return self.mode_var.get() != "CAN frames only" and bool(self.pids)

    def extra_log_check(self):
        if self.mode_var.get() == "PIDs only" and not self.pids:
            raise ValueError("No PIDs are chosen. On the PIDs tab, load a DBC and add the signals "
                             "to log, or set 'Start logging saves' to include CAN frames.")

    def extra_log_start(self, session):
        if not self.log_pids() or self.pid_log:
            return None
        cols = [pidlog.Column(p.key, p.signal.name, p.signal.unit) for p in self.pids]
        self.pid_log = pidlog.PidLog(self.data_dir, cols, int(self.interval_var.get()) / 1000.0,
                                     start_ts=session.backend.now(),
                                     notes=f"LOG PRO, {session.backend.name}, "
                                           f"{' + '.join(session.buses)}")
        for p in self.pids:                          # start from the values already on screen
            e = self.latest.get(p.key)
            if e:
                self.pid_log.update(p.key, e[0])
        self.mode_box.config(state="disabled")
        self.interval_box.config(state="disabled")
        return f"{self.pid_log.path.name} ({len(cols)} PIDs)"

    def extra_log_stop(self):
        log, self.pid_log = self.pid_log, None
        if not log:
            return None
        if self.session:
            log.advance(self.session.backend.now() - IDLE_HOLD_S)
        log.close()
        self.mode_box.config(state="readonly")
        self.interval_box.config(state="readonly")
        return f"{log.path.name} ({log.rows} rows)"

    def extra_count_text(self) -> str:
        log = self.pid_log
        return f" · logging {len(log.columns)} PIDs ({log.rows} rows)" if log else ""

    # ------------------------------------------------------------------ settings
    @property
    def settings_path(self) -> Path:
        return self.data_dir / "log_pro_settings.json"

    def save_settings(self):
        if not self.settings_ready:
            return
        data = {
            "dbcs": [{"path": x.path, "bus": x.bus} for x in self.dbcs],
            "pids": [{"bus": p.bus, "ext": p.message.ext, "id": p.message.frame_id,
                      "signal": p.signal.name} for p in self.pids],
            "log_mode": self.mode_var.get(),
            "interval_ms": self.interval_var.get(),
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def restore_settings(self):
        """Reload the DBC files and PID list from last time, where they still exist."""
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        missing = []
        for d in data.get("dbcs", []):
            if not self.load_dbc(d.get("path", ""), d.get("bus", "Auto"), quiet=True):
                missing.append(Path(d.get("path", "?")).name)
        by_key = {p.key: p for p in self.available}
        picks = [by_key[k] for k in ((d.get("bus"), d.get("ext"), d.get("id"), d.get("signal"))
                                     for d in data.get("pids", [])) if k in by_key]
        if picks:
            self.add_pids(picks, quiet=True)
        if data.get("log_mode") in LOG_MODES:
            self.mode_var.set(data["log_mode"])
        if str(data.get("interval_ms")) in INTERVALS_MS:
            self.interval_var.set(str(data["interval_ms"]))
        self.settings_ready = True
        if missing:
            self.status.set("Could not reload from last time: " + ", ".join(missing))
        elif self.dbcs:
            self.status.set(f"Reloaded {len(self.dbcs)} DBC file{'s' if len(self.dbcs) != 1 else ''} "
                            f"and {len(self.pids)} PIDs from last time.")
