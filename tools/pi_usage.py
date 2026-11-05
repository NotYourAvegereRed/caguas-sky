#!/usr/bin/env python3
"""
pi_usage.py - CPU / RAM / temperature of the Pi while the dashboard shows one screen. Stdlib only.

    python3 pi_usage.py --label main --seconds 60
    (swipe to the telemetry page)
    python3 pi_usage.py --label tele-all --seconds 60

Prints a summary and appends it to ~/pi_usage.csv so the runs can be compared.
CPU % is of the WHOLE Pi (4 cores = 100 %). Per process, 100 % = one full core.
"""
import argparse
import os
import subprocess
import time

GROUPS = [   # (label, test on the process name / command line)
    ("chromium",     lambda n, c: "chrom" in n),
    ("hud-service",  lambda n, c: "hud_service" in c),
    ("camilladsp",   lambda n, c: "camilladsp" in n),
    ("camillagui",   lambda n, c: "camillagui" in c or ("python" in n and "main.py" in c)),
    ("sky (http)",   lambda n, c: "http.server" in c),
    ("pipewire",     lambda n, c: n.startswith("pipewire") or n == "wireplumber"),
    ("rp2040-link",  lambda n, c: "rp2040" in c),
    ("desktop",      lambda n, c: n in ("labwc", "wayfire", "Xorg", "Xwayland", "wf-panel-pi", "pcmanfm")),
]
HZ = os.sysconf("SC_CLK_TCK")


def cpu_total():
    v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v), idle


def procs():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            st = open(f"/proc/{pid}/stat").read()
            name = st[st.index("(") + 1:st.rindex(")")]
            f = st[st.rindex(")") + 2:].split()
            ticks = int(f[11]) + int(f[12])                     # utime + stime
            rss = int(open(f"/proc/{pid}/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            cmd = open(f"/proc/{pid}/cmdline").read().replace("\0", " ")
            out[pid] = (name, cmd, ticks, rss)
        except (OSError, ValueError, IndexError):
            pass
    return out


def group_of(name, cmd):
    for g, test in GROUPS:
        if test(name, cmd):
            return g
    return None


def mem():
    m = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        m[k] = int(v.split()[0]) * 1024
    return m["MemTotal"], m["MemTotal"] - m["MemAvailable"]


def temp():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except OSError:
        return None


def freq():
    try:
        return int(open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read()) / 1000
    except OSError:
        return None


def throttled():
    try:
        return subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return "n/a"


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--label", required=True)
    a.add_argument("--seconds", type=int, default=60)
    a = a.parse_args()
    ncpu = os.cpu_count() or 4
    samples = []
    t0, i0 = cpu_total()
    p0 = procs()
    print(f"Measuring '{a.label}' for {a.seconds} s (leave the screen on that page)...")
    for s in range(a.seconds):
        time.sleep(1)
        t1, i1 = cpu_total()
        p1 = procs()
        dt = t1 - t0
        total = 100 * (1 - (i1 - i0) / dt) if dt else 0
        per, rss = {}, {}
        for pid, (n, c, ticks, r) in p1.items():
            g = group_of(n, c)
            if not g:
                continue
            prev = p0.get(pid)
            d = ticks - prev[2] if prev and prev[0] == n else 0
            per[g] = per.get(g, 0) + 100 * d / (dt / ncpu)        # % of one core
            rss[g] = rss.get(g, 0) + r
        _, used = mem()
        samples.append({"cpu": total, "per": per, "rss": rss, "used": used, "temp": temp(), "freq": freq()})
        t0, i0, p0 = t1, i1, p1
        print(f"\r  {s + 1:3d}/{a.seconds}  CPU {total:5.1f} %  RAM {used / 2**20:6.0f} MB  "
              f"temp {samples[-1]['temp'] or 0:4.1f} C   ", end="", flush=True)
    print()

    total_mem, _ = mem()
    avg = lambda xs: sum(xs) / len(xs) if xs else 0
    cpu = [s["cpu"] for s in samples]
    used = [s["used"] for s in samples]
    temps = [s["temp"] for s in samples if s["temp"] is not None]
    freqs = [s["freq"] for s in samples if s["freq"] is not None]
    print(f"\n=== {a.label} ({a.seconds} s) ===")
    print(f"  CPU (whole Pi, 4 cores = 100%)  avg {avg(cpu):5.1f} %   max {max(cpu):5.1f} %")
    print(f"  RAM used                        avg {avg(used) / 2**20:5.0f} MB  max {max(used) / 2**20:5.0f} MB  of {total_mem / 2**20:.0f} MB")
    if temps:
        print(f"  SoC temp                        avg {avg(temps):5.1f} C   max {max(temps):5.1f} C")
    if freqs:
        print(f"  CPU clock                       avg {avg(freqs):5.0f} MHz min {min(freqs):5.0f} MHz")
    print(f"  Throttling: {throttled()}   (0x0 = never throttled)")
    print("\n  per process (100 % = one full core)        CPU avg   CPU max    RAM")
    rows = []
    for g, _ in GROUPS:
        c = [s["per"].get(g, 0) for s in samples]
        r = [s["rss"].get(g, 0) for s in samples]
        if max(r, default=0) == 0:
            continue
        rows.append((g, avg(c), max(c), max(r) / 2**20))
        print(f"    {g:<14}                            {avg(c):6.1f} %  {max(c):6.1f} %  {max(r) / 2**20:5.0f} MB")
    if not any(g == "chromium" for g, *_ in rows):
        print("\n  NOTE: no Chromium on the Pi. If the dashboard is open on the LAPTOP, the Pi only runs the backend:")
        print("  the page's own cost (animations, gauges) is not in these numbers.")

    path = os.path.expanduser("~/pi_usage.csv")
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new:
            f.write("time,label,seconds,cpu_avg,cpu_max,ram_avg_mb,ram_max_mb,temp_avg,temp_max," +
                    ",".join(f"{g}_cpu_avg" for g, _ in GROUPS) + "\n")
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{a.label},{a.seconds},{avg(cpu):.1f},{max(cpu):.1f},"
                f"{avg(used) / 2**20:.0f},{max(used) / 2**20:.0f},{avg(temps):.1f},{max(temps, default=0):.1f}," +
                ",".join(f"{avg([s['per'].get(g, 0) for s in samples]):.1f}" for g, _ in GROUPS) + "\n")
    print(f"\n  Saved to {path}")


if __name__ == "__main__":
    main()
