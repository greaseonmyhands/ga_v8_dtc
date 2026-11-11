# CAN PRO: GM LS / HS CAN logger and script sender (J2534)

A small Windows window app for bench and vehicle work on GM modules. It logs CAN traffic
through any installed J2534 pass-thru device (normally the Autel V200) and can send single
frames or run a simple text script.

- **LS**: low-speed single-wire GMLAN, 33.3 kbps, OBD pin 1 (J2534-2 `SW_CAN_PS`).
- **HS**: high-speed CAN, 500 kbps, OBD pins 6/14 (J2534 `CAN`).
- Both at once, if the device allows two channels.
- 11-bit and 29-bit IDs on both buses.

It is **receive only** until you click **Enable transmit**. Nothing is ever sent when a
capture starts.

## What has and has not been tested

| | Simulator | Autel V200 on a vehicle (2026-11-11) |
|---|---|---|
| Window, views, filter, pause, log files | tested | not driven through the window |
| Listing the installed J2534 devices | - | tested |
| LS capture (receive only) | tested | **tested**: 649 frames in 5 s, 35 IDs, 29-bit and 11-bit |
| HS capture (receive only) | tested | **tested**: 5135 frames in 4 s, 55 IDs |
| LS + HS together | tested | **tested**: both channels open at once, 90 IDs |
| Device timestamps | - | **tested**: HS 0C9 averages 12.500 ms, LS 100 ms frames within 0.3 ms |
| Send box, script runner, both example scripts | tested | **not tested** |
| High-voltage wake-up (`hv`) | - | **not tested** |

The hardware captures were made with the console `--probe` (same capture code as the
window). Nothing has been transmitted through real hardware yet; treat sending as unproven
until it has been tried on a bench module.

## Running it

**The exe:** double-click `dist\CAN PRO.exe` (build it first, below). Logs go in a
`can_logs` folder beside the exe.

**From source:** double-click `tools\j2534_can\CAN PRO.bat`. Logs go in `data\j2534_can\`
(git-ignored).

The J2534 driver DLLs on this PC are all 32-bit, so the program must be 32-bit too. Under
64-bit Python the window opens but only the simulator will connect.

### Setup (once)

1. Install the interface's own J2534 driver (for the V200: Autel Maxi PC Suite). It is
   already installed if the device shows in the Device list.
2. To build or run from source: 32-bit Python 3.12, reached as `py -3.12-32`, then

       py -3.12-32 -m pip install pyinstaller

3. Build: run `tools\j2534_can\build_exe.bat`. Output: `dist\CAN PRO.exe`.

## Using it

1. **Device**: pick the interface. The list comes from
   `HKLM\SOFTWARE\WOW6432Node\PassThruSupport.04.04`. `Simulator (no hardware)` makes up
   traffic so you can try everything with nothing plugged in.
2. **Bus**: LS, HS, or LS + HS together.
3. **Start**. Frames scroll live: time, bus, direction, ID, 11/29-bit, DLC, data.
   The status line says what connected and which files are being written.
4. **Stop** ends the capture and closes the log files.

### Views

- **Scrolling view** (default): one row per frame, newest at the bottom. It follows the
  newest frame while you are scrolled to the bottom; scroll up to hold your place. It keeps
  the last 2000 rows and draws at most 300 new rows per screen update, so a busy bus cannot
  lock the window up. Rows it did not draw are counted on screen and are still in the log.
- **Overwrite view**: one row per ID, updated in place, with a count and the period in ms.
  Use this on a busy bus.
- **Pause** freezes the display only. The log files keep recording.
- **Clear** empties the display, not the log files.

### Filter

Type in the Filter box; it applies as you type. Space or comma separated:

| Text | Meaning |
|---|---|
| `7E8` | only that ID |
| `7E0-7EF` | a range |
| `!0C9` | hide an ID (or `!100-1FF` a range) |
| `LS` or `HS` | only that bus |

The filter changes what is shown, never what is logged. Right-click a row for
"Show only this ID", "Hide this ID", copy, and "Put in the Send frame box".

### Log files

Every capture is written, as it runs, to two files named `can_YYYYMMDD_HHMMSS`:

- **`.csv`**: `time_s, utc_time, bus, dir, id, id_bits, dlc, data`. Opens in Excel or
  anything else. Sent frames have `dir` = `Tx`.
- **`.log`**: Linux can-utils *candump* format, one line per frame:
  `(1700000000.001000) ls0 10242040#0102030405060708`. LS is interface `ls0`, HS is `hs0`.
  A 3-digit ID is 11-bit, an 8-digit ID is 29-bit.

