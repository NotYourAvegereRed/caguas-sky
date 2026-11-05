#!/usr/bin/env python3
"""gpu_usage.py: how busy the Pi 5's GPU (VideoCore VII, driver v3d) is, every 2 s. Ctrl+C to stop.

Reads the v3d driver's own counters (sysfs file 'gpu_stats': per queue, total jobs and total busy time in ns)
and prints the % of time each queue was busy. 'render' is the one that matters for Chromium (drawing the
dashboard); 'bin' is the geometry step before it. Also prints the GPU clock and the SoC temperature.
Usage:  python3 ~/gpu_usage.py          (sudo only if it says it cannot read the file)
"""
import glob, subprocess, sys, time

INTERVAL = 2.0


def find_stats():
    # direct paths only: a recursive search of /sys loops through its symlinks forever
    for pat in ("/sys/bus/platform/drivers/v3d/*/gpu_stats", "/sys/class/drm/card*/device/gpu_stats",
                "/sys/devices/platform/axi/*v3d*/gpu_stats", "/sys/devices/platform/*v3d*/gpu_stats"):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def read(path):
    """{queue: (timestamp_ns, jobs, runtime_ns)}"""
    out = {}
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) == 4 and p[1].isdigit():
                out[p[0]] = (int(p[1]), int(p[2]), int(p[3]))
    return out


def vc(*args):
    try:
        return subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "?"


path = find_stats()
if not path:
    sys.exit("No encontre gpu_stats del driver v3d (kernel muy viejo o driver distinto). "
             "Mira al menos: vcgencmd measure_clock v3d")
try:
    prev = read(path)
except PermissionError:
    sys.exit(f"Sin permiso para leer {path}: corre con sudo")
print(f"leyendo {path} (Ctrl+C para parar)")
try:
    while True:
        time.sleep(INTERVAL)
        cur = read(path)
        parts = []
        for q in ("render", "bin", "csd", "tfu"):
            if q in cur and q in prev:
                dt = cur[q][0] - prev[q][0]
                busy = cur[q][2] - prev[q][2]
                jobs = cur[q][1] - prev[q][1]
                if dt > 0:
                    parts.append(f"{q} {100 * busy / dt:5.1f}% ({jobs / INTERVAL:4.0f} jobs/s)")
        clk = vc("measure_clock", "v3d").split("=")[-1]
        try:
            mhz = f"{int(clk) / 1e6:.0f} MHz"
        except ValueError:
            mhz = clk
        temp = vc("measure_temp").replace("temp=", "")
        print(f"{time.strftime('%H:%M:%S')}  " + "  |  ".join(parts) + f"  |  GPU {mhz}  |  SoC {temp}", flush=True)
        prev = cur
except KeyboardInterrupt:
    pass
