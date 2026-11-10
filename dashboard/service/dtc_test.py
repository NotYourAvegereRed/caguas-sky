"""Self-check for the trouble code scan in cvt_reader.py (decode + the scan against a scripted ELM327).
Run:  python3 dashboard/service/dtc_test.py"""
import cvt_reader as c

assert c.dtc_code(0x04, 0x20) == "P0420" and c.dtc_code(0xD0, 0x00) == "U1000" and c.dtc_code(0x97, 0x1A) == "B171A"
assert c.dtc_decode([0x59, 0x02, 0x0D, 0x04, 0x20, 0x00, 0x08, 0xD0, 0x00, 0x00, 0x08]) == ["P0420", "U1000"]
assert c.dtc_decode([0x58, 0x01, 0x17, 0xF0, 0x60]) == ["P17F0"]
assert c.dtc_decode([0x43, 0x01, 0x03, 0x00]) == ["P0300"] and c.dtc_decode([0x47, 0x01, 0x01, 0x13]) == ["P0113"]
assert c.dtc_decode([0x59, 0x02, 0x0D]) == [] and c.dtc_decode([0x58, 0x00]) == [] and c.dtc_decode([0x43, 0x00]) == []
assert c.dtc_decode([0x7F, 0x19, 0x11]) is None and c.dtc_decode(None) is None
# "response pending" is skipped, multi-frame is joined
assert c.isotp_payload("765 03 7F 19 78\r765 03 59 02 0D", "765") == [0x59, 0x02, 0x0D]
assert c.isotp_payload("7E9 10 08 58 02 17 F0 60 07\r7E9 21 10 60", "7E9") == [0x58, 2, 0x17, 0xF0, 0x60, 0x07, 0x10, 0x60]


class FakeElm:
    """answers by (header, request); anything else = NO DATA"""
    def __init__(self, replies):
        self.replies, self.hdr, self.buf, self.sent = replies, None, b"", []

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        if cmd.startswith("ATSH"):
            self.hdr = cmd[4:]
        self.buf = (self.replies.get((self.hdr, cmd), "OK" if cmd.startswith("AT") else "NO DATA") + "\r>").encode()

    in_waiting = 0

    def read(self, n):
        out, self.buf = self.buf, b""
        return out


# like the Rogue with the MAF unplugged: ECM P0102 stored + P0102 / P0113 pending, TCM and BCM no codes
ROGUE = {("7DF", "03"): "7E8 04 43 01 01 02", ("7DF", "07"): "7E8 06 47 02 01 02 01 13",
         ("7E1", "19020D"): "7E9 03 59 02 0D", ("745", "19020D"): "765 03 59 02 0D"}
elm = FakeElm(ROGUE)
r = c.CvtReader()
assert r.dtcs() == [] and r._read_dtcs(elm) is True
assert r.dtcs() == [{"mod": "ECM", "code": "P0102"}, {"mod": "ECM", "code": "P0113"}] and r.dtc_version == 1
assert elm.sent[-2:] == ["ATAR", "ATAT2"] and not any(s.startswith(("14", "04")) for s in elm.sent)     # a scan never clears
assert elm.sent.index("1081") < elm.sent.index("03") and r.pop_scans() == [r.dtcs()] and r.pop_scans() == []

# ---- CLEAR button: refused unless engine off + stopped; ECM sequence from the snoop; only modules with a code ----
import time
class ClearElm(FakeElm):
    def write(self, data):
        if data.decode().strip() == "14FF00":              # the ECM forgets its codes
            self.replies = {**self.replies, ("7DF", "03"): "7E8 02 43 00", ("7DF", "07"): "7E8 02 47 00"}
        super().write(data)
def reader(rpm, speed):
    r = c.CvtReader()
    r._eng, r._rpm_ts = {"rpm": rpm}, time.monotonic()
    r._data, r._ts = {"speed_kmh": speed}, time.monotonic()
    return r
for rpm, speed in ((700, 0), (0, 5), (None, 0)):
    elm = ClearElm({**ROGUE, ("7E0", "14FF00"): "7E8 03 54 FF 00"})
    assert reader(rpm, speed)._clear_dtcs(elm).startswith("NOT CLEARED") and elm.sent == []
elm = ClearElm({**ROGUE, ("7E0", "10C0"): "7E8 02 50 C0", ("7E0", "14FF00"): "7E8 03 54 FF 00"})
r = reader(0, 0)
assert r._clear_dtcs(elm) == "CODES CLEARED" and r.dtcs() == []
w = [s for s in elm.sent if s.startswith("14")]
assert w == ["14FF00"] and elm.sent[elm.sent.index("14FF00") - 1] == "10C0"       # TCM / BCM had no codes: not touched
assert len(r.pop_scans()) == 2                                                  # before (for the history) and after
assert reader(0, 0)._clear_dtcs(FakeElm({k: v for k, v in ROGUE.items() if k[0] != "7DF"})) == "NO CODES TO CLEAR"
# ECM silent (still in its session): the other codes are kept, but the scan is not done -> the loop retries
r = c.CvtReader()
assert r._read_dtcs(FakeElm({k: v for k, v in ROGUE.items() if k[0] != "7DF"})) is False and r.dtc_version == 1
# car asleep: nothing answers -> not counted as a scan
r = c.CvtReader()
assert r._read_dtcs(FakeElm({})) is False and r.dtc_version == 0
print("dtc ok")

# ---- alerts: CVT fluid 100 C, voltage 11.7 / 11.8 V ----
a = c.Alerts()
a.cvt_temp(99); assert not a.list()
a.cvt_temp(100); assert a.list()[0]["id"] == "cvt_temp"
a.cvt_temp(98); assert a.list()                       # holds until below 97
a.cvt_temp(96); assert not a.list()
a.rpm(700, 0.0)
a.battery(11.6, 5.0); a.battery(11.6, 9.0); assert not a.list()          # engine running < 10 s
a.battery(11.68, 12.0); assert not a.list()                             # one low reading is not enough
a.battery(12.0, 19.0); a.battery(11.68, 26.0); assert not a.list()      # smart charging at 12 V, single dips
a.battery(11.68, 33.0); assert a.list()[0] == {"id": "battery", "value": 11.68}
a.battery(11.76, 40.0); assert a.list()                                 # clears at 11.8, not before
a.battery(11.84, 47.0); assert not a.list()
a.battery(15.04, 54.0); assert a.list(); a.battery(14.88, 61.0); assert a.list(); a.battery(14.72, 68.0); assert not a.list()
a.battery(11.6, 75.0); a.battery(11.6, 82.0); assert a.list()
a.rpm(0, 83.0); assert not a.list()                                     # engine off: not charging is normal

# ---- code history (hud_service; needs its imports, so this part only runs where they are installed) ----
import os, tempfile
os.environ["HUD_DTC_FILE"] = os.path.join(tempfile.mkdtemp(), "hud", "dtcs.json")
try:
    import hud_service as h
except ImportError as e:
    print("history check skipped:", e)
else:
    hist = h.load_dtc_hist()
    assert hist == []
    h.log_dtcs(hist, [{"mod": "ECM", "code": "P0420"}]); h.log_dtcs(hist, [])
    h.log_dtcs(hist, [{"mod": "ECM", "code": "P0420"}, {"mod": "BCM", "code": "U1000"}])
    assert [(e["code"], e["n"]) for e in hist] == [("P0420", 2), ("U1000", 1)] and h.load_dtc_hist() == hist
print("alerts + history ok")
