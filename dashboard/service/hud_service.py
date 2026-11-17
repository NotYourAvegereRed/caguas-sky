#!/usr/bin/env python3
"""HUD service: reads what the phone is playing (BlueZ AVRCP) and serves it to the
HUD page over a local WebSocket. Also receives play/pause/next/prev from the touch buttons.

    ws://<host>:8766   server -> page: {"media": {...} | null}
                       page -> server: {"cmd": "toggle" | "play" | "pause" | "next" | "prev"}
                                       {"cmd": "vol", "db": -23.5}   (touch volume slider)
                       server -> page: {"vol": {db, mute, min_db, max_db} | null}  (CamillaDSP main volume)

media = {status, title, artist, album, position_ms, duration_ms, codec, art}
  art = data URI of the album cover from the Spotify Web API ("currently playing"), cached on disk.
  null while it is not found / not playing from Spotify / no internet (the page shows its placeholder).
  The once-a-second pushes leave "art" OUT while the cover is the same (the page keeps the one it has).

CVT data (cvt_reader.py, vLinker FS on USB, read-only except the CLEAR button below) is pushed separately, ~12 times a second:
    server -> page: {"cvt": {p_primary_target_mpa, p_primary_actual_mpa, p_secondary_target_mpa,
                             p_secondary_actual_mpa, p_line_target_mpa, temp_c, slip_rpm,
                             lockup: "open"|"engaging"|"releasing"|"locked"|"coast",
                             tcc_a (TCC solenoid current), lock_status, gear: "P"|"R"|"N"|"D", speed_kmh, rpm, hz} | null}
null = no adapter / car off. Kept apart from "media" so the cover image is not resent 12x a second.

Telemetry window (swipe left on the main screen):
    page -> server: {"cmd": "view", "view": "main" | "all" | "dyno" | "codes" | "dsp"}   what the screen shows,
                    so cvt_reader only polls what is needed (main = CVT + background alert reads)
    server -> page: {"eng": {rpm, coolant_c, oil_c, iat_c, fuel_c, eop_mv, eop_psi, eop_warn_mv, eop_crit_mv,
                             maf_gs, load_pct, timing_deg, stft_pct, ltft_pct, inj_ms, ecm_v, exhcam_deg,
                             vtc_pct, thr1_v, thr2_v, ped1_v, ped2_v, af_v, ac_v, fluid_wear, eng_hz, cvt_hz} | null}
                    {"alerts": [{id: "oil_pressure", mv, psi, rpm, min_mv} |
                                {id: "coolant"|"oil_temp"|"cvt_temp"|"battery", value}]}
                    {"dtcs": [{mod: "ECM"|"TCM"|"BCM", code: "P0420", quiet}], "dtc_scans": n,
                     "dtc_hist": [{mod, code, quiet, first, last, n}]}   trouble codes, read once per engine start.
                     quiet = in DTC_QUIET (common, harmless: no yellow badge). History in ~/.local/share/hud/dtcs.json
                     (HUD_DTC_FILE): first / last time seen and in how many scans.
    page -> server: {"cmd": "dtc_clear"}   hold-to-clear button on the CODES tab (only taken while that tab is open);
                    cvt_reader clears only with the car in P and stopped, then scans again
    server -> page: {"dtc_clear": "CODES CLEARED" | "NOT CLEARED · ..." | "STILL STORED · ..." | "NO CODES TO CLEAR"}
    On the main screen only eop_mv/rpm (1 s) and coolant/oil temp (7 s) are fresh; the rest is old or missing.

Screen (real backlight: RP2040 firmware v0.9 PWMs the Santek's backlight driver enable):
    this service writes /dev/shm/caguas-bl {"t", "bl": 0-100, "night": night_pct}; rp2040-link sends it to
    the RP2040 as 'BL n' / 'BLN n'. bl = 0 while the car is OFF / STANDBY (state from /dev/shm/caguas-car,
    written by rp2040-link), so the boot and the parked car stay black. IGN / CRANK: backlight on (once the page
    is connected) while the page holds pure black until the first welcome, so the welcome starts from black. The first RUN of
    each boot (once the page is connected) sends server -> page {"welcome": {name, greet}} and the screen comes
    up with it (the RP2040 fades 0 -> level in 1.2 s). Level = 100 by day, night_pct while the car's lights are
    on (ILL via rp2040-link -> /dev/shm/caguas-lights; a fresh '-test' file wins: rp2040-link.py test ill 1);
    a change must hold delay_s; an alert lifts it to 100. Page gets {"welcomed": bool} (holds the normal
    engine-start animation until the welcome ran). Bye: /usr/local/bin/caguas-bye (run by rp2040-link at
    shutdown) writes /dev/shm/caguas-bye -> server -> page {"bye": {name, greet}} -> page ends in black;
    rp2040-link then sends BL 0. Door opened with the engine off (PARK): the music fades, then the same bye,
    then the backlight fades to 0 (BYE_FADE_S) and stays off while the Pi waits for ACC to end; key ON again
    brings the screen back. Settings in ~/.config/hud/display.json (created with the defaults, re-read
    when it changes): {"night_pct": 40, "delay_s": 3, "name": "Zen", "greet": {"morning", "afternoon",
    "evening", "bye", "bye_night"}, "units": "imperial" | "metric"}. No RP2040 state for 25 s after start
    (bench) = screen on, no welcome.
Trip summary: server -> page {"car": state} on every change and {"trip": {start, end, duration_min,
    distance_km, avg_kmh, max_kmh, cvt_max_c, cvt_avg_c, oil_max_c, coolant_max_c, rpm_max, alerts}} when the
    engine stops (RUN -> EXT); the page shows it in place of the CVT panel while the car is in EXT / PARK.
    One CSV line per trip in ~/.local/share/hud/trips.csv (HUD_TRIPS_FILE).

Steering wheel + cluster (rogue_can.py, radio emulator on the AV CAN bus, local UDP JSON):
    rogue_can -> 127.0.0.1:5005 {"type":"swc","button":"VOL +"|"VOL -"|"NEXT"|"PREV"|"SOURCE"|...,
                                 "state":"pressed"|"repeat"|"released"} | {"type":"session","state":"up"|"down"}
      VOL +/-: CamillaDSP main volume (1 dB per press, 0.5 dB every ~100 ms while held, only when not muted)
      NEXT / PREV: phone next / previous (AVRCP)     SOURCE: play / pause
    this service -> 127.0.0.1:5006 {"cmd":"track","title","artist","album"} on every track change
      (and again when the cluster session comes up), so the cluster shows the song.

Env: HUD_BIND (default 0.0.0.0, use 127.0.0.1 once installed in the car), HUD_PORT (default 8766),
     HUD_ART (1/0, default 1), HUD_ART_DIR (default ~/.cache/hud-cover),
     HUD_SPOTIFY_FILE (default ~/.config/hud/spotify.json, created by spotify_login.py),
     HUD_SPOTIFY_API / HUD_SPOTIFY_ACCOUNTS (only changed for tests),
     HUD_OBD (1/0), HUD_OBD_PORT (see cvt_reader.py).
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from cvt_reader import CvtReader
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

try:
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # older websockets
    from websockets import serve
    from websockets import connect as ws_connect

BIND = os.environ.get("HUD_BIND", "0.0.0.0")
PORT = int(os.environ.get("HUD_PORT", "8766"))
ART_ON = os.environ.get("HUD_ART", "1") != "0"
ART_DIR = os.path.expanduser(os.environ.get("HUD_ART_DIR", "~/.cache/hud-cover"))
SPOTIFY_FILE = os.path.expanduser(os.environ.get("HUD_SPOTIFY_FILE", "~/.config/hud/spotify.json"))
SPOTIFY_API = os.environ.get("HUD_SPOTIFY_API", "https://api.spotify.com")
SPOTIFY_ACCOUNTS = os.environ.get("HUD_SPOTIFY_ACCOUNTS", "https://accounts.spotify.com")
ART_MIN_WIDTH = 300     # px; the HUD shows the cover at ~252 px
ART_MAX_TRIES = 6       # per track
ART_RETRY_S = int(os.environ.get("HUD_ART_RETRY_S", "45"))     # after a network error
ART_QUICK_S = float(os.environ.get("HUD_ART_QUICK_S", "3"))    # Spotify lagging behind the phone
DSP_WS = os.environ.get("HUD_DSP_WS", "ws://127.0.0.1:1234")   # CamillaDSP websocket (main volume)
VOL_MIN_DB, VOL_MAX_DB = -60.0, 0.0
DSP_CONFIGS = os.path.expanduser(os.environ.get("HUD_DSP_CONFIGS", "~/camilladsp/configs"))   # presets = *.yml here, not test_*
DSP_DEFAULT = "rogue_xover.yml"          # loaded if CamillaDSP ever starts with no config (statefile lost it, 2026-11-05)
SUB_FADER, SUB_MIN_DB, SUB_MAX_DB = 1, -12.0, 12.0   # Aux1 = the sub_level Volume filter in every preset
TONE = {"bass": "Bass", "treble": "Treble"}         # shelf filters in every preset (same names CamillaGUI's Equalizer uses)
TONE_MIN, TONE_MAX = -12.0, 12.0
DSP_TONE_FILE = os.path.expanduser(os.environ.get("HUD_DSP_TONE", "~/.config/hud/dsp.json"))  # {"bass": dB, "treble": dB}
LIGHTS_FILE = os.environ.get("HUD_LIGHTS_FILE", "/dev/shm/caguas-lights")
LUX_FILE = os.environ.get("HUD_LUX_FILE", "/dev/shm/caguas-lux")      # light_reader.py: screen level from the VEML7700
DISPLAY_FILE = os.path.expanduser(os.environ.get("HUD_DISPLAY_FILE", "~/.config/hud/display.json"))
LIGHTS_FRESH_S = 5.0
BL_FILE = os.environ.get("HUD_BL_FILE", "/dev/shm/caguas-bl")        # read by rp2040-link -> RP2040 'BL n'
CAR_FILE = os.environ.get("HUD_CAR_FILE", "/dev/shm/caguas-car")     # RP2040 state, written by rp2040-link
BYE_FILE = os.environ.get("HUD_BYE_FILE", "/dev/shm/caguas-bye")     # written by /usr/local/bin/caguas-bye
TRIPS_FILE = os.path.expanduser(os.environ.get("HUD_TRIPS_FILE", "~/.local/share/hud/trips.csv"))
DTC_FILE = os.path.expanduser(os.environ.get("HUD_DTC_FILE", "~/.local/share/hud/dtcs.json"))
DTC_QUIET = {"U1000", "U1001"}   # CAN communication history codes: common and harmless -> code history only, no badge
NO_CAR_GRACE_S = 25.0     # no RP2040 state after this long (bench): show the screen anyway
WELCOME_WAIT_S = 20.0
PAGE_SETTLE_S = 2.0       # page just connected (Chromium still loading / first paint): the welcome waits this long
CAM_SHOW_S = 8.0          # reverse camera stays on screen this long after leaving R (like the factory radio)
CAM_HOLD_S = 10.0         # and stays powered (warm) this long after leaving P / N / R
CAM_R_CONFIRM_S = 0.5     # R must hold this long before the camera shows: P -> D passes through R
PARK_BYE_DELAY_S = 1.5    # door opened (PARK): let the music fade first
BYE_SHOW_S = 4.5          # then "Good night, Zen" on screen this long
BYE_FADE_S = 2.5          # then the backlight fades to black (the Pi stays on until ACC goes away)     # car in RUN but no page connected: give up on the welcome and show the screen
SWC_ADDR = ("127.0.0.1", 5005)          # events from rogue_can.py
CLUSTER_ADDR = ("127.0.0.1", 5006)      # track text to rogue_can.py
SWC_STEP_DB, SWC_HOLD_STEP_DB = 1.0, 0.5
SWC_HOLD_DELAY_S = 0.4                  # repeats start counting after this long (a quick tap = 1 step)
PLAYER_IFACE = "org.bluez.MediaPlayer1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"

CODEC_NAMES = {"ldac": "LDAC", "aptx": "aptX", "aptx_hd": "aptX HD", "aptx_ll": "aptX LL",
               "aac": "AAC", "sbc": "SBC", "sbc_xq": "SBC XQ", "faststream": "FastStream",
               "opus_05": "Opus", "lc3": "LC3"}


def unwrap(v):
    """dbus-fast Variant (possibly nested in dicts/lists) -> plain python."""
    if isinstance(v, Variant):
        return unwrap(v.value)
    if isinstance(v, dict):
        return {k: unwrap(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [unwrap(x) for x in v]
    return v


def find_codec(obj):
    """Find the first "api.bluez5.codec" value anywhere in a pw-dump JSON tree."""
    if isinstance(obj, dict):
        if "api.bluez5.codec" in obj:
            return obj["api.bluez5.codec"]
        for x in obj.values():
            r = find_codec(x)
            if r:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = find_codec(x)
            if r:
                return r
    return None


def read_codec():
    """Active Bluetooth codec from PipeWire (blocking, run in an executor)."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=4).stdout
        c = find_codec(json.loads(out))
        return CODEC_NAMES.get(c, str(c).upper()) if c else None
    except Exception:
        return None


