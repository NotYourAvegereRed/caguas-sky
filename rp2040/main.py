# RP2040 power controller v0.9
#   v0.9: backlight de la Santek por PWM en GP28 (20 kHz) -> 1 kOhm -> enable del driver (colector de Q3).
#         La Pi manda 'BL n' (0-100, nivel de pantalla) y 'BLN n' (nivel de noche para el respaldo).
#         Fade de 0 a 100 en BL_FADE_MS. Con R1 apagado el pin queda suelto (la Santek sin corriente).
#         Al prender R1 la pantalla queda negra mientras la Pi bootea (hasta el primer BL, max BL_BOOT_MAX);
#         sin BL fresco despues: respaldo por ILL (100 de dia, BLN de noche). En HALT siempre 0.
#         STATE se manda en cuanto cambia (antes solo cada 1 s).
#   v0.8: manda 'ILL 0/1' (luces / pin 9 del radio, GP14) y 'REV 0/1' (reversa, GP15) a la Pi:
#         en cuanto cambian (debounce 200 ms) y cada 1 s junto con el STATE. Solo informa: la logica de
#         estados y los relays NO cambian. Un rp2040-link viejo ignora estas lineas.
#   v0.7.1: al arrancar, si ACC o BLOWER estan vivos, R1 se queda en ON desde el primer instante
#           (un soft reset desde la Pi, para actualizar el firmware por Wi-Fi, no le corta la corriente a la Pi)
#           y el loop corre dentro de try/except: un error inesperado se imprime y el loop sigue (los relays nunca se congelan).
#   v0.7: datos de la Pi (opcionales, solo si llegaron en los ultimos 3 s):
#           ENG 0 = el carro no contesta el OBD | ENG 1 = contesta, RPM < 200 | ENG 2 = RPM > 400
#           DOOR 0/1 = puerta del chofer (por CAN)
#         RUN / IGN / crank por BLOWER (canal IGN) O por ENG. ENG 2 manda aunque el BLOWER falle.
#         Estado PARK (amarillo): desde EXT con la puerta del chofer abierta -> fade-out, amps fuera,
#         la Pi sigue prendida. Llave en ON -> IGN (o RUN si ENG 2). ACC + BLOWER fuera -> SHUTDOWN.
#         Sin datos de puerta (CAN caido): EXT pasa a PARK a los EXT_NOCAN_MAX.
#   v0.6: el canal IGN (GP4) lee el fusible BLOWER (vivo en ON, 0 V al crankear, 0 V con motor apagado).
#         Crank = BLOWER se apaga y regresa antes de CRANK_MAX.
#   v0.5.1: sin lightsleep, REV=GP15 / ILL=GP14 (placa final)
#   v0.4: link por UART1 de la Pi (GPIO0/1, pines 27/28) a 115200, mensajes con checksum XOR
from machine import Pin, UART, PWM
import neopixel, time

# ---- PIN MAP ----
PIN_UART_TX = 0    # -> 1k -> pin 28 de la Pi (GPIO1, RX)
PIN_UART_RX = 1    # <- 1k <- pin 27 de la Pi (GPIO0, TX)
PIN_ACC     = 8    # ELEC_AUTO_ACC (fuse tap)
PIN_IGN     = 4    # BLOWER (fuse tap) en el canal IGN
PIN_R1_PI   = 5
PIN_R2_REM  = 6
PIN_REV     = 15   # reversa (opto IN3)
PIN_ILL     = 14   # luces / ILL (opto IN4)
PIN_LED     = 16
PIN_BL      = 29   # backlight Santek (PWM -> 1k -> A1 / enable del driver)
BAUD = 115200

ACC = Pin(PIN_ACC, Pin.IN, Pin.PULL_UP)   # LOW = on (igual que las optos)
IGN = Pin(PIN_IGN, Pin.IN, Pin.PULL_UP)
REV = Pin(PIN_REV, Pin.IN, Pin.PULL_UP)
ILL = Pin(PIN_ILL, Pin.IN, Pin.PULL_UP)
_car_on = ACC.value() == 0 or IGN.value() == 0
R1 = Pin(PIN_R1_PI, Pin.OUT, value=1 if _car_on else 0)   # sin bajarlo si ya estaba en ON (soft reset)
R2 = Pin(PIN_R2_REM, Pin.OUT, value=0)
led = neopixel.NeoPixel(Pin(PIN_LED), 1)

