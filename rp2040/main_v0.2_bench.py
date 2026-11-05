# RP2040 power controller v0.2 (bench)
from machine import Pin, UART
import neopixel, time

# ---- PIN MAP (GP2 de esta placa esta danado: no usar) ----
PIN_UART_TX = 28
PIN_UART_RX = 29
PIN_ACC     = 8
PIN_IGN     = 4
PIN_R1_PI   = 5
PIN_R2_REM  = 6
PIN_REV     = 14   # futuro (opto)
PIN_ILL     = 15   # futuro (opto)
PIN_LED     = 16

ACC = Pin(PIN_ACC, Pin.IN, Pin.PULL_UP)   # LOW = on (igual que las optos)
IGN = Pin(PIN_IGN, Pin.IN, Pin.PULL_UP)
REV = Pin(PIN_REV, Pin.IN, Pin.PULL_UP)
ILL = Pin(PIN_ILL, Pin.IN, Pin.PULL_UP)
R1 = Pin(PIN_R1_PI, Pin.OUT, value=0)
R2 = Pin(PIN_R2_REM, Pin.OUT, value=0)
led = neopixel.NeoPixel(Pin(PIN_LED), 1)

# ---- tiempos (ms) ----
DEBOUNCE, OFF_CONFIRM, HALT_WAIT = 200, 500, 15000
BYE_TIMEOUT, AMP_DELAY, ALIVE_TIMEOUT = 300000, 1000, 6000
AMP_HOLD = 5000               # ms max de amps en SHUTDOWN (fade de la Pi)
AMP_STATES = ("RUN", "EXT")   # quita "EXT" si no quieres amps con el motor apagado

COLOR = {"OFF": (0,0,0), "STANDBY": (0,0,20), "IGN": (0,15,15),
         "CRANK": (20,10,0), "RUN": (0,20,0), "EXT": (12,12,12), "SHUTDOWN": (20,0,0),
         "HALT": (12,0,12)}

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
uart = None; buf = b""
state = "OFF"; t_state = time.ticks_ms()
cranked = False; alive_t = None; dsp_ok = False; bye_t = None
both_off_t = None; last_tx = 0; last_in = None

def uart_on():
    global uart
    if uart is None:
        uart = UART(0, 2400, tx=Pin(PIN_UART_TX), rx=Pin(PIN_UART_RX))

def uart_off():
    global uart, buf
    if uart is not None:
        uart.deinit(); uart = None
    Pin(PIN_UART_TX, Pin.OUT, value=0)   # TX en LOW con la Pi apagada
    buf = b""

def send(msg):
    if uart is not None:
        uart.write(msg + "\n")

def go(new):
    global state, t_state
    state = new; t_state = time.ticks_ms()
    led[0] = COLOR[new]; led.write()
    print("->", new)

def read_uart(now):
    global buf, alive_t, dsp_ok, bye_t
    if uart is None or not uart.any():
        return
    data = uart.read()
    if data:
        buf += data
    if len(buf) > 256:
        buf = b""
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.strip()
        if line.startswith(b"ALIVE"):
            alive_t = now; dsp_ok = line.endswith(b"1")
        elif line == b"BYE":
            bye_t = now

def cut():
    global cranked, alive_t, dsp_ok, bye_t, both_off_t
    R2.value(0); R1.value(0); uart_off()
    cranked = False; alive_t = None; dsp_ok = False; bye_t = None; both_off_t = None
    go("OFF")

uart_off(); go("OFF")
while True:
    now = time.ticks_ms()
    acc = acc_in.update(now); ign = ign_in.update(now)
    read_uart(now)

    if (acc, ign) != last_in:
        last_in = (acc, ign)
        print("in: ACC", int(acc), "IGN", int(ign))

    if acc or ign:
        both_off_t = None
    elif both_off_t is None:
        both_off_t = now
    off_long = both_off_t is not None and time.ticks_diff(now, both_off_t) >= OFF_CONFIRM
    pi_ok = alive_t is not None and time.ticks_diff(now, alive_t) < ALIVE_TIMEOUT

    if bye_t is not None and state != "HALT":
        R2.value(0); go("HALT")          # la Pi ya se esta apagando

    if state == "OFF":
        if acc or ign:
            R1.value(1); uart_on(); go("STANDBY")
    elif state in ("STANDBY", "IGN", "CRANK", "RUN", "EXT"):
        if ign and not acc:
            cranked = True               # el ACC se cae al crankear
        if state == "EXT" and ign and acc:
            cranked = False              # llave en ON otra vez: espera nuevo crank
        if off_long:
            go("SHUTDOWN")                   # R2 sigue hasta el BYE
        else:
            if ign and not acc: want = "CRANK"
            elif cranked:       want = "RUN" if ign else "EXT"
            elif ign:           want = "IGN"
            else:               want = "STANDBY"
            if want != state:
                go(want)
            amps = (state in AMP_STATES and pi_ok and dsp_ok
                    and (state == "EXT" or time.ticks_diff(now, t_state) >= AMP_DELAY))
            R2.value(1 if amps else 0)
    elif state == "SHUTDOWN":
        if not pi_ok or time.ticks_diff(now, t_state) >= AMP_HOLD:
            R2.value(0)
        if acc or ign:
            go("STANDBY")                # el proximo loop elige el estado correcto
        elif time.ticks_diff(now, t_state) >= BYE_TIMEOUT:
            cut()
    elif state == "HALT":
        if time.ticks_diff(now, bye_t) >= HALT_WAIT:
            cut()

    if uart is not None and state != "HALT" and time.ticks_diff(now, last_tx) >= 1000:
        last_tx = now
        send("SHUTDOWN" if state == "SHUTDOWN" else "STATE " + state)
    time.sleep_ms(20)
