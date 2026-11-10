"""Live CVT data from the Nissan Rogue TCM through the vLinker FS (USB, ELM327 commands).

READ-ONLY: only mode 22 (read data by identifier), mode 01 (standard PIDs) and reading trouble codes
(19 02 / mode 03 / mode 07). Never a clear (14 / 04).
PIDs checked against CVTz50 and a 52 min drive log (Setup Log sections 25-26).
TCM PIDs are asked in ONE multi-DID request (header 7E1, replies 7E9): 1-byte PIDs + the 2-byte
TCC solenoid current 1219 (ISOLT1, mA; found 2026-10-27 with cvt_probe.py watch 12).
Engine RPM comes from the ECM's own rpm DID (1201): on the main screen from the 1 s background alert read
(0.5 s on the CVT tab), on the engine/all tabs from every round. The old standard 010C every 2nd loop is gone:
laptop test 2026-10-28 (rogue_main_test.py) gave 11.0 CVT readings/s with it, 15.4 without (16.7 = CVT only),
and the 010C never answered while the ECM was in the 10C0 session anyway.

Telemetry window (2026-10-27): what is polled depends on the screen the page is showing
(set_view, sent by the page):
  main    CVT at full speed + in the background oil pressure (EOP) + RPM every 1 s and
          coolant + oil temp + control module voltage every 7 s, only for the alerts
  dyno    same as main (rpm every 0.5 s) + torque / power every 2nd loop + CVT fluid wear (2103) once a minute
  all     engine DIDs in rounds (TIER_1 every round, TIER_4 / TIER_8 every 4th / 8th) + the CVT request
          every 2nd round + fluid wear
(the ENGINE and CVT tabs are gone since 2026-11-10; the codes tab and the DSP page poll like main)
The ECM only answers mode 22 inside the Nissan diagnostic session (10C0, otherwise 7F 22 80),
so 10C0 is re-sent every SESSION_EVERY s while ECM data is read, and again on 7F replies.
ECM DIDs found 2026-10-27 from a Bluetooth snoop of Car Scanner, checked against its values.

Runs in its own thread; the HUD service reads .latest() / .latest_eng() / .alerts()
and pushes them to the page.

Engine status for the RP2040 power controller (2026-10-31): every loop (at most 4 times a second) it writes
/dev/shm/caguas-engine = {"t": monotonic, "ok": the car answered in the last 2 s, "rpm": ECM rpm < 2 s old or null}.
rp2040-link reads it and sends ENG 0/1/2. If this reader stops (adapter unplugged), the file goes stale and the
RP2040 falls back to the BLOWER signal alone.
In P / N on the main and CVT screens: ECM rpm every loop and the TCM (CVT) request every PN_CVT_EVERY-th loop,
so engine start / stop is seen fast. D / R (and an unknown gear) stay exactly as before.
Trouble codes (2026-11-10): read once per engine start, DTC_SETTLE_S after the engine is running (steady
voltage, every module awake), from the ECM, TCM and BCM (_read_dtcs). .dtcs() -> [{"mod", "code"}] for the page (yellow badge).
Env: HUD_OBD (1/0, default 1), HUD_OBD_PORT (default: auto, first /dev/serial/by-id
     entry that is not the RP2040).
"""
import collections
import glob
import json
import os
import threading
import time

try:
    import serial
except ImportError:   # pyserial missing: the reader stays off, the rest of the HUD works
    serial = None

TCM_HEADER, TCM_REPLY, ECM_HEADER = "7E1", "7E9", "7E0"
STALE_S = 2.0                     # no TCM answer for this long -> no data
GEARS = {1: "P", 2: "R", 3: "N", 4: "D"}


def s8(a):
    return a - 256 if a > 127 else a


# DID -> (json key, formula, bytes). Order = order in the request.
PIDS = {
    "110F": ("p_primary_target_mpa",   lambda a: round(a * 0.025, 3), 1),
    "110B": ("p_primary_actual_mpa",   lambda a: round(a * 0.025, 3), 1),
    "1111": ("p_secondary_target_mpa", lambda a: round(a * 0.025, 3), 1),
    "110A": ("p_secondary_actual_mpa", lambda a: round(a * 0.025, 3), 1),
    "110E": ("p_line_target_mpa",      lambda a: round(a * 0.025, 3), 1),
    "1105": ("slip_rpm",               s8, 1),
    "110C": ("temp_c",                 lambda a: a - 40, 1),
    "1119": ("gear",                   lambda a: GEARS.get(a), 1),
    "1101": ("speed_kmh",              lambda a: a, 1),
    "112A": ("lock_status",            lambda a: a, 1),        # TCM status byte, 64 and up = coasting
    "1219": ("tcc_a",                  lambda a: round(a * 0.001, 3), 2),   # TCC solenoid current, A
}
SIZES = {did: v[2] for did, v in PIDS.items()}
REQUEST = "22" + "".join(PIDS)