# ---- tiempos (ms) ----
DEBOUNCE, OFF_CONFIRM, HALT_WAIT = 200, 500, 15000
BYE_TIMEOUT, AMP_DELAY, ALIVE_TIMEOUT = 300000, 1000, 6000
AMP_HOLD = 5000               # ms max de amps en SHUTDOWN (fade de la Pi)
AMP_STATES = ("RUN", "EXT")   # PARK no: la musica ya paro
CRANK_MAX = 8000              # un BLOWER apagado mas corto que esto = crank
INFO_FRESH = 3000             # ENG / DOOR valen solo si llegaron hace menos de esto
EXT_BLW_CONFIRM = 2000        # en EXT: BLOWER vivo tanto tiempo sin ENG 2 = llave en ON otra vez (IGN)
EXT_NOCAN_MAX = 120000        # en EXT sin datos de puerta: pasar a PARK a los 2 min
BL_FREQ = 20000               # Hz (probado: parejo de 5 a 100 %, sin parpadeo ni pitido)
BL_FADE_MS = 1200             # ms de 0 a 100 %
BL_BOOT_MAX = 60000           # pantalla negra esperando a la Pi; si no llega nada, respaldo por ILL
BL_NIGHT_DEFAULT = 40

COLOR = {"OFF": (0,0,0), "STANDBY": (0,0,20), "IGN": (0,15,15),
         "CRANK": (20,10,0), "RUN": (0,20,0), "EXT": (12,12,12), "PARK": (16,16,0),
         "SHUTDOWN": (20,0,0), "HALT": (12,0,12)}


def cs(body):
    x = 0
    for c in body:
        x ^= c
    return ("%02X" % x).encode()


def check(line):
    """Devuelve el cuerpo si el checksum cuadra, si no None."""
    i = line.rfind(b"*")
    if i < 0 or len(line) - i != 3:
        return None
    body = line[:i]
    return body if cs(body) == line[i + 1:].upper() else None


class Inp:
    def __init__(s, pin):
        s.pin = pin; s.state = s.cand = (pin.value() == 0); s.t = time.ticks_ms()
    def update(s, now):
        r = s.pin.value() == 0
        if r != s.cand:
            s.cand = r; s.t = now
        elif r != s.state and time.ticks_diff(now, s.t) >= DEBOUNCE:
            s.state = r
        return s.state

acc_in, ign_in = Inp(ACC), Inp(IGN)
ill_in, rev_in = Inp(ILL), Inp(REV)
uart = None; buf = b""; bad = 0
state = "OFF"; t_state = time.ticks_ms()
alive_t = None; dsp_ok = False; bye_t = None
both_off_t = None; last_tx = 0; last_in = None
blw_off_t = None; blw_on_t = None; blw_seen = False
last_aux = None
bl_pwm = None; bl_cur = 0.0; bl_shown = -1; bl_last_t = time.ticks_ms()
bl_v = None; bl_t = None; bl_night = BL_NIGHT_DEFAULT; bl_boot = False; bl_boot_t = 0
state_dirty = False
eng_v = None; eng_t = None; door_v = None; door_t = None

def uart_on():
    global uart
    if uart is None:
        uart = UART(0, BAUD, tx=Pin(PIN_UART_TX), rx=Pin(PIN_UART_RX), rxbuf=512)

def uart_off():
    global uart, buf
    if uart is not None:
        uart.deinit(); uart = None
    Pin(PIN_UART_TX, Pin.OUT, value=0)   # TX en LOW con la Pi apagada
    buf = b""

def send(msg):
    if uart is not None:
        body = msg.encode()
        uart.write(body + b"*" + cs(body) + b"\n")

def go(new):
    global state, t_state, state_dirty
    state = new; t_state = time.ticks_ms(); state_dirty = True
    led[0] = COLOR[new]; led.write()
    print("->", new)

def bl_start(level, boot):
    """R1 prendido: PWM activo. boot=True: negra hasta que la Pi mande BL."""
    global bl_pwm, bl_cur, bl_shown, bl_boot, bl_boot_t
    if bl_pwm is None:
        bl_pwm = PWM(Pin(PIN_BL)); bl_pwm.freq(BL_FREQ)
    bl_cur = float(level); bl_shown = -1; bl_boot = boot; bl_boot_t = time.ticks_ms()

