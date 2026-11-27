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
LANDING_ERR, CONTROLLERS, FUNCTIONS, MODULE, Q_AUDIO, Q_PROGTYPE, Q_HARDWARE = \
    (FIX / "tis2web_steps.html").read_text(encoding="utf-8").split("===PAGE===")
FAKE_VIN = "1GCNCPEHXEZ000000"  # made up; valid check digit
# A made-up vehicle in the shape get_vehicle() returns (codes from RPO PRO).
VEHICLE = {"codes": ["IO4", "UQ5", "AKO", "KC4"], "engine": "6 Cyl 4.3L GEN 5, SIDI",
           "transmission": "6-Speed Automatic Transmission, HMD, 6L-80"}


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


class QuestionPages(unittest.TestCase):
    def test_audio_system(self):
        i = t.classify_page(Q_AUDIO)
        self.assertEqual((i["kind"], i["label"]), ("question", "Audio System"))
        self.assertEqual(len(i["options"]), 4)

    def test_programming_type(self):
        i = t.classify_page(Q_PROGTYPE)
        self.assertEqual((i["kind"], i["label"], i["options"]),
                         ("question", "Programming Type", ["Normal", "VCI"]))

    def test_hardware_with_prompt(self):
        i = t.classify_page(Q_HARDWARE)
        self.assertEqual((i["kind"], i["label"]), ("question", "Hardware"))
        self.assertIn("Boot Software Part Number from the list below", i["prompt"])

    def test_known_steps_keep_their_kind(self):
        self.assertEqual(t.classify_page(MODULE)["label"], "Module")
        self.assertEqual(t.classify_page(FUNCTIONS)["kind"], "function")


class Answering(unittest.TestCase):
    AUDIO = t.classify_page(Q_AUDIO)["options"]

    def test_rpo_picks_uplevel_radio_not_bulletin(self):
        got, how = t.decide("Audio System", self.AUDIO, VEHICLE)
        self.assertEqual(how, "rpo")
        self.assertEqual(got, [("RPO IO4/IO5/IO6 (Uplevel radio)", "vehicle has IO4")])

    def test_base_radio_vehicle(self):
        got, _ = t.decide("Audio System", self.AUDIO, dict(VEHICLE, codes=["IO3"]))
        self.assertEqual(got[0][0], "RPO IO3 (Base radio)")

    def test_programming_type_normal(self):
        self.assertEqual(t.decide("Programming Type", ["Normal", "VCI"], VEHICLE),
                         ([("Normal", "normal programming")], "rule"))

    def test_hardware_cannot_be_told_so_all(self):
        got, how = t.decide("Hardware", ["22751984", "23117730"], VEHICLE)
        self.assertEqual((how, [g[0] for g in got]), ("all", ["22751984", "23117730"]))

    def test_no_rpo_codes_means_all(self):
        got, how = t.decide("Audio System", self.AUDIO, None)
        self.assertEqual((how, len(got)), ("all", 2))  # bulletin-only paths left out

    def test_function_skips_bulletin_only(self):
        got, how = t.decide("Function", ["Bulletin 15-08-44-001 Programming Only", "Programming"],
                            VEHICLE)
        self.assertEqual((got, how), ([("Programming", "")], "only"))

    def test_engine_by_displacement_and_negation(self):
        self.assertEqual(t.decide("Engine", ["4.3L LV3", "5.3L L83"], VEHICLE)[0][0][0], "4.3L LV3")
        self.assertEqual(t.decide("Speakers", ["w/o UQ5", "with UQ5"], VEHICLE)[0][0][0], "with UQ5")

    def test_plain_words_are_not_codes(self):
        self.assertEqual(t.option_codes("VCI", set()), (set(), set()))
        self.assertEqual(t.option_codes("RPO IO4/IO5/IO6 (Uplevel radio)", set())[0],
                         {"IO4", "IO5", "IO6"})

    def test_m4455_option_marked(self):
        h = t.parse_history_html(HISTORY)
        warn = h["modules"][2]
        t.pick_module_options(warn, VEHICLE)
        self.assertEqual(warn["vehicle_option"], 1)  # "For UQ3/UQ5 speaker packages"
        self.assertEqual(warn["vehicle_option_why"], "vehicle has UQ5")


