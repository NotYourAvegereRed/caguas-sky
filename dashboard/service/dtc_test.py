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
for tx in ("740", "744", "74D"):                       # 2026-11-19 modules, no codes: Nissan session around 19 02
    rx = f"{int(tx, 16) + 0x20:03X}"
    ROGUE.update({(tx, "10C0"): f"{rx} 02 50 C0", (tx, "19020D"): f"{rx} 03 59 02 09", (tx, "1081"): f"{rx} 02 50 81"})
ROGUE.update({("752", "19020D"): "772 03 59 02 09", ("758", "19020D"): "778 03 59 02 09"})


def isotp(rx, payload):
    """payload bytes -> the ELM text of its ISO-TP frames (first frame + consecutive frames)"""
    if len(payload) <= 7:
        return f"{rx} {len(payload):02X} " + " ".join(f"{b:02X}" for b in payload)
    frames, rest, n = [f"{rx} 10 {len(payload):02X} " + " ".join(f"{b:02X}" for b in payload[:6])], payload[6:], 1
    while rest:
        frames.append(f"{rx} 2{n % 16:X} " + " ".join(f"{b:02X}" for b in rest[:7])); rest, n = rest[7:], n + 1
    return "\r".join(frames)
elm = FakeElm(ROGUE)
r = c.CvtReader()
assert r.dtcs() == [] and r._read_dtcs(elm) is True
assert r.dtcs() == [{"mod": "ECM", "code": "P0102", "active": True}, {"mod": "ECM", "code": "P0113", "active": True}] and r.dtc_version == 1
assert elm.sent[-3:] == ["ATAR", "ATFCSM0", "ATAT2"] and not any(s.startswith(("14", "04")) for s in elm.sent)     # a scan never clears
assert elm.sent.index("1081") < elm.sent.index("03") and r.pop_scans() == [r.dtcs()] and r.pop_scans() == []

# ---- CLEAR button: refused unless in P + stopped; ECM sequence from the snoop; only modules with a code ----
import time
class ClearElm(FakeElm):
    def write(self, data):
        if data.decode().strip() == "14FF00":              # the ECM forgets its codes
            self.replies = {**self.replies, ("7DF", "03"): "7E8 02 43 00", ("7DF", "07"): "7E8 02 47 00"}
        super().write(data)
def reader(gear, speed):
    r = c.CvtReader()
    r._data, r._ts = {"gear": gear, "speed_kmh": speed}, time.monotonic()
    return r
assert c.CvtReader()._clear_dtcs(FakeElm({})).startswith("NOT CLEARED")              # no TCM data at all
for rpm, speed in (("D", 0), ("P", 5), ("N", 0), (None, 0), ("P", None)):
    elm = ClearElm({**ROGUE, ("7E0", "14FF00"): "7E8 03 54 FF 00"})
    assert reader(rpm, speed)._clear_dtcs(elm).startswith("NOT CLEARED") and elm.sent == []
elm = ClearElm({**ROGUE, ("7E0", "10C0"): "7E8 02 50 C0", ("7E0", "14FF00"): "7E8 03 54 FF 00"})
r = reader("P", 0)
assert r._clear_dtcs(elm) == "CODES CLEARED" and r.dtcs() == []
w = [s for s in elm.sent if s.startswith("14")]
assert w == ["14FF00"] and elm.sent[elm.sent.index("14FF00") - 1] == "10C0"       # TCM / BCM had no codes: not touched
assert len(r.pop_scans()) == 2                                                  # before (for the history) and after
assert reader("P", 0)._clear_dtcs(FakeElm({k: v for k, v in ROGUE.items() if k[0] != "7DF"})) == "NO CODES TO CLEAR"
# ECM silent (still in its session): the other codes are kept, but the scan is not done -> the loop retries
r = c.CvtReader()
assert r._read_dtcs(FakeElm({k: v for k, v in ROGUE.items() if k[0] != "7DF"})) is False and r.dtc_version == 1
# car asleep: nothing answers -> not counted as a scan
r = c.CvtReader()
assert r._read_dtcs(FakeElm({})) is False and r.dtc_version == 0
# CHECK FOR CODES: read-only, any gear, text for the page
r = c.CvtReader()
elm = FakeElm(ROGUE)
assert r._scan_dtcs(elm) == "SCANNED · 2 CODES · 2 ACTIVE" and not any(s.startswith(("14", "04")) for s in elm.sent)
assert r._scan_dtcs(FakeElm({k: v for k, v in ROGUE.items() if k[0] != "7DF"})) == "SCANNED · NO CODES · A MODULE DID NOT ANSWER"
assert r._scan_dtcs(FakeElm({})) == "NO ANSWER · ENGINE OFF?"
# 2026-11-19 snoop: ABS history codes (status 08) are stored, not active; status 09 = failing now
abs_reply = isotp("760", [0x59, 0x02, 0x09, 0x51, 0x30, 0x00, 0x08, 0xD0, 0x02, 0x01, 0x08, 0x51, 0x99, 0x00, 0x09])
assert c.dtc_records(c.isotp_payload(abs_reply, "760")) == {"C1130": False, "U1002": False, "C1199": True}
assert c.dtc_records([0x59, 0x02, 0xB9, 0xA0, 0xBD, 0x96, 0x28, 0xA0, 0xBD, 0x14, 0x2C]) == {"B20BD": True}   # same code twice: active if any
r = c.CvtReader()
elm = FakeElm({**ROGUE, ("740", "19020D"): abs_reply, ("752", "19020D"): "772 07 59 02 09 80 A0 8F 08"})
assert r._read_dtcs(elm) is True
assert [(d["mod"], d["code"], d["active"]) for d in r.dtcs() if d["mod"] in ("ABS", "SRS")] == \
       [("ABS", "C1130", False), ("ABS", "U1002", False), ("ABS", "C1199", True), ("SRS", "B00A0", False)]
