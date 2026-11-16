#!/usr/bin/env python3
# rp2040-link v0.9: UART con el RP2040 + musica (AVRCP pause/play) + volumen estilo radio en CamillaDSP
#
# Volumen estilo headunit:
#   - telefono al 100% (rutina de Samsung) y el sink de PipeWire FIJO en 1.0 (nunca mas alto: clipping digital)
#   - el volumen que escuchas = volumen principal de CamillaDSP (dB), por el websocket en 127.0.0.1:1234
#   - fade-in al ULTIMO volumen que usaste, con tope al encender (POWER_ON_MAX_DB)
#   - fade-out al apagar: baja, MUTE, y deja guardado tu volumen (asi el statefile no se queda en -60 dB)
#
# Pruebas sin el RP2040 (no abren el puerto serial, se pueden correr con el servicio activo):
#   sudo python3 /usr/local/bin/rp2040-link.py test status
#   sudo python3 /usr/local/bin/rp2040-link.py test fadeout
#   sudo python3 /usr/local/bin/rp2040-link.py test fadein
#   sudo python3 /usr/local/bin/rp2040-link.py test vol -3      (sube/baja 3 dB, como un boton)
#   sudo python3 /usr/local/bin/rp2040-link.py test set -25     (pone -25 dB)
#   sudo python3 /usr/local/bin/rp2040-link.py test door 1      (simula la puerta del chofer abierta; 0 = cerrada; Ctrl+C termina)
#   sudo python3 /usr/local/bin/rp2040-link.py test eng 2       (simula motor encendido; 1 = llave ON motor apagado; 0 = no contesta)
#   sudo python3 /usr/local/bin/rp2040-link.py test ill 1       (simula las luces prendidas = pantalla en modo noche; Ctrl+C termina)
#   sudo python3 /usr/local/bin/rp2040-link.py test rev 1       (simula reversa)
#   sudo python3 /usr/local/bin/rp2040-link.py test bye         (corre solo la animacion de bye, sin apagar nada)
#
# v0.5: link por UART1 (GPIO0/1, pines 27/28, dtoverlay=uart1-pi5) = /dev/ttyAMA1 a 115200
#       cada mensaje lleva checksum XOR: "ALIVE 1*4F\n"; lineas con checksum malo se ignoran
#       si el puerto se cae (o el RP2040 se reinicia) reabre solo, sin tumbar el servicio
# v0.5.1: al arrancar ya no guarda como "tu volumen" lo que CamillaDSP trae de su statefile
# v0.6: le manda al RP2040 'ENG n' y 'DOOR n' (al cambiar y cada 1 s), leidos de /dev/shm:
#         caguas-engine  {"t": monotonic, "ok": el carro contesta el OBD, "rpm": int o null}  (lo escribe cvt_reader.py)
#         caguas-door    {"t": monotonic, "door": 0/1}                                     (lo escribira el lector de CAN)
#       los archivos '-test' ganan mientras esten frescos (comandos test door / test eng).
#       Si un archivo tiene mas de 5 s, no se manda nada: el RP2040 sigue solo con ACC + BLOWER.
#       ENG: 0 = no contesta | 1 = contesta y RPM < 200 | 2 = RPM > 400 (entre 200 y 400 se queda el anterior)
#       PARK (puerta abierta con el motor apagado): fade-out + pausa, como cualquier estado sin musica.
#       v0.6.1: 'el telefono reanudo solo' solo cuenta si el audio Bluetooth de verdad esta corriendo
#       (MediaTransport1 State = active o pending); si el telefono sigue 'playing' despues de 3 pausas, se deja de insistir
#       (1 vez cada 30 s). En STANDBY CamillaDSP esta en mute igual.
#       Al apagar: corre BYE_HOOK (animacion de bye del dashboard) antes del fade y del BYE, si existe.
# v0.7: el RP2040 (firmware v0.8) manda 'ILL 0/1' y 'REV 0/1' (al cambiar y cada 1 s). Se escriben en
#       /dev/shm/caguas-lights {"t": monotonic, "ill": 0/1, "rev": 0/1}; hud_service lo lee para el modo noche.
# v0.8: backlight real (firmware v0.9): hud_service escribe /dev/shm/caguas-bl {"t", "bl": 0-100, "night": 0-100}
#       y este servicio lo manda al RP2040 como 'BL n' y 'BLN n' (al cambiar y cada 1 s; viejo = no se manda).
#       Escribe /dev/shm/caguas-car {"t", "state"} con cada STATE (hud_service: pantalla negra en STANDBY/IGN,
#       welcome al primer RUN). Al apagar: fade de la musica -> animacion de bye (sigue mandando BL mientras corre)
#       -> fade lento del backlight a 0 hecho por el RP2040 (BYE_FADE_S) -> BYE.
#       El bye normal ahora es al abrir la puerta (PARK, lo hace hud_service): si la pantalla ya esta en 0
#       al llegar el SHUTDOWN, va directo al BYE.
# v0.9: los fades de la musica (3 s sin el loop principal) ya no frenan la pantalla: wait() sigue mandando BL
#       (antes el welcome corria con el backlight en 0 si coincidia con el fade-in: 'se corta').
#       Camara de reversa: hud_service pone "cam": 0/1 en caguas-bl -> 'CAM n' al RP2040 (firmware v0.10, GP26).
import base64, json, os, socket, struct, subprocess, sys, time