Why candump rather than Vector `.asc`: it is the simplest format that SavvyCAN, can-utils
(`canplayer`, `log2asc`), python-can and Wireshark all read, it carries 29-bit IDs and two
buses cleanly, and it is easy to grep. `log2asc` converts it to `.asc` if needed. The
candump file does not mark sent frames; use the CSV for that.

Do not commit captures from vehicles. `data/j2534_can/` and `can_logs/` are in `.gitignore`.

### Sending one frame

On the **Send frame** tab:

1. Click **Enable transmit** and confirm. This lasts for the current capture only.
2. Pick the bus, type the ID and data in hex, click **Send** (or press Enter).

ID width follows the number of digits: up to 3 hex digits is an 11-bit ID (`7E0`), 4 to 8
digits is a 29-bit ID (`10242040`; write `00000123` for 29-bit ID 123). Data is 0 to 8 bytes,
`02 01 00` or `020100`. Sent frames show in blue with direction `Tx`.

### Scripts

On the **Script** tab: **Load script...** shows a preview of exactly what will be sent.
Nothing goes out until transmit is enabled and you press **Start script**. **Stop script**
ends it at once.

A script is a text file (`.canscript` or `.txt`), one command per line:

| Command | Meaning |
|---|---|
| `# text` | comment (also after a command) |
| `bus LS` / `bus HS` | default bus for the lines that follow |
| `send [LS\|HS] <id> [bytes...] [hv]` | send one frame. `hv` = high-voltage wake-up (LS only) |
| `wait <ms>` | wait that many milliseconds |
| `waitfor [LS\|HS] <id> [bytes...] [timeout <ms>] [continue]` | wait for a received frame with that ID whose data starts with the bytes; `??` matches any byte. Default timeout 1000 ms. On timeout the script stops, unless the line ends with `continue` |
| `repeat <n>` ... `end` | run the lines between n times. Can be nested |
| `repeat forever` ... `end` | run until Stop script. Must contain a `wait` |

Every `send` and `waitfor` needs a bus, on the line or from an earlier `bus` line. A
`waitfor` sees frames received since the previous `send`, so a fast reply is not missed.
Timing is good to roughly 5 to 20 ms; this is not a hard real-time sender.

Example:

    bus HS
    send 7E0 02 01 00 00 00 00 00 00      # request
    waitfor 7E8 ?? 41 00 timeout 1000     # reply
    repeat 5
        send LS 10242040 01 02 03
        wait 100
    end

Two examples are in `examples\`:

- `ls_wake_keepalive.canscript`: LS high-voltage wake-up, then functional TesterPresent
  every 2 s, 10 times.
- `hs_request_reply.canscript`: HS request to 7E0 and wait for the reply on 7E8. The
  simulator answers this one.

A script error (unknown command, more than 8 bytes, missing `end`, and so on) is reported
with its line number when the script is loaded, before anything can be sent.

## How it talks to the device

Standard J2534 04.04 calls on the vendor DLL named in the registry:
`PassThruOpen`, `PassThruConnect`, `PassThruIoctl`, `PassThruStartMsgFilter`,
`PassThruReadMsgs`, `PassThruWriteMsgs`, `PassThruDisconnect`, `PassThruClose`.

- HS: `Connect(CAN, CAN_ID_BOTH, 500000)`.
- LS: `Connect(SW_CAN_PS, CAN_ID_BOTH, 33333)`, then `SET_CONFIG J1962_PINS = 0x0100`
  (pin 1). If 33333 is refused it tries 33300.
- If the device refuses `CAN_ID_BOTH` it falls back to 29-bit only on LS or 11-bit only on
  HS, and says so in the status line.
- Pass-all filters (mask 0, pattern 0), one for 11-bit and one for 29-bit IDs.
- A device without `SW_CAN_PS`, an unplugged device, a device in use, or a refused second
  channel each give a plain message instead of a crash.
- Timestamps come from the device clock (microseconds), lined up with PC time at the first
  frame.

## Console checks (no window)

    py -3.12-32 tools\j2534_can\can_pro.pyw --probe V200 --bus LS --seconds 5
    py -3.12-32 tools\j2534_can\can_pro.pyw --probe sim --bus BOTH --seconds 2

`--probe` opens the named device receive-only, counts frames per ID for a few seconds,
saves the log files and prints a summary. It never sends.

## Tests

    python -m unittest discover -s tests -v

`tests\test_j2534_can.py` runs offline against the simulator and never opens a device.

## Files

| File | What |
|---|---|
| `can_pro.pyw` | the window app |
| `canlog.py` | frames, filter, log files, simulator, capture session, script language |
| `j2534.py` | J2534 registry listing and DLL calls |
| `examples\` | example scripts |
| `build_exe.bat`, `make_icon.py`, `can_pro.ico` | build and icon |