k = elm.sent.index("ATSH740")
assert elm.sent[k + 1:k + 4] == ["10C0", "19020D", "1081"], elm.sent[k:k + 5]          # ABS read inside 10C0 ... 1081
assert elm.sent[k - 3:k] == ["ATFCSH740", "ATFCSD300000", "ATFCSM1"] and elm.sent.index("ATFCSM0") > k   # FCSH before FCSM1
# clear: ABS 10C0 -> 14FFFFFF -> 1081, SRS 1003 -> 14FFFFFF -> 1001 (the app's recipes); the ECM is not touched
class AllClear(FakeElm):
    def write(self, data):
        if data.decode().strip() == "14FFFFFF":
            self.replies = {k: v for k, v in self.replies.items() if k not in (("740", "19020D"), ("752", "19020D"))}
            self.replies.update({("740", "19020D"): "760 03 59 02 09", ("752", "19020D"): "772 03 59 02 09"})
        super().write(data)
noecm = {k: v for k, v in ROGUE.items() if k[0] != "7DF"}
noecm.update({("7DF", "03"): "7E8 02 43 00", ("7DF", "07"): "7E8 02 47 00"})
elm = AllClear({**noecm, ("740", "19020D"): abs_reply, ("752", "19020D"): "772 07 59 02 09 80 A0 8F 08",
                **{(tx, "14FFFFFF"): f"{rx} 01 54" for tx, rx in (("740", "760"), ("752", "772"))},
                ("752", "1003"): "772 06 50 03 00 32 01 F4", ("752", "1001"): "772 06 50 01 00 32 01 F4"})
r = reader("P", 0)
assert r._clear_dtcs(elm) == "CODES CLEARED", r.dtcs()
hdr_cmds = [s for s in elm.sent if s.startswith("ATSH") or s in ("10C0", "1003", "14FFFFFF", "1081", "1001")]
i = hdr_cmds.index("14FFFFFF")
assert hdr_cmds[i - 2:i + 2] == ["ATSH740", "10C0", "14FFFFFF", "1081"], hdr_cmds
j = hdr_cmds.index("14FFFFFF", i + 1)
assert hdr_cmds[j - 2:j + 2] == ["ATSH752", "1003", "14FFFFFF", "1001"], hdr_cmds
assert "14FF00" not in elm.sent
# engine start: only ECM / TCM / BCM; CHECK FOR CODES: all, and the report says who answered
r = c.CvtReader()
elm = FakeElm(ROGUE)
assert r._read_dtcs(elm, c.DTC_STARTUP) is True and not any(s in elm.sent for s in ("ATSH740", "ATSH744", "ATSH752", "ATSH758"))
rep = r.dtc_report()
assert rep["scope"] == "startup" and [m["mod"] for m in rep["mods"]] == ["ECM", "TCM", "BCM"] and all(m["ok"] for m in rep["mods"])
assert r._read_dtcs(FakeElm({k: v for k, v in ROGUE.items() if k[0] != "758"})) is False
rep = r.dtc_report()
assert rep["scope"] == "all" and len(rep["mods"]) == len(c.DTC_MODULES) and [m["mod"] for m in rep["mods"] if not m["ok"]] == ["TPMS"]
assert c.CvtReader().dtc_report() is None
# run hours: the TCM's 21 10 block from the CVTz50 log (2026-05-26), the app showed 4929.0 / 3114.8 h
blk = [0x61, 0x10, 0, 0, 0, 2, 0x73, 0x86, 0, 0, 0, 0, 0x1A, 0x73, 0, 0, 0, 0, 0, 0xA9, 0, 0, 0, 0, 0, 0x41, 0x49, 0x01] + [0] * 36
assert c.run_hours(blk) == (4929.0, 3114.8)
assert c.run_hours(blk[:20]) is None and c.run_hours([0x7F, 0x21, 0x12]) is None and c.run_hours(None) is None
assert c.run_hours(c.isotp_payload(isotp("7E9", blk), "7E9")) == (4929.0, 3114.8)
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
    assert h.dtc_desc("P0102") == "MAF sensor - low input (no signal)" and h.dtc_desc("P1234") == "Engine / CVT - Nissan code"
    assert h.dtc_desc("C0999") == "Chassis (brakes / ABS / TPMS) - generic code" and h.dtc_desc("X0000").startswith("Unknown")
print("alerts + history ok")