PORT = "/dev/ttyAMA1"
BAUD = 115200
DSP_SERVICE = "camilladsp"
DSP_WS = ("127.0.0.1", 1234)
USER, UID = "zen", 1000
PLAY_STATES = ("RUN", "EXT")
FADE_IN_DELAY = 1.5                 # espera a que el RP2040 prenda los amps
SHM = "/dev/shm"
ENG_FILE, DOOR_FILE = f"{SHM}/caguas-engine", f"{SHM}/caguas-door"
LIGHTS_FILE = f"{SHM}/caguas-lights"   # lo escribe este servicio (ILL / REV del RP2040)
BL_FILE = f"{SHM}/caguas-bl"           # lo escribe hud_service (nivel de la pantalla)
CAR_FILE = f"{SHM}/caguas-car"         # lo escribe este servicio (estado del RP2040)
BYE_FADE_S = 2.5                       # s: fade lento del backlight a negro despues de la animacion de bye
INFO_FRESH_S = 5.0                  # archivo mas viejo que esto = no sabemos (sin respuesta del carro, cvt_reader da vueltas de ~3 s)
RPM_ON, RPM_OFF = 400, 200
BYE_HOOK = "/usr/local/bin/caguas-bye"   # animacion de bye (dashboard); si no existe, se salta
BYE_HOOK_MAX_S = 8

# ---- volumen ----
FADE_TIME = 1.5                     # segundos
FADE_STEPS = 30
SILENT_DB = -60.0                   # "apagado" (piso del fade-out, antes del mute)
FADE_FROM_DB = -45.0                # el fade-in empieza aqui: de -60 a -45 casi no se oye, se sentiria muerto
VOL_MIN_DB, VOL_MAX_DB = -60.0, 0.0
POWER_ON_MAX_DB = -20.0             # si apagaste con el volumen alto, arranca aqui como maximo
DEFAULT_DB = -25.0                  # si no hay volumen guardado
VOL_FILE = "/var/lib/rp2040-link/last_volume"


