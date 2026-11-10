"""Self-check for the trouble code scan in cvt_reader.py (decode + the scan against a scripted ELM327).
Run:  python3 dashboard/service/dtc_test.py"""
import cvt_reader as c

assert c.dtc_code(0x04, 0x20) == "P0420" and c.dtc_code(0xD0, 0x00) == "U1000" and c.dtc_code(0x97, 0x1A) == "B171A"
assert c.dtc_decode([0x59, 0x02, 0x0D, 0x04, 0x20, 0x00, 0x08, 0xD0, 0x00, 0x00, 0x08]) == ["P0420", "U1000"]
assert c.dtc_decode([0x58, 0x01, 0x17, 0xF0, 0x60]) == ["P17F0"]
assert c.dtc_decode([0x43, 0x01, 0x03, 0x00]) == ["P0300"]
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


# ECM: refuses 19, answers 18 with one code. TCM: 19 with no codes. BCM: silent.
elm = FakeElm({("7E0", "10C01"): "7E8 02 50 C0", ("7E0", "19020D"): "7E8 03 7F 19 11",
               ("7E0", "1802FF00"): "7E8 05 58 01 04 20 60", ("7E1", "19020D"): "7E9 03 59 02 0D"})
r = c.CvtReader()
assert r.dtcs() == [] and r._read_dtcs(elm) is True
assert r.dtcs() == [{"mod": "ECM", "code": "P0420"}] and r.dtc_version == 1
assert elm.sent[-1] == "ATAR" and not any(s.startswith(("14", "04")) for s in elm.sent)     # read-only
# car asleep: nothing answers -> not counted as a scan
r = c.CvtReader()
assert r._read_dtcs(FakeElm({})) is False and r.dtc_version == 0
print("dtc ok")
