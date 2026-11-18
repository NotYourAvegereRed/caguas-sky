#!/usr/bin/env python3
"""
isotp_decode.py - decode a candump log of a diagnostic session (CVTz50 or any scan tool) on the Rogue OBD bus.

Reassembles ISO-TP (single / first / consecutive frames), pairs each request with its module, decodes trouble
code replies, and ends with a summary: every request each module was sent and whether it answered positively.
That summary is the list of "things CVTz50 can check".

Capture on the Pi (READ ONLY: listen-only sends nothing, not even ACKs). CANable on OBD pins 6 (H) / 14 (L), 500k:
  sudo ip link set can0 down
  sudo ip link set can0 type can bitrate 500000 listen-only on
  sudo ip link set can0 up
  candump -L can0,700:700 > cvtz50.log        (only 0x700-0x7FF: diagnostic ids)
  ... run CVTz50 through every module / screen, then Ctrl+C

Decode:
  python3 tools/isotp_decode.py cvtz50.log            full trace + summary
  python3 tools/isotp_decode.py cvtz50.log --summary  summary only
Self-check: python3 tools/isotp_decode.py --test
"""
import re
import sys
from collections import defaultdict

# Known Rogue ids (request -> name). Replies are request + 8 (7E0 -> 7E8) or + 0x20 (745 -> 765).
KNOWN = {0x7DF: "OBD broadcast", 0x7E0: "ECM", 0x7E1: "TCM", 0x740: "ABS", 0x745: "BCM"}
SERVICES = {0x01: "OBD current data", 0x03: "OBD stored DTCs", 0x07: "OBD pending DTCs", 0x09: "OBD vehicle info",
            0x10: "session", 0x14: "CLEAR DTCs", 0x18: "KWP read DTCs", 0x19: "UDS read DTCs",
            0x1A: "KWP read ECU id", 0x21: "read local id", 0x22: "read data id", 0x2E: "WRITE data id",
            0x30: "IO control (KWP)", 0x2F: "IO control", 0x31: "routine", 0x3B: "WRITE local id",
            0x3E: "tester present"}
LINE = re.compile(r"\(([\d.]+)\)\s+\S+\s+([0-9A-Fa-f]{3,8})#([0-9A-Fa-f]*)")


def dtc_code(hi, lo):
    return f"{'PCBU'[hi >> 6]}{(hi >> 4) & 3}{hi & 0xF:X}{lo:02X}"


def dtc_text(pl):
    """trouble codes in a positive reply, with the Nissan failure-type byte for 59 02 (B171A-1C); '' if none."""
    if pl[:1] == b"\x59" and len(pl) > 2:
        recs = pl[3:]
        return " ".join(f"{dtc_code(recs[i], recs[i+1])}-{recs[i+2]:02X} st{recs[i+3]:02X}"
                        for i in range(0, len(recs) - 3, 4))
    if pl[:1] in (b"\x43", b"\x47") and len(pl) > 1:
        recs = pl[2:]
        return " ".join(dtc_code(recs[i], recs[i+1]) for i in range(0, len(recs) - 1, 2) if recs[i] or recs[i+1])
    if pl[:1] == b"\x58" and len(pl) > 1:
        recs = pl[2:]
        return " ".join(f"{dtc_code(recs[i], recs[i+1])} st{recs[i+2]:02X}" for i in range(0, len(recs) - 2, 3))
    return ""


def module_of(can_id):
    """(request id, name) for a request or reply id."""
    for req in (can_id, can_id - 8, can_id - 0x20):
        if req in KNOWN:
            return req, KNOWN[req]
    return can_id, f"{can_id:03X}"


def frames(lines):
    for line in lines:
        m = LINE.search(line)
        if m:
            yield float(m.group(1)), int(m.group(2), 16), bytes.fromhex(m.group(3))


def messages(lines):
    """(time, can id, ISO-TP payload). Flow-control frames (3x) are dropped."""
    partial = {}                                          # id -> [time, expected length, bytes]
    for t, cid, d in frames(lines):
        if not d:
            continue
        kind = d[0] >> 4
        if kind == 0 and 0 < (d[0] & 0xF) <= 7:
            yield t, cid, d[1:1 + (d[0] & 0xF)]
        elif kind == 1 and len(d) >= 2:
            partial[cid] = [t, ((d[0] & 0xF) << 8) | d[1], bytearray(d[2:])]
        elif kind == 2 and cid in partial:
            p = partial[cid]
            p[2] += d[1:]
            if len(p[2]) >= p[1]:
                yield p[0], cid, bytes(p[2][:p[1]])
                del partial[cid]


def decode(lines, trace=True):
    """prints the trace; returns {module: {request hex: answered positively?}}."""
    asked = defaultdict(dict)
    last = {}                                             # module -> last request hex
    for t, cid, pl in messages(lines):
        req_id, name = module_of(cid)
        is_reply = cid != req_id
        hexs = pl.hex(" ").upper()
        if not is_reply:
            key = pl[:3].hex().upper() if pl[0] in (0x19, 0x21, 0x22, 0x31, 0x2F, 0x30, 0x10) else pl.hex().upper()
            last[name] = (t, key)
            asked[name].setdefault(key, False)
            note = SERVICES.get(pl[0], "")
        else:
            ok = pl[0] != 0x7F
            src = max((last[n] for n in (name, "OBD broadcast") if n in last), default=None)   # newest request
            if src and ok:
                asked[name if last.get(name) == src else "OBD broadcast"][src[1]] = True
            note = f"REFUSED {SERVICES.get(pl[1], f'{pl[1]:02X}')} NRC {pl[2]:02X}" if not ok and len(pl) > 2 \
                else dtc_text(pl)
        if trace:
            print(f"{t:.3f} {cid:03X} {name:<13} {'<-' if is_reply else '->'} {hexs}  {note}")
    return asked


def summary(asked):
    for name, reqs in sorted(asked.items()):
        print(f"\n{name}: {len(reqs)} requests")
        for key, ok in sorted(reqs.items()):
            print(f"  {key:<14} {'ok' if ok else 'no answer / refused'}  {SERVICES.get(int(key[:2], 16), '')}")


def _test():
    log = ["(1.000) can0 7E1#0319020D00000000",          # TCM 19 02 0D, single frame
           "(1.010) can0 7E9#100B59020D971A1C",          # first frame, 11 bytes
           "(1.011) can0 7E1#3000000000000000",          # flow control (dropped)
           "(1.020) can0 7E9#2109D0000008AAAA",          # consecutive
           "(1.100) can0 7E0#0219020000000000",          # ECM refuses 19
           "(1.110) can0 7E8#037F191100000000",
           "(1.200) can0 7DF#0103000000000000",          # OBD mode 03
           "(1.210) can0 7E8#0443010102000000"]
    msgs = list(messages(log))
    assert len(msgs) == 6, msgs
    assert msgs[1][2] == bytes.fromhex("59020D971A1C09D0000008"), msgs[1]
    assert dtc_text(msgs[1][2]) == "B171A-1C st09 U1000-00 st08", dtc_text(msgs[1][2])
    assert dtc_text(msgs[5][2]) == "P0102", dtc_text(msgs[5][2])
    a = decode(log, trace=False)
    assert a["TCM"] == {"19020D": True} and a["ECM"] == {"1902": False} and a["OBD broadcast"] == {"03": True}, a
    assert module_of(0x765) == (0x745, "BCM")
    print("ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
        sys.exit()
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with open(sys.argv[1]) as f:
        result = decode(f, trace="--summary" not in sys.argv)
    summary(result)
