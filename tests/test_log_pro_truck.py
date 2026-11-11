"""Offline tests for LOG PRO's built-in truck set, PID poller and bidirectional controls.

Run:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "log_pro"))
sys.path.insert(0, str(ROOT / "tools" / "j2534_can"))
import canlog  # noqa: E402
import controls as ctl  # noqa: E402
import pidlog  # noqa: E402
import poller  # noqa: E402
import truck  # noqa: E402


def item(**kw):
    base = dict(key="k", req=0x7E0, resp=0x7E8, service=0x22, ident=0x1234, nbytes=2,
                factor=0.5, offset=-10.0, interval_s=0.1)
    base.update(kw)
    return poller.PollItem(**base)


class TruckSetTests(unittest.TestCase):
    def setUp(self):
        self.d = truck.build()

    def test_counts(self):
        sigs = [s for m in self.d.messages for s in m.signals]
        self.assertEqual(sum(1 for s in sigs if s.poll), len(truck.DATA["polled"]))
        self.assertEqual(sum(1 for s in sigs if not s.poll), len(truck.DATA["broadcast"]))
        self.assertTrue(all(not m.ext for m in self.d.messages))

    def test_rpm_uses_hpt_channel(self):
        rpm = next(s for m in self.d.messages for s in m.signals if s.name == "engRpm")
        self.assertEqual(rpm.hpt_id, 12)
        self.assertEqual(rpm.display_name, "Engine RPM (SAE)")
        data = bytes([0, 0x1F, 0x40, 0, 0, 0, 0, 0])        # 0x1F40 * 0.25 = 2000 rpm
        self.assertAlmostEqual(rpm.decode(data), 2000.0)

    def test_safety_signals_in_display_units(self):
        spd = truck.broadcast_signal("vehSpeed")
        self.assertEqual(spd.message.frame_id, 0x3E9)
        raw = round(100 / 0.015625)                          # 100 km/h on the wire
        data = bytearray(8)
        data[0] = (raw >> 8) & 0x7F                         # Motorola: start bit 6, 15 bits
        data[1] = raw & 0xFF
        self.assertAlmostEqual(spd.decode(bytes(data)), 62.1371, places=3)
        gear = truck.broadcast_signal("transGearCmd")
        self.assertEqual(gear.message.frame_id, 0x1F5)

    def test_polled_signals_have_requests(self):
        for m in self.d.messages:
            for s in m.signals:
                if s.poll:
                    self.assertIn(s.poll["service"], (0x01, 0x22))
                    self.assertIn(s.poll["req"], (0x7E0, 0x241, 0x7E2, 0x7E7))   # 7E7: wideband
                    self.assertTrue(m.name.startswith("Polled:"))

    def test_hpt_ids_reach_the_log(self):
        rpm = next(s for m in self.d.messages for s in m.signals if s.name == "engRpm")
        other = next(s for m in self.d.messages for s in m.signals if not s.hpt_id)
        with tempfile.TemporaryDirectory() as tmp:
            cols = [pidlog.Column(1, rpm.display_name, rpm.unit, rpm.hpt_id),
                    pidlog.Column(2, other.display_name, other.unit, other.hpt_id)]
            log = pidlog.PidLog(tmp, cols, 0.1, 0.0, now=datetime(2026, 11, 11, 9, 0, 0))
            log.close()
            lines = log.path.read_text("utf-8").splitlines()
        i = lines.index("[Channel Information]")
        self.assertEqual(lines[i + 1], "0,12,0")
        self.assertTrue(lines[i + 2].startswith("Offset,Engine RPM (SAE),"))

    def test_controls_catalog(self):
        cs = truck.controls()
        mods = truck.modules()
        self.assertEqual(len(cs), 23)
        for c in cs:
            self.assertIn(c["module"], mods)
            self.assertGreater(c["max_run_s"], 0)
            self.assertNotIn("lock", c["label"].lower())
            self.assertNotIn("start", c["label"].lower())
        self.assertTrue(all(c["hold_to_run"] for c in cs if "window" in c["label"].lower()))


class PollerTests(unittest.TestCase):
    def test_request_frames(self):
        self.assertEqual(item().request(), bytes([3, 0x22, 0x12, 0x34, 0, 0, 0, 0]))
        self.assertEqual(item(service=0x01, ident=0x0C).request(), bytes([2, 0x01, 0x0C, 0, 0, 0, 0, 0]))

    def test_reply_value(self):
        p = poller.Poller([item()])
        self.assertEqual(p.due(0.0), [(0x7E0, item().request())])
        self.assertEqual(p.due(0.01), [])                       # one pending per module
        got = p.on_frame(0x7E8, bytes([5, 0x62, 0x12, 0x34, 0x00, 0x64, 0, 0]), 0.02)
        self.assertEqual(got, ("k", 100 * 0.5 - 10.0))
        self.assertEqual(p.due(0.05), [])                       # not due again yet
        self.assertEqual(len(p.due(0.11)), 1)

    def test_mode01_reply(self):
        p = poller.Poller([item(service=0x01, ident=0x0C, factor=0.25, offset=0.0)])
        p.due(0.0)
        got = p.on_frame(0x7E8, bytes([4, 0x41, 0x0C, 0x1F, 0x40, 0, 0, 0]), 0.01)
        self.assertEqual(got, ("k", 2000.0))

    def test_one_request_per_module_round_robin(self):
        a, b = item(key="a"), item(key="b", ident=0x1235)
        c = item(key="c", req=0x241, resp=0x641)
        p = poller.Poller([a, b, c])
        first = p.due(0.0)
        self.assertEqual(sorted(r for r, _ in first), [0x241, 0x7E0])
        p.on_frame(0x7E8, bytes([5, 0x62, 0x12, 0x34, 0, 1, 0, 0]), 0.01)
        nxt = p.due(0.02)
        self.assertEqual(nxt, [(0x7E0, b.request())])

    def test_unsupported_and_pending(self):
        it = item()
        p = poller.Poller([it])
        p.due(0.0)
        self.assertIsNone(p.on_frame(0x7E8, bytes([3, 0x7F, 0x22, 0x78, 0, 0, 0, 0]), 0.2))
        self.assertEqual(p.due(0.3), [])                        # 78 restarted the wait
        p.on_frame(0x7E8, bytes([3, 0x7F, 0x22, 0x31, 0, 0, 0, 0]), 0.35)
        self.assertIn("31", it.unsupported)
        self.assertEqual(p.due(5.0), [])                        # never asked again

    def test_timeouts_back_off(self):
        it = item()
        p = poller.Poller([it])
        t = 0.0
        for _ in range(poller.BACKOFF_AFTER):
            self.assertEqual(len(p.due(t)), 1)
            t += poller.REPLY_TIMEOUT_S + 0.2
        p.due(t)                                                 # clears the last one
        self.assertGreaterEqual(it.fails, poller.BACKOFF_AFTER)
        self.assertEqual(p.due(t + 0.5), [])

    def test_short_reply_ignored(self):
        p = poller.Poller([item(nbytes=2)])
        p.due(0.0)
        self.assertIsNone(p.on_frame(0x7E8, bytes([4, 0x62, 0x12, 0x34, 0x00, 0, 0, 0]), 0.01))


CONTROL = {"group": "Fans", "label": "Fans 50%", "cpid": 0x2B, "bytes": [0x80, 0x32],
           "max_run_s": 10, "watch": "fanSpeedPct", "confirmed": True, "engine_test": False,
           "module": "ECM", "hold_to_run": False}


def reply(*content):
    return ctl.frame(*content)


class ControlTests(unittest.TestCase):
    def test_full_run(self):
        r = ctl.ControlRun(CONTROL, 0x7E0, 0x7E8, 0.0)
        self.assertEqual(r.step(0.0), [ctl.frame(0xAE, 0x2B, 0x80, 0x32)])
        r.on_frame(0x7E8, reply(0xEE, 0x2B), 0.05)
        self.assertEqual(r.state, "running")
        self.assertEqual(r.step(1.0), [])
        self.assertEqual(r.step(2.1), [ctl.frame(0x3E)])        # TesterPresent keep-alive
        r.stop()
        self.assertEqual(r.step(3.0), [ctl.frame(0xAE, 0x00)])
        r.on_frame(0x7E8, reply(0xEE, 0x00), 3.05)
        self.assertEqual(r.step(3.06), [ctl.frame(0x20)])
        r.on_frame(0x7E8, reply(0x60), 3.1)
        self.assertTrue(r.finished)
        self.assertEqual(r.state, "done")

    def test_time_limit_stops_it(self):
        r = ctl.ControlRun(CONTROL, 0x7E0, 0x7E8, 0.0)
        r.step(0.0)
        r.on_frame(0x7E8, reply(0xEE, 0x2B), 0.05)
        self.assertEqual(r.step(10.1), [ctl.frame(0xAE, 0x00)])

    def test_refused(self):
        r = ctl.ControlRun(CONTROL, 0x7E0, 0x7E8, 0.0)
        r.step(0.0)
        r.on_frame(0x7E8, reply(0x7F, 0xAE, 0x22), 0.05)
        self.assertEqual(r.state, "refused")
        self.assertIn("22", r.message)

    def test_no_answer_still_cancels(self):
        r = ctl.ControlRun(CONTROL, 0x7E0, 0x7E8, 0.0)
        r.step(0.0)
        self.assertEqual(r.step(1.5), [ctl.frame(0xAE, 0x00)])
        self.assertEqual(r.state, "no_answer")

    def test_module_ends_it(self):
        r = ctl.ControlRun(CONTROL, 0x7E0, 0x7E8, 0.0)
        r.step(0.0)
        r.on_frame(0x7E8, reply(0xEE, 0x2B), 0.05)
        r.on_frame(0x7E8, reply(0x7F, 0xAE, 0xE3), 2.0)
        self.assertEqual(r.state, "ended_by_module")
        self.assertEqual(r.step(2.01), [ctl.frame(0x20)])

    def test_other_ids_ignored(self):
        r = ctl.ControlRun(CONTROL, 0x7E0, 0x7E8, 0.0)
        r.step(0.0)
        r.on_frame(0x641, reply(0xEE, 0x2B), 0.05)
        self.assertEqual(r.state, "sending")

    def test_interlock(self):
        eng = dict(CONTROL, engine_test=True)
        self.assertEqual(ctl.interlock(CONTROL, 0.0, None, None), "")
        self.assertEqual(ctl.interlock(CONTROL, None, None, None), "")
        self.assertIn("moving", ctl.interlock(CONTROL, 15.0, 800, 15))
        self.assertIn("engine speed", ctl.interlock(eng, 0.0, None, 15))
        self.assertIn("idle", ctl.interlock(eng, 0.0, 2500, 15))
        self.assertIn("Park", ctl.interlock(eng, 0.0, 700, 1))
        self.assertEqual(ctl.interlock(eng, 0.0, 700, 13), "")


class SimulatorReplyTests(unittest.TestCase):
    """The simulator answers the polls and controls, so LOG PRO can be tried without a truck."""

    def run_session(self, sends, poll=False, tx=False):
        frames = []
        s = canlog.Session(canlog.SimBackend(), ("HS",), on_frames=frames.extend,
                           on_event=lambda *a: None)
        s.start()
        try:
            for _ in range(100):
                if s.running:
                    break
                time.sleep(0.02)
            s.poll_enabled, s.tx_enabled = poll, tx
            results = [s.queue_send("HS", req, False, data, poll=poll) for req, data in sends]
            time.sleep(0.4)
        finally:
            s.stop(wait=2.0)
        return results, [f for f in frames if not f.tx and f.arb_id in (0x7E8, 0x641, 0x7EA)]

    def test_nothing_sent_without_enable(self):
        ok, got = self.run_session([(0x7E0, item().request())])
        self.assertEqual(ok, [False])
        self.assertEqual(got, [])

    def test_poll_reply(self):
        ok, got = self.run_session([(0x7E0, item().request())], poll=True)
        self.assertEqual(ok, [True])
        self.assertEqual(got[0].data[:4], bytes([5, 0x62, 0x12, 0x34]))

    def test_control_reply(self):
        ok, got = self.run_session([(0x241, ctl.frame(0xAE, 0x10, 1))], tx=True)
        self.assertEqual(ok, [True])
        self.assertEqual(got[0].arb_id, 0x641)
        self.assertEqual(got[0].data[:3], bytes([2, 0xEE, 0x10]))


if __name__ == "__main__":
    unittest.main()
