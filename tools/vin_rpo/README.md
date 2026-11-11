# RPO PRO (vin_rpo)

Looks up the factory RPO codes for a GM VIN using the free decoder at
<https://www.gmpartsgiant.com/vin-decoder.html>, by driving a real, visible Chrome window.
Personal, low-volume use only.

## Setup

```bash
pip install playwright
playwright install chrome   # only if Google Chrome is not already installed
```

The tool uses its own persistent Chrome profile in `tools/vin_rpo/.profile/` (git-ignored),
not your everyday profile, so it works while your normal Chrome is open.

## Usage

```bash
python tools/vin_rpo/vin_rpo.py <VIN> [<VIN>...]      # print codes, write data/vin_rpo/<VIN>.json
python tools/vin_rpo/vin_rpo.py <VIN> --csv           # also write data/vin_rpo/<VIN>.csv
python tools/vin_rpo/vin_rpo.py <VIN> --refresh       # ignore the cached JSON
python tools/vin_rpo/vin_rpo.py --from-html page.html # parse a saved results page, no browser
```

- VINs are validated first (17 characters, no I/O/Q, check digit); a bad one exits before any
  browser opens.
- A VIN that already has `data/vin_rpo/<VIN>.json` is printed from that file; no lookup is made.
- Lookups run one at a time with a pause between them (`--delay`, default 5 s, minimum 3 s).

Exit codes: `0` ok, `1` a VIN had no result, `2` bad input / Playwright missing, `3` stopped
because the site showed a block page.

## Window app

Double-click `tools/vin_rpo/RPO PRO.bat` (or `vin_rpo_gui.pyw` if `.pyw` files open with
Python on your machine). Type one or more VINs, press Enter, and the codes show in a filterable
table. Saved VINs are listed on the left and open without a lookup. It uses the same code, cache
folder and stop-on-block behaviour as the command line.

Window buttons: **Refresh data** looks the VIN up again and replaces the saved result (it asks first); **Save as CSV** writes the result on screen to `<VIN>.csv` in the data folder.

Window features: the VIN box forces capitals and shows a live `n / 17` count (red until the
VIN is complete and its check digit is right); **Cluster RPOs only** narrows the table to the
codes the gauge-cluster XML files reference (list in `CLUSTER_RPOS` in `vin_rpo.py`; the CLI
has `--cluster`). The site lists engine and transmission only as text, not as RPO codes
(no `LV3` / `MYC` rows), so check those yourself.

The window app always keeps the real browser window minimized during a lookup and brings it
back if the site needs you (CLI: `--minimized`). It is not headless mode.

## Standalone exe

`tools\vin_rpo\build_exe.bat` builds `dist\RPO PRO.exe` (`pip install pyinstaller`
first). The exe needs no Python on the target PC, but it does need Google Chrome or Microsoft
Edge installed (Chrome is tried first; Edge ships with Windows 10/11). It saves results in a
`vin_rpo_data` folder beside the exe and keeps its browser profile in
`%LOCALAPPDATA%\vin_rpo\profile`.

## Output

```json
{
  "vin": "...", "lookup_date": "2026-11-10",
  "source_url": "https://www.gmpartsgiant.com/vin-decoder.html?vin=...",
  "source": "api-response",
  "vehicle": "...", "vehicle_information": {}, "major_attributes": {}, "catalog": {},
  "codes": [{"code": "AKO", "description": "WINDOW TYPE PRIVACY", "category": "WNT"}]
}
```

`category` is the site's row label (e.g. `WNT`); `code` is the RPO the vehicle actually carries.

## How it works

The results page is `vin-decoder.html?vin=<VIN>`. On load the page itself POSTs
`{"vin": ...}` to `/api/vehicle/mul/decode-vin-attributes` and renders the answer into
`div.vin-decoder-sr > div.decoded-detail > ul > li` as `<b>label</b> <span>CODE-DESCRIPTION</span>`.
The tool navigates to that page and reads the response the page received (falling back to the
rendered rows). It never calls the API on its own.

## If the site blocks it

If a CAPTCHA, block page, login wall or 403/429 shows up, the tool stops, names the VIN it
stopped at and leaves the Chrome window open so you can finish that lookup by hand. It does not
solve CAPTCHAs and uses no stealth plugins, fingerprint spoofing or proxies. Don't add them.

## Tests

```bash
python -m unittest discover -s tests -v
```

Offline only: they parse `tests/fixtures/vin_decoder_sample.html` (made-up VIN) and never open
a browser.

`data/vin_rpo/`, the browser profile, cookies and HAR files are git-ignored; keep real VIN
output out of commits.