# Telemetry pages only (not the main screen): torque, gear ratio, pulley speeds. Found 2026-10-28 from a
# 24 min drive (torque_log) and the watch-12 drive log: 1213 / 10 = engine torque in Nm (follows air per rev
# r = 0.95, curve peaks 226 Nm at 4000-4500 rpm vs 237 Nm @ 4400 on the QR25DE spec sheet), 1210 / 10000 =
# actual ratio (= primary / secondary speed, median error 0.002), 1218 / 10000 = target ratio (2.631 = lowest),
# 1201 primary pulley rpm, 1203 secondary pulley rpm (41.8 rpm per km/h), 1204 engine rpm as the TCM sees it.
# At a standstill 1210 holds 27270 (no secondary speed to measure), so the page shows the target there.
POWER_PIDS = {"1213": 2, "1210": 2, "1218": 2, "1201": 2, "1203": 2, "1204": 2}
REQUEST_POWER = "22" + "".join(POWER_PIDS)
FINAL_DRIVE = 5.65        # secondary pulley -> wheel, measured: 41.8 rpm/km/h with 225/65R17 (5.60-5.71 by speed source)
DRIVELINE_EFF = 0.90      # belt + gears + diff, estimate
TC_STALL_TR = 2.0         # torque converter multiplication at stall, estimate (1.0 once the speed ratio passes 0.85)


def s16(v):
    return v - 65536 if v > 32767 else v


def power_calc(raw, lockup):
    """raw POWER_PIDS values -> dict for the page. Wheel numbers are estimates (efficiency, converter)."""
    if "1213" not in raw:
        return {}
    nm = s16(raw["1213"]) / 10
    eng, pri, sec = raw.get("1204"), raw.get("1201"), raw.get("1203")
    tgt = raw.get("1218", 0) / 10000 or None
    act = raw.get("1210", 0) / 10000 or None
    ratio = act if sec and sec >= 150 else tgt              # below ~3.5 km/h the TCM has no real ratio, show the target
    if lockup in ("locked", "coast"):
        tc = 1.0
    else:
        sr = pri / eng if eng and eng > 300 and pri is not None else 0.0
        tc = 1 + (TC_STALL_TR - 1) * max(0.0, 1 - sr / 0.85)
    out = {"eng_nm": round(nm, 1), "hp": round(max(0.0, nm * (eng or 0) / 7121), 1),
           "ratio": round(ratio, 3) if ratio else None, "ratio_target": round(tgt, 3) if tgt else None,
           "pri_rpm": pri, "sec_rpm": sec, "tc_factor": round(tc, 2)}
    if ratio:
        wnm = nm * tc * ratio * FINAL_DRIVE * DRIVELINE_EFF
        out["wheel_nm"] = round(wnm)
        out["whp"] = round(max(0.0, wnm * (sec or 0) / FINAL_DRIVE / 7121), 1)
    return out
REQUEST_NO_TCC = "22" + "".join(d for d in PIDS if d != "1219")


class Lockup:
    """Torque converter state for the page:
         open      no current on the TCC solenoid (0 A): converter open, e.g. launching
         engaging  current applied and rising, slip still big
         releasing current applied and falling, slip big
         locked    current applied, slip ~0
         coast     locked with the pedal off (TCM status byte 112A >= 64)
    With the TCC current (1219) this is the TCM's own command, so it needs very little filtering.
    Without it (TCM not answering 1219) it falls back to the old slip estimate with hysteresis."""
    TCC_OFF_A = 0.05                       # below this the clutch is not applied
    LOCK_SLIP = 30                         # |rpm| above this with current applied = still engaging/releasing
    HOLD = 0.25                            # s a new state must last before it is shown (no flicker)
    # slip-only fallback (tuned on the 2026-10-27 52 min drive)
    FB_LOCK_SLIP, FB_OPEN_SLIP = 15, 40

    def __init__(self):
        self.state, self._cand, self._since, self._prev_a = "open", None, 0.0, None

    def _want(self, slip, amps, status):
        if amps is not None:
            prev, self._prev_a = self._prev_a, amps
            if amps < self.TCC_OFF_A:
                return "open"
            if slip is not None and abs(slip) > self.LOCK_SLIP:
                return "releasing" if prev is not None and amps < prev - 0.005 else "engaging"
            return "coast" if status is not None and status >= 64 else "locked"
        s = abs(slip)                      # fallback: slip only
        if s <= self.FB_LOCK_SLIP:
            return "locked"
        if s > self.FB_OPEN_SLIP:
            return "open"
        return "locked" if self.state in ("locked", "coast") else "engaging"

    def update(self, gear, slip, amps, status, now):
        if gear not in ("D", "R") or (slip is None and amps is None):
            self.state, self._cand, self._prev_a = "open", None, None
            return self.state
        want = self._want(slip, amps, status)
        if want == self.state:
            self._cand = None
        elif want != self._cand:
            self._cand, self._since = want, now
        elif now - self._since >= self.HOLD:
            self.state, self._cand = want, None
        return self.state


