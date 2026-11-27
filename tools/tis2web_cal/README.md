# CAL PRO (tis2web_cal)

Pulls the calibration history for each module of a GM VIN from the public TIS2Web
"SPS Info" page, <https://tis2web.service.opel.com/tis2web/>, by driving a real Chrome or
Edge window (kept minimized). Personal, low-volume use only.

## How the site works (what the tool clicks through)

0. First the vehicle's RPO codes, engine and transmission are fetched the same way RPO PRO
   does (gmpartsgiant.com), and saved with RPO PRO's data, so a VIN RPO PRO already looked
   up costs nothing extra.
1. Start page: enter the VIN, press **Get CAL ID**. An unknown VIN gives `E2769: Invalid VIN!`.
2. **Controller** list: the controllers TIS has for that VIN (17 for a 2014 Silverado).
3. **Function**: "Programming" (bulletin-only functions are left out).
4. Sometimes TIS asks about the vehicle first: "Audio System", "Vehicle Options",
   "Programming Type", "Hardware" and so on. The tool answers from the RPO codes
   (e.g. "RPO IO4/IO5/IO6 (Uplevel radio)" because the truck has IO4), "Normal" for
   Programming Type. If the only choice needs an option the vehicle lacks (say RPO UEU on
   a truck without it), the controller is marked **not fitted** without clicking on. If
   the codes cannot tell (e.g. a hardware part number read with GDS), every choice is
   fetched and the results are grouped by answer.
5. **Module** page: has a **Complete History** button that opens every module of that
   controller with all its calibration part numbers, CVNs and descriptions, newest first.
6. Back to the controller list, next controller.

TIS sometimes has nothing to show for the choice that fits: "no valid calibration"
(`sps.exception.no-valid-cop`) is shown as not fitted; an internal error (seen with
`E4451` on a T3 amplifier with RPO UQ5, and the same when done by hand) is shown as a
**TIS error** note rather than a failure.

The form and field names are random per session, so the tool finds things by their labels.
The site ignores a second click within 5 s of the previous one; the tool waits that out,
which also keeps its pace gentle (about 15-20 s per controller).

Where TIS lists more than one valid calibration for a module it says so (message `M4455`,
e.g. "Only for UQA/UQG/UQS speaker packages" vs "For UQ3/UQ5"); both options are kept with
that note. Where the RPO codes match one of them, it is marked as the one that fits.

## Setup

```bash
pip install playwright
```

It uses the installed Google Chrome (or Microsoft Edge) with its own profile in
`tools/tis2web_cal/.profile/` (git-ignored).

## Window app

Double-click `CAL PRO.exe` (or run `python tools/tis2web_cal/cal_pro_gui.pyw`).

1. Type the VIN (capitals forced, live `n / 17` check) and press **Find modules**. TIS's
   controller list for that VIN appears as checkboxes. A saved VIN shows its list without a
   lookup.
2. Tick what you want (**All**, **None**, **Not saved**) and press **Get history**. Each
   module shows queued / working / saved as it goes, with a progress bar. **Stop** stops
   after the current module. Each module is saved as soon as it arrives.
3. Results: controller headings, module name and current part number in bold, earlier
   parts in grey (**Current only** hides them). Filter by any text; right-click to copy a
   part number or row; **Copy current parts** copies controller / module / part lines.
   Under each controller it says what TIS asked and what was answered, and why.

**Refresh data** asks TIS again (module list plus the ticked modules) and replaces what is
saved. **Save as CSV** writes `<VIN>.csv`.

## Command line

```bash
python tools/tis2web_cal/tis2web_cal.py <VIN> --list               # controllers TIS has
python tools/tis2web_cal/tis2web_cal.py <VIN> --modules P16,K20    # by code or name word
python tools/tis2web_cal/tis2web_cal.py <VIN>                      # every controller
python tools/tis2web_cal/tis2web_cal.py <VIN> --csv --refresh
python tools/tis2web_cal/tis2web_cal.py --from-html page.html      # parse a saved history page
```

Results are saved to `data/tis2web_cal/<VIN>.json` (git-ignored), one file per VIN,
added to as you fetch more modules. Already saved modules are not fetched again unless
`--refresh`. Exit codes: `0` ok, `1` something failed, `2` bad input, `3` stopped at a
block page.

## Standalone exe

`tools\tis2web_cal\build_exe.bat` builds `dist\CAL PRO.exe` (`pip install pyinstaller`
first). It needs Chrome or Edge on the PC, not Python. It saves results in a `cal_pro_data`
folder beside the exe and keeps its browser profile in `%LOCALAPPDATA%\cal_pro\profile`.

## If the site blocks it

If a CAPTCHA, block page, login or HTTP 403/429 appears, the tool stops, brings the
browser window up and leaves it to you. No CAPTCHA solving, stealth or proxies.

## Tests

```bash
python -m unittest tests.test_tis2web_cal -v
```

Offline only: they parse trimmed copies of the site's pages in `tests/fixtures/`
(made-up VIN) and never open a browser.

## Later

The window is a `CalPanel` frame, so it can be added to RPO PRO as a tab.
