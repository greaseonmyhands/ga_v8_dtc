"""Offline tests for tools/vin_rpo - never touches the network or a browser.

Run:  python -m unittest discover -s tests -v     (also works under pytest)
"""
import datetime
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        self.assertEqual(codes, ["LV3", "MYC", "1SZ", "R9N", "2WT", "4D7", "6B1", "A31", "A60", "AKO",
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


class Cluster(unittest.TestCase):
    def test_subset(self):
        r = vin_rpo.parse_html(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual([c["code"] for c in vin_rpo.cluster_codes(r)], ["LV3", "MYC", "AXK"])
        self.assertEqual(len(vin_rpo.CLUSTER_RPOS), 99)
        self.assertTrue(all(len(c) == 3 for c in vin_rpo.CLUSTER_RPOS))


class Powertrain(unittest.TestCase):
    def r(self, engine, trans="6-Speed Automatic Transmission, HMD, 6L-80", year="2014", codes=()):
        return {"catalog": {"YEAR_FROM": year}, "codes": [{"code": c} for c in codes],
                "major_attributes": {"Engine": engine, "Transmission": trans}}

    def codes(self, *a, **k):
        return [c["code"] for c in vin_rpo.infer_powertrain(self.r(*a, **k))]

    def test_decoder_texts_seen_live(self):
        self.assertEqual(self.codes("6 Cyl 4.3L GEN 5, SIDI, VVT, OHV, E85 MAX, ALUM"), ["LV3", "MYC"])
        self.assertEqual(self.codes("8 Cyl 5.3L SIDI VVT, AFM, E85 MAX", year="2015"), ["L83", "MYC"])

    def test_iron_43_is_lv1(self):
        self.assertEqual(self.codes("6 Cyl 4.3L SIDI, VVT, E85 MAX, IRON", trans=""), ["LV1"])

    def test_6l90_is_myd(self):
        self.assertEqual(self.codes("", trans="6-Speed Automatic, 6L90"), ["MYD"])

    def test_unknown_or_ambiguous_text_infers_nothing(self):
        self.assertEqual(self.codes("4 Cyl 2.7L Turbo", trans="8-Speed Automatic, 8L90"), [])
        self.assertEqual(self.codes("6 Cyl 4.3L", trans=""), [])  # fits LV1 and LV3

    def test_engine_outside_covered_years(self):
        self.assertEqual(self.codes("8 Cyl 5.3L SIDI VVT, AFM, E85 MAX", year="2020"), ["MYC"])

    def test_existing_code_not_duplicated_and_idempotent(self):
        self.assertEqual(self.codes("8 Cyl 5.3L SIDI VVT, AFM, E85 MAX", codes=["L83"]), ["MYC"])
        r = self.r("6 Cyl 4.3L GEN 5, SIDI, VVT, OHV, E85 MAX, ALUM")
        vin_rpo.with_inferred(r)
        vin_rpo.with_inferred(r)
        self.assertEqual([c["code"] for c in r["codes"]], ["LV3", "MYC"])
        self.assertTrue(all(c["inferred"] for c in r["codes"]))


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
            self.assertEqual(len(lines), 14)  # header + 11 option rows + LV3 + MYC

    def test_from_html_imports_and_saves(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = vin_rpo.main(["--import", str(FIXTURE), "--out-dir", d])
            self.assertEqual(rc, 0)
            self.assertIn(FAKE_VIN, buf.getvalue())
            saved = json.loads((Path(d) / f"{FAKE_VIN}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["source"], "saved-page")
            self.assertEqual(saved["imported_from"], FIXTURE.name)

    def test_recent_block_pauses_live_lookup_before_browser(self):
        with tempfile.TemporaryDirectory() as d:
            vin_rpo.record_block(Path(d), "page looks like a block page")
            err = io.StringIO()
            with redirect_stderr(err):
                rc = vin_rpo.main([FAKE_VIN, "--out-dir", d])
            self.assertEqual(rc, 3)
            self.assertIn("blocked an automated lookup", err.getvalue())

    def test_cache_hit_still_works_while_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            vin_rpo.record_block(Path(d), "x")
            r = vin_rpo.finish(vin_rpo.parse_html(FIXTURE.read_text(encoding="utf-8")),
                               FAKE_VIN, "fixture")
            (Path(d) / f"{FAKE_VIN}.json").write_text(json.dumps(r), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(vin_rpo.main([FAKE_VIN, "--out-dir", d]), 0)


# Shaped like the firewall page gmpartsgiant.com showed in 2026-11 (IP and IDs made up).
FIREWALL_PAGE = """<html><head><title></title></head><body>
<table><tr><td><img src="x.gif"></td><td><h1>Web Page Blocked!</h1>
<p>The page cannot be displayed. Please contact the administrator for additional information.</p>
<p>URL: www.gmpartsgiant.com/vin-decoder.html?vin=1GCNCPEHXEZ000000</p>
<p>Client IP: 192.0.2.1<br>Attack ID: 20000000<br>Message ID: 000000000000</p>
</td></tr></table></body></html>"""


class SiteBlock(unittest.TestCase):
    def test_firewall_page_is_a_block_not_a_missing_result(self):
        with self.assertRaises(vin_rpo.BlockedError):
            vin_rpo.parse_html(FIREWALL_PAGE)
        self.assertIsNotNone(vin_rpo.looks_blocked("", "Web Page Blocked! The page cannot be displayed."))

    def test_cooldown_window(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertIsNone(vin_rpo.recent_block(d))
            vin_rpo.record_block(d, "matched 'web page blocked'")
            when, reason = vin_rpo.recent_block(d)
            self.assertIn("web page blocked", reason)
            later = when + vin_rpo.BLOCK_COOLDOWN + datetime.timedelta(minutes=1)
            self.assertIsNone(vin_rpo.recent_block(d, now=later))
            vin_rpo.clear_block(d)
            self.assertIsNone(vin_rpo.recent_block(d))
            # The marker must not show up as a saved VIN (the window lists *.json).
            vin_rpo.record_block(d, "x")
            self.assertEqual(list(d.glob("*.json")), [])


class ImportSavedPage(unittest.TestCase):
    def _mhtml(self, html: str) -> bytes:
        # Laid out the way Chrome's "Webpage, Single File" is: multipart/related, with the
        # page as a quoted-printable text/html part.
        import quopri
        body = quopri.encodestring(html.encode("utf-8"))
        b = b"----MultipartBoundary--test"
        return (b"From: <Saved by Blink>\r\nSubject: Free GM VIN Decoder | GMPartsGiant\r\n"
                b"MIME-Version: 1.0\r\nContent-Type: multipart/related;\r\n\ttype=\"text/html\";\r\n"
                b"\tboundary=\"" + b + b"\"\r\n\r\n--" + b + b"\r\n"
                b"Content-Type: text/html\r\nContent-ID: <frame-1@mhtml.blink>\r\n"
                b"Content-Transfer-Encoding: quoted-printable\r\n"
                b"Content-Location: https://www.gmpartsgiant.com/vin-decoder.html?vin=x\r\n\r\n"
                + body + b"\r\n--" + b + b"--\r\n")

    def test_mhtml(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "VIN Decoder.mhtml"
            p.write_bytes(self._mhtml(FIXTURE.read_text(encoding="utf-8")))
            r = vin_rpo.import_page(p)
            self.assertEqual(r["vin"], FAKE_VIN)
            self.assertIn("AKO", [c["code"] for c in r["codes"]])
            self.assertEqual(r["source_url"], f"{vin_rpo.DECODER_URL}?vin={FAKE_VIN}")

    def test_vin_from_box_when_page_lacks_it(self):
        html = FIXTURE.read_text(encoding="utf-8").replace("Your VIN:", "Vehicle:")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "page.html"
            p.write_text(html, encoding="utf-8")
            with self.assertRaises(vin_rpo.NoResultError):
                vin_rpo.import_page(p)
            self.assertEqual(vin_rpo.import_page(p, "1gcncpehxez000000")["vin"], FAKE_VIN)

    def test_saved_block_page_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "blocked.html"
            p.write_text(FIREWALL_PAGE, encoding="utf-8")
            with self.assertRaises(vin_rpo.BlockedError):
                vin_rpo.import_page(p)


if __name__ == "__main__":
    unittest.main()
