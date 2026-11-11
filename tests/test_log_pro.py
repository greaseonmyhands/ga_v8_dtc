"""Offline tests for tools/log_pro: DBC parsing/decoding and the PID log writer.

Run:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "log_pro"))
sys.path.insert(0, str(ROOT / "tools" / "j2534_can"))
import dbc  # noqa: E402
import pidlog  # noqa: E402

DEMO = ROOT / "tools" / "log_pro" / "examples" / "simulator_demo.dbc"

SAMPLE = '''VERSION ""
BU_: ECM BCM
BO_ 201 Engine_Status: 8 ECM
 SG_ EngSpd : 15|16@0+ (0.25,0) [0|16383.75] "rpm"  BCM
 SG_ EngRun : 7|1@0+ (1,0) [0|1] ""  BCM
 SG_ Temp : 39|8@0+ (1,-40) [-40|215] "degC"  BCM
 SG_ Torque : 47|12@0- (0.5,0) [-1024|1023.5] "Nm"  BCM
 SG_ IntelWord : 48|16@1+ (1,0) [0|65535] ""  BCM

BO_ 2149851136 Power_Mode_LS: 1 BCM
 SG_ SysPwrMd : 1|2@0+ (1,0) [0|3] ""  ECM

BO_ 2416189504 Full_Id_Msg: 2 BCM
 SG_ Whole : 7|16@0+ (1,0) [0|65535] ""  ECM

BO_ 1000 Muxed: 3 ECM
 SG_ Page M : 7|8@0+ (1,0) [0|255] ""  BCM
 SG_ OnPage1 m1 : 15|8@0+ (1,0) [0|255] ""  BCM
 SG_ OnPage2 m2 : 15|16@0+ (1,0) [0|65535] ""  BCM

BO_ 3221225472 VECTOR__INDEPENDENT_SIG_MSG: 0 Vector__XXX
 SG_ Orphan : 0|8@1+ (1,0) [0|0] "" Vector__XXX

CM_ SG_ 201 EngSpd "Engine speed";
VAL_ 2149851136 SysPwrMd 3 "Crank Request" 2 "Run" 1 "Accessory"
 0 "Off" ;
VAL_ 201 EngRun 1 "true" 0 "false" ;
'''


class DbcParsing(unittest.TestCase):
    def setUp(self):
        self.d = dbc.parse(SAMPLE, "sample.dbc")

    def test_messages(self):
        self.assertEqual([m.name for m in self.d.messages],
                         ["Engine_Status", "Power_Mode_LS", "Full_Id_Msg", "Muxed"])
        eng, pwr, full, _ = self.d.messages
        self.assertEqual((eng.frame_id, eng.ext, eng.dlc, eng.id_text), (0x0C9, False, 8, "0C9"))
        self.assertEqual((pwr.frame_id, pwr.ext, pwr.id_text), (0x00242000, True, "00242000"))
        self.assertTrue(pwr.param_only)
        self.assertEqual(full.frame_id, 0x10042040)
        self.assertFalse(full.param_only)
        self.assertEqual(self.d.signal_count, 5 + 1 + 1 + 3)

    def test_signal_fields_and_value_tables(self):
        spd = self.d.messages[0].signals[0]
        self.assertEqual((spd.name, spd.start, spd.length, spd.little, spd.signed, spd.factor,
                          spd.unit), ("EngSpd", 15, 16, False, False, 0.25, "rpm"))
        pwr = self.d.messages[1].signals[0]
        self.assertEqual(pwr.choices, {3: "Crank Request", 2: "Run", 1: "Accessory", 0: "Off"})
        self.assertEqual(pwr.text(2), "2  (Run)")
        self.assertEqual(spd.text(850.25), "850.25")
        self.assertEqual(spd.text(None), "")

    def test_decode_motorola_intel_signed_offset(self):
        eng = self.d.messages[0]
        v = eng.decode(bytes.fromhex("800D48005AFFE034"))
        self.assertEqual(v["EngRun"], 1)
        self.assertEqual(v["EngSpd"], 0x0D48 * 0.25)          # bytes 1-2, big-endian
        self.assertEqual(v["Temp"], 0x5A - 40)                # byte 4, offset
        self.assertEqual(v["Torque"], -2 * 0.5)               # bytes 5-6 top 12 bits = 0xFFE = -2
        self.assertEqual(v["IntelWord"], 0x34E0)              # bytes 6-7, little-endian
        self.assertIsInstance(v["Temp"], int)
        self.assertIsInstance(v["EngSpd"], float)

    def test_short_frame_gives_no_value_instead_of_garbage(self):
        eng = self.d.messages[0]
        v = eng.decode(bytes.fromhex("800D"))
        self.assertEqual(set(v), {"EngRun"})                  # EngSpd needs byte 2 as well
        self.assertIsNone(eng.signals[0].decode(bytes.fromhex("800D")))
        self.assertEqual(eng.signals[0].decode(bytes.fromhex("800D48")), 0x0D48 * 0.25)

    def test_lookup_exact_and_gmlan_parameter_id(self):
        d = self.d
        self.assertEqual(d.lookup(0x0C9, False).name, "Engine_Status")
        self.assertIsNone(d.lookup(0x0C9, True))
        self.assertIsNone(d.lookup(0x0CA, False))
        # LS frame as seen on the bus: priority 4, sender 0x40 -> matched by parameter id
        self.assertEqual(d.lookup(0x10242040, True).name, "Power_Mode_LS")
        self.assertEqual(d.lookup(0x0C242099, True).name, "Power_Mode_LS")
        self.assertIsNone(d.lookup(0x10244040, True))
        # a message written with its full ID only matches that exact ID
        self.assertEqual(d.lookup(0x10042040, True).name, "Full_Id_Msg")
        self.assertIsNone(d.lookup(0x10042041, True))
        self.assertEqual(d.lookup(0x10242040, True).decode(b"\x02"), {"SysPwrMd": 2})

    def test_multiplexed(self):
        m = self.d.lookup(1000, False)
        self.assertEqual(m.decode(bytes([1, 0x55, 0xAA])), {"Page": 1, "OnPage1": 0x55})
        self.assertEqual(m.decode(bytes([2, 0x55, 0xAA])), {"Page": 2, "OnPage2": 0x55AA})
        self.assertEqual(m.decode(bytes([9, 0x55, 0xAA])), {"Page": 9})

    def test_not_a_dbc(self):
        with self.assertRaises(dbc.DbcError):
            dbc.parse("hello,world\n1,2\n", "x.csv")

    def test_guess_bus(self):
        self.assertEqual(dbc.guess_bus("GlobalA - LS (1).dbc"), "LS")
        self.assertEqual(dbc.guess_bus("GlobalA - HS (1).dbc"), "HS")
        self.assertEqual(dbc.guess_bus("gm_global_a_lowspeed.dbc"), "LS")
        self.assertEqual(dbc.guess_bus("gm_global_a_powertrain_expansion.dbc"), "HS")
        self.assertEqual(dbc.guess_bus("GMW8762_PPEI.dbc"), "Any")
        self.assertEqual(dbc.guess_bus("flashstuff.dbc"), "Any")       # 'ls'/'hs' inside a word

    def test_demo_dbc_matches_the_simulator(self):
        import canlog
        d = dbc.load(DEMO)
        sim = canlog.SimBackend()
        frames = {}
        sim.open(("LS", "HS"))
        import time
        time.sleep(0.3)
        for bus in ("LS", "HS"):
            for f in sim.read(bus):
                frames[(f.bus, f.arb_id)] = f
        eng = frames[("HS", 0x0C9)]
        v = d.lookup(eng.arb_id, eng.ext).decode(eng.data)
        self.assertEqual(v["SimTemp"], 32.5)
        self.assertEqual(v["SimGear"], 10)
        self.assertEqual(v["SimCounter16"] & 0xFF, v["SimCounterLo"])
        body = frames[("LS", 0x10242040)]
        v = d.lookup(body.arb_id, body.ext).decode(body.data)
        self.assertEqual(v["SimVolts"], 25.5)
        self.assertTrue(-128 <= v["SimSigned"] <= 127)


class PidLogFile(unittest.TestCase):
    def make(self, d, **kw):
        cols = [pidlog.Column("a", "Engine RPM", "rpm"), pidlog.Column("b", "Gear", ""),
                pidlog.Column("c", "Engine RPM", "rpm"), pidlog.Column("d", 'Bad, "name"', "deg,C")]
        return pidlog.PidLog(Path(d), cols, 0.1, start_ts=5.0, stem="t",
                             now=datetime(2026, 10, 5, 22, 32, 37), notes="bench", **kw)

    def test_hpt_layout_matches_a_vcm_scanner_export(self):
        with tempfile.TemporaryDirectory() as d:
            log = self.make(d)
            log.advance(4.9)                      # before the start: nothing
            self.assertEqual(log.rows, 0)
            log.advance(5.0)                      # row 0: nothing known yet
            log.update("a", 850.25)
            log.update("b", 3)
            log.update("zzz", 1)                  # not a column: ignored
            log.advance(5.25)                     # rows at 0.1 and 0.2
            log.update("a", 1 / 3)
            log.update("d", -4)
            log.advance(5.3)
            log.close()
            log.advance(9.0)                      # closed: ignored
            raw = (Path(d) / "t.csv").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))            # no BOM, like the export
        lines = raw.decode("utf-8").split("\r\n")
        self.assertEqual(lines[:13], [
            "HP Tuners CSV Log File",
            "Version: 1.0",
            "",
            "[Log Information]",
            "Creation Time: 10/5/2026 10:32:37 PM",
            "Notes: bench",
            "",
            "[Channel Information]",
            "0,900001,900002,900003,900004",
            "Offset,Engine RPM,Gear,Engine RPM (2),Bad name",
            "s,rpm,,rpm,deg C",
            "",
            "[Channel Data]",
        ])
        self.assertEqual(lines[13:], [
            "0.000,,,,",
            "0.100,850.25,3,,",
            "0.200,850.25,3,,",
            "0.300,0.3333333333,3,,-4",
            "",
        ])
        self.assertEqual(log.rows, 4)

    def test_plain_layout(self):
        with tempfile.TemporaryDirectory() as d:
            log = self.make(d, style="plain")
            log.update("a", 1)
            log.advance(5.0)
            log.close()
            lines = (Path(d) / "t.csv").read_text("utf-8").splitlines()
        self.assertEqual(lines, ["Time (s),Engine RPM (rpm),Gear,Engine RPM (2) (rpm),Bad name (deg C)",
                                 "0.000,1,,,"])

    def test_names_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 1, 2, 3, 4, 5)
            a = pidlog.PidLog(Path(d), [], 0.1, 0.0, now=now)
            b = pidlog.PidLog(Path(d), [], 0.1, 0.0, now=now)
            a.close(); b.close()
            self.assertEqual((a.path.name, b.path.name),
                             ("pids_20260102_030405.csv", "pids_20260102_030405_2.csv"))

    def test_creation_time_format(self):
        self.assertEqual(pidlog.creation_time(datetime(2026, 1, 9, 0, 5, 7)), "1/9/2026 12:05:07 AM")
        self.assertEqual(pidlog.creation_time(datetime(2026, 12, 31, 12, 0, 0)), "12/31/2026 12:00:00 PM")
        self.assertEqual(pidlog.creation_time(datetime(2026, 10, 5, 22, 32, 37)), "10/5/2026 10:32:37 PM")

    def test_catch_up_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            log = pidlog.PidLog(Path(d), [pidlog.Column("a", "A")], 0.001, 0.0, stem="t")
            log.advance(10 ** 6)
            self.assertEqual(log.rows, pidlog.PidLog.MAX_CATCH_UP)
            log.close()


if __name__ == "__main__":
    unittest.main()
