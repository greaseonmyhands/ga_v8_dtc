"""Offline tests for tools/j2534_can - simulator only, never opens a J2534 device.

Run:  python -m unittest discover -s tests -v
"""
import csv
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "j2534_can"))
import canlog  # noqa: E402
import j2534  # noqa: E402
from canlog import Frame  # noqa: E402

EXAMPLES = ROOT / "tools" / "j2534_can" / "examples"


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Parsing(unittest.TestCase):
    def test_id_widths(self):
        self.assertEqual(canlog.parse_id("7e0"), (0x7E0, False))
        self.assertEqual(canlog.parse_id("0C9"), (0x0C9, False))
        self.assertEqual(canlog.parse_id("10242040"), (0x10242040, True))
        self.assertEqual(canlog.parse_id("00000123"), (0x123, True))   # 8 digits = 29-bit
        self.assertEqual(canlog.parse_id("0x7DF"), (0x7DF, False))

    def test_id_errors(self):
        for bad, msg in (("", "empty"), ("7G0", "not hex"), ("800", "above 7FF"),
                         ("20000000", "above 1FFFFFFF"), ("123456789", "more than 8")):
            with self.assertRaisesRegex(ValueError, msg):
                canlog.parse_id(bad)

    def test_data(self):
        self.assertEqual(canlog.parse_data("02 01 00"), b"\x02\x01\x00")
        self.assertEqual(canlog.parse_data("FE013E"), b"\xfe\x01\x3e")
        self.assertEqual(canlog.parse_data(""), b"")
        with self.assertRaisesRegex(ValueError, "at most 8"):
            canlog.parse_data("00 11 22 33 44 55 66 77 88")
        with self.assertRaisesRegex(ValueError, "whole bytes"):
            canlog.parse_data("0 1")
        with self.assertRaisesRegex(ValueError, "not hex"):
            canlog.parse_data("ZZ")

    def test_pattern(self):
        p = canlog.parse_pattern("?? 41 00")
        self.assertEqual(p, [None, 0x41, 0x00])
        self.assertTrue(canlog.pattern_matches(p, bytes.fromhex("0641001122")))
        self.assertFalse(canlog.pattern_matches(p, bytes.fromhex("06410C")))
        self.assertFalse(canlog.pattern_matches(p, bytes.fromhex("0641")))
        self.assertTrue(canlog.pattern_matches([], b""))


class Filter(unittest.TestCase):
    def f(self, bus, arb, ext=False):
        return Frame(0.0, bus, arb, ext, b"")

    def test_empty_passes_all(self):
        self.assertTrue(canlog.IdFilter("").match(self.f("LS", 0x10242040, True)))

    def test_ids_ranges_buses_excludes(self):
        flt = canlog.IdFilter("7E0-7EF, 0C9 !7E5 hs")
        self.assertTrue(flt.match(self.f("HS", 0x7E8)))
        self.assertTrue(flt.match(self.f("HS", 0x0C9)))
        self.assertFalse(flt.match(self.f("HS", 0x7E5)))
        self.assertFalse(flt.match(self.f("HS", 0x100)))
        self.assertFalse(flt.match(self.f("LS", 0x7E8)))
        only_hide = canlog.IdFilter("!10242040")
        self.assertFalse(only_hide.match(self.f("LS", 0x10242040, True)))
        self.assertTrue(only_hide.match(self.f("LS", 0x621)))

    def test_bad(self):
        for bad in ("XYZ", "7EF-7E0", "12345678901"):
            with self.assertRaises(ValueError):
                canlog.IdFilter(bad)