# ---------- cover art (Spotify Web API) ----------
def norm(s):
    """lowercase, no accents, no (..)/[..] parts, no 'feat' tails, only letters/digits/spaces."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def split_artists(artist):
    parts = re.split(r",|&|;|\bfeat\.?\b|\bft\.?\b|\bx\b|\by\b|\band\b", (artist or "").lower())
    return [a for a in (norm(p) for p in parts) if a]


def same_track(item, title, artist):
    """Does Spotify's 'currently playing' item match what the phone reports over AVRCP?"""
    t, rt = norm(title), norm(item.get("name"))
    if not t or not rt or not (t == rt or t in rt or rt in t):
        return False
    wanted = split_artists(artist)
    have = [norm(a.get("name")) for a in item.get("artists", [])]
    return not wanted or any(w in h or h in w for w in wanted for h in have if h)


def pick_image(images):
    """Smallest image at least ART_MIN_WIDTH wide, else the largest one."""
    imgs = [i for i in images if i.get("url")]
    if not imgs:
        return None
    big = sorted((i for i in imgs if (i.get("width") or 0) >= ART_MIN_WIDTH), key=lambda i: i["width"])
    return (big[0] if big else max(imgs, key=lambda i: i.get("width") or 0))["url"]


def sniff_mime(img):
    """image/jpeg or image/png from the first bytes, else None (rejects empty or non-image data)."""
    if len(img) > 200 and img[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(img) > 200 and img[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def data_uri(img):
    return f"data:{sniff_mime(img)};base64," + base64.b64encode(img).decode()


def http_request(method, url, headers=None, data=None, timeout=8, limit=3_000_000):
    """(status, body, headers). HTTP error statuses are returned, network errors raise."""
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "hud-service/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(limit + 1)
            status, hdrs = r.status, r.headers
    except urllib.error.HTTPError as e:
        body, status, hdrs = e.read(limit + 1), e.code, e.headers
    if len(body) > limit:
        raise ValueError("response too large")
    return status, body, hdrs


class SpotifyError(Exception):
    pass


class Spotify:
    """Minimal Spotify client: refresh-token (PKCE) -> access token -> currently-playing."""

    def __init__(self, path):
        self.path, self.cfg, self.token, self.expires = path, None, None, 0
        self.dead = False                 # refresh token rejected: needs spotify_login.py again
        self.warned = set()

    def warn_once(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            print(msg, flush=True)

    def configured(self):
        if self.dead:
            return False
        if self.cfg is None:
            try:
                with open(self.path) as f:
                    cfg = json.load(f)
                if cfg.get("client_id") and cfg.get("refresh_token"):
                    self.cfg = cfg
            except (OSError, ValueError):
                pass
        if self.cfg is None:
            self.warn_once("nocfg", f"Spotify not set up ({self.path} missing): covers disabled. Run spotify_login.py")
        return self.cfg is not None

    def access_token(self):
        if self.token and time.time() < self.expires - 30:
            return self.token
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.cfg["refresh_token"],
                                       "client_id": self.cfg["client_id"]}).encode()
        status, resp, _ = http_request("POST", f"{SPOTIFY_ACCOUNTS}/api/token", data=body,
                                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status == 400:
            self.dead = True
            self.warn_once("dead", "Spotify refused the saved login (revoked or expired). Run spotify_login.py again.")
            raise SpotifyError("refresh token rejected")
        if status != 200:
            raise SpotifyError(f"token endpoint HTTP {status}")
        d = json.loads(resp)
        self.token, self.expires = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        if d.get("refresh_token") and d["refresh_token"] != self.cfg["refresh_token"]:   # rotated
            self.cfg["refresh_token"] = d["refresh_token"]
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.cfg, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except OSError as e:
                print("could not save rotated Spotify token:", e, flush=True)
        return self.token

    def now_playing(self):
        """The currently playing track item (dict) or None when nothing / not a track. Raises on errors."""
        for attempt in (1, 2):
            status, body, hdrs = http_request("GET", f"{SPOTIFY_API}/v1/me/player/currently-playing",
                                              headers={"Authorization": f"Bearer {self.access_token()}"})
            if status == 401 and attempt == 1:
                self.token = None          # expired early: refresh once and retry
                continue
            break
        if status == 204:
            return None
        if status == 429:
            raise SpotifyError(f"rate limited, retry after {hdrs.get('Retry-After', '?')} s")
        if status != 200:
            raise SpotifyError(f"currently-playing HTTP {status}")
        d = json.loads(body or b"{}")
        item = d.get("item")
        return item if item and d.get("currently_playing_type", "track") == "track" else None


def find_cover(sp, title, artist):
    """Blocking. ("ok", image_bytes) | ("idle", None) nothing/another track on Spotify. Raises on errors."""
    item = sp.now_playing()
    if not item or not same_track(item, title, artist):
        return "idle", None
    url = pick_image(item.get("album", {}).get("images", []))
    if not url:
        return "idle", None
    status, img, _ = http_request("GET", url)
    if status != 200 or not sniff_mime(img):
        raise SpotifyError(f"cover download failed (HTTP {status})")
    return "ok", img


class ArtCache:
    """Covers found before, kept on disk so they show instantly and without internet."""

    def __init__(self, folder):
        self.dir = folder
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            print("art cache unavailable:", e, flush=True)

    @staticmethod
    def key(title, artist, album):
        raw = "|".join((norm(title), ",".join(split_artists(artist)), norm(album)))
        return hashlib.sha1(raw.encode()).hexdigest()[:20]

    def get(self, key):
        try:
            with open(os.path.join(self.dir, key + ".img"), "rb") as f:
                img = f.read()
            return data_uri(img) if sniff_mime(img) else None
        except OSError:
            return None

    def put(self, key, img):
        try:
            with open(os.path.join(self.dir, key + ".img"), "wb") as f:
                f.write(img)
        except OSError as e:
            print("art cache write failed:", e, flush=True)


def dsp_presets():
    """[{file, title}] for every *.yml in DSP_CONFIGS except test_*; title = the config's own title: line."""
    out = []
    try:
        names = sorted((f for f in os.listdir(DSP_CONFIGS) if f.endswith(".yml") and not f.startswith("test_")),
                       key=lambda f: (f != DSP_DEFAULT, f))       # Normal first
    except OSError:
        return out
    for f in names:
        title = None
        try:
            with open(os.path.join(DSP_CONFIGS, f)) as fh:
                for line in fh:
                    if line.startswith("title:"):
                        title = line.split(":", 1)[1].strip().strip("'\"")
                        break
        except OSError:
            continue
        out.append({"file": f, "title": title if title and title != "null" else f[:-4]})
    return out


def load_tone():
    try:
        with open(DSP_TONE_FILE) as f:
            d = json.load(f)
        return {k: max(TONE_MIN, min(TONE_MAX, float(d[k]))) for k in TONE if k in d}
    except (OSError, ValueError, TypeError):
        return {}


def save_tone(d):
    """live patches are not in the .yml: keep them here and re-apply after every (re)load"""
    os.makedirs(os.path.dirname(DSP_TONE_FILE), exist_ok=True)
    tmp = DSP_TONE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, DSP_TONE_FILE)


def load_dtc_hist():
    try:
        with open(DTC_FILE) as f:
            h = json.load(f)
        return [e for e in h if isinstance(e, dict) and e.get("mod") and e.get("code")] if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def log_dtcs(hist, found):
    """Add one scan to the code history (first / last time seen, number of engine starts) and save it."""
    if not found:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M")
    for d in found:
        e = next((h for h in hist if (h["mod"], h["code"]) == (d["mod"], d["code"])), None)
        if e is None:
            e = {"mod": d["mod"], "code": d["code"], "first": stamp, "n": 0}
            hist.append(e)
        e["last"], e["n"] = stamp, int(e.get("n") or 0) + 1
    try:
        os.makedirs(os.path.dirname(DTC_FILE), exist_ok=True)
        tmp = DTC_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f, indent=1)
        os.replace(tmp, DTC_FILE)
    except OSError as e:
        print(f"could not write {DTC_FILE}: {e}", flush=True)


