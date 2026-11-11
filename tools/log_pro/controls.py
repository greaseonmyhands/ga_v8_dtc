"""Bidirectional controls (GM DeviceControl, $AE), as the CAN display runs them: one control
at a time, each ended by its time limit or by Stop.

    start:  $AE <cpid> <bytes>          -> $EE <cpid>   (7F AE <nrc> = refused)
    hold:   $3E TesterPresent every 2 s  (the module ends the control ~5 s after it stops)
    stop:   $AE 00 (cancel all)          -> $EE 00
            $20 ReturnToNormalMode       -> $60

Pure logic: step() returns the frames to send; the caller sends them and passes every
received frame to on_frame(). The caller does the safety checks before start().
"""
from __future__ import annotations

REPLY_TIMEOUT_S = 1.0
RETURN_NORMAL_TIMEOUT_S = 0.3
TESTER_PRESENT_S = 2.0
NRC_NAMES = {0x11: "service not supported", 0x12: "wrong length / not supported here",
             0x22: "conditions not correct", 0x31: "not supported, or needs a security unlock",
             0x33: "needs a security unlock", 0x78: "response pending",
             0xE3: "the module's own safety limit ended it"}


def frame(*content) -> bytes:
    body = bytes([len(content), *content])
    return body + bytes(8 - len(body))


def nrc_text(nrc: int) -> str:
    return f"NRC {nrc:02X}: {NRC_NAMES.get(nrc, 'refused')}"


class ControlRun:
    """One control from start to finished. state: sending, running, stopping, returning,
    done, refused, no_answer, ended_by_module, stop_unconfirmed."""

    def __init__(self, control: dict, req: int, resp: int, now: float):
        self.control, self.req, self.resp = control, req, resp
        self.state = "sending"
        self.message = "Sending..."
        self.t_sent = now
        self.t_started = None
        self.t_tp = now
        self.stop_wanted = False
        self.finished = False
        self._out = [frame(0xAE, control["cpid"], *control["bytes"])]

    def stop(self):
        self.stop_wanted = True

    def _begin_stop(self, now):
        self.state = "stopping"
        self.t_sent = now
        self._out.append(frame(0xAE, 0x00))

    def _finish(self, state, message):
        self.state, self.message, self.finished = state, message, True

    def step(self, now: float) -> list:
        """Frames to send now (data only; they go to self.req)."""
        st = self.state
        if st == "sending":
            if self.stop_wanted:                   # stop pressed before the reply: cancel anyway
                self._begin_stop(now)
            elif now - self.t_sent > REPLY_TIMEOUT_S:
                self._finish("no_answer", "No answer from the module (wrong module or ignored).")
                self._out.append(frame(0xAE, 0x00))  # in case it was applied but the reply was lost
        elif st == "running":
            if self.stop_wanted or now - self.t_started >= self.control["max_run_s"]:
                self._begin_stop(now)
            elif now - self.t_tp >= TESTER_PRESENT_S:
                self.t_tp = now
                self._out.append(frame(0x3E))
        elif st == "stopping":
            if now - self.t_sent > REPLY_TIMEOUT_S:
                self._finish("stop_unconfirmed", "Stop was not answered; the module ends it on "
                                                 "its own within about 5 s.")
        elif st == "returning":
            if now - self.t_sent > RETURN_NORMAL_TIMEOUT_S:
                self._finish("done", "Done.")
        out, self._out = self._out, []
        return out

    def on_frame(self, arb_id: int, data: bytes, now: float):
        if arb_id != self.resp or len(data) < 3 or self.finished:
            return
        sid = data[1]
        cpid = self.control["cpid"]
        if sid == 0xEE and self.state == "sending" and data[2] == cpid:
            self.state, self.t_started, self.t_tp = "running", now, now
            self.message = "Running (module accepted it)."
        elif sid == 0xEE and self.state == "stopping" and data[2] == 0x00:
            self.state, self.t_sent = "returning", now
            self._out.append(frame(0x20))
        elif sid == 0x60 and self.state == "returning":
            self._finish("done", "Done.")
        elif sid == 0x7F and data[2] == 0xAE:
            nrc = data[3] if len(data) > 3 else 0
            if nrc == 0x78:
                self.t_sent = now
            elif self.state == "sending":
                self._finish("refused", "Refused: " + nrc_text(nrc))
            elif self.state == "running":
                self._finish("ended_by_module", "Ended by the module: " + nrc_text(nrc))
                self._out.append(frame(0x20))
            elif self.state == "stopping":
                self.state, self.t_sent = "returning", now
                self._out.append(frame(0x20))
        elif sid == 0x7F and data[2] == 0x20 and self.state == "returning":
            self._finish("done", "Done.")


def interlock(control: dict, speed_mph, rpm, gear) -> str:
    """Reason the control must not run, or ''. Values are None when not seen on the bus.
    Speed is checked by the caller as well (unknown speed needs the user's say-so)."""
    if speed_mph is not None and speed_mph > 2.0:
        return f"the vehicle is moving ({speed_mph:.0f} mph)"
    if control.get("engine_test"):
        if rpm is None or gear is None:
            return "engine tests need engine speed and gear on the HS bus (engine idling in P or N)"
        if not 500 <= rpm <= 1500:
            return f"engine tests run only at idle (engine speed is {rpm:.0f} rpm)"
        if int(round(gear)) not in (13, 15):
            return "engine tests run only in Park or Neutral"
    return ""