# ---------------- engine (ECM 7E0, inside the 10C0 session) ----------------
ECM_REPLY = "7E8"
SESSION_EVERY = 2.5               # s between 10C0 keep-alives while ECM data is read
ALERT_FAST_S = 1.0                # oil pressure + rpm on the main screen (alerts; also the CVT rpm)
ALERT_FAST_CVT_S = 0.5            # same on the Dyno tab, so its rpm moves a bit faster
RPM_FRESH_S = 3.0                 # older than this: no rpm in the CVT data
ALERT_TEMP_S = 7.0                # coolant + oil temp + voltage on the main screen (alerts)
WEAR_EVERY = 60.0                 # s between CVT fluid wear reads (it changes very slowly)
ENG_STALE_S = 10.0                # no ECM answer for this long -> engine data null, alerts cleared
ECM_CHUNK = 8                     # DIDs per multi-DID request
ENGINE_FILE = "/dev/shm/caguas-engine"   # engine status for rp2040-link
ENGINE_WRITE_S = 0.25
ANSWER_FRESH_S = 2.0              # "ok" = the car answered (TCM or ECM) within this
PN_CVT_EVERY = 8                  # P / N: TCM request every 8th loop, ECM rpm every loop

# DID -> (json key, formula, bytes). Order = order on the page / in the request.
ECM_PIDS = {
    "1201": ("rpm",        lambda v: round(v * 12.5), 2),
    "1101": ("coolant_c",  lambda v: v - 50, 1),
    "111F": ("oil_c",      lambda v: v - 50, 1),
    "1106": ("iat_c",      lambda v: v - 50, 1),
    "1111": ("fuel_c",     lambda v: v - 50, 1),
    "1278": ("eop_mv",     lambda v: v * 5, 2),                       # oil pressure sensor
    "1209": ("maf_gs",     lambda v: round(v / 100, 2), 2),
    "1117": ("load_pct",   lambda v: round(v * 100 / 256, 1), 1),
    "110A": ("timing_deg", lambda v: 110 - v, 1),
    "1123": ("stft_pct",   lambda v: v - 100, 1),
    "1125": ("ltft_pct",   lambda v: v - 100, 1),
    "1206": ("inj_ms",     lambda v: round(v / 100, 2), 2),
    "1103": ("ecm_v",      lambda v: round(v * 0.08, 2), 1),
    "1164": ("exhcam_deg", lambda v: v / 2 - 64, 1),
    "122F": ("vtc_pct",    lambda v: round(v * 100 / 1024, 1), 2),
    "120F": ("thr1_v",     lambda v: round(v * 0.005, 3), 2),
    "1210": ("thr2_v",     lambda v: round(v * 0.005, 3), 2),
    "120D": ("ped1_v",     lambda v: round(v * 0.005, 3), 2),
    "120E": ("ped2_v",     lambda v: round(v * 0.005, 3), 2),
    "1225": ("af_v",       lambda v: round(v * 0.005, 3), 2),
    "1223": ("ac_v",       lambda v: round(v * 0.005, 3), 2),
}
ECM_SIZES = {did: v[2] for did, v in ECM_PIDS.items()}
ECM_ALL = list(ECM_PIDS)
# Telemetry tabs: the Rogue's ECM answers ONE DID per request (~22 ms each with the "1" suffix), so slow
# PIDs are read less often. Tested on the laptop 2026-10-28 (rogue_tiers.py, 30 s each, CVT request included):
#   tiers + suffix 3.44 rounds/s | all every round + suffix 2.29 | tiers without suffix 1.80
TIER_4 = ["1125", "1206", "122F", "1225", "1106", "1164",   # LTFT, injector, exhaust VTC, A/F, intake air, exhaust cam,
          "1210", "120E"]                                   # throttle 2 + pedal 2 (backup sensors, track sensor 1)
TIER_8 = ["1101", "111F", "1111", "1103", "1223"]           # coolant, oil temp, fuel temp, control module V, A/C pressure
TIER_1 = [d for d in ECM_PIDS if d not in TIER_4 and d not in TIER_8]   # rpm, oil pressure, MAF, load, timing, STFT, throttle 1, pedal 1
# 2026-10-28 laptop test: sensor 2s in TIER_4 + CVT every 2nd round on ALL -> 4.54 rounds/s (was 3.44).
# No faster route: the ECM refuses even 2 DIDs per request (7F 22 12), and its service 21 blocks (01-05, A0, A1)
# never changed while revving (not live data).
DYNO_POWER_EVERY = 2             # Dyno tab: torque / power every 2nd CVT loop (hp / Nm move faster, speed still ~fast)
ALL_CVT_EVERY = 2                 # ALL tab: CVT request every 2nd round (it is an overview)


