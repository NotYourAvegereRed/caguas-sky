#!/usr/bin/env python3
"""
rogue_can.py - Factory-radio emulator for the Rogue AV bus, Raspberry Pi version (SocketCAN).
Same logic as rogue_radio_emu.py (tested on the laptop), using the CANable as can0.

What it does:
  * Emulates the radio (0x603 / 0x628): heartbeats, session join, ACKs, flow control
  * Decodes steering wheel buttons (0x683) and power state (0x6A8)
  * Sends track text to the cluster
  * Talks to the rest of Caguas-sky over local UDP (JSON), so other services stay separate:
      events OUT -> 127.0.0.1:5005   {"type":"swc","button":"VOL +","state":"pressed"}
                                     {"type":"swc","button":"VOL +","state":"repeat"}   (every ~100 ms while held)
                                     {"type":"swc","button":"VOL +","state":"released"}
                                     {"type":"power","state":"ACC"|"IGNITION ON"|"RADIO OFF ..."}
                                     {"type":"session","state":"up"|"down"}
      commands IN <- 127.0.0.1:5006  {"cmd":"track","title":"...","artist":"...","album":"..."}

Run (needs root to configure can0):
  sudo python3 rogue_can.py --listen          decode only (factory radio plugged in, lever 2 OFF)
  sudo python3 rogue_can.py                   emulate radio (radio UNPLUGGED, lever 2 ON)
  python3 rogue_can.py text "Title" "Artist" ["Album"]     send text to a running instance

Setup: sudo apt install -y can-utils python3-can
"""

import argparse
import datetime
import json
import os
import socket
import subprocess
import sys
import time

import can

IFACE = "can0"
BITRATE = 500_000
EVENT_ADDR = ("127.0.0.1", 5005)
DOOR_FILE = "/dev/shm/caguas-door"      # read by rp2040-link (PARK): {"t": monotonic, "door": 0/1}
CMD_ADDR = ("127.0.0.1", 5006)

HEARTBEAT = [0xF0, 0x80, 0x0A, 0x01, 0xFF, 0xFF, 0xFF, 0xFF]
HB_PERIOD = 0.5
BUS_QUIET_S = 1.5
RETRY_S = 0.3
MIN_GAP_S = 0.010
MAX_TRIES = 5
JOIN_RETRY_S = 2.0
CAN_ERR_BUSOFF = 0x00000040

SWC = {0x01: "SOURCE", 0x02: "NEXT", 0x03: "PREV", 0x04: "ANSWER",
       0x06: "VOL -", 0x07: "VOL +", 0x08: "END CALL"}
POWER = {0x02: "RADIO OFF", 0x03: "ACC", 0x04: "IGNITION ON"}
MODULES = {0x683, 0x6A8, 0x6A9}

def clock_payload(h24, minute):
    """Clock for 0x683 (the radio sends it on join and on every minute change):
    40 0D <02 AM | 03 PM> <hour BCD 0-23, 'F' = blank tens digit> <minute BCD>.
    Confirmed on the car: 16:33 -> 4:33 PM, 20:00 -> 8:00 PM."""
    ampm = 0x02 if h24 < 12 else 0x03
    hb = (0xF0 | h24) if h24 < 10 else int(str(h24), 16)
    return [0x40, 0x0D, ampm, hb, int(f"{minute:02d}", 16)]


BT_SOURCE_EMPTY = [0x22, 0x0D, 0x01, 0x01, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x05, 0x00,
                   0x00, 0x00, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x05, 0x00]
AUDIO_ON = [0x80, 0x0D, 0x01, 0x00, 0x05, 0x00, 0x06, 0x00, 0x02, 0x00]


RTC_MIN_EPOCH = 1767225600     # 2026-01-01: anything older = the clock was never set / RTC battery dead


def clock_status():
    """('ok', why) if the Pi's time can be trusted, else ('bad', why).
    Trusted = synced over the internet (NTP, e.g. through the phone hotspot) OR the RTC holds a sane time.
    With a dead RTC battery the Pi can boot with an old saved time that still looks like 2026,
    so the RTC itself is checked, not only the system year."""
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return "ok", "NTP"
    try:
        with open("/sys/class/rtc/rtc0/since_epoch") as f:
            if int(f.read().strip()) >= RTC_MIN_EPOCH and time.time() >= RTC_MIN_EPOCH:
                return "ok", "RTC"
        return "bad", "RTC time not set"
    except (OSError, ValueError):
        return "bad", "RTC unreadable"