def bl_stop():
    """R1 apagado: pin suelto, nada de voltaje hacia una Santek sin corriente."""
    global bl_pwm, bl_cur, bl_shown, bl_v, bl_t
    if bl_pwm is not None:
        bl_pwm.deinit(); bl_pwm = None
    Pin(PIN_BL, Pin.IN, None)
    bl_cur = 0.0; bl_shown = -1; bl_v = bl_t = None

def bl_update(now, ill):
    global bl_cur, bl_shown, bl_last_t, bl_boot
    dt = time.ticks_diff(now, bl_last_t); bl_last_t = now
    if bl_pwm is None:
        return
    v = fresh(bl_v, bl_t, now)
    if v is not None:
        bl_boot = False
    if state == "HALT":
        tgt = 0
    elif v is not None:
        tgt = v
    elif bl_boot and time.ticks_diff(now, bl_boot_t) < BL_BOOT_MAX:
        tgt = 0                              # la Pi esta booteando: no se ve el arranque
    else:
        tgt = bl_night if ill else 100       # sin la Pi: respaldo por las luces
    step = 100.0 * dt / BL_FADE_MS
    if bl_cur < tgt:
        bl_cur = min(tgt, bl_cur + step)
    elif bl_cur > tgt:
        bl_cur = max(tgt, bl_cur - step)
    d = int(bl_cur * 655.35 + 0.5)
    if d != bl_shown:
        bl_pwm.duty_u16(d); bl_shown = d

def num(b):
    """0-100 desde bytes, o None si no es un numero."""
    try:
        return max(0, min(100, int(b.decode())))
    except (ValueError, UnicodeError):
        return None

def read_uart(now):
    global buf, alive_t, dsp_ok, bye_t, bad, eng_v, eng_t, door_v, door_t, bl_v, bl_t, bl_night
    if uart is None or not uart.any():
        return
    data = uart.read()
    if data:
        buf += data
    if len(buf) > 256:
        buf = b""
    while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        line = check(raw.strip())
        if line is None:
            if raw.strip():
                bad += 1
                print("checksum malo (", bad, "):", raw)
            continue
        if line.startswith(b"ALIVE"):
            alive_t = now; dsp_ok = line.endswith(b"1")
        elif line == b"BYE":
            bye_t = now
        elif line.startswith(b"ENG ") and line[4:] in (b"0", b"1", b"2"):
            eng_v = int(line[4:]); eng_t = now
        elif line.startswith(b"DOOR ") and line[5:] in (b"0", b"1"):
            door_v = int(line[5:]); door_t = now
        elif line.startswith(b"BL "):
            n = num(line[3:])
            if n is not None:
                bl_v = n; bl_t = now
        elif line.startswith(b"BLN "):
            n = num(line[4:])
            if n is not None:
                bl_night = n

def fresh(v, t, now):
    return v if (t is not None and time.ticks_diff(now, t) < INFO_FRESH) else None

def cut():
    global alive_t, dsp_ok, bye_t, both_off_t, eng_v, eng_t, door_v, door_t
    R2.value(0); R1.value(0); uart_off(); bl_stop()
    alive_t = None; dsp_ok = False; bye_t = None; both_off_t = None
    eng_v = eng_t = door_v = door_t = None
    go("OFF")

if _car_on:
    uart_on(); go("STANDBY")     # el proximo loop elige el estado correcto (IGN / RUN con ENG 2)
    bl_start(100, False)         # soft reset (update por Wi-Fi): la pantalla sigue prendida, la Pi manda el nivel
else:
    uart_off(); bl_stop(); go("OFF")
errors = 0

