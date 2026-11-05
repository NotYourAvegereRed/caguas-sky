#!/usr/bin/env python3
"""light_reader.py v1.0 (2026-11-03): VEML7700 light sensor on the Pi's I2C1 (pin 3 SDA, pin 5 SCL, pin 1 3V3).

Reads lux with auto-range, turns it into a screen level and writes /dev/shm/caguas-lux:
    {"t": monotonic, "lux": 12.3, "raw": 1464, "range": "dark", "target": 38, "pct": 37}
hud_service uses "pct" as the screen level when it is fresh; when it is stale (sensor unplugged, this service
stopped) hud_service goes back to ILL + night_pct. The RP2040 firmware does not change.

Curve (log scale, like the eye): lux <= lux_dark -> min_pct, lux >= lux_bright -> 100, in between on a log line.
Smoothing: brighter reaches the target in ~up_s, darker in ~down_s (an overpass does not blink the screen).
It starts moving at once: there is no wait before it reacts. Settings in ~/.config/hud/display.json
(lux_dark, lux_bright, min_pct, up_s, down_s), re-read when the file changes.

Usage:
    light_reader.py              run (the service does this)
    light_reader.py watch        show the live values from /dev/shm/caguas-lux (Ctrl+C to stop)
    light_reader.py test 5       fake 5 lux into /dev/shm/caguas-lux-test (wins over the sensor) until Ctrl+C;
                                 'test 5 500' alternates 5 and 500 lux every 8 s to see the smoothing
"""
import json, math, os, sys, time

ADDR = 0x10
BUS_N = 1
OUT = "/dev/shm/caguas-lux"
CFG = os.path.expanduser("~/.config/hud/display.json")
DEFAULTS = {"lux_dark": 2.0, "lux_bright": 800.0, "min_pct": 20, "up_s": 1.5, "down_s": 3.0}
MIN_SPEED = 10.0  # %/s, so the end of a fade does not crawl
STEP = 2          # publish a new pct only when it moved >= 2 % (or reached the target)

# auto-range: (name, gain bits, IT bits, lux per count, integration s). Resolution from the Vishay datasheet
# (0.0042 lux/count at gain x2 / 800 ms).
RANGES = [("dark",   0b01, 0b0010, 0.0084, 0.400),   # gain x2,   400 ms: up to ~550 lux
          ("mid",    0b00, 0b0000, 0.0672, 0.100),   # gain x1,   100 ms: up to ~4,400 lux
          ("bright", 0b10, 0b1100, 2.1504, 0.025)]   # gain x1/8,  25 ms: up to ~140,000 lux
UP_RAW, DOWN_RAW = 50000, 500   # raw above -> less sensitive range; below -> more sensitive


def load_cfg(cache={"m": None, "c": dict(DEFAULTS)}):
    try:
        m = os.stat(CFG).st_mtime
    except OSError:
        m = None
    if m == cache["m"]:
        return cache["c"]
    c = dict(DEFAULTS)
    if m is not None:
        try:
            d = json.load(open(CFG))
            c["lux_dark"] = max(0.1, float(d.get("lux_dark", c["lux_dark"])))
            c["lux_bright"] = max(c["lux_dark"] * 2, float(d.get("lux_bright", c["lux_bright"])))
            c["min_pct"] = max(5, min(100, int(d.get("min_pct", c["min_pct"]))))
            c["up_s"] = max(0.0, min(30.0, float(d.get("up_s", c["up_s"]))))
            c["down_s"] = max(0.0, min(30.0, float(d.get("down_s", c["down_s"]))))
        except (OSError, ValueError, TypeError, AttributeError) as e:
            print(f"display.json ignored ({e}), using defaults", flush=True)
    if c != cache["c"] or cache["m"] is None:
        print(f"light settings: {c}", flush=True)
    cache["m"], cache["c"] = m, c
    return c


def target_pct(lux, c):
    if lux <= c["lux_dark"]:
        return float(c["min_pct"])
    if lux >= c["lux_bright"]:
        return 100.0
    k = (math.log(lux) - math.log(c["lux_dark"])) / (math.log(c["lux_bright"]) - math.log(c["lux_dark"]))
    return c["min_pct"] + (100 - c["min_pct"]) * k