def ecm_round(r):
    """DIDs for telemetry round r. Tier 4 / 8 are spread over the rounds so every round takes about the same time."""
    return (TIER_1 + [d for i, d in enumerate(TIER_4) if i % 4 == r % 4]
                   + [d for i, d in enumerate(TIER_8) if i % 8 == r % 8])
FAST_DIDS = ["1278", "1201"]      # oil pressure + rpm
TEMP_DIDS = ["1101", "111F", "1103"]      # coolant + oil temp + control module voltage


# oil pressure: psi = (V - 0.77) * 20.7. Zero = 770 mV, read twice with ignition ON / engine OFF
# (warm, and again cold after the car sat, coolant 45 C, 2026-10-28). Slope 20.7 psi/V not yet checked with a gauge.
OIL_ZERO_V, OIL_PSI_PER_V = 0.77, 20.7
EOP_IDLE_WARN_MV, EOP_IDLE_CRIT_MV = 1200, 1000     # idle floors Zen chose (hot idle reads ~1480-1550 mV)


def psi_from_mv(mv):
    return max(0.0, (mv / 1000 - OIL_ZERO_V) * OIL_PSI_PER_V)


def mv_from_psi(psi):
    return (psi / OIL_PSI_PER_V + OIL_ZERO_V) * 1000


class Alerts:
    """Dangerous values -> alerts for the page (red badge on the main screen, banner on the telemetry one).
       coolant >= 110 C (clears below 108), oil temp >= 130 C (clears below 127)
       CVT fluid >= 100 C (clears below 97; the hottest seen so far is 93 C for 1-2 minutes)
       voltage (ECM) with the engine running 10 s: below 11.7 V twice in a row (smart charging sits at 12 V;
           clears at 11.8) or 15.0 V and up (clears below 14.8)
       oil pressure below a minimum that grows with rpm:
           warn (yellow tile only) = max(1200 mV, 9 psi per 1000 rpm), alert = max(1000 mV, 6 psi per 1000 rpm),
           both stop growing at 4000 rpm (alert 24 psi = 1930 mV, warn 36 psi = 2510 mV).
         - compared with the LOWER of the rpm now and ~2 s ago (pressure lags a quick blip),
         - only with the engine running (>= 400 rpm) for 10 s,
         - 2 low readings in a row to fire, 3 good ones to clear."""
    COOLANT_ON, COOLANT_OFF = 110, 108
    OILT_ON, OILT_OFF = 130, 127
    CVT_ON, CVT_OFF = 100, 97
    BATT_LOW, BATT_LOW_OFF, BATT_HIGH, BATT_HIGH_OFF = 11.7, 11.8, 15.0, 14.8
    RUN_MIN_RPM, SETTLE_S, LAG_S = 400, 10.0, 2.0

    def __init__(self):
        self.active = {}                       # id -> dict for the page
        self.version = 0
        self._rpm = collections.deque(maxlen=40)
        self._run_since = None
        self._low = self._ok = self._batt = 0

    @staticmethod
    def eop_limits(rpm):
        k = min(4.0, max(0.0, (rpm or 0) / 1000))
        return (round(max(EOP_IDLE_WARN_MV, mv_from_psi(9 * k))),
                round(max(EOP_IDLE_CRIT_MV, mv_from_psi(6 * k))))

    def _set(self, key, value):
        if value is None:
            if key in self.active:
                del self.active[key]
                self.version += 1
        elif self.active.get(key) != value:
            self.active[key] = value
            self.version += 1

    def clear(self):
        if self.active:
            self.active, self.version = {}, self.version + 1
        self._run_since, self._low, self._ok, self._batt = None, 0, 0, 0

    def rpm(self, rpm, now):
        self._rpm.append((now, rpm))
        if rpm >= self.RUN_MIN_RPM:
            self._run_since = self._run_since or now
        else:
            self._run_since, self._low, self._ok = None, 0, 0
            self._set("oil_pressure", None)    # engine off: no pressure expected
            self._set("battery", None)         # engine off: not charging is normal
            self._batt = 0

    def ref_rpm(self, now):
        """lower of the rpm now and the rpm ~LAG_S ago"""
        if not self._rpm:
            return None
        now_rpm = self._rpm[-1][1]
        old = [r for t, r in self._rpm if t <= now - self.LAG_S]
        return min(now_rpm, old[-1]) if old else now_rpm

    def temps(self, coolant, oil_c):
        if coolant is not None:
            on = "coolant" in self.active
            hot = coolant >= self.COOLANT_ON or (on and coolant >= self.COOLANT_OFF)
            self._set("coolant", {"id": "coolant", "value": coolant} if hot else None)
        if oil_c is not None:
            on = "oil_temp" in self.active
            hot = oil_c >= self.OILT_ON or (on and oil_c >= self.OILT_OFF)
            self._set("oil_temp", {"id": "oil_temp", "value": oil_c} if hot else None)

    def cvt_temp(self, c):
        hot = c >= self.CVT_ON or ("cvt_temp" in self.active and c >= self.CVT_OFF)
        self._set("cvt_temp", {"id": "cvt_temp", "value": c} if hot else None)

    def battery(self, v, now):
        if self._run_since is None or now - self._run_since < self.SETTLE_S:
            return
        on = "battery" in self.active
        low = v < self.BATT_LOW or (on and v < self.BATT_LOW_OFF)
        self._batt = self._batt + 1 if low else 0
        bad = self._batt >= 2 or v >= self.BATT_HIGH or (on and v >= self.BATT_HIGH_OFF)
        if bad or not low:                      # one low reading alone: wait for the next
            self._set("battery", {"id": "battery", "value": v} if bad else None)

    def oil_pressure(self, mv, now):
        rpm = self.ref_rpm(now)
        if rpm is None or self._run_since is None or now - self._run_since < self.SETTLE_S:
            return
        _, crit = self.eop_limits(rpm)
        if mv < crit:
            self._low, self._ok = self._low + 1, 0
        else:
            self._ok, self._low = self._ok + 1, 0
        if self._low >= 2:
            self._set("oil_pressure", {"id": "oil_pressure", "mv": mv, "psi": round(psi_from_mv(mv), 1),
                                       "rpm": rpm, "min_mv": crit})
        elif self._ok >= 3:
            self._set("oil_pressure", None)

    def list(self):
        order = ["oil_pressure", "coolant", "oil_temp", "cvt_temp", "battery"]          # most urgent first
        return [self.active[k] for k in order if k in self.active]


