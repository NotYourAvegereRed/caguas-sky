#!/usr/bin/env python3
"""cam_relay.py v1.2 (2026-11-15): backup camera on while in reverse, and 8 s after leaving R (like the factory radio).

Reads REV from /dev/shm/caguas-lights {"t": monotonic, "ill", "rev"} (written by rp2040-link; a fresh "-test" file
wins, so 'rp2040-link.py test rev 1' works). Camera on = REV seen in the last HOLD_S seconds.
  - GPIO26 (header pin 37) -> relay R3 IN -> DROK 12->6 V -> camera power. 3.3 V is enough (same red module as R1).
    GPIO26 has a pull-down at boot, so R3 stays off while the Pi boots or is off.
  - /dev/shm/caguas-cam {"t": monotonic, "on": 0/1}: hud_service tells the page to show the camera.
Stale or missing REV data counts as not in reverse (the hold still runs out).
"""
import json, os, subprocess, time

LIGHTS = "/dev/shm/caguas-lights"
CAM_FILE = "/dev/shm/caguas-cam"
FRESH_S = 5.0
HOLD_S = 8.0
PIN = 26


def rev_on(now):
    for p in (LIGHTS + "-test", LIGHTS):
        try:
            d = json.load(open(p))
            if now - float(d.get("t", 0)) <= FRESH_S and d.get("rev") in (0, 1):
                return d["rev"] == 1
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return False


def cam_wanted(last_rev, now):
    return last_rev is not None and now - last_rev <= HOLD_S


def write(on, now):
    tmp = CAM_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t": now, "on": int(on)}, f)
    os.replace(tmp, CAM_FILE)


def set_pin(on):
    # pinctrl ships with Pi OS; gpiozero + lgpio failed on this Pi 5 ('unexpected error' claiming the pin)
    subprocess.run(["pinctrl", "set", str(PIN), "op", "dh" if on else "dl"], check=True)


def main():
    set_pin(False)
    on, last_rev, last_write = False, None, 0.0
    print(f"cam_relay v1.2: GPIO{PIN} (pin 37) -> R3, hold {HOLD_S:.0f} s", flush=True)
    while True:
        now = time.monotonic()
        if rev_on(now):
            last_rev = now
        want = cam_wanted(last_rev, now)
        if want != on:
            set_pin(want); on = want
            print(f"camera {'ON' if on else 'off'}", flush=True)
        if want != on or now - last_write >= 1.0 or last_write == 0.0:
            write(on, now); last_write = now
        time.sleep(0.1)


if __name__ == "__main__":
    main()