class DspVolume:
    """CamillaDSP main volume over its websocket. The listening volume lives here (headunit style:
    phone at 100 %, PipeWire sink fixed at 1.0). rp2040-link remembers it across power cycles."""
    def __init__(self, url):
        self.url, self.ws = url, None
        self.lock = asyncio.Lock()
        self.db, self.mute = None, None
        self.warned = False

    async def _cmd(self, name, arg=None):
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if self.ws is None:
                        self.ws = await asyncio.wait_for(ws_connect(self.url), 2)
                        self.warned = False
                    await self.ws.send(json.dumps(name if arg is None else {name: arg}))
                    reply = json.loads(await asyncio.wait_for(self.ws.recv(), 2)).get(name, {})
                    return reply.get("value")
                except Exception as e:
                    try:
                        if self.ws:
                            await self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                    if attempt == 2 and not self.warned:
                        self.warned = True
                        print(f"CamillaDSP volume: no connection ({type(e).__name__}: {e})", flush=True)
            return None

    async def poll(self):
        """Refresh db/mute. Returns True when something changed."""
        db, mute = await self._cmd("GetVolume"), await self._cmd("GetMute")
        if db is None:
            changed = self.db is not None
            self.db = self.mute = None
            return changed
        db = round(float(db), 1)
        changed = (db, mute) != (self.db, self.mute)
        self.db, self.mute = db, bool(mute)
        return changed

    async def set_db(self, db):
        db = round(max(VOL_MIN_DB, min(VOL_MAX_DB, float(db))), 1)
        await self._cmd("SetVolume", db)
        self.db = db

    def state(self):
        return None if self.db is None else {"db": self.db, "mute": self.mute,
                                             "min_db": VOL_MIN_DB, "max_db": VOL_MAX_DB}