# ---------------- helpers ----------------
def clean_text(s):
    """Remove what the cluster can't show: Spotify's ' • Lossless' tag and any '•'."""
    s = s.replace(" • Lossless", "").replace("•", "-")
    return " ".join(s.split())


def utf8_field(text, limit=63):
    b = text.encode("utf-8")[:limit]
    return list(b.decode("utf-8", "ignore").encode("utf-8"))


def track_payload(title, album, artist):
    t, a, r = utf8_field(clean_text(title)), utf8_field(clean_text(album)), utf8_field(clean_text(artist))
    return ([0x22, 0x0D, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x05, len(t)] + t
            + [0x00, 0x02, 0x00, 0x00, 0x05, len(a)] + a
            + [0x00, 0x01, 0x05, len(r)] + r)


def pad(b):
    b = list(b)
    return bytes(b + [0xFF] * (8 - len(b)))


def hexs(b):
    return " ".join(f"{x:02X}" for x in b)


def wait_for_iface():
    """Block quietly until the CANable (can0) is plugged in, instead of crash-looping."""
    if os.path.exists(f"/sys/class/net/{IFACE}"):
        return
    print(f"[i] {IFACE} not found (CANable unplugged?) -> waiting for it...", flush=True)
    while not os.path.exists(f"/sys/class/net/{IFACE}"):
        time.sleep(2.0)
    time.sleep(1.0)                    # let the gs_usb driver finish setting it up
    print(f"[i] {IFACE} appeared", flush=True)


def configure_iface(listen_only):
    """(Re)configure can0. gs_usb has no restart-ms, so recovery = this down/up re-init."""
    mode = "on" if listen_only else "off"
    cmds = [
        ["ip", "link", "set", IFACE, "down"],
        ["ip", "link", "set", IFACE, "type", "can", "bitrate", str(BITRATE),
         "listen-only", mode],
        ["ip", "link", "set", IFACE, "up"],
    ]
    for c in cmds:
        subprocess.run(c, check=True)


class Chan:
    def __init__(self, my_id, peer_id, join_req, join_ok):
        self.my_id, self.peer_id = my_id, peer_id
        self.join_req, self.join_ok = join_req, join_ok
        self.reset()

    def reset(self):
        self.seq = 0x00
        self.joined = False
        self.pending = {}
        self.last_join_try = 0.0
        self.isotp = None

    def next_seq(self):
        s = self.seq
        self.seq = (self.seq + 0x10) & 0x70
        return s


