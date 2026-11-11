"""Offline tests for tools/tis2web_cal (CAL PRO) - never touches the network or a browser.

Run:  python -m unittest discover -s tests -v
"""
import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "tis2web_cal"))
import tis2web_cal as t  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
HISTORY = (FIX / "tis2web_history_sample.html").read_text(encoding="utf-8")
LANDING_ERR, CONTROLLERS, FUNCTIONS, MODULE = \
    (FIX / "tis2web_steps.html").read_text(encoding="utf-8").split("===PAGE===")
FAKE_VIN = "1GCNCPEHXEZ000000"  # made up; valid check digit


class History(unittest.TestCase):
    def setUp(self):
        self.h = t.parse_history_html(HISTORY)

    def test_selected_options(self):
        self.assertEqual((self.h["vin"], self.h["controller"], self.h["function"]),
                         (FAKE_VIN, "P16 Instrument Cluster", "Programming"))

    def test_modules_in_order(self):
        self.assertEqual([m["module"] for m in self.h["modules"]],
                         ["Operating system", "Lighting", "Warning", "Graphic Resources"])

    def test_rows_newest_first_and_blanks(self):
        os_ = self.h["modules"][0]["options"][0]
        self.assertEqual([r["part_number"] for r in os_], ["23352994", "23448506", "23149185"])
        self.assertIsNone(os_[0]["cvn"])            # N/A
        self.assertIsNone(os_[0]["bulletin"])       # -
        light = self.h["modules"][1]["options"][0]
        self.assertEqual(light[0]["cvn"], "0000B4B9")
        self.assertEqual(light[1]["bulletin"], "12345")

    def test_multiple_valid_options_keep_their_note(self):
        warn = self.h["modules"][2]
        self.assertEqual(len(warn["options"]), 2)
        self.assertTrue(warn["note"].startswith("M4455:"))
        self.assertEqual([o[0]["part_number"] for o in warn["options"]], ["23179947", "23464330"])
        self.assertIsNone(self.h["modules"][3]["note"])  # note does not leak to the next module

    def test_empty_page(self):
        with self.assertRaises(t.NoResultError):
            t.parse_history_html("<html><body>nothing</body></html>")


class WizardPages(unittest.TestCase):
    def test_landing_error(self):
        i = t.classify_page(LANDING_ERR)
        self.assertEqual(i["kind"], "landing")
        self.assertEqual(i["error"], "E2769: Invalid VIN!")
        self.assertIsNone(i["blocked"])

    def test_controller_list(self):
        i = t.classify_page(CONTROLLERS)
        self.assertEqual(i["kind"], "controller")
        self.assertEqual(i["options"], ["A11 Radio", "K20 Engine Control Module",
                                        "K9 Body Control Module", "P16 Instrument Cluster"])
        self.assertIsNone(i["error"])

    def test_function_step(self):
        i = t.classify_page(FUNCTIONS)
        self.assertEqual((i["kind"], i["options"]), ("function", ["Programming"]))

    def test_module_step_and_fallback(self):
        i = t.classify_page(MODULE)
        self.assertEqual(i["kind"], "module")
        self.assertTrue(i["history_button"])
        self.assertEqual(i["selected"], "Main Operating System")
        m = t.parse_module_page(MODULE)
        self.assertEqual(m["controller"], "K20 Engine Control Module")
        self.assertEqual([r["part_number"] for r in m["modules"][0]["options"][0]],
                         ["12663390", "12650001"])

    def test_history_page(self):
        self.assertEqual(t.classify_page(HISTORY)["kind"], "history")

    def test_block_page(self):
        i = t.classify_page("<html><head><title>Just a moment...</title></head>"
                            "<body>Verify you are human</body></html>")
        self.assertTrue(i["blocked"])


class Matching(unittest.TestCase):
    AV = ["A11 Radio", "K20 Engine Control Module", "K9 Body Control Module",
          "P16 Instrument Cluster"]

    def test_by_code_and_word(self):
        self.assertEqual(t.match_controllers(self.AV, ["p16", "engine"]),
                         ["P16 Instrument Cluster", "K20 Engine Control Module"])
        self.assertEqual(t.match_controllers(self.AV, ["K9"]), ["K9 Body Control Module"])

    def test_all_and_unknown(self):
        self.assertEqual(t.match_controllers(self.AV, []), self.AV)
        with self.assertRaises(ValueError):
            t.match_controllers(self.AV, ["X99"])


def _record():
    h = t.parse_history_html(HISTORY)
    rec = t.new_record(FAKE_VIN)
    rec["controllers_available"] = ["A11 Radio", "P16 Instrument Cluster"]
    rec["controllers"]["P16 Instrument Cluster"] = {
        "lookup_date": "2026-11-11", "functions": ["Programming"],
        "modules": [dict(m, function="Programming") for m in h["modules"]]}
    return rec


class Output(unittest.TestCase):
    def test_csv(self):
        buf = io.StringIO()
        csv.writer(buf).writerows(t.csv_rows(_record()))
        rows = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(rows[0][:4], ["vin", "controller", "function", "module"])
        self.assertEqual(len(rows) - 1, 3 + 2 + 4 + 1)
        cur = [r for r in rows[1:] if r[6] == "yes"]
        self.assertEqual([r[7] for r in cur], ["23352994", "23179945", "23179947", "23464330",
                                               "22880023"])

    def test_cli_cache_hit_no_browser(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / f"{FAKE_VIN}.json").write_text(json.dumps(_record()), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = t.main([FAKE_VIN, "--modules", "P16", "--csv", "--out-dir", d])
            self.assertEqual(rc, 0)
            self.assertIn("23352994", buf.getvalue())
            self.assertTrue((Path(d) / f"{FAKE_VIN}.csv").exists())
            with redirect_stdout(io.StringIO()) as b2:
                self.assertEqual(t.main([FAKE_VIN, "--list", "--out-dir", d]), 0)
            self.assertIn("A11 Radio", b2.getvalue())

    def test_bad_vin_exits_before_browser(self):
        self.assertEqual(t.main(["1GCNCPEH1EZ000000"]), 2)

    def test_from_html(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.html"
            p.write_text(HISTORY, encoding="utf-8")
            with redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(t.main(["--from-html", str(p)]), 0)
            self.assertIn("option 2 of 2", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