class Display:
    """Screen level for the Santek's real backlight (RP2040 PWM on the driver enable, firmware v0.9),
    plus the welcome (first RUN of each boot) and bye animations."""
    DEFAULTS = {"night_pct": 40, "delay_s": 3.0, "name": "Zen",
                "greet": {"morning": "Good morning", "afternoon": "Good afternoon", "evening": "Good evening",
                          "bye": "See you", "bye_night": "Good night"}, "units": "imperial"}
    DARK = ("OFF", "STANDBY")                    # backlight off: car open / Pi booting
    READY = ("IGN", "CRANK")                     # backlight on, page held black until the welcome (first boot)

    def __init__(self):
        self.cfg = json.loads(json.dumps(self.DEFAULTS))
        self.cfg_mtime = None
        self.night = None           # None = no ILL reading yet
        self.cand = None            # pending ILL change and when it started
        self.cand_t = 0.0
        self.t_start = time.monotonic()
        self.seen_car = False       # got a state from rp2040-link at least once
        self.car = None
        self.welcomed = False       # welcome done (or skipped) for this boot
        self.run_t = None           # first RUN seen, waiting for the page
        self.bl = 0
        self.bye_seen = None        # 't' of the last bye request handled
        self.park_t = None          # when the car went to PARK (door opened): bye, then screen off
        self.park_bye = False
        self.page_t = None          # since when a page is connected
        self.cam = 0                # reverse camera: 0 off, 1 warm (powered + capturing, hidden), 2 on screen
        self.cam_t = self.rev_t = -1e9
        self.r_since = None         # R (wire or gear) seen without a break since then
        self.ensure_cfg_file()

    # ---------- settings ----------
    def ensure_cfg_file(self):
        if os.path.exists(DISPLAY_FILE):
            return
        try:
            os.makedirs(os.path.dirname(DISPLAY_FILE), exist_ok=True)
            with open(DISPLAY_FILE, "w") as f:
                json.dump(self.DEFAULTS, f, indent=2)
            print(f"created {DISPLAY_FILE} with the defaults", flush=True)
        except OSError as e:
            print(f"could not create {DISPLAY_FILE}: {e}", flush=True)

    def load_cfg(self):
        try:
            mtime = os.stat(DISPLAY_FILE).st_mtime
        except OSError:
            mtime = None
        if mtime == self.cfg_mtime:
            return
        self.cfg_mtime = mtime
        cfg = json.loads(json.dumps(self.DEFAULTS))
        if mtime is not None:
            try:
                with open(DISPLAY_FILE) as f:
                    d = json.load(f)
                cfg["night_pct"] = max(5, min(100, int(d.get("night_pct", cfg["night_pct"]))))
                cfg["delay_s"] = max(0.0, min(30.0, float(d.get("delay_s", cfg["delay_s"]))))
                if isinstance(d.get("name"), str) and d["name"].strip():
                    cfg["name"] = d["name"].strip()[:24]
                if d.get("units") in ("imperial", "metric"):
                    cfg["units"] = d["units"]
                if isinstance(d.get("greet"), dict):
                    for k in cfg["greet"]:
                        v = d["greet"].get(k)
                        if isinstance(v, str) and v.strip():
                            cfg["greet"][k] = v.strip()[:32]
            except (OSError, ValueError, TypeError, AttributeError) as e:
                print(f"display.json ignored ({e}), using defaults", flush=True)
        if cfg != self.cfg:
            print(f"display settings: {cfg}", flush=True)
        self.cfg = cfg

    # ---------- inputs ----------
    @staticmethod
    def read_fresh(path, key, ok, test=True):
        for p in ((path + "-test", path) if test else (path,)):
            try:
                with open(p) as f:
                    d = json.load(f)
                if time.monotonic() - float(d.get("t", 0)) <= LIGHTS_FRESH_S and ok(d.get(key)):
                    return d[key]
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                pass
        return None

    def update_night(self, now):
        ill = self.read_fresh(LIGHTS_FILE, "ill", lambda v: v in (0, 1))
        if ill is None:
            return                                   # no fresh data: keep the current level
        want = bool(ill)
        if self.night is None:
            self.night = want                        # first reading: apply now
        elif want == self.night:
            self.cand = None
        elif self.cand != want:
            self.cand, self.cand_t = want, now
        elif now - self.cand_t >= self.cfg["delay_s"]:
            self.night, self.cand = want, None
            print(f"lights {'on' if want else 'off'}: screen {self.level()} %", flush=True)

    def level(self):
        # light sensor first (light_reader.py, fresh '-test' file wins); stale -> ILL + night_pct as before
        pct = self.read_fresh(LUX_FILE, "pct", lambda v: isinstance(v, (int, float)) and 0 <= v <= 100)
        if pct is not None:
            return int(pct)
        return self.cfg["night_pct"] if self.night else 100

    def greeting(self, bye=False):
        h, g = time.localtime().tm_hour, self.cfg["greet"]
        if bye:
            return g["bye_night"] if (h >= 18 or h < 5) else g["bye"]
        return g["morning"] if 5 <= h < 12 else g["afternoon"] if 12 <= h < 18 else g["evening"]

    def update_cam(self, gear, rev, now):
        """Camera powered and capturing in P / N (you always pass through one of them on the way to R), so the
        picture is already there in R. On screen with the reverse wire (RP2040 REV) or the TCM gear, whichever
        comes first, once it held CAM_R_CONFIRM_S. cam_relay.py powers the camera (R3, GPIO26) from the 'cam' in BL_FILE."""
        in_r = rev == 1 or gear == "R"
        self.r_since = (now if self.r_since is None else self.r_since) if in_r else None
        if in_r and now - self.r_since >= CAM_R_CONFIRM_S:
            self.rev_t = now
        show = now - self.rev_t < CAM_SHOW_S
        if show or in_r or gear in ("P", "N"):
            self.cam_t = now
        cam = 2 if show else 1 if now - self.cam_t < CAM_HOLD_S else 0
        changed, self.cam = cam != self.cam, cam
        return changed

    # ---------- the decision, every 0.25 s ----------
    def update(self, has_page, alert, gear=None):
        """Returns a list of messages for the page (welcome / bye / welcomed / cam)."""
        self.load_cfg()
        now = time.monotonic()
        self.update_night(now)
        out = []
        self.page_t = (self.page_t or now) if has_page else None
        if self.update_cam(gear, self.read_fresh(LIGHTS_FILE, "rev", lambda v: v in (0, 1)), now):
            out.append({"cam": self.cam})
            print(f"camera {('off', 'warm', 'on screen')[self.cam]}", flush=True)
        car = self.read_fresh(CAR_FILE, "state", lambda v: isinstance(v, str), test=False)
        if car is not None:
            self.seen_car = True
        self.car = car
        lvl = 100 if alert else self.level()        # an alert lifts the screen to full
        bl = self.bl
        if car is None:
            if not self.seen_car and now - self.t_start > NO_CAR_GRACE_S:
                bl = lvl                                 # bench, no RP2040: just show the screen
                if not self.welcomed:
                    self.welcomed = True; out.append({"welcomed": True})
            elif not self.seen_car:
                bl = 0                                   # booting: wait for the link
            # link restarting: keep the last level
        elif car in self.DARK:
            bl = 0
        elif car in self.READY:
            # key ON: light the screen so the welcome can start from black. Before the first welcome the page
            # is held black ({"welcomed": false}); without a page (still booting) keep it off.
            bl = lvl if (self.welcomed or has_page) else 0
        elif car == "SHUTDOWN" and self.bl == 0:
            bl = 0                                       # already dark (bye at the door, or never lit): stay dark
        elif car == "PARK":
            # driver's door opened with the engine off: goodbye, then the screen goes dark until ACC ends
            self.park_t = self.park_t or now
            el = now - self.park_t
            if el >= PARK_BYE_DELAY_S and not self.park_bye:
                self.park_bye = True
                out.append({"bye": {"name": self.cfg["name"], "greet": self.greeting(bye=True)}})
                print(f"bye (door): {self.greeting(bye=True)}, {self.cfg['name']}", flush=True)
            fade0 = PARK_BYE_DELAY_S + BYE_SHOW_S
            k = 0.0 if el < fade0 else min(1.0, (el - fade0) / BYE_FADE_S)
            bl = round(self.level() * (1 - k) ** 2)
            if not self.welcomed:
                self.welcomed = True; out.append({"welcomed": True})
        elif not self.welcomed:
            if car == "RUN":
                self.run_t = self.run_t or now
                if has_page and now - self.page_t >= PAGE_SETTLE_S:
                    self.welcomed = True; bl = lvl
                    out.append({"welcome": {"name": self.cfg["name"], "greet": self.greeting()}})
                    out.append({"welcomed": True})
                    print(f"welcome: {self.greeting()}, {self.cfg['name']}", flush=True)
                elif now - self.run_t > WELCOME_WAIT_S:
                    self.welcomed = True; bl = lvl; out.append({"welcomed": True})
                    print("welcome skipped: no page connected", flush=True)
                else:
                    bl = 0                               # wait for the page so the welcome is seen
            else:                                        # booted straight into EXT / PARK / SHUTDOWN
                self.welcomed = True; bl = lvl; out.append({"welcomed": True})
        else:
            bl = lvl
        if car != "PARK":
            self.park_t = None; self.park_bye = False
        if bl != self.bl:
            if car != "PARK" or bl == 0:             # the PARK fade changes every tick: log only the end
                print(f"screen {self.bl} -> {bl} % (car {car})", flush=True)
            self.bl = bl
        try:
            tmp = BL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"t": now, "bl": self.bl, "night": self.cfg["night_pct"], "cam": int(self.cam > 0)}, f)
            os.replace(tmp, BL_FILE)
        except OSError as e:
            print(f"could not write {BL_FILE}: {e}", flush=True)
        t_bye = self.read_fresh(BYE_FILE, "t", lambda v: isinstance(v, (int, float)), test=False)
        if t_bye is not None and t_bye != self.bye_seen:
            self.bye_seen = t_bye
            out.append({"bye": {"name": self.cfg["name"], "greet": self.greeting(bye=True)}})
            print(f"bye: {self.greeting(bye=True)}, {self.cfg['name']}", flush=True)
        return out


