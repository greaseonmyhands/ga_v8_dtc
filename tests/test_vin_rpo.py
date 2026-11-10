"""Offline tests for tools/vin_rpo - never touches the network or a browser.

Run:  python -m unittest discover -s tests -v     (also works under pytest)
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "vin_rpo"))
import vin_rpo  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "vin_decoder_sample.html"
FAKE_VIN = "1GCNCPEHXEZ000000"  # made up; valid check digit


class VinValidation(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(vin_rpo.validate_vin(" 1gcncpehxez000000 "), FAKE_VIN)
        self.assertEqual(vin_rpo.validate_vin("1M8GDM9AXKP042788"), "1M8GDM9AXKP042788")

    def test_length(self):
        with self.assertRaisesRegex(ValueError, "17 characters"):
            vin_rpo.validate_vin(FAKE_VIN[:-1])

    def test_ioq(self):
        with self.assertRaisesRegex(ValueError, "invalid character"):
            vin_rpo.validate_vin("1GCNCPEHXEZ00000O")

    def test_check_digit(self):
        with self.assertRaisesRegex(ValueError, "check digit"):
            vin_rpo.validate_vin("1GCNCPEH1EZ000000")


class RowSplitting(unittest.TestCase):
    def test_single(self):
        self.assertEqual(vin_rpo.split_spec_row("WNT", "AKO-WINDOW TYPE PRIVACY"),
                         [{"code": "AKO", "description": "WINDOW TYPE PRIVACY", "category": "WNT"}])

    def test_multi_and_inner_dashes(self):
        rows = vin_rpo.split_spec_row("FIN", "1SZ-PACKAGE OPTION-;R9N-CONTROL - SALES ITEM NO. 89")
        self.assertEqual([(r["code"], r["description"]) for r in rows],
                         [("1SZ", "PACKAGE OPTION"), ("R9N", "CONTROL - SALES ITEM NO. 89")])

    def test_semicolon_inside_description_is_kept(self):
        rows = vin_rpo.split_spec_row("X", "ABC-FOO; BAR BAZ")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], "FOO; BAR BAZ")

    def test_code_without_description_is_metadata(self):
        self.assertEqual(vin_rpo.split_spec_row("MRKT_GME", "OPM-"), [])
        r = vin_rpo._build("X", None, [], [], [("MRKT_GME", "OPM-"), ("BR_PRDCT", "gmna-"),
                                               ("WNT", "AKO-WINDOW TYPE PRIVACY")])
        self.assertEqual([c["code"] for c in r["codes"]], ["AKO"])
        self.assertEqual(r["catalog"], {"MRKT_GME": "OPM", "BR_PRDCT": "gmna"})

    def test_non_rpo_ignored(self):
        self.assertEqual(vin_rpo.split_spec_row("YEAR_FROM", "2014-"), [])


class HtmlFixture(unittest.TestCase):
    def setUp(self):
        self.r = vin_rpo.parse_html(FIXTURE.read_text(encoding="utf-8"))

    def test_vin_and_vehicle(self):
        self.assertEqual(self.r["vin"], FAKE_VIN)
        self.assertTrue(self.r["vehicle"].startswith("2014 Light Truck Silverado"))
        self.assertEqual(self.r["major_attributes"]["Transmission"],
                         "6-Speed Automatic Transmission, HMD, 6L-80")
        self.assertEqual(self.r["vehicle_information"]["CatalogCode"], "5C1")

    def test_codes(self):
        codes = [c["code"] for c in self.r["codes"]]
        self.assertEqual(codes, ["1SZ", "R9N", "2WT", "4D7", "6B1", "A31", "A60", "AKO",
                                 "AQQ", "AXK", "AY0"])
        by = {c["code"]: c for c in self.r["codes"]}
        self.assertEqual(by["AKO"]["category"], "WNT")
        self.assertEqual(by["AKO"]["description"], "WINDOW TYPE PRIVACY")
        self.assertIn("DRIVER & PASS", by["AY0"]["description"])  # entity decoded

    def test_metadata_rows_are_not_codes(self):
        self.assertEqual(self.r["catalog"], {"CATALOG": "5C1", "MD": "C", "MK": "L",
                                             "YEAR_FROM": "2014", "YEAR_TO": "2014",
                                             "BR": "US", "MRKT_GME": "OPM"})
        self.assertNotIn("5C1", [c["code"] for c in self.r["codes"]])
        self.assertNotIn("ZZZ", [c["code"] for c in self.r["codes"]])  # inside <script>

    def test_block_page(self):
        html = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"
        with self.assertRaises(vin_rpo.BlockedError):
            vin_rpo.parse_html(html)

    def test_empty_page(self):
        with self.assertRaises(vin_rpo.NoResultError):
            vin_rpo.parse_html("<html><body><div class='vin-decoder-sr'></div></body></html>")


class ApiJson(unittest.TestCase):
    PAYLOAD = {"url": None, "code": 0, "message": None, "data": {"vinInfos": [{
        "vehicleInfo": "2014 Light Truck Silverado",
        "redirectUrl": "/page_product/category_vin_%s.html" % FAKE_VIN,
        "vehicleInformation": [{"name": "CatalogCode", "desc": "5C1"}],
        "majorAttribute": [{"name": "Engine", "desc": "6 Cyl 4.3L"}],
        "specification": [{"name": "CATALOG", "desc": "5C1-"},
                          {"name": "FIN", "desc": "1SZ-PACKAGE OPTION-;R9N-CONTROL - SALES ITEM NO. 89"},
                          {"name": "WNT", "desc": "AKO-WINDOW TYPE PRIVACY"}]}]}}

    def test_parse(self):
        r = vin_rpo.parse_api_json(self.PAYLOAD)
        self.assertEqual(r["vin"], FAKE_VIN)
        self.assertEqual([c["code"] for c in r["codes"]], ["1SZ", "R9N", "AKO"])

    def test_no_vehicle(self):
        with self.assertRaises(vin_rpo.NoResultError):
            vin_rpo.parse_api_json({"data": {"vinInfos": []}, "message": "nope"})


class Cli(unittest.TestCase):
    def test_bad_vin_exits_before_browser(self):
        self.assertEqual(vin_rpo.main(["1GCNCPEH1EZ000000"]), 2)

    def test_cache_hit_and_csv_without_browser(self):
        with tempfile.TemporaryDirectory() as d:
            r = vin_rpo.finish(vin_rpo.parse_html(FIXTURE.read_text(encoding="utf-8")),
                               FAKE_VIN, "fixture")
            (Path(d) / f"{FAKE_VIN}.json").write_text(json.dumps(r), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = vin_rpo.main([FAKE_VIN, "--csv", "--out-dir", d])
            self.assertEqual(rc, 0)
            self.assertIn("AKO  WINDOW TYPE PRIVACY", buf.getvalue())
            lines = (Path(d) / f"{FAKE_VIN}.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "vin,code,category,description")
            self.assertEqual(len(lines), 12)

    def test_from_html(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = vin_rpo.main(["--from-html", str(FIXTURE)])
        self.assertEqual(rc, 0)
        self.assertIn(FAKE_VIN, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