class Smoother:
    """Moves toward the target right away; ~95 % of the way in up_s (brighter) or down_s (darker)."""
    def __init__(self):
        self.v = None
        self.pub = None
        self.t = None

    def step(self, target, now, c):
        if self.v is None:
            self.v, self.t = target, now
        else:
            dt = max(0.0, now - self.t); self.t = now
            span = c["up_s"] if target > self.v else c["down_s"]
            a = 1.0 if span <= 0 else 1 - math.exp(-3 * dt / span)
            d = target - self.v
            mv = d * a
            floor = MIN_SPEED * dt                     # tail: never crawl the last few %
            if abs(mv) < floor:
                mv = math.copysign(min(floor, abs(d)), d)
            self.v += mv
        v = round(self.v)
        if self.pub is None or abs(v - self.pub) >= STEP or v == round(target):
            self.pub = v
        return self.pub


def write(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


class Sensor:
    def __init__(self, bus):
        self.bus, self.i = bus, 1

    def config(self):
        _, g, it, _, its = RANGES[self.i]
        self.bus.write_word_data(ADDR, 0x00, (g << 11) | (it << 6))     # power on, no interrupt
        time.sleep(its * 2 + 0.01)                                      # first result after a change is stale

    def read(self):
        """One reading: (lux, raw, range name). Changes range when raw is near the ends."""
        name, _, _, res, its = RANGES[self.i]
        time.sleep(its + 0.01)
        raw = self.bus.read_word_data(ADDR, 0x04)
        if raw > UP_RAW and self.i < len(RANGES) - 1:
            self.i += 1; self.config()
        elif raw < DOWN_RAW and self.i > 0:
            self.i -= 1; self.config()
        return raw * res, raw, name   # linear; above ~1,000 lux it reads a bit low, but that is 100 % anyway


def open_bus():
    try:
        from smbus2 import SMBus
    except ImportError:
        from smbus import SMBus
    return SMBus(BUS_N)


def run():
    sm, sensor, bus, err = Smoother(), None, None, None
    print("light_reader v1.0 running", flush=True)
    while True:
        try:
            if sensor is None:
                bus = open_bus(); sensor = Sensor(bus); sensor.config()
                print("VEML7700 found at 0x10", flush=True); err = None
            lux, raw, rng = sensor.read()
            c = load_cfg(); now = time.monotonic(); tgt = target_pct(lux, c)
            pct = sm.step(tgt, now, c)
            write(OUT, {"t": now, "lux": round(lux, 2), "raw": raw, "range": rng, "target": round(tgt), "pct": pct})
        except OSError as e:
            if str(e) != err:
                print(f"sensor not answering ({e}); hud_service falls back to ILL. Retrying every 2 s", flush=True)
                err = str(e)
            sensor = None
            try:
                bus and bus.close()
            except OSError:
                pass
            time.sleep(2)


def watch():
    try:
        while True:
            try:
                d = json.load(open(OUT + "-test")) if os.path.exists(OUT + "-test") else json.load(open(OUT))
                age = time.monotonic() - d["t"]
                tag = "  TEST" if os.path.exists(OUT + "-test") else ""
                print(f"{d['lux']:9.2f} lux  raw {d.get('raw', '-'):>5}  {d.get('range', '-'):6}  target {d['target']:3d} %"
                      f"  screen {d['pct']:3d} %  ({age:.1f} s old){tag}", flush=True)
            except (OSError, ValueError, KeyError):
                print("no data in /dev/shm/caguas-lux (is light-reader running?)", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def test(vals):
    sm, i, t0 = Smoother(), 0, time.monotonic()
    print(f"faking {' / '.join(str(v) for v in vals)} lux into {OUT}-test (Ctrl+C to stop and delete it)", flush=True)
    try:
        while True:
            now = time.monotonic()
            lux = vals[int((now - t0) // 8) % len(vals)]
            c = load_cfg(); tgt = target_pct(lux, c); pct = sm.step(tgt, now, c)
            write(OUT + "-test", {"t": now, "lux": lux, "raw": None, "range": "test", "target": round(tgt), "pct": pct})
            print(f"{lux:9.2f} lux  target {round(tgt):3d} %  screen {pct:3d} %", flush=True)
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(OUT + "-test")
        except OSError:
            pass


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["watch"]:
        watch()
    elif a[:1] == ["test"] and len(a) >= 2:
        test([float(x) for x in a[1:]])
    else:
        run()
