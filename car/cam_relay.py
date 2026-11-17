#!/usr/bin/env python3
"""cam_relay.py v1.4 (2026-11-17): backup camera power.
v1.4: only with the engine running (RP2040 state RUN for RUN_SETTLE_S, from /dev/shm/caguas-car): never through the
      crank dip (~10.5 V) on the DROK / camera. No car state (rp2040-link down): REV alone, as before.
v1.3: also on while hud_service asks for it ("cam": 1 in
/dev/shm/caguas-bl: P / N, so the picture is ready in R); the page now follows hud_service, not /dev/shm/caguas-cam.
v1.2 (2026-11-15): on while in reverse, and 8 s after leaving R (like the factory radio).

Reads REV from /dev/shm/caguas-lights {"t": monotonic, "ill", "rev"} (written by rp2040-link; a fresh "-test" file
wins, so 'rp2040-link.py test rev 1' works). Camera on = REV seen in the last HOLD_S seconds.
  - GPIO26 (header pin 37) -> relay R3 IN -> DROK 12->6 V -> camera power. 3.3 V is enough (same red module as R1).
    GPIO26 has a pull-down at boot, so R3 stays off while the Pi boots or is off.
Stale or missing REV data counts as not in reverse (the hold still runs out).
"""
import json, subprocess, time

LIGHTS = "/dev/shm/caguas-lights"
BL_FILE = "/dev/shm/caguas-bl"     # hud_service: {"t", "bl", "night", "cam": 0/1}
CAR_FILE = "/dev/shm/caguas-car"   # rp2040-link: {"t", "state"}
RUN_SETTLE_S = 2.0                 # after RUN starts: let the voltage recover from the crank first
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


def hud_wants(now):
    try:
        d = json.load(open(BL_FILE))
        return now - float(d.get("t", 0)) <= FRESH_S and d.get("cam") == 1
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def car_state(now):
    try:
        d = json.load(open(CAR_FILE))
        return d["state"] if now - float(d.get("t", 0)) <= FRESH_S and isinstance(d.get("state"), str) else None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def cam_wanted(last_rev, now, state, run_t):
    rev = last_rev is not None and now - last_rev <= HOLD_S
    if state is None:
        return rev                                   # no car state: the reverse wire alone (no crank in R)
    return state == "RUN" and run_t is not None and now - run_t >= RUN_SETTLE_S and (rev or hud_wants(now))


def set_pin(on):
    # pinctrl ships with Pi OS; gpiozero + lgpio failed on this Pi 5 ('unexpected error' claiming the pin)
    subprocess.run(["pinctrl", "set", str(PIN), "op", "dh" if on else "dl"], check=True)


def main():
    set_pin(False)
    on, last_rev, run_t = False, None, None
    print(f"cam_relay v1.4: GPIO{PIN} (pin 37) -> R3, hold {HOLD_S:.0f} s, only in RUN", flush=True)
    while True:
        now = time.monotonic()
        if rev_on(now):
            last_rev = now
        state = car_state(now)
        run_t = (run_t or now) if state == "RUN" else None
        want = cam_wanted(last_rev, now, state, run_t)
        if want != on:
            set_pin(want); on = want
            print(f"camera {'ON' if on else 'off'}", flush=True)
        time.sleep(0.1)


if __name__ == "__main__":
    main()
