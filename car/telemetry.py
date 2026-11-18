#!/usr/bin/env python3
"""telemetry.py v1.2 (2026-11-18): v1.2: also uploads the drives (hud_service trips.csv -> "Drives") and the drive
samples every 5 s (drive_samples.csv -> "Drive Samples"); a CSV header line is skipped.
v1.1: one upload at a time (file lock): a manual "send" while the service
uploads sent the same lines twice.
v1.0 (2026-11-18): Pi temperature log -> Google Sheet ("Pi Temps" tab of the Setup Log).

Every SAMPLE_S it appends one line to CSV (local, on the SD card, so nothing is lost without signal):
    time (Pi local clock), SoC temp C, 5 V rail V (PMIC EXT5V_V), vcgencmd get_throttled, RP2040 car state
Every UPLOAD_S, while there is internet (home Wi-Fi or the phone hotspot), it POSTs the lines not sent yet
to the Apps Script web app in tools/telemetry_apps_script.gs, which appends them to the sheet. A line counts
as sent only when the script answers "ok"; until then it is retried (backing off to 5 min without internet).
Config (NOT in the repo, it holds the secret): ~/.config/telemetry.json {"url": "<web app /exec URL>", "secret": "..."}
Without it the log still runs; nothing is uploaded.

Usage:
    telemetry.py           run (the service does this)
    telemetry.py send      upload what is pending now, once, and print the result
    telemetry.py --test    self-check
"""
import fcntl, json, os, subprocess, sys, time, urllib.request

SAMPLE_S = 30
UPLOAD_S = 60
MAX_BACKOFF_S = 300
BATCH = 500
DIR = os.path.expanduser("~/.local/share/telemetry")
CSV = os.path.join(DIR, "temps.csv")
SENT = os.path.join(DIR, "temps.sent")       # number of CSV lines already in the sheet
CFG = os.path.expanduser("~/.config/telemetry.json")
CAR_FILE = "/dev/shm/caguas-car"             # rp2040-link: {"t", "state"}
SHEET = "Pi Temps"
HUD = os.path.expanduser("~/.local/share/hud")
SOURCES = [(CSV, SENT, SHEET),                                                   # (CSV, sent counter, tab)
           (os.path.join(HUD, "trips.csv"), os.path.join(DIR, "drives.sent"), "Drives"),
           (os.path.join(HUD, "drive_samples.csv"), os.path.join(DIR, "samples.sent"), "Drive Samples")]


def vc(*args):
    try:
        return subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def sample():
    try:
        temp = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except (OSError, ValueError):
        temp = ""
    volts = vc("pmic_read_adc", "EXT5V_V").rpartition("=")[2].rstrip("V")
    try:
        volts = round(float(volts), 3)
    except ValueError:
        volts = ""
    try:
        d = json.load(open(CAR_FILE))
        car = d["state"] if time.monotonic() - float(d.get("t", 0)) <= 5 else ""
    except (OSError, ValueError, KeyError, TypeError):
        car = ""
    thr = vc("get_throttled").rpartition("=")[2]
    return [time.strftime("%Y-%m-%d %H:%M:%S"), temp, volts, thr, car]


def append(row, path=CSV):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(",".join(str(v) for v in row) + "\n")


def pending(path=CSV, sent_path=SENT):
    """(lines not uploaded yet, as rows, at most BATCH; how many were already sent)"""
    try:
        sent = int(open(sent_path).read().strip() or 0)
    except (OSError, ValueError):
        sent = 0
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return [], sent
    if sent == 0 and lines and not lines[0][:1].isdigit():
        sent = 1                                     # header line ("start,end,..."): not data
    return [l.split(",") for l in lines[sent:sent + BATCH]], sent


def upload(post, path=CSV, sent_path=SENT, sheet=SHEET):
    """post(payload) -> response text. Returns rows sent (0 = nothing pending), or raises on failure."""
    os.makedirs(os.path.dirname(sent_path), exist_ok=True)
    with open(sent_path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)               # the service and a manual 'send' never upload the same lines
        rows, sent = pending(path, sent_path)
        if not rows:
            return 0
        if post({"sheet": sheet, "rows": rows}).strip() != "ok":
            raise RuntimeError("the sheet did not answer ok")
        with open(sent_path + ".tmp", "w") as f:
            f.write(str(sent + len(rows)))
        os.replace(sent_path + ".tmp", sent_path)
        return len(rows)


