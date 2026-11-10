# Sky-only background (from Vantage, MIT)

Live sky for the Pi screen: real sun and moon position and moon phase for the
configured location, time-of-day colors (golden hour, dusk, night), stars.
No ground layers, no weather, no network.

Run (on the Pi):
    cd sky
    python3 -m http.server 8080
    chromium-browser --kiosk http://localhost:8080      # or chromium

Edit src/sky.js (CONFIG block): location, locality, motion, atmosphere, readability.
Preview any time of day: http://localhost:8080/index.html?qaTime=17:50&qaDate=2026-10-04
Time-lapse preview: add &speed=1200 (18 h in ~54 s), e.g.
?qaTime=05:00&qaDate=2026-10-04&speed=1200
(qaTime uses the browser's timezone, which on the Pi should be America/Puerto_Rico.)

Changes from upstream Vantage (https://github.com/SysAdminDoc/Vantage, MIT):
- src/utils/weather-source.js and integration-health.js are stubs (no network).
- src/widgets/background.js: parseSceneCoordinate no longer turns a missing
  param into 0,0 (Null Island).
- src/sky.css hides ground/foreground layers.
- src/sky.js and index.html are new.
Upstream license: LICENSE-Vantage-MIT.

## HUD (CVT + now playing)
src/hud.js / hud.css: two panels cover the whole screen. Left: 6 CVT tiles (target/actual 1,
target/actual 2, fluid temp, lockup + slip). Right: now playing with album art and touch buttons.
Live by default (CVT + media from hud-service). ?demo=1 runs FAKE data (DEMO tag) to judge the layout.
- live mode connects to ws://<host>:8766 and shows "--" until the service sends data.
- ?bodies=0 hides the sun/moon discs (by default they show, blurred, through the translucent panels).
- Data contract and the PLACEHOLDER ranges/thresholds are at the top of hud.js.
  media.art = image URL or data URI (placeholder when missing).
- Units: pressure in MPa, fluid temp in °C. Arc colors blend between stops (PRESSURE_STOPS / TEMP_STOPS at the top of hud.js).
  Fixed by you: pressure blue below 1.5, leaving green at 4.0, volcanic red at 6.0; temp orange from 90, red at 110, limit 130.
  Everything else (where green is reached, yellow/orange steps, cold end of temp) is a placeholder.
- ?gauges=cluster: instrument-cluster gauge concept (segmented LED arc, scale numbers, zone band, pointer). Without it: classic arcs.
- Lockup tile: OPEN gray, SLIP amber, LOCKED green.
- Console test helper: hud.setCvt({p_primary_target_mpa: 4.5, temp_c: 100, lockup: 'locked'})
- "1" = primary pulley, "2" = secondary pulley is an assumption; rename in CONFIG.

## HUD service (now playing from the phone)
service/hud_service.py reads the phone's AVRCP metadata from BlueZ (title, artist, album, duration,
status, position) and the active codec from PipeWire (pw-dump), and serves it on ws://<host>:8766.
Touch buttons send play/pause/next/prev back to the phone through BlueZ.
- Install (on the Pi):
    python3 -m venv ~/hud-venv && ~/hud-venv/bin/pip install websockets dbus-fast
    sudo cp ~/sky/service/hud-service.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now hud-service
- Page: http://<pi>:8080/index.html  (live)
- Cover art: this phone sends no cover over Bluetooth (no ObexPort), so the service asks the Spotify Web API
  ("currently playing") for the album image, checks that title+artist match what the phone reports, caches the image
  in ~/.cache/hud-cover and sends it as a data URI. Only works for music played from Spotify. No match / no internet /
  not Spotify -> the page shows its placeholder; it retries by itself (a few times, 3 s then 45 s apart).
  Setup (once): create an app at developer.spotify.com/dashboard (Web API, redirect URI http://127.0.0.1:8888/callback;
  the app owner needs Spotify Premium), then on the Pi: python3 ~/sky/service/spotify_login.py
  Turn off covers with HUD_ART=0. The login (refresh token) is in ~/.config/hud/spotify.json (owner-only).
- Tested against a fake BlueZ and a fake Spotify (token refresh, 401, stale data, 204, offline cache). The real Spotify
  API was not reachable from the test machine.

## Live CVT data (2026-10-26)
service/cvt_reader.py reads the TCM through the vLinker FS on a Pi USB port (READ-ONLY, mode 22 + 01),
all 9 CVT PIDs in one multi-DID request, ~12 readings/s, and hud_service.py pushes {"cvt": ...} to the page.
- needs pyserial in the venv: ~/hud-venv/bin/pip install pyserial ; user zen in group dialout
- port: auto (first /dev/serial/by-id entry that is not the RP2040), or Environment=HUD_OBD_PORT=/dev/...
- page: index.html (live by default). "NO OBD" tag = no adapter / car off.
- torque converter (2026-10-27): 1219 = TCC solenoid current ISOLT1 (2 bytes, mA), read in the same request.
  States (class Lockup in cvt_reader.py): Unlocked (0 A), Engaging / Releasing (current applied, slip still big,
  rising / falling), Locked, Locked + "coast" (status byte 112A >= 64, pedal off). A new state shows after 0.25 s.
  If the TCM ever stops answering 1219 it falls back to the old slip-only estimate.
  The tile's arc is the current (0-0.7 A), colored by state; small line under it = slip rpm (">+127" = saturated)
- needles: data ~12 Hz, drawn at 60 fps easing toward the newest value (SMOOTH_TAU in hud.js)
PIDs (header 7E1, 1 byte): 110B PRI actual, 110F PRI target, 110A SEC actual, 1111 SEC target,
110E line target (all A*0.025 MPa) | 1105 slip (signed) | 110C CVT temp (A-40) | 1119 gear 1P 2R 3N 4D | 1101 speed km/h
| 112A lock-up status | 1219 TCC current (2 bytes, mA)
Full list with formulas: cvt_pids.csv

## Volume slider (2026-10-27)
Tap anywhere on the dashboard (not on a button) to open a One UI-style volume pill left of the album cover, 0-100 like the
phone (drag it; tap outside or wait 3 s to close). It sets CamillaDSP's MAIN volume (dB, -60..0) through hud_service.py -> CamillaDSP websocket
(HUD_DSP_WS, default ws://127.0.0.1:1234). The phone stays at 100 % and the PipeWire sink at 1.0 (headunit style).
The slider follows changes from anywhere else too (rp2040-link fades, CamillaGUI). rp2040-link v0.4 remembers the
volume across power cycles (/var/lib/rp2040-link/last_volume) and fades back to it.

index.html loads every css/js with ?v=<date>: bump it on each release so the browser never mixes old and new files.

## Telemetry window (2026-10-27)
Swipe LEFT on the main screen (or tap the red warning badge) to open it; swipe RIGHT to go back.
src/telemetry.js / telemetry.css (fonts in assets/fonts, Barlow Condensed + JetBrains Mono, OFL, offline).
- Tabs ALL / ENGINE / CVT. The page tells hud-service which screen is open ({"cmd":"view"}) and cvt_reader.py
  only polls that: main = CVT at full speed + oil pressure/rpm every 1 s + coolant/oil temp every 7 s (alerts only);
  cvt = same + fluid wear; engine = all engine DIDs + CVT once a second; all = engine DIDs + CVT every round.
- Opening animation only the first time the page opens after ignition (page load, or OBD back after 60 s without data).
- ECM data needs the Nissan session 10C0 (otherwise 7F 22 80); the reader keeps it alive every 2.5 s while it reads
  the ECM and re-opens it on a 7F reply. Multi-DID requests, falls back to one DID at a time if the ECM refuses.
- Alerts (cvt_reader.py, class Alerts) -> red badge in the corner of the CVT panel + banner on the telemetry page:
  coolant >= 110 C (clears < 108), oil temp >= 130 C (clears < 127), oil pressure below a minimum that grows with rpm:
  alert = max(1000 mV, 6 psi/1000 rpm), yellow = max(1200 mV, 9 psi/1000 rpm), flat above 4000 rpm;
  uses the lower of rpm now and 2 s ago, only after 10 s of running, 2 low readings to fire, 3 good to clear.
- Oil pressure psi = (V - 0.77) * 20.7 (OIL_ZERO_V / OIL_PSI_PER_V in cvt_reader.py). Zero confirmed 2026-10-28:
  770 mV with ignition ON / engine OFF, warm and cold. The 20.7 psi/V slope is not yet checked with a gauge.
- Badge radius = CVT panel radius - its 8 px inset (concentric with the panel corner), read from hud.css at runtime.
- Volume: now opens with a TAP on the LEFT half of the main screen (a swipe never opens it).
- ?demo=1&alert=oil|coolant|oiltemp forces an alert in demo mode, ?page=tele opens on the telemetry page.
- Tested against a fake ELM327 (pty) with the real hud_service.py: per-view polling, session timeout, ECM without
  multi-DID, alerts on/off, car off. Not yet tested on the car.

## Engine-start animation + swipe/volume fix (2026-10-27)
- Main screen: while the car is on but the engine is not running (OBD data, rpm <= 500) the panels sit in a dim
  "standby"; when rpm stays above 500 for 0.8 s they wake up: brighten and lift, a soft sheen crosses the glass, the
  cover comes into focus, the gauges climb to their values. Once per trip (re-arms after 60 s with the engine off).
  No OBD for 15 s (bench, car off) = no dimming. Preview: ?demo=1&startup=1. Tune WAKE_RPM / WAKE in hud.js.
- Swiping with the volume pill open now works: the pill closes in 0.1 s and the page moves. Only a finger that
  starts ON the pill is a volume drag.

## Faster telemetry tabs (2026-10-28)
The Rogue's ECM refuses multi-DID requests, so cvt_reader.py reads it one DID at a time, now in rounds:
TIER_1 every round (rpm, oil pressure, MAF, load, timing, STFT, throttle 1/2, pedal 1/2), TIER_4 every 4th
(LTFT, injector, exhaust VTC, A/F, intake air, exhaust cam), TIER_8 every 8th (coolant, oil temp, fuel temp,
control module V, A/C pressure), spread so each round takes about the same time. Single-frame requests now end in
"1" (the ELM returns after the first reply): ~42 -> ~22 ms per request. Laptop test (rogue_tiers.py, 30 s, with the
CVT request): 3.44 rounds/s with tiers + suffix, 2.29 all-every-round + suffix, 1.80 tiers without suffix.

## Main screen: no more 010C, CVT refresh shown (2026-10-28)
cvt_reader.py no longer asks the standard 010C rpm every 2nd loop (3 extra commands per 2 loops: two header switches
+ the request, and it never answered while the ECM was in the 10C0 session). The CVT data's rpm now comes from the
ECM's 1201 read: every 1 s on the main screen (the alert read), every 0.5 s on the CVT tab, every round on ALL/ENGINE.
Laptop test (rogue_main_test.py, 20 s each): 11.0 CVT readings/s before, 15.4 after, 16.7 with the CVT alone.
The main screen shows the real rate next to "CVT" (cvt.hz). The engine-start animation now reacts ~1 s later.

## ALL tab: 3.4 -> 4.5 Hz, and no faster route on this ECM (2026-10-28)
Throttle 2 and pedal 2 (backup sensors) moved to TIER_4, and the ALL tab asks the CVT every 2nd round
(ALL_CVT_EVERY). Laptop test (rogue_tiers.py --sensor2-slow --cvt-every 2): 4.54 rounds/s, was 3.44.
Checked with rogue_block_probe.py: the ECM refuses even 2 DIDs per request (7F 22 12), and its service 21
blocks (01-05, A0, A1) never changed while revving, so they are not live data. One DID per request is the limit.

## Torque, HP, wheel torque, gear ratio (2026-10-28)
From a 24 min drive (rogue_torque_log.py, up to 5450 rpm / 115 km/h) and the watch-12 log. TCM DIDs (2 bytes):
1213 / 10 = engine torque Nm (signed; r = 0.95 with air per rev; curve peaks 226 Nm at 4000-4500 rpm, QR25DE spec
237 Nm @ 4400), 1210 / 10000 = actual ratio (= 1201 / 1203, median error 0.002; holds 2.727 at a standstill),
1218 / 10000 = target ratio (2.631 lowest), 1201 primary pulley rpm, 1203 secondary pulley rpm (41.8 per km/h),
1204 engine rpm (TCM). Computed in power_calc(): hp = Nm x rpm / 7121; wheel torque ~ = Nm x converter factor x
ratio x 5.65 x 0.90 (final reduction measured with 225/65R17; efficiency and converter multiplication are
estimates, exact only when Locked); wheel hp ~ from wheel torque x wheel rpm. Best pull in the log: 157 hp at
~5400 rpm (spec 170 hp @ 6000). Shown on ALL / ENGINE (OIL · POWER column, ratio row in the CVT table) and on the
CVT tab (gear ratio tile). Read only off the main screen: ALL / ENGINE every 2nd round, CVT tab every 4th loop.
1106-1109 are 4 copies of one pedal-like value; 1215 / 1208 / 1224 move with torque but are not engine torque.
Oil pressure on the same drive: lowest hot idle (oil 93 C) 1285 mV, plateau ~3500 mV (~57 psi) above 3500 rpm;
the alert never fired and kept 285+ mV of margin at idle.

## Trouble codes after engine start: yellow badge + CODES tab (2026-11-10)
cvt_reader.py reads the stored codes once per engine start, 10 s after the engine is running (`_read_dtcs`):
ECM (7E0 / 7E8), TCM (7E1 / 7E9), BCM (745 / 765). Per module the first request that answers wins: `19 02 0D`
(UDS: failed now, pending or confirmed), then `18 02 FF 00` (KWP, Nissan), and for the ECM also OBD2 mode 03.
Read-only, never a clear. The scan freezes the gauges for about 1-2 s.
- Page gets `{"dtcs": [{mod, code, quiet}], "dtc_hist": [...], "dtc_scans": n}`. A code = the warning badge in
  YELLOW, no pulse (a red alert still wins). Codes in `DTC_QUIET` (hud_service.py: U1000, U1001) are "common":
  no badge, no banner, only on the CODES tab. Add the ones your car always stores there.
- Telemetry tabs are now ALL / DYNO / CODES (ENGINE and CVT are gone, their polling too). CODES: left = the last
  scan, right = code history (first / last time seen, on how many engine starts), kept in
  `~/.local/share/hud/dtcs.json`. Opening the telemetry page shows the codes in a yellow banner for 6 s.
- Not yet tested on the car: the IDs and services are the ones in tools/dtc_probe.py. After the first start check
  `journalctl -u hud-service | grep DTC` (one line per module and request: codes, "no codes", "no answer" or the refusal).
- Preview: `?demo=1&alert=dtc`. Self-check: `python3 dashboard/service/dtc_test.py`.

## New alerts: CVT fluid, battery (2026-11-10)
- CVT fluid >= 100 C (clears below 97). The main gauge and the telemetry bar are red from 100 too (was 110).
- Voltage (ECM 1103, now also read every 7 s on the main screen): below 11.7 V on two readings in a row with the
  engine running 10 s (clears at 11.8; smart charging sits at 12 V), or 15.0 V and up (clears below 14.8).
  The DID steps 0.08 V, so "below 11.7" means a reading of 11.68 or less.
- `?demo=1&alert=cvt|battery`.

## Cover art sent only when it changes (2026-11-10)
The once-a-second media push no longer carries the cover (50-100 kB of base64): hud_service leaves `art` out while
it is the same, hud.js keeps the cover it has. A new connection still gets it in its first message.