# ---------------- emulator ----------------
class Emu:
    def __init__(self, listen, log):
        self.listen, self.log = listen, log
        self.t0 = time.monotonic()
        self.listen_only_mode = None
        self.bus = None
        self.tx_on = False
        self.locked_out = False
        self.bus_awake = False
        self.last_mod_rx = 0.0
        self.last_hb = 0.0
        self.c603 = Chan(0x603, 0x683, lambda s: [0x03, s, 0x00, 0x21], lambda s: [0x03, s, 0x00, 0x2C])
        self.c628 = Chan(0x628, 0x6A8, lambda s: [0x04, s, 0x00, 0x21, 0xE0], lambda s: [0x04, s, 0x00, 0x2C, 0xE0])
        self.by_peer = {0x683: self.c603, 0x6A8: self.c628}
        self.swc_held = None
        self.txq, self.last_tx = {}, {}
        self.iso_q, self.iso_cur = [], None
        self.last_track = None
        self.ev = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.clock_sent = None     # (hour, minute) last sent to 0x683
        self.clock_warned = False
        self.warn_until = 0.0
        self.door = None           # 1 = cluster says RADIO OFF (driver door / key out, engine off)
        self.last_door_w = 0.0

    # ----- io -----
    def set_listen_only(self, on):
        if on == self.listen_only_mode:
            return
        if self.bus:
            self.bus.shutdown()
        configure_iface(on)
        self.bus = can.Bus(channel=IFACE, interface="socketcan")
        self.listen_only_mode = on

    def t(self):
        return time.monotonic() - self.t0

    def say(self, msg):
        print(f"[{self.t():8.2f}] {msg}", flush=True)
        if self.log:
            self.log.write(f"// {self.t():11.6f} {msg}\n")

    def emit(self, **ev):
        try:
            self.ev.sendto(json.dumps(ev).encode(), EVENT_ADDR)
        except OSError:
            pass

    def logf(self, direction, can_id, data):
        if self.log:
            self.log.write(f"{self.t():11.6f} 1  {can_id:x}{'':<12} {direction}   d {len(data)} {hexs(data)}\n")

    def send(self, can_id, data):
        if self.tx_on and not self.locked_out:
            self.txq.setdefault(can_id, []).append(pad(data))

    def flush_tx(self):
        if not self.tx_on or self.locked_out:
            self.txq.clear()
            return
        now = time.monotonic()
        for can_id, q in self.txq.items():
            if q and now - self.last_tx.get(can_id, 0.0) >= MIN_GAP_S:
                data = q.pop(0)
                self.last_tx[can_id] = now
                try:
                    self.bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=False), timeout=0.05)
                    self.logf("Tx", can_id, data)
                except can.CanError as e:
                    self.say(f"TX error {e}")

    def send_tracked(self, ch, build):
        s = ch.next_seq()
        data = build(s)
        ch.pending[s] = [data, time.monotonic(), 1]
        self.send(ch.my_id, data)

    # ----- long messages to the cluster -----
    def send_long(self, payload, name):
        self.iso_q.append((list(payload), name))

    def iso_tick(self, now):
        cur = self.iso_cur
        if cur is None:
            if not self.iso_q or not self.c628.joined:
                return
            payload, name = self.iso_q.pop(0)
            seq = self.c628.next_seq()
            length = 1 + len(payload)
            ff = [0x10 | (length >> 8), length & 0xFF, seq] + payload[:5]
            rest, cfs, idx = payload[5:], [], 1
            while rest:
                cfs.append([0x20 | (idx & 0x0F)] + rest[:7])
                rest, idx = rest[7:], idx + 1
            self.iso_cur = {"seq": seq, "ff": ff, "cfs": cfs, "stage": "fc", "t": now, "tries": 1, "name": name}
            self.send(0x628, ff)
            return
        if cur["stage"] == "fc" and now - cur["t"] > 0.5:
            if cur["tries"] >= 3:
                self.say(f"No flow control for {cur['name']} - dropped")
                self.iso_cur = None
            else:
                cur["tries"] += 1
                cur["t"] = now
                self.send(0x628, cur["ff"])
        elif cur["stage"] == "ack" and now - cur["t"] > 1.0:
            self.say(f"No ACK for {cur['name']} - dropped")
            self.iso_cur = None

    def show_track(self, title, album, artist):
        self.last_track = (title, album, artist)     # re-sent automatically after a (re)join
        if time.monotonic() < self.warn_until:
            return                                   # a warning is on the cluster; tick sends this after
        if not self.c628.joined or not self.tx_on:
            self.say("Text received, cluster session not up yet -> will send when it is")
            return
        self.iso_q.clear()
        self.say(f"Cluster text: '{clean_text(title)}' / '{clean_text(artist)}'")
        self.send_long(AUDIO_ON, "audio on")
        self.send_long(BT_SOURCE_EMPTY, "Bluetooth source")
        self.send_long(track_payload(title, album, artist), "track text")

    # ----- state -----
    def go_quiet(self, why):
        if self.tx_on:
            self.say(f"Going quiet: {why}")
            self.emit(type="session", state="down")
        self.tx_on = False
        for ch in (self.c603, self.c628):
            ch.reset()
        self.iso_q.clear()
        self.iso_cur = None
        self.set_listen_only(True)

    def go_active(self):
        if self.listen or self.locked_out or self.tx_on:
            return
        self.set_listen_only(False)
        self.tx_on = True
        self.last_hb = 0.0
        self.say("RADIO EMULATION ON")

    def joined(self, ch):
        if ch.joined:
            return
        ch.joined = True
        self.say(f"Session OK: {ch.my_id:03X} <-> {ch.peer_id:03X}")
        if ch is self.c603:
            self.clock_sent = None             # send the real time right away (tick does it)
        else:
            self.send_tracked(ch, lambda s: [0x03, s, 0xC6, 0x21])
            if self.last_track:
                self.show_track(*self.last_track)
        if self.c603.joined and self.c628.joined:
            self.emit(type="session", state="up")

    # ----- rx -----
    def on_frame(self, can_id, data):
        now = time.monotonic()
        if can_id in MODULES:
            self.last_mod_rx = now
            if not self.bus_awake:
                self.bus_awake = True
                self.say(f"Bus awake (first frame from {can_id:03X})")

        ch = self.by_peer.get(can_id)
        if not ch or not data:
            return
        d0 = data[0]
        if d0 in (0xF0, 0xF1):
            return
        if self.listen:                    # decode only, never answer
            if 1 <= d0 <= 7:
                self.decode(ch, list(data[2:1 + d0]))
            return
        if d0 == 0x30:
            cur = self.iso_cur
            if ch is self.c628 and cur and cur["stage"] == "fc":
                for cf in cur["cfs"]:
                    self.send(0x628, cf)
                cur["stage"], cur["t"] = "ack", time.monotonic()
            return
        if d0 == 0xE0:
            ch.pending.pop(data[1], None)
            cur = self.iso_cur
            if ch is self.c628 and cur and cur["stage"] == "ack" and data[1] == cur["seq"]:
                self.say(f"Cluster accepted: {cur['name']}")
                self.iso_cur = None
            return
        if d0 & 0xF0 == 0x10:
            total = ((d0 & 0x0F) << 8) | data[1]
            ch.isotp = [data[2], total - 6]
            self.send(ch.my_id, [0x30, 0x00, 0x0A])
            return
        if d0 & 0xF0 == 0x20 and ch.isotp:
            ch.isotp[1] -= 7
            if ch.isotp[1] <= 0:
                self.send(ch.my_id, [0xE0, ch.isotp[0]])
                ch.isotp = None
            return
        if 1 <= d0 <= 7:
            seq, p = data[1], list(data[2:1 + d0])
            self.send(ch.my_id, [0xE0, seq])
            if p[:2] == [0x00, 0x20]:
                self.send_tracked(ch, ch.join_ok)
                self.joined(ch)
            elif p[:2] == [0x00, 0x2C]:
                self.joined(ch)
            else:
                self.decode(ch, p)

    def decode(self, ch, p):
        if ch.peer_id == 0x683 and p[:2] == [0x20, 0x0D] and len(p) > 2:
            code = p[2]
            name = SWC.get(code & 0x7F, f"0x{code & 0x7F:02X}")
            if code & 0x80:
                state = "repeat" if self.swc_held == name else "pressed"
                self.swc_held = name
            else:
                state, self.swc_held = "released", None
            if state != "repeat":
                self.say(f"SWC {name} {state}")
            self.emit(type="swc", button=name, state=state)
        elif ch.peer_id == 0x6A8 and p[:1] == [0xC6] and len(p) > 2:
            st = POWER.get(p[2], hex(p[2]))
            self.say(f"POWER: {st}")
            self.emit(type="power", state=st)
            if p[2] in (0x02, 0x03, 0x04):
                self.door = 1 if p[2] == 0x02 else 0
                self.write_door(force=True)

    def write_door(self, force=False):
        """Same file format rp2040-link already reads. Only written while the bus is awake,
        so it goes stale (= unknown) when the car sleeps."""
        now = time.monotonic()
        if self.door is None or not self.bus_awake or (not force and now - self.last_door_w < 1.0):
            return
        self.last_door_w = now
        try:
            tmp = DOOR_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"t": now, "door": self.door}, f)
            os.replace(tmp, DOOR_FILE)
        except OSError as e:
            self.say(f"door file: {e}")

    # ----- periodic -----
    def tick(self):
        now = time.monotonic()
        if self.bus_awake and now - self.last_mod_rx > BUS_QUIET_S:
            self.bus_awake = False
            self.say("Modules silent -> car asleep / ACC off")
            self.door = None
            self.go_quiet("bus asleep")
            return
        if self.bus_awake and not self.tx_on:
            self.go_active()
        self.write_door()
        self.flush_tx()
        if not self.tx_on:
            return
        self.iso_tick(now)
        if self.warn_until and now >= self.warn_until:
            self.warn_until = 0.0
            if self.last_track:
                self.show_track(*self.last_track)
        if self.c603.joined:
            lt = time.localtime()
            if (lt.tm_hour, lt.tm_min) != self.clock_sent:
                self.clock_sent = (lt.tm_hour, lt.tm_min)
                ok, why = clock_status()
                if ok == "ok":
                    if self.clock_warned:
                        self.say(f"Clock OK again ({why}) -> sending time to the cluster")
                        self.clock_warned = False
                    pl = clock_payload(lt.tm_hour, lt.tm_min)
                    self.send_tracked(self.c603, lambda s, pl=pl: [1 + len(pl), s] + pl)
                elif not self.clock_warned:
                    # don't overwrite the cluster's (good) time with a bad one; tell the driver once
                    self.clock_warned = True
                    self.say(f"Clock NOT trusted ({why}): not sending time. Check RTC battery?")
                    self.emit(type="warning", id="rtc", text="Check RTC battery?")
                    real = self.last_track
                    self.show_track("Check RTC battery?", "", "Clock not set")
                    self.warn_until = now + 8.0      # keep it on screen 8 s, then back to the song
                    self.last_track = real
        if now - self.last_hb >= HB_PERIOD:
            self.last_hb = now
            self.send(0x628, HEARTBEAT)
            self.send(0x603, HEARTBEAT)
        for ch in (self.c603, self.c628):
            if not ch.joined and now - ch.last_join_try >= JOIN_RETRY_S:
                ch.last_join_try = now
                self.send_tracked(ch, ch.join_req)
            for s, item in list(ch.pending.items()):
                data, last, tries = item
                if now - last >= RETRY_S:
                    if tries >= MAX_TRIES:
                        self.say(f"No ACK from {ch.peer_id:03X} for {hexs(data)} - giving up")
                        del ch.pending[s]
                    else:
                        item[1], item[2] = now, tries + 1
                        self.send(ch.my_id, data)


