#!/usr/bin/env python3
# lux_test.py: reads the VEML7700 every 0.5 s. Ctrl+C to stop.
import time
try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus
ADDR = 0x10
bus = SMBus(1)
bus.write_word_data(ADDR, 0x00, 0x0000)   # gain x1, 100 ms, sensor on
time.sleep(0.2)
try:
    while True:
        raw = bus.read_word_data(ADDR, 0x04)  # ALS
        print(f"raw {raw:5d}   ~{raw * 0.0672:8.1f} lux", flush=True)
        time.sleep(0.5)
except KeyboardInterrupt:
    bus.write_word_data(ADDR, 0x00, 0x0001)   # sensor off