class Trip:
    """Drive summary: RUN -> EXT. Speed, CVT temp and rpm from the CVT stream (main screen polling, nothing
    extra asked of the car), oil / coolant from the background alert reads. Shown in place of the CVT panel
    while the engine is off (EXT / PARK); one CSV line per trip in TRIPS_FILE (~100 bytes each)."""
    FIELDS = ["start", "end", "duration_min", "distance_km", "avg_kmh", "max_kmh", "cvt_max_c", "cvt_avg_c",
              "oil_max_c", "coolant_max_c", "rpm_max", "alerts"]
    MIN_S, MIN_KM = 60.0, 0.1     # shorter than both (a stall, a test crank): not a trip

    def __init__(self):
        self.cur = None
        self.last = None            # summary of the last finished trip (shown in EXT / PARK)
        self.prev_car = None
        self.alert_on = False

    def _new(self):
        return {"t0": time.monotonic(), "start": time.time(), "km": 0.0, "max_kmh": 0, "cvt_sum": 0.0,
                "cvt_s": 0.0, "cvt_max": None, "oil_max": None, "cool_max": None, "rpm_max": 0, "alerts": 0,
                "last_t": None}

    @staticmethod
    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def cvt(self, d):
        c = self.cur
        if c is None or d is None:
            return
        now = time.monotonic()
        dt = 0.0 if c["last_t"] is None else min(1.0, now - c["last_t"])   # gaps (no data) are not driven
        c["last_t"] = now
        v, t, r = self._num(d.get("speed_kmh")), self._num(d.get("temp_c")), self._num(d.get("rpm"))
        if v is not None:
            c["km"] += v * dt / 3600.0
            c["max_kmh"] = max(c["max_kmh"], v)
        if t is not None:
            c["cvt_sum"] += t * dt; c["cvt_s"] += dt
            c["cvt_max"] = t if c["cvt_max"] is None else max(c["cvt_max"], t)
        if r is not None:
            c["rpm_max"] = max(c["rpm_max"], r)

    def eng(self, e):
        c = self.cur
        if c is None or not e:
            return
        for key, slot in (("oil_c", "oil_max"), ("coolant_c", "cool_max")):
            v = self._num(e.get(key))
            if v is not None:
                c[slot] = v if c[slot] is None else max(c[slot], v)

    def alerts(self, on):
        if self.cur is not None and on and not self.alert_on:
            self.cur["alerts"] += 1
        self.alert_on = on

    def car(self, state):
        """Feed the car state; returns the summary when a trip just ended."""
        out = None
        if state == "RUN" and self.cur is None:
            self.cur = self._new()
        elif state in ("EXT", "PARK", "SHUTDOWN") and self.cur is not None:
            out = self._finish()
        self.prev_car = state
        return out

    def _finish(self):
        c, self.cur = self.cur, None
        dur = time.monotonic() - c["t0"]
        if dur < self.MIN_S and c["km"] < self.MIN_KM:
            return None
        hm = lambda ts: time.strftime("%H:%M", time.localtime(ts))
        end = time.time()
        r1 = lambda v: None if v is None else round(v, 1)
        summ = {"start": hm(c["start"]), "end": hm(end), "duration_min": round(dur / 60, 1),
                "distance_km": round(c["km"], 2), "avg_kmh": round(c["km"] / (dur / 3600), 1) if dur > 0 else 0,
                "max_kmh": round(c["max_kmh"]), "cvt_max_c": r1(c["cvt_max"]),
                "cvt_avg_c": r1(c["cvt_sum"] / c["cvt_s"]) if c["cvt_s"] > 0 else None,
                "oil_max_c": r1(c["oil_max"]), "coolant_max_c": r1(c["cool_max"]),
                "rpm_max": round(c["rpm_max"]), "alerts": c["alerts"]}
        self.last = summ
        print(f"trip: {summ}", flush=True)
        try:
            os.makedirs(os.path.dirname(TRIPS_FILE), exist_ok=True)
            new = not os.path.exists(TRIPS_FILE)
            with open(TRIPS_FILE, "a") as f:
                if new:
                    f.write(",".join(self.FIELDS) + "\n")
                row = dict(summ, start=time.strftime("%Y-%m-%d %H:%M", time.localtime(c["start"])),
                           end=time.strftime("%Y-%m-%d %H:%M", time.localtime(end)))
                f.write(",".join("" if row[k] is None else str(row[k]) for k in self.FIELDS) + "\n")
        except OSError as e:
            print(f"could not write {TRIPS_FILE}: {e}", flush=True)
        return summ