# ---------------- trouble codes (ECM / TCM / BCM), read once at startup ----------------
# Checked on the Rogue with tools/dtc_probe.py, 2026-11-10 (MAF unplugged -> P0102): nobody answers KWP 18,
# the ECM refuses 19 (7F 19 11) and only gives OBD2 mode 03 (stored) / 07 (pending); TCM and BCM answer 19 02.
# Mask 0D = failed now, pending or confirmed: with FF the TCM lists all 62 codes it knows (status 40 = test not run).
# (module, request header, reply id, requests: every one is asked, the codes are joined)
DTC_MODULES = [("ECM", "7DF", "7E8", ["03", "07"]),
               ("TCM", "7E1", "7E9", ["19020D"]),
               ("BCM", "745", "765", ["19020D"])]
DTC_TRIES = 3                                  # scans with no module answering before giving up (per engine start)
DTC_SETTLE_S = 10.0                            # engine running this long before the scan


def dtc_code(hi, lo):
    return f"{'PCBU'[hi >> 6]}{(hi >> 4) & 3}{hi & 0xF:X}{lo:02X}"


def dtc_decode(pl):
    """positive DTC reply -> ['P0420', ...] (may be empty); None if it is not one (no answer, 7F refusal)."""
    size = {0x59: (3, 4),      # 59 02 mask [hi lo failure-type status]...
            0x58: (2, 3),      # 58 count [hi lo status]...
            0x43: (2, 2), 0x47: (2, 2)}.get(pl[0] if pl else None)      # 43 / 47 count [hi lo]... (stored / pending)
    if not size:
        return None
    recs, n = pl[size[0]:], size[1]
    codes = [dtc_code(recs[i], recs[i + 1]) for i in range(0, len(recs) - n + 1, n) if recs[i] or recs[i + 1]]
    return list(dict.fromkeys(codes))


def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def find_port():
    forced = os.environ.get("HUD_OBD_PORT")
    if forced:
        return forced
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        name = os.path.basename(p).lower()
        if "micropython" in name or "rp2" in name or "raspberry" in name:
            continue                       # the RP2040 power controller
        return p
    return None


def isotp_payload(text, rx=TCM_REPLY):
    """Join ISO-TP frames (single 0x, first 1x, consecutive 2x) into one message."""
    payload, length = [], None
    for line in text.replace("\r", "\n").split("\n"):
        t = line.strip().upper().split()
        if len(t) < 2 or t[0] != rx:
            continue
        try:
            b = [int(x, 16) for x in t[1:]]
        except ValueError:
            continue
        kind = b[0] >> 4
        if kind == 0:
            frame = b[1:1 + (b[0] & 0x0F)]
            if len(frame) >= 3 and frame[0] == 0x7F and frame[2] == 0x78:
                continue                   # "response pending": the real answer follows
            return frame
        if kind == 1:
            length = ((b[0] & 0x0F) << 8) | b[1]
            payload = b[2:]
        elif kind == 2 and length is not None:
            payload += b[1:]
    return payload[:length] if length else None


def parse_multi(payload, sizes=SIZES):
    """62 [DIDhi DIDlo value(1 or 2 bytes, big-endian)]... -> {'110E': value, ...}"""
    if not payload or payload[0] != 0x62:
        return {}
    out, i = {}, 1
    while i + 2 <= len(payload):
        did = f"{payload[i]:02X}{payload[i + 1]:02X}"
        n = sizes.get(did)
        if n is None or i + 2 + n > len(payload):
            break                          # unknown DID or truncated reply: keep what was parsed
        v = 0
        for b in payload[i + 2:i + 2 + n]:
            v = (v << 8) | b
        out[did] = v
        i += 2 + n
    return out


