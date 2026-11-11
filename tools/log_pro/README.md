# LOG PRO: CAN logger plus PID logging (J2534)

LOG PRO is CAN PRO with two more tabs. The **CAN bus** tab is CAN PRO's window: the live
table, filter, pause, overwrite view, Start/Stop logging, the send box and the script runner
(see [../j2534_can/README.md](../j2534_can/README.md)). It also adds:

- **PIDs**: pick signals ("PIDs") and watch their live values. You can log them on their
  own or together with the raw frames. The PID log is a CSV laid out like an HP Tuners VCM
  Scanner export, so VCM Scanner can open it.
- **Controls**: the CAN display's bidirectional controls (GM DeviceControl `$AE`).

Start it with `LOG PRO.bat` (source, 32-bit Python) or `dist\LOG PRO.exe`
(build it with `build_exe.bat`).

## Sending

LOG PRO is receive only until you choose to send:

| What | Sends anything? |
|---|---|
| Starting a capture, loading a DBC, choosing PIDs, broadcast PIDs, logging | No |
| Polled PIDs | Only after **Start polling** and its confirmation. It sends read-only requests (Mode 01 / `$22`) on HS until Stop polling or the end of the capture. |
| Controls | Only with **Enable transmit** on, and one control at a time, when you press Run or Hold to run |
| Send box, Script tab | Same as CAN PRO |

## PIDs tab

- **Load DBC...** loads a .dbc for LS, HS or either bus. **Auto** guesses the bus from the
  file name and IDs. GMLAN 29-bit DBCs that hold only the parameter ID still match the
  frames on the bus.
- **Add GM truck set** loads the signals the silverado-can-display logs: 86 HS broadcast
  signals and 46 polled PIDs (ECM `7E0`, BCM `241`, wideband `7E7`). A signal that the
  display maps to an HP Tuners channel takes that channel's name and unit.
- Search, then double-click a signal or press **Add ▶**. **Seen on bus only** hides
  messages that have not appeared in this capture.
- **PID row every** sets the PID log's sample interval, from 10 to 1000 ms. Each row holds
  the latest value of every PID. Rows follow the capture clock, so a quiet bus still gets
  rows.
- **Start logging saves** sets what Start logging writes: CAN frames + PIDs, PIDs only, or
  CAN frames only.
- The DBC list, chosen PIDs and settings come back the next time LOG PRO opens.

### The PID log and VCM Scanner

Files are named `pids_YYYYMMDD_HHMMSS.csv`. They are written with the same header lines as
a VCM Scanner "Export Log File" CSV (`[Log Information]`, `[Channel Information]`,
`[Channel Data]`).

The channel-id line says how VCM Scanner shows each column:

- **A PID with an HP Tuners channel** is written under that channel's id. VCM Scanner
  treats it as its own channel, so it shows the HP Tuners name and drives the gauges.
  Example: Engine RPM is id 12.
- **Any other PID** is written with id 0. VCM Scanner then shows the name and unit from
  the file. Example: "Tire Pressure LF  35.5 psi".

Checked by opening sample files in VCM Scanner 5.2.3 on 2026-11-11. A non-zero id that
HP Tuners does not know shows as "Unknown: <id>", which is why ids are not made up.

## Controls tab

Each control is the same frame and time limit the CAN display uses:

- fuel pump
- A/C relay
- cooling fans at 20 to 90 %
- spark retard
- injector cut
- the four windows (hold to run)

Each run follows the same sequence:

1. Start: `AE <cpid> <bytes>`. The module answers `EE <cpid>`.
2. Keep-alive: TesterPresent (`3E`) every 2 s.
3. End, on Stop, on the time limit, or when you release Hold to run: `AE 00`, then
   `20` ReturnToNormalMode.

The tab's safety rules:

- **Only one control runs at a time.**
- **It needs a capture with HS open and Enable transmit on.**
- **Moving vehicle: refused, and a running control is stopped.** A control counts as moving
  above 2 mph, read from HS `3E9`. If speed is not on the bus (a bench), LOG PRO asks you to
  confirm the vehicle is parked.
- **Engine tests run only at idle in P or N.** The engine has to be at 500 to 1500 rpm
  (`0C9`) with the gear (`1F5`) in Park or Neutral. If either value is missing, the test
  does not run.
- **Rows marked "not confirmed" do not run.** These are the injector cuts: the display
  has not proven those frames.
- **Stopping the capture ends a running control first.** Turning transmit off or closing
  the window does the same.
- **Some controls are left out on purpose:** door locks, remote start, the cluster, and
  anything that needs security access.

## What has and has not been tested

| | Simulator | Autel V200 on the bench: BCM, cluster, ECM (2026-11-11) |
|---|---|---|
| DBC load, search, PID list, live values, the three logging modes, settings restore | tested (window driven by script) | DBC decode of a bench capture: 88 of 90 IDs matched the GlobalA LS/HS DBCs |
| GM truck set: broadcast decode | tested | **tested**, receive only: 19 of the 31 broadcast messages present on the bench decoded (speed 0 mph, temperatures 68 °F, baro 12.98 psi) |
| Polling | tested: replies decoded; no reply → "waiting..."; NRC 31 → not supported; back-off | not yet |
| Controls: run, stop, time limit, hold to run, refusals, capture stop | tested | not yet |
| PID log opens in VCM Scanner | - | **tested** in VCM Scanner 5.2.3 (sample files): HP Tuners ids show as their channels; id 0 shows the file's name and unit |
| `LOG PRO.exe` | builds; selftest ok (32-bit, devices listed, simulator); window opens | - |

Offline tests: `python -m unittest discover -s tests` (`test_log_pro.py`, `test_log_pro_truck.py`).

## Files

| File | What it is |
|---|---|
| `logpro_ui.py` | The window (`LogProApp`, built on CAN PRO's `App`) |
| `dbc.py` | DBC parser and signal decoder |
| `pidlog.py` | PID log writer (HP Tuners CSV layout) |
| `truck.py`, `truck_catalog_data.py` | The built-in GM truck set |
| `import_display_catalog.py` | Regenerates `truck_catalog_data.py` from the silverado-can-display firmware catalogs |
| `poller.py` | Polled PIDs: one request per module at a time, timeouts, back-off |
| `controls.py` | Bidirectional control sequence and the interlock rules |
| `examples/simulator_demo.dbc` | A DBC that decodes the simulator's frames |

Logs go to `data\log_pro` when LOG PRO runs from source. The exe writes them to
`log_pro_logs` beside it. Both folders are git-ignored.
