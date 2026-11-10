# Caguas-sky

**Zen's Raspberry Pi 5 radio / DSP project** · 2017 Nissan Rogue · Raspberry Pi 5

## The goal

LDAC and CVT pressures on my radio screen with a nice UI, but I just kept digging.

## What it turned into

| | |
|---|---|
| **Audio** | The Pi 5 is the car's head unit: Bluetooth LDAC in, CamillaDSP crossovers and EQ out to a HiFiBerry DAC8x (3-way + sub). |
| **CVT gauges** | Pulley pressures, line pressure, CVT temp, slip and torque converter lockup on the main screen at 15-16 Hz. |
| **Telemetry pages** | ALL / ENGINE / CVT / DYNO tabs: 21 ECM PIDs, engine torque, HP, wheel torque, gear ratio, virtual gears, and a 0-60 timer (locked above 82 °C CVT fluid). |
| **Audio / DSP page** | Live levels for every channel, presets, bass / treble / sub level. |
| **Alerts** | Oil pressure scaled by rpm, coolant and oil temp, with a red badge on the main screen. Trouble codes (ECM / TCM / BCM) are read once at startup: yellow badge. |
| **Live sky** | Real sun and moon position and moon phase over Caguas as the wallpaper. |
| **Car integration** | RP2040 power controller (ACC / IGN, relays, backlight PWM), factory radio emulated on the CAN bus (steering wheel buttons, cluster text and clock), light sensor screen dimming. |
| **PID hunting** | First PID scan, CVTz50 and Car Scanner Bluetooth snoops, then drive logs to find what the apps never asked for (torque, ratio, pulley speeds). |
| **Still digging** | Dashcam, reverse camera, case with two Noctua fans and PWM control, and maybe a plug-and-play CVT gauge to sell. |

## What's in this repo

| Folder | What it is |
|---|---|
| `dashboard/` | The screen: `index.html` + `src/` (HTML / CSS / JS, shown full screen by Chromium) and `service/` (`hud_service.py`: media, CVT / OBD reader, DSP control, screen level over a WebSocket). `kiosk.sh` starts the screen. |
| `car/` | Car-side services: `rogue_can.py` (CAN radio emulator), `rp2040-link.py` (power link to the RP2040), `light_reader.py` (VEML7700), `phone-autoconnect.sh`, `wifi-prefer-home.sh`. |
| `rp2040/` | MicroPython firmware for the RP2040-Zero power controller. |
| `camilladsp/configs/` | CamillaDSP configs. Every `*.yml` here (except `test_*`) shows up as a preset on the DSP page. |
| `systemd/` | Service and timer files, copies of what is installed in `/etc/systemd/system`. |
| `tools/` | Test and diagnostic scripts (`pi_usage.py`, `gpu_usage.py`, `lux_test.py`, `dtc_probe.py`, ...). |

## How it runs on the Pi

Everything starts by itself at boot. The live copies are linked, not copied: `~/sky` → `dashboard/`, `/usr/local/bin/*` → `car/`, `~/camilladsp/configs` → `camilladsp/configs/`.

| Service | Does |
|---|---|
| `caguas-sky` | Serves the dashboard on port 8080 |
| `hud-service` | Data for the screen (WebSocket 8766) |
| `camilladsp` / `camillagui` | DSP (WebSocket 1234) / its web GUI (port 5005) |
| `rogue-can` | Factory radio emulator on the CAN bus |
| `rp2040-link` | Power states, music fade, backlight |
| `light-reader` | Light sensor → screen level |
| `wifi-prefer-home.timer` | Leaves the phone hotspot when home Wi-Fi is in range |
| `phone-autoconnect` (user) | Reconnects the phone over Bluetooth |

Preview the screen from any browser on the same network: `http://zenraspberrypi5.local:8080/index.html?demo=1`
(`&page=tele` or `&page=dsp` opens the other pages).

## Documentation

The full build log lives in the Google Sheet **Zen's Raspberry Pi 5 Radio/DSP Project**: setup log (every change, test and result, in order), hardware and Amazon BOMs, planned wire list, radio harness readouts, and every PID with its formula. Drive logs, torque logs, snoops, scans and videos are in the Google Drive folder **Rogue Pi**.

The live sky background is based on [Vantage](https://github.com/SysAdminDoc/Vantage) (MIT, see `dashboard/LICENSE-Vantage-MIT`).