class FakeSite(t.Tis2Web):
    """The wizard as a little tree of pages, so the walk can be tested offline."""
    PAGES = {
        "ctl": {"kind": "controller", "label": "Controller",
                "options": ["A11 Radio", "K43 Steering", "FCM Camera", "T3 Amp"],
                "next": {"A11 Radio": "fnA", "K43 Steering": "fnK", "FCM Camera": "fnF",
                         "T3 Amp": "fnT", "T4 Amp": "fnU"}},
        # T4: the choice that fits the vehicle (UQ5) gets TIS's own internal error.
        "fnU": {"kind": "function", "label": "Function", "options": ["Programming"],
                "next": {"Programming": "qAmp"}},
        "qAmp": {"kind": "question", "label": "Audio System",
                 "options": ["Only for trucks with RPO UQA/UQS", "With RPO UQ3/UQ5"],
                 "next": {"With RPO UQ3/UQ5": "e4451"}},
        "e4451": {"kind": "unknown", "label": "", "options": [], "back2": True,
                  "error": "E4451: An internal error has occurred ! Unable to proceed !"},
        "fnF": {"kind": "function", "label": "Function", "options": ["Programming"],
                "next": {"Programming": "qVeh"}},
        "qVeh": {"kind": "question", "label": "Vehicle Options",
                 "options": ["With Forward Collision Alert (RPO UEU)"],
                 "next": {"With Forward Collision Alert (RPO UEU)": "err"}},
        # T3: two speaker choices; the first leads to TIS's error page, whose Back jumps
        # two steps (past the question), the second to a real module page.
        "fnT": {"kind": "function", "label": "Function", "options": ["Programming"],
                "next": {"Programming": "qSpk"}},
        "qSpk": {"kind": "question", "label": "Speakers", "options": ["Bose", "Base"],
                 "next": {"Bose": "err", "Base": "mod"}},
        "err": {"kind": "unknown", "label": "", "options": [], "back2": True,
                "error": "TIS has no valid calibration for this vehicle (sps.exception.no-valid-cop)"},
        "fnA": {"kind": "function", "label": "Function",
                "options": ["Bulletin 15-08-44-001 Programming Only", "Programming"],
                "next": {"Programming": "qAudio"}},
        "qAudio": {"kind": "question", "label": "Audio System",
                   "options": ["RPO IO3 (Base radio)", "RPO IO4/IO5/IO6 (Uplevel radio)"],
                   "next": {"RPO IO4/IO5/IO6 (Uplevel radio)": "mod"}},
        "fnK": {"kind": "function", "label": "Function", "options": ["Programming"],
                "next": {"Programming": "qHW"}},
        "qHW": {"kind": "question", "label": "Hardware", "options": ["22751984", "23117730"],
                "next": {"22751984": "mod", "23117730": "mod"}},
        "mod": {"kind": "module", "label": "Module", "options": ["Operating system"],
                "history_button": True},
    }

    def __init__(self, vehicle):
        super().__init__(page=None, vin=FAKE_VIN, vehicle=vehicle)
        self.stack, self.histories, self.chosen = ["ctl"], 0, []

    def _state(self):
        p = self.PAGES[self.stack[-1]]
        return {"prompt": "", "error": None, "history_button": False, "selected": None,
                **{k: v for k, v in p.items() if k not in ("next", "back2")}}

    def _choose(self, text, label="Next >"):
        self.chosen.append(text)
        self.stack.append(self.PAGES[self.stack[-1]]["next"][text])
        return self._state()

    def _click(self, label):
        assert label == "< Back"
        for _ in range(2 if self.PAGES[self.stack[-1]].get("back2") else 1):
            self.stack.pop()
        return self._state()

    def _open_history(self):
        self.histories += 1
        return HISTORY


class Walk(unittest.TestCase):
    def test_radio_answered_from_rpo(self):
        site = FakeSite(VEHICLE)
        r = site.fetch("A11 Radio")
        self.assertEqual(site.stack, ["ctl"])           # back at the controller list
        self.assertEqual(site.histories, 1)            # one path only
        self.assertEqual(r["modules"][0]["answers"][0]["answer"], "RPO IO4/IO5/IO6 (Uplevel radio)")
        self.assertEqual(r["modules"][0]["function"], "Programming")
        self.assertEqual(r["questions"][0]["how"], "rpo")
        warn = next(m for m in r["modules"] if m["module"] == "Warning")
        self.assertEqual(warn["vehicle_option"], 1)

    def test_hardware_fetches_every_choice(self):
        site = FakeSite(VEHICLE)
        r = site.fetch("K43 Steering")
        self.assertEqual(site.stack, ["ctl"])
        self.assertEqual(site.histories, 2)
        self.assertEqual(sorted({t.answers_text(m) for m in r["modules"]}),
                         ["Hardware = 22751984", "Hardware = 23117730"])

    def test_option_vehicle_lacks_is_not_fitted_without_clicking(self):
        site = FakeSite(VEHICLE)  # no UEU
        site.stack = ["ctl"]
        r = site.fetch("FCM Camera")
        self.assertEqual(site.stack, ["ctl"])
        self.assertEqual(r["modules"], [])
        self.assertIn("UEU", r["not_fitted"])
        self.assertNotIn("With Forward Collision Alert (RPO UEU)", site.chosen)

    def test_tis_error_page_that_jumps_back_two_steps(self):
        site = FakeSite(None)  # no RPO codes: both speaker choices are tried
        r = site.fetch("T3 Amp")
        self.assertEqual(site.stack, ["ctl"])
        self.assertEqual(site.histories, 1)  # "Base" still fetched after "Bose" failed
        self.assertEqual({t.answers_text(m) for m in r["modules"]}, {"Speakers = Base"})
        self.assertTrue(any("no valid calibration" in s for s in r["skipped"]))

    def test_tis_internal_error_on_the_fitting_choice(self):
        site = FakeSite(VEHICLE)  # has UQ5
        r = site.fetch("T4 Amp")  # returns instead of raising: it is TIS's answer
        self.assertEqual(site.stack, ["ctl"])
        self.assertEqual(r["modules"], [])
        self.assertIn("E4451", r["tis_error"])
        self.assertIn("With RPO UQ3/UQ5", r["tis_error"])
        self.assertNotIn("Only for trucks with RPO UQA/UQS", site.chosen)


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
        self.assertEqual(rows[0][:5], ["vin", "controller", "function", "tis_answers", "module"])
        self.assertEqual(len(rows) - 1, 3 + 2 + 4 + 1)
        h = rows[0]
        cur = [r for r in rows[1:] if r[h.index("current")] == "yes"]
        self.assertEqual([r[h.index("part_number")] for r in cur],
                         ["23352994", "23179945", "23179947", "23464330", "22880023"])

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