def upload_all(post, sources=SOURCES):
    """every source until nothing is pending -> {tab: lines sent}"""
    out = {}
    for path, sent_path, sheet in sources:
        n = total = upload(post, path, sent_path, sheet)
        while n == BATCH:
            n = upload(post, path, sent_path, sheet); total += n
        out[sheet] = total
    return out


def http_post(cfg):
    def post(payload):
        body = json.dumps(dict(payload, secret=cfg["secret"])).encode()
        req = urllib.request.Request(cfg["url"], body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:   # Apps Script answers through a redirect: urllib follows it
            return r.read().decode(errors="replace")
    return post


def load_cfg():
    try:
        cfg = json.load(open(CFG))
        return cfg if cfg.get("url") and cfg.get("secret") else None
    except (OSError, ValueError):
        return None


def main():
    if "--test" in sys.argv:
        return self_test()
    cfg = load_cfg()
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        if not cfg:
            sys.exit(f"no url / secret in {CFG}")
        print(upload_all(http_post(cfg)))
        return
    print(f"telemetry v1.2: {CSV} every {SAMPLE_S} s, upload {'on' if cfg else f'off (no {CFG})'}", flush=True)
    next_up, backoff, failing = 0.0, UPLOAD_S, False
    while True:
        append(sample())
        now = time.monotonic()
        if cfg and now >= next_up:
            try:
                n = upload_all(http_post(cfg))
                if failing or any(n.values()):
                    print(f"uploaded {n}", flush=True)
                backoff, failing = UPLOAD_S, False
            except Exception as e:                   # no internet, DNS, timeout, script error: keep the lines, retry later
                if not failing:
                    print(f"upload failed ({type(e).__name__}: {e}), retrying", flush=True)
                failing, backoff = True, min(MAX_BACKOFF_S, backoff * 2)
            next_up = now + backoff
        time.sleep(SAMPLE_S)


def self_test():
    import tempfile
    d = tempfile.mkdtemp()
    csv, sent = os.path.join(d, "t.csv"), os.path.join(d, "t.sent")
    got = []
    ok = lambda p: (got.append(p), "ok")[1]
    assert upload(ok, csv, sent) == 0 and not got                        # nothing logged yet
    for i in range(3):
        append([f"2026-11-18 10:00:0{i}", 50 + i, 4.85, "0x0", "RUN"], csv)
    assert upload(ok, csv, sent) == 3 and got[0]["sheet"] == SHEET and got[0]["rows"][2][1] == "52"
    assert upload(ok, csv, sent) == 0                                    # already sent
    append(["2026-11-18 10:00:03", 53, "", "0x0", ""], csv)
    try:
        upload(lambda p: "Error: bad secret", csv, sent)                 # script refused: line stays pending
        raise AssertionError("should have failed")
    except RuntimeError:
        pass
    assert pending(csv, sent)[0] == [["2026-11-18 10:00:03", "53", "", "0x0", ""]]
    assert upload(ok, csv, sent) == 1 and pending(csv, sent)[0] == []
    import threading                                                     # two uploads at once: each line goes once
    for i in range(4):
        append([f"2026-11-18 10:01:0{i}", 50, "", "0x0", ""], csv)
    slow = lambda p: (time.sleep(0.3), got.append(p), "ok")[2]
    ts = [threading.Thread(target=upload, args=(slow, csv, sent)) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(len(p["rows"]) for p in got[-2:] if p["rows"][0][0].startswith("2026-11-18 10:01")) == 4, got[-2:]
    trips, tsent = os.path.join(d, "trips.csv"), os.path.join(d, "trips.sent")    # header skipped, tab passed on
    open(trips, "w").write("start,end,duration_min\n2026-11-18 10:06,2026-11-18 10:52,46.0\n")
    r = upload_all(ok, [(csv, sent, SHEET), (trips, tsent, "Drives")])
    assert r == {SHEET: 0, "Drives": 1} and got[-1] == {"sheet": "Drives", "rows": [["2026-11-18 10:06", "2026-11-18 10:52", "46.0"]]}, (r, got[-1])
    assert upload_all(ok, [(trips, tsent, "Drives")]) == {"Drives": 0}
    row = sample()
    assert len(row) == 5 and len(row[0]) == 19
    print("telemetry: PASS")


if __name__ == "__main__":
    main()