def log(m): print(m, flush=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def dsp_running():
    return run(["systemctl", "is-active", "--quiet", DSP_SERVICE]).returncode == 0


# ---- CamillaDSP websocket (cliente minimo, solo libreria estandar) ----
class Dsp:
    def __init__(self):
        self.sock = None

    def _connect(self):
        s = socket.create_connection(DSP_WS, timeout=2)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: {DSP_WS[0]}:{DSP_WS[1]}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(1024)
            if not chunk:
                raise ConnectionError("CamillaDSP cerro la conexion")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            raise ConnectionError("CamillaDSP no acepto el websocket")
        self.sock = s

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("conexion cerrada")
            buf += chunk
        return buf

    def _send_frame(self, data, opcode=0x1):
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_text(self):
        while True:
            b0, b1 = self._recv_exact(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b1 & 0x80 else None
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                       # ping -> pong
                self._send_frame(data, 0xA)
            elif opcode == 0x8:
                raise ConnectionError("CamillaDSP cerro el websocket")
            elif opcode in (0x1, 0x0):
                return data.decode()

    def cmd(self, name, arg=None):
        """Manda un comando y devuelve 'value' (o None). Reconecta una vez si CamillaDSP se reinicio."""
        msg = json.dumps(name if arg is None else {name: arg}).encode()
        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                self._send_frame(msg)
                reply = json.loads(self._recv_text()).get(name, {})
                if reply.get("result") != "Ok":
                    log(f"CamillaDSP {name}: {reply}")
                return reply.get("value")
            except (OSError, ConnectionError, ValueError) as e:
                if self.sock:
                    try: self.sock.close()
                    except OSError: pass
                self.sock = None
                if attempt == 2:
                    log(f"CamillaDSP sin conexion ({e})")
        return None

    def close(self):
        if self.sock:
            try:
                self._send_frame(struct.pack(">H", 1000), 0x8)
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def get_vol(self): return self.cmd("GetVolume")
    def set_vol(self, db): self.cmd("SetVolume", round(max(VOL_MIN_DB, min(VOL_MAX_DB, db)), 2))
    def get_mute(self): return self.cmd("GetMute")
    def set_mute(self, m): self.cmd("SetMute", bool(m))


dsp = Dsp()


# ---- volumen guardado (el de escucha, nunca el de un fade) ----
def load_listen_db():
    try:
        return float(open(VOL_FILE).read().strip())
    except (OSError, ValueError):
        return DEFAULT_DB


def save_listen_db(db):
    try:
        os.makedirs(os.path.dirname(VOL_FILE), exist_ok=True)
        with open(VOL_FILE, "w") as f:
            f.write(f"{db:.2f}\n")
    except OSError as e:
        log(f"no pude guardar el volumen: {e}")


def sink_full():
    """Entrada de CamillaDSP (sink de PipeWire) fija en 1.0: el volumen real es el de CamillaDSP."""
    run(["sudo", "-u", USER, "env", f"XDG_RUNTIME_DIR=/run/user/{UID}",
         "wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.0"])


pump = lambda: None                 # el servicio lo cambia por pump_bl (modo test: nada)


def wait(sec):
    """sleep, pero el nivel de pantalla sigue llegando al RP2040"""
    end = time.monotonic() + sec
    while True:
        pump()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


def ramp(a, b):
    for i in range(1, FADE_STEPS + 1):
        dsp.set_vol(a + (b - a) * i / FADE_STEPS)
        wait(FADE_TIME / FADE_STEPS)


def fade_in():
    target = min(load_listen_db(), POWER_ON_MAX_DB)
    sink_full()
    start = min(FADE_FROM_DB, target)
    dsp.set_vol(start)
    dsp.set_mute(False)
    ramp(start, target)
    save_listen_db(target)
    log(f"fade-in a {target:.1f} dB")


def fade_out(with_fade=True):
    cur = dsp.get_vol()
    muted = dsp.get_mute()
    # solo se recuerda el volumen si de verdad estabas escuchando (with_fade = venias de RUN/EXT).
    # Al arrancar, CamillaDSP puede traer un volumen a mitad de fade de su statefile: ese NO se guarda.
    if with_fade and cur is not None and not muted and cur > SILENT_DB + 1:
        save_listen_db(cur)                         # tu volumen de ahora es el que se recuerda
        ramp(cur, SILENT_DB)
    dsp.set_mute(True)
    dsp.set_vol(load_listen_db())                   # muteado: el statefile guarda tu volumen, no -60
    log("fade-out, mute")


def track_volume():
    """Cada 2 s en RUN/EXT: si cambiaste el volumen (GUI, dashboard, botones), recordarlo."""
    cur, muted = dsp.get_vol(), dsp.get_mute()
    if cur is not None and not muted and cur > SILENT_DB + 1 and abs(cur - load_listen_db()) >= 0.25:
        save_listen_db(cur)


# ---- telefono: BlueZ AVRCP ----
def player_path():
    for line in run(["busctl", "tree", "--list", "org.bluez"]).stdout.splitlines():
        line = line.strip()
        if "/player" in line and line.count("/") == 5:
            return line
    return None


def player_cmd(method):
    p = player_path()
    if p:
        r = run(["busctl", "call", "org.bluez", p, "org.bluez.MediaPlayer1", method])
        log(f"AVRCP {method}: {'ok' if r.returncode == 0 else r.stderr.strip()}")
    return p


def player_status(p):
    out = run(["busctl", "get-property", "org.bluez", p, "org.bluez.MediaPlayer1", "Status"]).stdout
    return out.split('"')[1] if '"' in out else None


def audio_flowing(p):
    """True si el transporte A2DP del telefono esta 'active' (audio llegando de verdad).
    Si no se encuentra el transporte, None (no sabemos)."""
    dev = p.rsplit("/", 1)[0]                     # /org/bluez/hci0/dev_XX
    for line in run(["busctl", "tree", "--list", "org.bluez"]).stdout.splitlines():
        t = line.strip()
        if t.startswith(dev + "/") and "/fd" in t:
            out = run(["busctl", "get-property", "org.bluez", t, "org.bluez.MediaTransport1", "State"]).stdout
            if '"' in out:
                return out.split('"')[1] in ("active", "pending")   # pending = el telefono empezo a mandar audio
    return None


def start_music():
    global last_player
    last_player = player_cmd("Play")
    wait(FADE_IN_DELAY)
    fade_in()


def stop_music(with_fade):
    global last_player
    pump()
    fade_out(with_fade)
    last_player = player_cmd("Pause")


# ---- datos de motor / puerta para el RP2040 ----
def write_json(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def read_info(path):
    """el archivo '-test' gana si esta fresco; None si no hay nada fresco"""
    for p in (path + "-test", path):
        try:
            with open(p) as f:
                d = json.load(f)
            if time.monotonic() - float(d.get("t", 0)) <= INFO_FRESH_S:
                return d
        except (OSError, ValueError):
            pass
    return None


def eng_state(prev):
    d = read_info(ENG_FILE)
    if d is None:
        return None
    if not d.get("ok"):
        return 0
    rpm = d.get("rpm")
    if rpm is not None and rpm > RPM_ON:
        return 2
    if rpm is not None and rpm < RPM_OFF:
        return 1
    return prev if prev in (1, 2) else 1         # crank (200-400) o rpm desconocida


def door_state():
    d = read_info(DOOR_FILE)
    return None if d is None or d.get("door") not in (0, 1) else int(d["door"])


def bl_state():
    """(bl, night, cam) del archivo de hud_service, o (None, None, None) si esta viejo."""
    d = read_info(BL_FILE)
    if d is None:
        return None, None, None
    try:
        bl = max(0, min(100, int(d.get("bl"))))
    except (TypeError, ValueError):
        return None, None, None
    try:
        night = max(0, min(100, int(d.get("night"))))
    except (TypeError, ValueError):
        night = None
    return bl, night, (d.get("cam") if d.get("cam") in (0, 1) else None)


def bye_animation(keep_alive=None):
    """Corre el hook de bye; mientras corre, keep_alive() se llama cada 0.5 s (para seguir mandando BL)."""
    if not os.path.exists(BYE_HOOK):
        return
    try:
        p = subprocess.Popen([BYE_HOOK])
    except OSError as e:
        log(f"bye hook: {e}"); return
    t0 = time.monotonic()
    while p.poll() is None:
        if time.monotonic() - t0 > BYE_HOOK_MAX_S:
            p.kill(); log("bye hook: timeout"); break
        if keep_alive:
            keep_alive()
        time.sleep(0.5)


# ---- modo prueba (sin RP2040) ----
def test(args):
    what = args[0] if args else "status"
    if what == "fadein":
        fade_in()
    elif what == "fadeout":
        fade_out(True)
    elif what == "vol" and len(args) > 1:
        cur = dsp.get_vol()
        cur = load_listen_db() if cur is None else cur
        dsp.set_vol(cur + float(args[1])); save_listen_db(dsp.get_vol())
    elif what == "set" and len(args) > 1:
        dsp.set_vol(float(args[1])); save_listen_db(dsp.get_vol())
    elif what == "bye":
        bye_animation(); print("bye listo"); return
    elif what in ("door", "eng", "ill", "rev") and len(args) > 1 and args[1] in ("0", "1", "2"):
        v = int(args[1])
        path = {"door": DOOR_FILE, "eng": ENG_FILE}.get(what, LIGHTS_FILE) + "-test"
        data = ({"ok": v > 0, "rpm": {0: None, 1: 0, 2: 800}[v]} if what == "eng"
                else {what: min(v, 1)})
        print(f"simulando {what} = {v} (Ctrl+C para terminar)")
        try:
            while True:
                write_json(path, dict(data, t=time.monotonic()))
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            try: os.remove(path)
            except OSError: pass
            print("simulacion terminada")
        return
    print(f"CamillaDSP: {dsp.get_vol()} dB, mute {dsp.get_mute()} | guardado: {load_listen_db():.1f} dB "
          f"| arranque: {min(load_listen_db(), POWER_ON_MAX_DB):.1f} dB (tope {POWER_ON_MAX_DB})")


if len(sys.argv) > 1 and sys.argv[1] == "test":
    test(sys.argv[2:])
    dsp.close()
    sys.exit(0)

import serial  # solo el servicio usa el puerto


def cs(body):
    x = 0
    for c in body:
        x ^= c
    return f"{x:02X}".encode()


def frame(msg):
    body = msg.encode()
    return body + b"*" + cs(body) + b"\n"


def unframe(raw):
    """Cuerpo del mensaje si el checksum cuadra, si no None."""
    raw = raw.strip()
    i = raw.rfind(b"*")
    if i < 0 or len(raw) - i != 3:
        return None
    body = raw[:i]
    return body.decode(errors="ignore") if cs(body) == raw[i + 1:].upper() else None


def open_port():
    while True:
        try:
            p = serial.Serial(PORT, BAUD, timeout=0.2)
            p.reset_input_buffer()
            log(f"puerto {PORT} abierto a {BAUD}")
            return p
        except (serial.SerialException, OSError) as e:
            log(f"no pude abrir {PORT} ({e}), reintento en 2 s")
            time.sleep(2)


s = open_port()
state = None; last_hb = 0; last_enf = 0; last_player = None; bad = 0
eng = door = None; sent_eng = sent_door = None; last_info = 0; resume_tries = 0; resume_t = 0
lights = {"ill": None, "rev": None}
sent_bl = sent_bln = sent_cam = None; sent_bl_t = 0


def send_bl(force=False):
    """BL / BLN al RP2040 si hud_service tiene un nivel fresco."""
    global sent_bl, sent_bln, sent_cam, sent_bl_t
    bl, night, cam = bl_state()
    if bl is None:
        return
    now = time.monotonic()
    if force or (bl, night, cam) != (sent_bl, sent_bln, sent_cam) or now - sent_bl_t >= 1:   # 1 s: fresco en el RP2040
        s.write(frame(f"BL {bl}"))
        if night is not None:
            s.write(frame(f"BLN {night}"))
        if cam is not None:
            s.write(frame(f"CAM {cam}"))
        if bl != sent_bl:
            log(f"pantalla {bl} %")
        if cam != sent_cam:
            log(f"camara {cam}")
        sent_bl, sent_bln, sent_cam, sent_bl_t = bl, night, cam, now


def pump_bl():
    try:
        send_bl()
    except (serial.SerialException, OSError):
        pass                                             # el loop principal reabre el puerto


pump = pump_bl


def write_car(st):
    try:
        write_json(CAR_FILE, {"t": time.monotonic(), "state": st})
    except OSError as e:
        log(f"no pude escribir {CAR_FILE}: {e}")
sink_full()
log("rp2040-link v0.9 arriba")
while True:
    now = time.monotonic()
    try:
        if now - last_hb >= 2:
            last_hb = now
            s.write(frame(f"ALIVE {1 if dsp_running() else 0}"))
        eng, door = eng_state(eng), door_state()
        if eng != sent_eng or door != sent_door or now - last_info >= 1:
            send_bl(force=True)
            if eng is not None:
                s.write(frame(f"ENG {eng}"))
            if door is not None:
                s.write(frame(f"DOOR {door}"))
            if eng != sent_eng or door != sent_door:
                log(f"ENG {eng} | DOOR {door}")
            sent_eng, sent_door, last_info = eng, door, now
        else:
            send_bl()                                    # cambio de nivel: enseguida (fade del welcome)
        raw = s.readline()
    except (serial.SerialException, OSError) as e:
        log(f"link caido ({e}), reabriendo")
        try: s.close()
        except Exception: pass
        time.sleep(1)
        s = open_port()
        continue

    line = unframe(raw) if raw.strip() else None
    if raw.strip() and line is None:
        bad += 1
        log(f"checksum malo #{bad}: {raw!r}")

    if line and line[:4] in ("ILL ", "REV ") and line[4:] in ("0", "1"):
        k, v = line[:3].lower(), int(line[4:])
        if v != lights[k]:
            log(f"{k.upper()} {v}" + (" (luces: modo noche)" if k == "ill" and v else " (luces apagadas)" if k == "ill" else ""))
            lights[k] = v
        try:
            write_json(LIGHTS_FILE, dict(lights, t=time.monotonic()))
        except OSError as e:
            log(f"no pude escribir {LIGHTS_FILE}: {e}")
    elif line and line.startswith("STATE "):
        new = line.split()[1]
        write_car(new)
        if new != state:
            log(f"{state} -> {new}")
            old, state = state, new
            if new in PLAY_STATES and old not in PLAY_STATES:
                start_music()
            elif new not in PLAY_STATES:
                stop_music(with_fade=(old in PLAY_STATES))
    elif line == "SHUTDOWN":
        log("SHUTDOWN recibido")
        write_car("SHUTDOWN")
        stop_music(with_fade=(state in PLAY_STATES))   # primero la musica (si todavia sonaba)
        send_bl()
        if sent_bl == 0:
            log("pantalla ya apagada (bye al abrir la puerta): BYE directo")
        else:
            bye_animation(lambda: send_bl(force=True))   # despues la animacion (la pantalla sigue en su nivel)
        bl0 = sent_bl if sent_bl is not None else 100   # el RP2040 baja la pantalla a negro, lento y parejo
        t_off = time.monotonic()
        while bl0 > 0:
            k = min(1.0, (time.monotonic() - t_off) / BYE_FADE_S)
            s.write(frame(f"BL {round(bl0 * (1 - k) ** 2)}"))   # curva: se ve pareja al ojo
            if k >= 1:
                break
            time.sleep(0.1)
        time.sleep(0.3)
        log("pantalla 0 %, BYE")
        s.write(frame("BYE")); s.flush()
        subprocess.run(["systemctl", "poweroff"])
        break
    # cada 2 s: el telefono puede reanudar solo al conectarse; y recordar tu volumen
    if state and now - last_enf >= 2:
        last_enf = now
        p = player_path()
        if p and p != last_player and state in PLAY_STATES:
            log("telefono conectado en " + state); start_music()
        elif p and state not in PLAY_STATES and player_status(p) == "playing" and audio_flowing(p) is not False:
            if resume_tries < 3 or now - resume_t >= 30:
                resume_tries += 1; resume_t = now
                log("el telefono reanudo solo: pausa" + (f" (intento {resume_tries})" if resume_tries > 1 else ""))
                stop_music(with_fade=False)
                if resume_tries == 3:
                    log("el telefono sigue diciendo 'playing': dejo de insistir (1 vez cada 30 s)")
        elif state in PLAY_STATES:
            track_volume()
        if state in PLAY_STATES or (p and player_status(p) != "playing"):
            resume_tries = 0
        last_player = p