# ---------------- main ----------------
def send_text_cmd(argv):
    title = argv[0] if argv else ""
    artist = argv[1] if len(argv) > 1 else ""
    album = argv[2] if len(argv) > 2 else ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps({"cmd": "track", "title": title, "artist": artist, "album": album}).encode(), CMD_ADDR)
    print("sent")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "text":
        send_text_cmd(sys.argv[2:])
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", action="store_true", help="decode only, never transmit")
    ap.add_argument("--log", default="/home/zen/can_logs", help="folder for .asc logs ('' = no log)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("[!] Run with sudo (needed to configure can0)")
        sys.exit(1)

    log = None
    if args.log:
        os.makedirs(args.log, exist_ok=True)
        path = os.path.join(args.log, datetime.datetime.now().strftime(
            f"{'listen' if args.listen else 'emu'}_%Y%m%d_%H%M%S.asc"))
        log = open(path, "w", encoding="utf-8", buffering=1)
        log.write("base hex  timestamps absolute\nBegin Triggerblock\n")
        print(f"[i] logging -> {path}")

    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd.bind(CMD_ADDR)
    cmd.setblocking(False)

    wait_for_iface()
    emu = Emu(args.listen, log)
    emu.set_listen_only(True)
    print(f"[i] {'LISTEN ONLY' if args.listen else 'EMULATION'} on {IFACE}. Ctrl+C to stop.", flush=True)

    last_err = last_good = None
    try:
        while True:
            msg = emu.bus.recv(timeout=0.005)
            if msg is not None:
                if msg.is_error_frame:
                    last_err = time.monotonic()
                    if emu.log:
                        emu.log.write(f"{emu.t():11.6f} 1  ErrorFrame\n")
                    if msg.arbitration_id & CAN_ERR_BUSOFF:
                        if emu.tx_on:
                            emu.locked_out = True
                            emu.say("BUS-OFF while transmitting -> TX disabled until restart")
                            emu.go_quiet("bus-off")
                        else:
                            emu.say("BUS-OFF while listening -> re-init can0")
                            emu.listen_only_mode = None
                            emu.set_listen_only(True)
                else:
                    last_good = time.monotonic()
                    emu.logf("Rx", msg.arbitration_id, msg.data)
                    emu.on_frame(msg.arbitration_id, bytes(msg.data))
            if last_err and (last_good is None or last_good < last_err) and time.monotonic() - last_err > 0.5:
                last_err = None
                emu.say("Only error frames -> re-init can0")
                mode = emu.listen_only_mode
                emu.listen_only_mode = None
                emu.set_listen_only(mode if mode is not None else True)
            try:
                data, _ = cmd.recvfrom(4096)
                c = json.loads(data)
                emu.say(f"Command received: {c}")
                if c.get("cmd") == "track":
                    emu.show_track(c.get("title", ""), c.get("album", ""), c.get("artist", ""))
            except BlockingIOError:
                pass
            except (ValueError, OSError) as e:
                emu.say(f"bad command: {e}")
            emu.tick()
    except KeyboardInterrupt:
        pass
    except OSError as e:
        # CANable unplugged mid-run: exit, systemd restarts us and we wait quietly in wait_for_iface()
        print(f"[!] CAN interface lost: {e}", flush=True)
        raise SystemExit(1)
    finally:
        emu.tx_on = False
        try:
            emu.bus.shutdown()
        except Exception:
            pass
        try:
            configure_iface(True)       # leave can0 silent
        except Exception:
            pass
        if log:
            log.write("End TriggerBlock\n")
            log.close()


if __name__ == "__main__":
    main()