def tick():
    global state, t_state, last_in, blw_off_t, blw_on_t, blw_seen, both_off_t, last_tx, last_aux, state_dirty
    now = time.ticks_ms()
    acc = acc_in.update(now); blw = ign_in.update(now)
    ill = ill_in.update(now); rev = rev_in.update(now)
    aux_changed = (ill, rev) != last_aux
    if aux_changed:
        last_aux = (ill, rev)
        print("in: ILL", int(ill), "REV", int(rev))
    read_uart(now)
    eng = fresh(eng_v, eng_t, now)       # None = no sabemos (Pi apagada, vLinker fuera...)
    door = fresh(door_v, door_t, now)    # None = no sabemos (CAN caido)
    key_on = blw or eng in (1, 2)

    if (acc, blw) != last_in:
        last_in = (acc, blw)
        print("in: ACC", int(acc), "BLOWER", int(blw))

    # BLOWER: cuanto lleva apagado / prendido
    if blw:
        blw_off_t = None
        if blw_on_t is None:
            blw_on_t = now
    else:
        blw_on_t = None
        if blw_off_t is None:
            blw_off_t = now
    short_gap = blw_off_t is not None and time.ticks_diff(now, blw_off_t) < CRANK_MAX

    if acc or blw:
        both_off_t = None
    elif both_off_t is None:
        both_off_t = now
    off_long = both_off_t is not None and time.ticks_diff(now, both_off_t) >= OFF_CONFIRM
    pi_ok = alive_t is not None and time.ticks_diff(now, alive_t) < ALIVE_TIMEOUT

    if bye_t is not None and state != "HALT":
        R2.value(0); go("HALT")          # la Pi ya se esta apagando

    if state == "OFF":
        if acc or blw:
            R1.value(1); uart_on(); bl_start(0, True); go("STANDBY")
    elif state in ("STANDBY", "IGN", "CRANK", "RUN", "EXT", "PARK"):
        if off_long:
            go("SHUTDOWN")                   # R2 sigue hasta el BYE (o AMP_HOLD)
        else:
            if state == "STANDBY":
                if eng == 2:
                    want = "RUN"             # p. ej. el RP2040 se reinicio manejando
                elif key_on:
                    want = "IGN"; blw_seen = blw
                else:
                    want = "STANDBY"
            elif state == "IGN":
                if eng == 2:
                    want = "RUN"
                elif blw:
                    want = "IGN"; blw_seen = True
                elif blw_seen:
                    want = "CRANK"           # el BLOWER se cayo: crank o llave a OFF
                elif eng == 1:
                    want = "IGN"             # llave en ON sin BLOWER (fusible malo): esperar ENG 2
                else:
                    want = "STANDBY"
            elif state == "CRANK":
                if eng == 2 or blw:
                    want = "RUN"             # arranco
                elif short_gap:
                    want = "CRANK"
                else:
                    want = "STANDBY"         # el BLOWER nunca volvio: llave fuera
            elif state == "RUN":
                if blw or eng == 2:
                    want = "RUN"             # llave en ON (aunque el motor se apague) o motor andando
                elif eng in (0, 1):
                    want = "EXT"             # el OBD dice motor apagado: EXT enseguida
                else:
                    want = "RUN" if short_gap else "EXT"   # sin OBD: regla de los 8 s
            elif state == "EXT":
                if eng == 2:
                    want = "RUN"             # volvio a prender
                elif door == 1:
                    want = "PARK"            # abriste la puerta del chofer
                elif blw and time.ticks_diff(now, blw_on_t) >= EXT_BLW_CONFIRM:
                    want = "IGN"; blw_seen = True   # llave en ON otra vez, sin arrancar
                elif door is None and time.ticks_diff(now, t_state) >= EXT_NOCAN_MAX:
                    want = "PARK"            # sin CAN: respaldo de 2 min
                else:
                    want = "EXT"
            else:  # PARK
                if eng == 2:
                    want = "RUN"
                elif key_on:
                    want = "IGN"; blw_seen = blw
                else:
                    want = "PARK"            # cerrar la puerta no lo saca
            if want != state:
                go(want)
            amps = (state in AMP_STATES and pi_ok and dsp_ok
                    and (state == "EXT" or time.ticks_diff(now, t_state) >= AMP_DELAY))
            R2.value(1 if amps else 0)
    elif state == "SHUTDOWN":
        if not pi_ok or time.ticks_diff(now, t_state) >= AMP_HOLD:
            R2.value(0)
        if acc or blw:
            go("STANDBY")                # el proximo loop elige el estado correcto
        elif time.ticks_diff(now, t_state) >= BYE_TIMEOUT:
            cut()
    elif state == "HALT":
        if time.ticks_diff(now, bye_t) >= HALT_WAIT:
            cut()

    bl_update(now, ill)

    if uart is not None and state != "HALT" and (aux_changed or state_dirty or time.ticks_diff(now, last_tx) >= 1000):
        last_tx = now; state_dirty = False
        send("SHUTDOWN" if state == "SHUTDOWN" else "STATE " + state)
        send("ILL %d" % ill)
        send("REV %d" % rev)


while True:
    try:
        tick()
    except Exception as e:           # nunca dejar los relays congelados por un error
        errors += 1
        print("ERROR en el loop (", errors, "):", repr(e))
        time.sleep_ms(100)
    time.sleep_ms(20)