def parse_rpm(text):
    t = text.upper().split()
    for i in range(len(t) - 3):
        if t[i] == "41" and t[i + 1] == "0C":
            try:
                return (int(t[i + 2], 16) * 256 + int(t[i + 3], 16)) // 4
            except ValueError:
                return None
    return None


class CvtReader:
    def __init__(self):
        self._lock = threading.Lock()
        self._data, self._ts = None, 0.0
        self.hz = 0.0
        self.enabled = serial is not None and os.environ.get("HUD_OBD", "1") != "0"
        self._warned = set()
        self._lockup = Lockup()
        self.view = "main"                 # main | all | engine | cvt (set by the page)
        self._view_changed = True
        self._eng, self._eng_ts, self.eng_version = {}, 0.0, 0
        self.eng_hz = 0.0
        self.alert = Alerts()
        self._hdr = None
        self._sess_t = -1e9
        self._ecm_combined, self._ecm_strikes = True, 0
        self._eng_round = 0
        self._power, self._power_ts, self._tcm_loops = {}, 0.0, 0
        self._ans_ts, self._rpm_ts, self._status_ts, self._pn_loop = -1e9, -1e9, -1e9, 0
        self._dtcs, self.dtc_version, self._dtc_left, self._dtc_run = [], 0, DTC_TRIES, None

    def _write_status(self, now):
        """engine status for rp2040-link (/dev/shm/caguas-engine), at most every ENGINE_WRITE_S"""
        if now - self._status_ts < ENGINE_WRITE_S:
            return
        self._status_ts = now
        with self._lock:
            rpm = self._eng.get("rpm") if now - self._rpm_ts < ANSWER_FRESH_S else None
        d = {"t": time.monotonic(), "ok": now - self._ans_ts < ANSWER_FRESH_S, "rpm": rpm}
        try:
            tmp = ENGINE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, ENGINE_FILE)
        except OSError as e:
            self._warn("status", f"CVT reader: can't write {ENGINE_FILE}: {e}")

    def set_view(self, view):
        if view in ("main", "all", "dyno") and view != self.view:
            self.view, self._view_changed = view, True

    def latest_eng(self):
        """engine dict for the telemetry page, or None (no ECM data for ENG_STALE_S)."""
        with self._lock:
            if not self._eng or time.monotonic() - self._eng_ts > ENG_STALE_S:
                return None
            return dict(self._eng)

    def alerts(self):
        with self._lock:
            return self.alert.list()

    def dtcs(self):
        """[{"mod": "ECM", "code": "P0420"}, ...] from the last scan ([] = none, or not scanned yet: dtc_version 0)."""
        with self._lock:
            return list(self._dtcs)

    def _warn(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            print(msg, flush=True)

    def latest(self):
        """dict for the page, or None (no adapter / car off / stale)."""
        with self._lock:
            if self._data is None or time.monotonic() - self._ts > STALE_S:
                return None
            return dict(self._data)

    def start(self):
        if not self.enabled:
            self._warn("off", "CVT reader off (pyserial missing or HUD_OBD=0)")
            return
        threading.Thread(target=self._run, daemon=True, name="cvt-reader").start()

    # ---------- ELM327 ----------
    @staticmethod
    def _send(ser, cmd, timeout=1.0):
        ser.reset_input_buffer()
        ser.write((cmd + "\r").encode("ascii"))
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        return buf.decode("ascii", errors="ignore").replace(">", "").strip()

    def _init(self, ser):
        self._send(ser, "ATZ", 5)
        for c in ("ATE0", "ATL0", "ATS1", "ATH1", "ATSP6", "ATAR", "ATAT2"):
            self._send(ser, c)
        self._hdr = None
        self._sess_t = -1e9
        self._header(ser, TCM_HEADER)

    def _header(self, ser, h):
        if self._hdr != h:
            self._send(ser, "ATSH" + h)
            self._hdr = h

    # ---------- ECM (engine) ----------
    def _session(self, ser, force=False):
        now = time.monotonic()
        if force or now - self._sess_t > SESSION_EVERY:
            self._send(ser, "10C01", 0.5)        # trailing "1": return after the first reply
            self._sess_t = now

    def _ecm_request(self, ser, dids):
        """one mode 22 request (1 or more DIDs) -> {did: raw}. Re-opens the session once on a 7F reply."""
        req = "22" + "".join(dids) + ("1" if len(dids) == 1 else "")   # "1" only on single-frame requests
        for attempt in (1, 2):
            pl = isotp_payload(self._send(ser, req, 1.0), ECM_REPLY)
            if pl and pl[0] == 0x7F and attempt == 1:
                self._session(ser, force=True)
                continue
            return parse_multi(pl, ECM_SIZES), pl
        return {}, None

    def _ecm(self, ser, dids):
        """read ECM DIDs -> {did: raw}. Multi-DID requests; if the ECM refuses them, one DID at a time."""
        self._header(ser, ECM_HEADER)
        self._session(ser)
        out = {}
        if self._ecm_combined:
            for part in chunks(dids, ECM_CHUNK):
                got, pl = self._ecm_request(ser, part)
                out.update(got)
                if len(part) > 1 and pl and (pl[0] == 0x7F or len(got) < len(part)):
                    self._ecm_strikes += 1
                    if self._ecm_strikes >= 3:
                        self._ecm_combined = False
                        print("CVT reader: ECM refuses multi-DID requests, reading one DID at a time", flush=True)
                        break
                elif got:
                    self._ecm_strikes = 0
        if not self._ecm_combined:
            for d in dids:
                if d not in out:
                    out.update(self._ecm_request(ser, [d])[0])
        return out

    def _merge_eng(self, raw, now):
        d = {ECM_PIDS[did][0]: ECM_PIDS[did][1](v) for did, v in raw.items() if did in ECM_PIDS}
        with self._lock:
            self._eng.update(d)
            if "rpm" in d:
                self.alert.rpm(d["rpm"], now)
                self._rpm_ts = now
            if "coolant_c" in d or "oil_c" in d:
                self.alert.temps(d.get("coolant_c"), d.get("oil_c"))
            if "eop_mv" in d:
                self.alert.oil_pressure(d["eop_mv"], now)
            if "ecm_v" in d:
                self.alert.battery(d["ecm_v"], now)
            rpm = self.alert.ref_rpm(now)
            if rpm is not None:
                w, c = Alerts.eop_limits(rpm)
                self._eng.update(eop_warn_mv=w, eop_crit_mv=c)
            if "eop_mv" in self._eng:
                self._eng["eop_psi"] = round(psi_from_mv(self._eng["eop_mv"]), 1)
            self._eng_ts = now
            self.eng_version += 1

    def _fluid_wear(self, ser):
        """CVT fluid wear counter (TCM service 21, LID 03): 61 03 + 4 bytes. Same number as CVTz50."""
        self._header(ser, TCM_HEADER)
        pl = isotp_payload(self._send(ser, "21031", 1.0))
        if pl and len(pl) >= 6 and pl[0] == 0x61 and pl[1] == 0x03:
            v = int.from_bytes(bytes(pl[2:6]), "big")
            with self._lock:
                self._eng["fluid_wear"] = v
                self.eng_version += 1

    def _read_dtcs(self, ser):
        """Ask each module for its stored codes (read-only). True when at least one module answered."""
        found, answered = [], False
        for name, tx, rx, reqs in DTC_MODULES:
            self._send(ser, "ATCRA" + rx)              # only this module's replies (the BCM is outside 7E8-7EF)
            self._header(ser, tx)
            for req in reqs:
                pl = isotp_payload(self._send(ser, req, 2.0), rx)
                codes = dtc_decode(pl)
                print(f"DTC {name} {req}: " + ("no answer" if not pl else
                      " ".join(f"{b:02X}" for b in pl[:12]) if codes is None else
                      ", ".join(codes) or "no codes"), flush=True)
                if codes is not None:
                    found += [{"mod": name, "code": c} for c in codes if {"mod": name, "code": c} not in found]
                    answered = True
        self._send(ser, "ATAR")                        # back to automatic receive
        if answered:
            with self._lock:
                self._dtcs, self.dtc_version = found, self.dtc_version + 1
        return answered

    def _run(self):
        while True:
            port = find_port()
            if not port:
                self._warn("noport", "CVT reader: no OBD adapter found in /dev/serial/by-id (waiting)")
                time.sleep(5)
                continue
            try:
                with serial.Serial(port, 115200, timeout=0.05) as ser:
                    print(f"CVT reader: adapter on {port}", flush=True)
                    self._warned.discard("noport")
                    self._init(ser)
                    self._loop(ser)
            except Exception as e:     # unplugged, permission, adapter reset...
                self._warn(f"err:{type(e).__name__}", f"CVT reader: {type(e).__name__}: {e} (retrying)")
                with self._lock:
                    self._data = None
                time.sleep(3)

    def _loop(self, ser):
        frame, rpm, misses = 0, None, 0
        n, t_hz = 0, time.monotonic()
        combined, tcc_missing = True, 0    # TCC current in the same request; own request if the TCM refuses
        last_fast = last_temp = last_wear = -1e9
        rounds, t_eng = 0, time.monotonic()
        dtc_next = -1e9
        while True:
            now = time.monotonic()
            view = self.view
            if self._view_changed:
                self._view_changed = False
                last_wear = -1e9 if view != "main" else last_wear
            engine_tab = view == "all"
            got_any = False
            with self._lock:
                gear = self._data.get("gear") if self._data and now - self._ts < STALE_S else None
            pn = gear in ("P", "N") and not engine_tab
            self._pn_loop = self._pn_loop + 1 if pn else 0

            # ---- ECM: all engine DIDs on the engine/all tabs, otherwise only what the alerts need ----
            if engine_tab:
                raw = self._ecm(ser, ecm_round(self._eng_round))
                self._eng_round += 1
                if raw:
                    self._merge_eng(raw, time.monotonic())
                    rounds, got_any = rounds + 1, True
                    last_fast = last_temp = now
            else:
                want = []
                if pn:
                    want.append("1201")            # P / N: rpm every loop (engine start / stop)
                if now - last_fast >= (ALERT_FAST_CVT_S if view == "dyno" else ALERT_FAST_S):
                    want += [d for d in FAST_DIDS if d not in want]
                    last_fast = now
                if now - last_temp >= ALERT_TEMP_S:
                    want += TEMP_DIDS
                    last_temp = now
                if want:
                    raw = self._ecm(ser, want)
                    if raw:
                        self._merge_eng(raw, time.monotonic())
                        got_any = True

            # ---- CVT fluid wear, once a minute while the telemetry page is open ----
            if view != "main" and now - last_wear >= WEAR_EVERY:
                last_wear = now
                self._fluid_wear(ser)

            # ---- TCM: every loop on main / Dyno tab, every 2nd round on ALL ----
            if (view == "all" and self._eng_round % ALL_CVT_EVERY == 0) or \
               (view in ("main", "dyno") and (not pn or self._pn_loop % PN_CVT_EVERY == 1)):
                rpm = self._eng.get("rpm") if now - self._eng_ts < RPM_FRESH_S else None   # from the ECM reads
                self._header(ser, TCM_HEADER)
                raw = parse_multi(isotp_payload(self._send(ser, REQUEST if combined else REQUEST_NO_TCC, 1.0)))
                if raw and combined and "1219" not in raw:
                    tcc_missing += 1
                    if tcc_missing >= 5:
                        combined = False
                        print("CVT reader: TCC current not in the combined reply, reading 221219 on its own", flush=True)
                if raw and not combined:
                    raw.update(parse_multi(isotp_payload(self._send(ser, "221219", 1.0))))
                if raw:
                    got_any = True
                    now = time.monotonic()
                    d = {key: f(raw[did]) for did, (key, f, _) in PIDS.items() if did in raw}
                    d["rpm"] = rpm
                    d["hz"] = round(self.hz, 1)                # CVT readings/s, shown next to "CVT" on the main
                    if view != "main" and now - self._power_ts < 3.0:
                        d.update(self._power)
                    d["lockup"] = self._lockup.update(d.get("gear"), d.get("slip_rpm"), d.get("tcc_a"),
                                                      d.get("lock_status"), now)
                    with self._lock:
                        self._data, self._ts = d, now
                        if d.get("temp_c") is not None:
                            self.alert.cvt_temp(d["temp_c"])
                    n += 1

            # ---- power (torque / ratio / pulleys): telemetry pages only; ALL every 2nd round, Dyno every 2nd loop ----
            if view == "dyno":
                self._tcm_loops += 1
            power_due = (view == "all" and self._eng_round % 2 == 0) or \
                        (view == "dyno" and self._tcm_loops % DYNO_POWER_EVERY == 0)
            if power_due:
                self._header(ser, TCM_HEADER)
                praw = parse_multi(isotp_payload(self._send(ser, REQUEST_POWER, 1.0)), POWER_PIDS)
                if praw:
                    got_any = True
                    pw = power_calc(praw, self._lockup.state)
                    with self._lock:
                        self._power, self._power_ts = pw, time.monotonic()
                        self._eng.update(pw)
                        self.eng_version += 1
                        if self._data is not None:
                            self._data.update(pw)

            now = time.monotonic()
            if now - t_hz >= 1.0:
                self.hz, n, t_hz = n / (now - t_hz), 0, now
            if now - t_eng >= 1.0:
                self.eng_hz, rounds, t_eng = rounds / (now - t_eng), 0, now
            with self._lock:
                if self._eng and now - self._eng_ts > ENG_STALE_S:
                    self._eng, self.eng_version = {}, self.eng_version + 1
                    self.alert.clear()
            if got_any:
                self._ans_ts = now
                run = self.alert._run_since            # trouble codes: once per engine start, after it settles
                if run is not None and run != self._dtc_run and now - run >= DTC_SETTLE_S and now >= dtc_next:
                    if self._read_dtcs(ser) or self._dtc_left <= 1:
                        self._dtc_run, self._dtc_left = run, DTC_TRIES
                    else:
                        self._dtc_left -= 1
                    now = time.monotonic()
                    dtc_next = now + 10
            self._write_status(now)
            if not got_any:
                misses += 1
                if misses >= 20:       # car off or bus asleep: slow down, keep trying
                    time.sleep(1.0)
            else:
                misses = 0
