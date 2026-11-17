#!/usr/bin/env python3
"""Timeline of the power chain from the journal: CAN (door / key) -> rp2040-link -> RP2040 states -> hud-service.
Shows where the time goes between "door opened" and the bye / poweroff, and between key ON and the music.
  python3 tools/timeline.py         this boot
  python3 tools/timeline.py -1      previous boot (after a shutdown; -2 = the one before, ...)
  python3 tools/timeline.py --test  self-check
t = seconds since the last change of the CAN 'POWER:' line (RADIO OFF = door opened), + = since the line above."""
import json, re, subprocess, sys

UNITS = ("rogue-can", "rp2040-link", "hud-service", "cam-relay")
KEEP = re.compile(r"POWER:|ENG .*DOOR| -> |SHUTDOWN|BYE|fade-|AVRCP|telefono|^bye|^welcome|^screen |^camera |^camara |^REV ")


def timeline(entries):
    """entries: journalctl -o json dicts -> text lines"""
    out, t0, prev, power = [], None, None, None
    for e in entries:
        msg, unit = e.get("MESSAGE"), e.get("_SYSTEMD_UNIT", "").replace(".service", "")
        if not isinstance(msg, str) or not KEEP.search(msg):
            continue
        t = int(e["__MONOTONIC_TIMESTAMP"]) / 1e6
        if "POWER:" in msg:
            if msg.split("POWER:")[1] == power:
                continue                              # the cluster repeats its state
            power, t0 = msg.split("POWER:")[1], t
            out.append("")
        out.append(f"{'' if t0 is None else f'{t - t0:8.2f}'} {'' if prev is None else f'{t - prev:+7.2f}':>8}  {unit:<12} {msg}")
        prev = t
    return out


if "--test" in sys.argv:
    E = lambda t, u, m: {"__MONOTONIC_TIMESTAMP": str(int(t * 1e6)), "_SYSTEMD_UNIT": u + ".service", "MESSAGE": m}
    got = timeline([E(10, "rogue-can", "[ 1.00] POWER: RADIO OFF"), E(10.3, "rp2040-link", "ENG 1 | DOOR 1"),
                    E(10.5, "rogue-can", "[ 1.50] POWER: RADIO OFF"), E(10.6, "hud-service", "noise"),
                    E(12.1, "hud-service", "bye (door): See you, Zen"), E(13, "x", [1, 2])])
    assert len(got) == 4 and got[2].split()[:2] == ["0.30", "+0.30"] and got[3].split()[:2] == ["2.10", "+1.80"], got
    print("timeline: PASS"); sys.exit(0)

boot = next((a for a in sys.argv[1:] if a.lstrip("-").isdigit()), "0")
cmd = ["journalctl", "-b", boot, "-o", "json", "--no-pager"] + [f"-u{u}" for u in UNITS]
lines = subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()
print("\n".join(timeline(json.loads(l) for l in lines)) or "nothing in the journal for that boot (try: sudo, or -1)")