class Hud:
    def __init__(self):
        self.bus = None
        self.clients = set()
        self.player = None          # object path of the current MediaPlayer1
        self.status = "stopped"
        self.track = {}
        self.base_pos = 0           # ms at base_ts
        self.base_ts = time.monotonic()
        self.codec = None
        self.dirty = asyncio.Event()
        self.cache = ArtCache(ART_DIR) if ART_ON else None
        self.spotify = Spotify(SPOTIFY_FILE)
        self.art_key = None         # key of the track the current art belongs to
        self.art_uri = None
        self.art_tries = {}         # key -> (attempts, next_try_monotonic)
        self.art_sent = False       # cover in the last broadcast (False = none sent yet)
        self.dtc_hist, self.clear_seen = load_dtc_hist(), 0
        self.cvt = CvtReader()
        self.vol = DspVolume(DSP_WS)
        self.view = "main"            # what the page shows (the DSP page polls levels only while open)
        self.dsp_dirty = asyncio.Event()
        self.tone_reapply = True        # saved bass / treble go back in after start, preset change, reload
        self.display = Display()
        self.trip = Trip()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent_track = None
        self.swc_t = 0.0             # when the current volume button was pressed

    # ---------- state -> JSON ----------
    def position_ms(self):
        pos = self.base_pos
        if self.status == "playing":
            pos += int((time.monotonic() - self.base_ts) * 1000)
        dur = self.track.get("Duration") or 0
        return min(pos, dur) if dur else pos

    def media(self):
        if not self.player:
            return None
        st = "playing" if self.status in ("playing", "forward-seek", "reverse-seek") else \
             "paused" if self.status == "paused" else "stopped"
        return {
            "status": st,
            "title": self.track.get("Title") or None,
            "artist": self.track.get("Artist") or None,
            "album": self.track.get("Album") or None,
            "position_ms": self.position_ms(),
            "duration_ms": self.track.get("Duration") or 0,
            "codec": self.codec,
            "art": self.art_uri,
        }

    def eng(self):
        e = self.cvt.latest_eng()
        if e is not None:
            e.update(eng_hz=round(self.cvt.eng_hz, 1), cvt_hz=round(self.cvt.hz, 1))
        return e

    def dtc_state(self):
        q = lambda codes: [dict(d, quiet=d["code"] in DTC_QUIET) for d in codes]
        return {"dtcs": q(self.cvt.dtcs()), "dtc_hist": q(self.dtc_hist), "dtc_scans": self.cvt.dtc_version}

    def snapshot(self, art=True):
        """art=False: leave the cover out (it has not changed; it is 50-100 kB of base64)."""
        media = self.media()
        if media and not art:
            del media["art"]
        return json.dumps({"media": media, "cvt": self.cvt.latest(), "vol": self.vol.state(),
                           "eng": self.eng(), "alerts": self.cvt.alerts(), **self.dtc_state(), "welcomed": self.display.welcomed,
                           "car": self.display.car, "cam": self.display.cam, "trip": self.trip.last, "units": self.display.cfg["units"]})

    # ---------- state updates ----------
    def set_position(self, ms):
        self.base_pos, self.base_ts = int(ms), time.monotonic()

    def apply_props(self, props):
        """props: plain dict of MediaPlayer1 properties (changed or full)."""
        if "Track" in props and isinstance(props["Track"], dict):
            new = props["Track"]
            if (new.get("Title"), new.get("Artist"), new.get("Duration")) != \
               (self.track.get("Title"), self.track.get("Artist"), self.track.get("Duration")):
                self.set_position(0)
            self.track = new
            self.refresh_art()
            self.send_cluster_track()
        if "Status" in props:
            self.set_position(self.position_ms())   # freeze/continue from the current spot
            self.status = props["Status"]
        if "Position" in props:
            self.set_position(props["Position"])
        self.dirty.set()

    def set_player(self, path, props):
        self.player = path
        self.status, self.track = "stopped", {}
        self.set_position(0)
        self.apply_props(props)

    def clear_player(self):
        self.player, self.status, self.track = None, "stopped", {}
        self.codec = None
        self.art_key, self.art_uri = None, None
        self.dirty.set()

    # ---------- steering wheel + cluster (rogue_can.py) ----------
    def send_cluster_track(self, force=False):
        t = (self.track.get("Title") or "", self.track.get("Artist") or "", self.track.get("Album") or "")
        if not t[0] or (t == self.sent_track and not force):
            return
        self.sent_track = t
        try:
            self.udp.sendto(json.dumps({"cmd": "track", "title": t[0], "artist": t[1], "album": t[2]}).encode(),
                            CLUSTER_ADDR)
        except OSError as e:
            print(f"cluster text: {e}", flush=True)

    async def on_swc(self, ev):
        if ev.get("type") == "session" and ev.get("state") == "up":
            self.send_cluster_track(force=True)
            return
        if ev.get("type") != "swc":
            return
        b, st = ev.get("button"), ev.get("state")
        now = time.monotonic()
        if b in ("VOL +", "VOL -") and st in ("pressed", "repeat"):
            if st == "pressed":
                self.swc_t, step = now, SWC_STEP_DB
            elif now - self.swc_t >= SWC_HOLD_DELAY_S:
                step = SWC_HOLD_STEP_DB
            else:
                return
            await self.vol.poll()
            if self.vol.db is None or self.vol.mute:      # car off / fading: buttons do nothing
                return
            await self.vol.set_db(self.vol.db + (step if b == "VOL +" else -step))
            await self.broadcast_vol()
        elif st == "pressed" and b in ("NEXT", "PREV", "SOURCE"):
            await self.command({"NEXT": "next", "PREV": "prev", "SOURCE": "toggle"}[b])

    # ---------- cover art ----------
    def refresh_art(self):
        """Called when the track changes: use the cache, or ask Spotify."""
        if not self.cache:
            return
        title, artist, album = (self.track.get(k) for k in ("Title", "Artist", "Album"))
        if not title:
            self.art_key, self.art_uri = None, None
            return
        key = ArtCache.key(title, artist, album)
        if key == self.art_key:
            return
        self.art_key, self.art_uri = key, None
        hit = self.cache.get(key)
        if hit:
            self.art_uri = hit
        else:
            asyncio.ensure_future(self.lookup_art(key, title, artist))

    async def lookup_art(self, key, title, artist):
        if not self.spotify.configured():
            return
        loop = asyncio.get_running_loop()
        n = self.art_tries.get(key, (0, 0))[0] + 1
        try:
            status, img = await loop.run_in_executor(None, find_cover, self.spotify, title, artist)
        except Exception as e:      # no internet, rate limit, token problem...
            self.art_tries[key] = (n, time.monotonic() + ART_RETRY_S)
            print(f"cover lookup failed ({n}/{ART_MAX_TRIES}): {type(e).__name__}: {e}", flush=True)
            return
        if status == "ok":
            self.art_tries.pop(key, None)
            self.cache.put(key, img)
            if key == self.art_key:
                self.art_uri = data_uri(img)
                self.dirty.set()
            print(f"cover: {title!r} / {artist!r} -> ok", flush=True)
        else:
            # Spotify may be a moment behind the phone, or the music is not from Spotify: try again soon, a few times
            self.art_tries[key] = (n, time.monotonic() + (ART_QUICK_S if n < 4 else ART_RETRY_S))
            print(f"cover: {title!r} / {artist!r} -> not on Spotify right now ({n}/{ART_MAX_TRIES})", flush=True)

    def retry_art(self):
        """Retry the current track's lookup if it failed or came back empty."""
        key = self.art_key
        if not key or self.art_uri or key not in self.art_tries:
            return
        n, at = self.art_tries[key]
        if n < ART_MAX_TRIES and time.monotonic() >= at:
            self.art_tries[key] = (n, time.monotonic() + 3600)   # block until this attempt reports back
            asyncio.ensure_future(self.lookup_art(key, self.track.get("Title"), self.track.get("Artist")))

    # ---------- D-Bus ----------
    async def call(self, **kw):
        reply = await self.bus.call(Message(**kw))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")
        return reply

    async def find_players(self):
        reply = await self.call(destination="org.bluez", path="/", interface=OM_IFACE,
                                member="GetManagedObjects")
        best = None
        for path, ifaces in unwrap(reply.body[0]).items():
            if PLAYER_IFACE in ifaces:
                props = ifaces[PLAYER_IFACE]
                if best is None or props.get("Status") == "playing":
                    best = (path, props)
        if best:
            self.set_player(*best)
        else:
            self.clear_player()

    def on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return None
        try:
            if msg.interface == PROPS_IFACE and msg.member == "PropertiesChanged":
                iface, changed, _ = msg.body
                if iface == PLAYER_IFACE:
                    if self.player is None:
                        asyncio.ensure_future(self.find_players())
                    elif msg.path == self.player:
                        self.apply_props(unwrap(changed))
            elif msg.interface == OM_IFACE and msg.member == "InterfacesAdded":
                path, ifaces = msg.body
                if PLAYER_IFACE in ifaces and self.player is None:
                    self.set_player(path, unwrap(ifaces[PLAYER_IFACE]))
            elif msg.interface == OM_IFACE and msg.member == "InterfacesRemoved":
                path, ifaces = msg.body
                if path == self.player and PLAYER_IFACE in ifaces:
                    self.clear_player()
                    asyncio.ensure_future(self.find_players())   # another phone may be connected
        except Exception as e:
            print("signal error:", e, flush=True)
        return None

    async def connect_bus(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.add_message_handler(self.on_message)
        for rule in (
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path_namespace='/org/bluez'",
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'",
        ):
            await self.call(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                            interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule])
        await self.find_players()

    async def command(self, cmd, msg=None):
        if cmd == "view":                      # {"cmd": "view", "view": "main"|"all"|"dyno"|"codes"|"dsp"}
            self.view = (msg or {}).get("view")
            self.cvt.set_view(self.view if self.view in ("all", "dyno") else "main")   # DSP / codes: OBD reads like the main screen
            self.dsp_dirty.set()
            return
        if cmd == "dtc_clear":                 # the reader thread checks P / stopped and answers {"dtc_clear": text}
            if self.view == "codes":
                self.cvt.request_clear()
            return
        if cmd == "dsp_preset":                # {"cmd": "dsp_preset", "file": "night.yml"}: only files that are presets
            f = os.path.basename(str((msg or {}).get("file", "")))
            if any(p["file"] == f for p in dsp_presets()):
                await self.vol._cmd("SetConfigFilePath", os.path.join(DSP_CONFIGS, f))
                await self.vol._cmd("Reload")
                self.tone_reapply = True
                print(f"DSP preset: {f}", flush=True)
            self.dsp_dirty.set()
            return
        if cmd == "dsp_adj":                   # {"cmd": "dsp_adj", "k": "sub"|"bass"|"treble", "step": 1 | -1}: 1 dB steps
            k = (msg or {}).get("k")
            try:
                step = max(-1.0, min(1.0, float((msg or {}).get("step", 0))))
            except (TypeError, ValueError):
                return
            if k == "sub":
                cur = await self.vol._cmd("GetFaderVolume", SUB_FADER)
                if cur:
                    db = round(max(SUB_MIN_DB, min(SUB_MAX_DB, float(cur[1]) + step)), 1)
                    await self.vol._cmd("SetFaderVolume", [SUB_FADER, db])
            elif k in TONE:
                cur = (await self.dsp_tone()).get(k)
                if cur is not None:
                    db = round(max(TONE_MIN, min(TONE_MAX, cur + step)), 1)
                    await self.vol._cmd("PatchConfig", {"filters": {TONE[k]: {"parameters": {"gain": db}}}})
                    saved = load_tone(); saved[k] = db; save_tone(saved)
            self.dsp_dirty.set()
            return
        if cmd == "vol":                       # {"cmd": "vol", "db": -23.5} from the touch slider
            await self.vol.set_db((msg or {}).get("db"))
            await self.broadcast_vol(skip=None)
            return
        if not self.player:
            return
        member = {"play": "Play", "pause": "Pause", "next": "Next", "prev": "Previous"}.get(cmd)
        if cmd == "toggle":
            member = "Pause" if self.status == "playing" else "Play"
        if not member:
            return
        try:
            await self.call(destination="org.bluez", path=self.player, interface=PLAYER_IFACE, member=member)
        except Exception as e:
            print(f"{cmd} failed: {e}", flush=True)

    # ---------- background loops ----------
    async def poll_loop(self):
        """Resync position every 3 s (BlueZ does not push it continuously) and codec every 6 s."""
        n = 0
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(3)
            n += 1
            self.retry_art()
            if self.player:
                try:
                    r = await self.call(destination="org.bluez", path=self.player, interface=PROPS_IFACE,
                                        member="Get", signature="ss", body=[PLAYER_IFACE, "Position"])
                    self.set_position(unwrap(r.body[0]))
                except Exception:
                    pass
                if n % 2 == 0:
                    c = await loop.run_in_executor(None, read_codec)
                    if c != self.codec:
                        self.codec = c
                        self.dirty.set()

    async def broadcast_loop(self):
        """Push on every change, and at least once a second so the progress bar moves."""
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.dirty.clear()
            if self.clients:
                msg = self.snapshot(art=self.art_uri != self.art_sent)
                self.art_sent = self.art_uri
                await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)))
            await asyncio.sleep(0.15)   # coalesce bursts of signals

    async def dsp_status(self):
        v = self.vol
        state = await v._cmd("GetState")
        if state is None:
            return {"state": None}
        path = await v._cmd("GetConfigFilePath")
        if not path:                                   # started with no config: load the default (statefile keeps it)
            path = os.path.join(DSP_CONFIGS, DSP_DEFAULT)
            await v._cmd("SetConfigFilePath", path)
            await v._cmd("Reload")
            print(f"CamillaDSP had no config: loaded {DSP_DEFAULT}", flush=True)
            state = await v._cmd("GetState")
            self.tone_reapply = True
        tone = await self.dsp_tone()
        if self.tone_reapply:
            self.tone_reapply = False
            for k, db in load_tone().items():
                if tone.get(k) is not None and tone[k] != db:
                    await v._cmd("PatchConfig", {"filters": {TONE[k]: {"parameters": {"gain": db}}}})
                    tone[k] = db
        sub = await v._cmd("GetFaderVolume", SUB_FADER)
        load = await v._cmd("GetProcessingLoad")
        return {"state": state, "config": os.path.basename(path), "presets": dsp_presets(),
                "rate": await v._cmd("GetCaptureRate"), "load": round(load, 1) if load is not None else None,
                "clipped": await v._cmd("GetClippedSamples"), "codec": self.codec, "vol": v.state(),
                "adj": {"bass": tone.get("bass"), "treble": tone.get("treble"), "sub": sub[1] if sub else None},
                "adj_range": {"bass": [TONE_MIN, TONE_MAX], "treble": [TONE_MIN, TONE_MAX], "sub": [SUB_MIN_DB, SUB_MAX_DB]}}

    async def dsp_tone(self):
        """{"bass": dB | None, "treble": dB | None} from the running config (None: preset has no such filter)"""
        try:
            cfg = json.loads(await self.vol._cmd("GetConfigJson") or "{}")
        except ValueError:
            return {}
        flt = cfg.get("filters") or {}
        return {k: (flt.get(name) or {}).get("parameters", {}).get("gain") for k, name in TONE.items()}

    async def dsp_loop(self):
        r1 = lambda xs: [round(x, 1) for x in xs or []]
        last_status = -1e9
        while True:
            is_open = self.view == "dsp" and self.clients
            try:
                await asyncio.wait_for(self.dsp_dirty.wait(), timeout=0.1 if is_open else 1.0)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if self.dsp_dirty.is_set() or now - last_status >= (1.0 if is_open else 10.0):
                self.dsp_dirty.clear()
                last_status = now
                st = await self.dsp_status()
                if is_open:
                    msg = json.dumps({"dsp": st})
                    await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)))
            if is_open:
                lv = await self.vol._cmd("GetSignalLevels")
                if lv:
                    msg = json.dumps({"dsp_lv": {"in": r1(lv.get("capture_rms")), "in_pk": r1(lv.get("capture_peak")),
                                                 "out": r1(lv.get("playback_rms")), "out_pk": r1(lv.get("playback_peak"))}})
                    await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)))

    async def cvt_loop(self):
        """Push CVT data as it arrives (~12/s), or one null when it goes away."""
        last = "unset"
        while True:
            await asyncio.sleep(1 / 15)
            d = self.cvt.latest()
            if d == last:
                continue
            last = d
            self.trip.cvt(d)
            if self.clients:
                msg = json.dumps({"cvt": d})
                await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)))

    async def eng_loop(self):
        """Push engine data when it changes (rate set by the view), alerts as soon as they change."""
        last_v, last_a = -1, -1
        while True:
            await asyncio.sleep(0.1)
            if not self.clients:
                continue
            msgs = []
            if self.cvt.eng_version != last_v:
                last_v = self.cvt.eng_version
                msgs.append(json.dumps({"eng": self.eng()}))
            if self.cvt.alert.version != last_a:
                last_a = self.cvt.alert.version
                msgs.append(json.dumps({"alerts": self.cvt.alerts()}))
            for msg in msgs:
                await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)))

    async def display_loop(self):
        """Screen level for the RP2040, welcome / bye for the page."""
        while True:
            try:
                alerts_on = bool(self.cvt.alerts())
                prev_car = self.display.car
                msgs = self.display.update(bool(self.clients), alerts_on, (self.cvt.latest() or {}).get("gear"))
                car = self.display.car
                self.trip.eng(self.cvt.latest_eng()); self.trip.alerts(alerts_on)
                summ = self.trip.car(car) if car is not None else None
                if car != prev_car:
                    msgs.append({"car": car})
                if summ:
                    msgs.append({"trip": summ, "units": self.display.cfg["units"]})
                scans = self.cvt.pop_scans()                       # scans finished: history, then the page
                for found in scans:
                    log_dtcs(self.dtc_hist, found)
                if scans:
                    msgs.append(self.dtc_state())
                if self.cvt.clear_version != self.clear_seen:
                    self.clear_seen = self.cvt.clear_version
                    msgs.append({"dtc_clear": self.cvt.clear_msg})
            except Exception as e:
                print(f"display: {e}", flush=True); msgs = []
            for m in msgs:
                if self.clients:
                    msg = json.dumps(m)
                    await asyncio.gather(*(self._send(c, msg) for c in list(self.clients)))
            await asyncio.sleep(0.25)

    async def vol_loop(self):
        """Follow the volume (fades from rp2040-link, CamillaGUI, the slider on another screen)."""
        while True:
            if await self.vol.poll():
                await self.broadcast_vol()
            await asyncio.sleep(0.3)

    async def broadcast_vol(self, skip=None):
        if self.clients:
            msg = json.dumps({"vol": self.vol.state()})
            await asyncio.gather(*(self._send(c, msg) for c in list(self.clients) if c is not skip))

    async def _send(self, ws, msg):
        try:
            await ws.send(msg)
        except Exception:
            self.clients.discard(ws)

    async def ws_handler(self, ws):
        self.clients.add(ws)
        try:
            await ws.send(self.snapshot())
            async for raw in ws:
                try:
                    m = json.loads(raw)
                    await self.command(m.get("cmd"), m)
                except Exception:
                    pass
        finally:
            self.clients.discard(ws)
            if not self.clients:
                self.cvt.set_view("main")      # screen gone: back to the cheap polling


async def main():
    hud = Hud()
    while True:   # wait for bluetoothd if it is not up yet
        try:
            await hud.connect_bus()
            break
        except Exception as e:
            print("waiting for BlueZ:", e, flush=True)
            await asyncio.sleep(3)
    hud.cvt.start()

    class SwcProto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            try:
                asyncio.ensure_future(hud.on_swc(json.loads(data)))
            except ValueError:
                pass

    await asyncio.get_running_loop().create_datagram_endpoint(SwcProto, local_addr=SWC_ADDR)
    async with serve(hud.ws_handler, BIND, PORT):
        print(f"HUD service on ws://{BIND}:{PORT}", flush=True)
        await asyncio.gather(hud.poll_loop(), hud.broadcast_loop(), hud.cvt_loop(), hud.vol_loop(), hud.eng_loop(), hud.display_loop(), hud.dsp_loop(),
                             asyncio.Future())


if __name__ == "__main__":
    asyncio.run(main())