class LogFiles(unittest.TestCase):
    def test_csv_and_candump_roundtrip(self):
        frames = [Frame(0.001, "LS", 0x10242040, True, bytes.fromhex("0102030405060708")),
                  Frame(0.5, "HS", 0x0C9, False, bytes.fromhex("AA55")),
                  Frame(1.25, "HS", 0x7E0, False, b"", tx=True)]
        with tempfile.TemporaryDirectory() as d:
            w = canlog.LogWriter(Path(d), start_wall=1700000000.0, stem="t")
            for f in frames:
                w.write(f)
            w.close()
            with open(w.csv_path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            lines = w.log_path.read_text("ascii").splitlines()
        self.assertEqual(rows[0], canlog.CSV_HEADER)
        self.assertEqual(rows[1], ["0.001000", "2023-11-14T22:13:20.001000Z", "LS", "Rx",
                                   "10242040", "29", "8", "01 02 03 04 05 06 07 08"])
        self.assertEqual(rows[3][2:], ["HS", "Tx", "7E0", "11", "0", ""])
        self.assertEqual(lines[0], "(1700000000.001000) ls0 10242040#0102030405060708")
        self.assertEqual(lines[1], "(1700000000.500000) hs0 0C9#AA55")
        self.assertEqual(lines[2], "(1700000001.250000) hs0 7E0#")
        back = [canlog.parse_candump_line(x) for x in lines]
        self.assertEqual([(b[2], b[3], b[4]) for b in back],
                         [(f.arb_id, f.ext, f.data) for f in frames])
        self.assertIsNone(canlog.parse_candump_line("not a frame"))


class ScriptParsing(unittest.TestCase):
    def test_basic(self):
        s = canlog.parse_script("""
            # comment
            bus LS
            send 100 hv
            wait 100
            repeat 3            # trailing comment
                send 10242040 01 02
                send HS 7E0 0201
                wait 50
            end
            waitfor HS 7E8 ?? 41 timeout 250 continue
        """)
        self.assertEqual(s.buses, ("LS", "HS"))
        self.assertEqual(s.sends, 7)
        self.assertEqual(s.wait_ms, 250)
        first = s.steps[0]
        self.assertEqual((first.kind, first.bus, first.arb_id, first.ext, first.hv),
                         ("send", "LS", 0x100, False, True))
        rep = s.steps[2]
        self.assertEqual((rep.kind, rep.count, len(rep.body)), ("repeat", 3, 3))
        self.assertEqual((rep.body[0].arb_id, rep.body[0].ext, rep.body[0].data),
                         (0x10242040, True, b"\x01\x02"))
        wf = s.steps[3]
        self.assertEqual((wf.bus, wf.arb_id, wf.pattern, wf.ms, wf.go_on),
                         ("HS", 0x7E8, (None, 0x41), 250, True))
        text = "\n".join(s.preview())
        self.assertIn("SEND LS 100  (no data)  [high-voltage wake-up]", text)
        self.assertIn("    SEND HS 7E0  02 01", text)
        self.assertIn("Will send 7 frames on LS, HS", s.summary())

    def test_forever(self):
        s = canlog.parse_script("repeat forever\n send LS 101 FE 01 3E\n wait 2000\nend")
        self.assertIsNone(s.sends)
        self.assertIn("forever", s.summary())

    def test_errors_carry_line_numbers(self):
        cases = [
            ("send 7E0 01", 1, "no bus"),
            ("bus LS\nsend 7E0 01 02 03 04 05 06 07 08 09", 2, "at most 8"),
            ("bus HS\nsend 100 hv", 2, "only for the LS bus"),
            ("wait abc", 1, "milliseconds"),
            ("repeat 2\nsend LS 1 00", 1, "no matching 'end'"),
            ("end", 1, "without a 'repeat'"),
            ("repeat 2\nend", 2, "is empty"),
            ("repeat forever\nsend LS 1 00\nend", 3, "flood"),
            ("frobnicate", 1, "unknown command"),
            ("# only a comment", 0, "no commands"),
            ("waitfor HS 7E8 timeout x", 1, "timeout needs"),
        ]
        for text, line, msg in cases:
            with self.assertRaisesRegex(canlog.ScriptError, msg) as cm:
                canlog.parse_script(text)
            self.assertEqual(cm.exception.line, line, text)

    def test_examples_parse(self):
        files = sorted(EXAMPLES.glob("*.canscript"))
        self.assertEqual(len(files), 3)
        for p in files:
            s = canlog.parse_script(p.read_text("utf-8"))
            self.assertTrue(s.sends, p.name)


class SimSession(unittest.TestCase):
    def open(self, buses=("LS", "HS"), traffic=True, **kw):
        s = canlog.Session(canlog.SimBackend(traffic=traffic), buses, **kw)
        s.start()
        s.wait_open(5)
        self.addCleanup(s.stop, 3)
        return s

    def test_capture_both_buses_and_log(self):
        got = []
        with tempfile.TemporaryDirectory() as d:
            s = self.open(on_frames=got.extend,
                          log_factory=lambda: canlog.LogWriter(Path(d), stem="cap"))
            self.assertTrue(wait_until(lambda: {("LS", True), ("HS", False)}
                                       <= {(f.bus, f.ext) for f in got}))
            time.sleep(0.3)
            s.stop(3)
            n = len(got)
            lines = (Path(d) / "cap.log").read_text("ascii").splitlines()
            with open(Path(d) / "cap.csv", newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(s.tx_count, 0)                 # nothing sent on connect
        self.assertEqual(s.backend.sent, [])
        self.assertEqual(len(lines), n)                 # every frame reached both files
        self.assertEqual(len(rows), n + 1)
        self.assertTrue(all(canlog.parse_candump_line(x) for x in lines))
        self.assertIn(0x10242040, {f.arb_id for f in got if f.bus == "LS"})
        ts = [f.ts for f in got if f.bus == "HS"]
        self.assertEqual(ts, sorted(ts))

    def test_logging_is_off_until_started_and_can_be_restarted(self):
        got = []
        with tempfile.TemporaryDirectory() as d:
            s = self.open(on_frames=got.extend)
            self.assertTrue(wait_until(lambda: len(got) > 20))
            self.assertIsNone(s.log)
            self.assertEqual(list(Path(d).iterdir()), [])          # nothing saved by default
            self.assertIsNone(s.stop_log())

            first = s.start_log(Path(d))
            self.assertIs(s.start_log(Path(d)), first)             # already logging: same files
            n0 = len(got)
            self.assertTrue(wait_until(lambda: len(got) > n0 + 20))
            closed = s.stop_log()
            self.assertIs(closed, first)
            self.assertTrue(closed.closed)
            lines = first.log_path.read_text("ascii").splitlines()
            self.assertEqual(len(lines), first.count)
            self.assertGreater(first.count, 10)
            self.assertLess(first.count, len(got))                 # earlier frames are not in it
            # times in the file continue from the capture clock, not from zero
            with open(first.csv_path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            self.assertGreater(float(rows[1][0]), 0.05)

            n1 = len(got)
            self.assertTrue(wait_until(lambda: len(got) > n1 + 20))
            self.assertEqual(len(first.log_path.read_text("ascii").splitlines()), first.count)

            second = s.start_log(Path(d))                          # a new pair, never overwritten
            self.assertNotEqual(second.csv_path, first.csv_path)
            self.assertTrue(wait_until(lambda: second.count > 5))
            s.stop(3)                                              # stopping the capture closes it
            self.assertTrue(second.closed)
            self.assertIs(s.log, second)
            self.assertEqual(len(second.log_path.read_text("ascii").splitlines()), second.count)
            self.assertEqual(len(list(Path(d).iterdir())), 4)

    def test_single_bus_only_gets_that_bus(self):
        got = []
        self.open(buses=("LS",), on_frames=got.extend)
        self.assertTrue(wait_until(lambda: len(got) >= 3))
        self.assertEqual({f.bus for f in got}, {"LS"})

    def test_transmit_is_off_by_default(self):
        s = self.open(traffic=False)
        self.assertFalse(s.tx_enabled)
        with self.assertRaises(canlog.TxDisabled):
            s.send("HS", 0x7E0, False, b"\x02\x01\x00")
        runner = canlog.ScriptRunner(s, canlog.parse_script("send HS 7E0 02 01 00"))
        with self.assertRaises(canlog.TxDisabled):
            runner.start()
        time.sleep(0.05)
        self.assertEqual(s.backend.sent, [])

    def test_send_and_reply(self):
        got = []
        s = self.open(traffic=False, on_frames=got.extend)
        s.tx_enabled = True
        mark = s.seq
        s.send("HS", 0x7E0, False, bytes.fromhex("0201000000000000"))
        hit = s.wait_for(lambda f: f.arb_id == 0x7E8, 1.0, mark)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.data[:3], bytes.fromhex("064100"))
        self.assertEqual(s.backend.sent, [("HS", 0x7E0, False, bytes.fromhex("0201000000000000"), False)])
        tx = [f for f in got if f.tx]
        self.assertEqual([(f.bus, f.arb_id) for f in tx], [("HS", 0x7E0)])

    def test_send_on_closed_bus(self):
        s = self.open(buses=("LS",), traffic=False)
        s.tx_enabled = True
        with self.assertRaisesRegex(canlog.BusError, "HS bus is not open"):
            s.send("HS", 0x7E0, False, b"")

    def run_script(self, s, text, timeout=10):
        events = []
        r = canlog.ScriptRunner(s, canlog.parse_script(text), on_event=lambda k, t: events.append((k, t)))
        r.start()
        r.join(timeout)
        self.assertFalse(r.running)
        return r, events

    def test_script_repeat_wait_and_29bit(self):
        s = self.open(traffic=False)
        s.tx_enabled = True
        t0 = time.monotonic()
        r, events = self.run_script(s, """
            send LS 100 hv
            repeat 3
                send LS 10242040 01 02 03
                wait 40
            end
        """)
        self.assertGreaterEqual(time.monotonic() - t0, 0.12)
        self.assertEqual(r.result[0], "done")
        self.assertEqual(r.sent, 4)
        self.assertEqual(s.backend.sent[0], ("LS", 0x100, False, b"", True))
        self.assertEqual(s.backend.sent[1:], [("LS", 0x10242040, True, b"\x01\x02\x03", False)] * 3)
        self.assertEqual(events[-1][0], "done")

    def test_script_waitfor(self):
        s = self.open(traffic=False)
        s.tx_enabled = True
        r, _ = self.run_script(s, "send HS 7E0 02 01 00\nwaitfor HS 7E8 ?? 41 00 timeout 1000\n"
                                  "send HS 7E0 02 01 0C")
        self.assertEqual(r.result[0], "done")
        self.assertEqual(r.sent, 2)

    def test_script_waitfor_timeout_stops_or_continues(self):
        s = self.open(traffic=False)
        s.tx_enabled = True
        r, _ = self.run_script(s, "send HS 123 00\nwaitfor HS 7E8 timeout 80\nsend HS 124 00")
        self.assertEqual(r.result[0], "error")
        self.assertIn("line 2", r.result[1])
        self.assertEqual(r.sent, 1)
        r, _ = self.run_script(s, "send HS 123 00\nwaitfor HS 7E8 timeout 80 continue\nsend HS 124 00")
        self.assertEqual(r.result[0], "done")
        self.assertEqual(r.sent, 2)

    def test_script_stop_button(self):
        s = self.open(traffic=False)
        s.tx_enabled = True
        r = canlog.ScriptRunner(s, canlog.parse_script(
            "repeat forever\nsend LS 101 FE 01 3E\nwait 20\nend"))
        r.start()
        self.assertTrue(wait_until(lambda: r.sent >= 3))
        r.stop()
        r.join(3)
        self.assertEqual(r.result[0], "stopped")

    def test_script_needs_its_bus_open(self):
        s = self.open(buses=("LS",), traffic=False)
        s.tx_enabled = True
        with self.assertRaisesRegex(canlog.BusError, "uses the HS bus"):
            canlog.ScriptRunner(s, canlog.parse_script("send HS 7E0 00")).start()

    def test_examples_run_on_simulator(self):
        s = self.open(traffic=False)
        s.tx_enabled = True
        text = (EXAMPLES / "hs_request_reply.canscript").read_text("utf-8")
        r, _ = self.run_script(s, text)
        self.assertEqual(r.result, ("done", "Script finished: 4 frames sent."))
        # the LS example, with its 2 s waits shortened
        text = (EXAMPLES / "ls_wake_keepalive.canscript").read_text("utf-8").replace("wait 2000", "wait 5")
        r, _ = self.run_script(s, text)
        self.assertEqual(r.result[0], "done")
        ls = [x for x in s.backend.sent if x[0] == "LS"]
        self.assertEqual(ls[0], ("LS", 0x100, False, b"", True))
        self.assertEqual(ls[1:], [("LS", 0x101, False, b"\xfe\x01\x3e", False)] * 10)

    def test_open_failure_is_reported_not_raised_on_thread(self):
        class Broken(canlog.SimBackend):
            def open(self, buses):
                raise canlog.BusError("device does not support single-wire CAN")
        events = []
        s = canlog.Session(Broken(), ("LS",), on_event=lambda k, a: events.append((k, a)))
        s.start()
        with self.assertRaisesRegex(canlog.BusError, "single-wire"):
            s.wait_open(5)
        self.assertTrue(wait_until(lambda: events))
        self.assertEqual(events, [("closed", "device does not support single-wire CAN")])


class J2534Binding(unittest.TestCase):
    """Structure layout and registry listing only; no device is opened."""

    def test_struct_sizes(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(j2534.PASSTHRU_MSG), 6 * 4 + 4128)
        self.assertEqual(ctypes.sizeof(j2534.SCONFIG), 8)

    def test_constants(self):
        self.assertEqual((j2534.CAN, j2534.SW_CAN_PS, j2534.J1962_PINS), (5, 0x8008, 0x8001))
        self.assertEqual(j2534.BUS_SETUP["LS"][:3], (j2534.SW_CAN_PS, (33333, 33300), 0x0100))
        self.assertEqual(j2534.BUS_SETUP["HS"][:3], (j2534.CAN, (500000,), None))

    def test_list_devices_shape(self):
        for d in j2534.list_devices():      # whatever is installed here; may be none
            self.assertTrue(d.key and d.dll)
            self.assertIsInstance(d.protocols, set)

    def test_missing_dll_message(self):
        info = j2534.DeviceInfo("X - Y", "Y", "X", r"C:\no\such\driver.dll")
        with self.assertRaisesRegex(canlog.BusError, "missing"):
            j2534.J2534Backend(info).open(("HS",))


class Detect(unittest.TestCase):
    """The presence check's result handling, with stand-in helper commands (no driver)."""

    def test_classify(self):
        base = j2534.DETECT_BASE
        self.assertEqual(j2534.classify_detect(base + 0), "detected")
        self.assertEqual(j2534.classify_detect(base + j2534.ERR_DEVICE_IN_USE), "in use")
        self.assertEqual(j2534.classify_detect(base + j2534.ERR_DEVICE_NOT_CONNECTED), "not detected")
        self.assertEqual(j2534.classify_detect(base + j2534.ERR_FAILED), "not detected")
        self.assertEqual(j2534.classify_detect(base + j2534.DETECT_NO_DRIVER), "not detected")
        self.assertEqual(j2534.classify_detect(base + j2534.ERR_TIMEOUT), "no answer")
        self.assertEqual(j2534.classify_detect(None), "no answer")
        self.assertEqual(j2534.classify_detect(1), "not detected")     # helper crashed

    def test_run_detect(self):
        def cmd(code):
            return [sys.executable, "-c", code]
        self.assertEqual(j2534.run_detect(cmd("import sys; sys.exit(100)")), "detected")
        self.assertEqual(j2534.run_detect(cmd("import sys; sys.exit(108)")), "not detected")
        self.assertEqual(j2534.run_detect(cmd("import sys; sys.exit(114)")), "in use")
        t0 = time.monotonic()
        self.assertEqual(j2534.run_detect(cmd("import time; time.sleep(30)"), timeout=0.5), "no answer")
        self.assertLess(time.monotonic() - t0, 10)                     # stalled helper is stopped
        self.assertEqual(j2534.run_detect([r"C:\no\such\helper.exe"]), "not detected")

    def test_mdi_driver_is_skipped(self):
        self.assertTrue(j2534.detect_skipped("C:/GM MDI Software/Dynamic Link Libraries/BVTX4J32.dll"))
        self.assertFalse(j2534.detect_skipped("C:/Windows/SysWOW64/VCIV200432.dll"))

    def test_child_reports_missing_driver(self):
        self.assertEqual(j2534.detect_child(r"C:\no\such\driver.dll"),
                         j2534.DETECT_BASE + j2534.DETECT_NO_DRIVER)


if __name__ == "__main__":
    unittest.main()
