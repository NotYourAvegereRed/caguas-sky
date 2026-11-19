#!/usr/bin/env python3
"""
dtc_probe.py - pregunta los DTCs (codes) a cada modulo del Rogue por el vLinker FS.

SOLO LEE. Nunca manda clear (0x14 / 0x04) ni nada que escriba en los modulos.

Prueba cada modulo con:
  1) 18 02 FF 00   -> KWP2000 ReadDTCByStatus (estilo Nissan)
  2) 19 02 FF      -> UDS ReadDTCInformation, reportDTCByStatusMask
  3) Si contesta "no en esta sesion" (NRC 7F/22), abre sesion Nissan 10 C0 y repite
  ECM ademas: 03 por 7DF (OBD2 Mode 03, codes de emisiones)

Uso (Windows, laptop):
  pip install pyserial
  python dtc_probe.py --port COM5              (cierra CVTz50 / otra app que use el puerto)
  python dtc_probe.py --port COM5 --baud 115200
  python dtc_probe.py --port COM5 --extra 743:763 --extra 7E2:7EA   (probar mas IDs)

Llave en ON (motor apagado esta bien). Guarda todo el trafico en dtc_probe_<fecha>.txt.
2026-11-19: flow control ISO-TP a mano (ATFCSH <modulo>, ATFCSD 30 00 00, ATFCSM 1), como el CVTz50: sin eso el
vLinker no lo manda a los IDs fuera de 7E0-7E7 y las respuestas largas (IPDM 74D: 407 bytes) llegaban cortadas.
Una respuesta cortada ahora sale como "sin respuesta valida", no como "OK, 0 codes". Orden: ATFCSH antes de ATFCSM 1
(con FCSM 1 primero el vLinker contesta "?" y se queda en automatico, Pi 2026-11-19).
"""
import argparse
import datetime
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("Falta pyserial:  pip install pyserial")

# IDs comunes en Nissan (request, respuesta). Confirmar en el Rogue.
MODULES = [
    ("ECM", "7E0", "7E8"),
    ("TCM", "7E1", "7E9"),
    ("ABS", "740", "760"),
    ("BCM", "745", "765"),
]

REQUESTS = [
    ("KWP 18 (Nissan)", "18 02 FF 00", 0x58),
    ("UDS 19 02", "19 02 FF", 0x59),
]

NRC = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectLength", 0x21: "busyRepeatRequest", 0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange", 0x33: "securityAccessDenied", 0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession", 0x7F: "serviceNotSupportedInActiveSession",
}


class Elm:
    def __init__(self, port, baud, log):
        self.ser = serial.Serial(port, baud, timeout=0.1)
        self.log = log

    def cmd(self, text, timeout=3.0):
        self.ser.reset_input_buffer()
        self.ser.write((text + "\r").encode())
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        out = buf.decode(errors="replace").replace(">", "").strip()
        lines = [l.strip() for l in out.replace("\r", "\n").split("\n") if l.strip()]
        lines = [l for l in lines if l != text]  # por si el echo sigue prendido
        self.log.write(f">> {text}\n" + "".join(f"<< {l}\n" for l in lines))
        self.log.flush()
        return lines

    def close(self):
        self.ser.close()


def hexbytes(s):
    return [int(x, 16) for x in s.split()]


def reassemble(lines, rx):
    """Arma la respuesta ISO-TP de las lineas con header (ATH1, ATS1). Devuelve lista de bytes."""
    data, total = [], None
    for l in lines:
        parts = l.split()
        if not parts or parts[0].upper() != rx.upper():
            continue
        try:
            b = [int(x, 16) for x in parts[1:]]
        except ValueError:
            continue
        if not b:
            continue
        pci = b[0] >> 4
        if pci == 0:                     # single frame
            n = b[0] & 0x0F
            frame = b[1:1 + n]
            if len(frame) >= 3 and frame[0] == 0x7F and frame[2] == 0x78:
                continue                 # responsePending: esperar la de verdad
            return frame
        if pci == 1:                     # first frame
            total = ((b[0] & 0x0F) << 8) | b[1]
            data = b[2:]
        elif pci == 2 and total is not None:  # consecutive
            data += b[1:]
    if total is not None and len(data) >= total:
        return data[:total]
    return None                          # nada, o cortada (llego el primer frame y no el resto)


def dtc_code(hi, lo):
    letter = "PCBU"[(hi >> 6) & 3]
    return f"{letter}{(hi >> 4) & 3}{hi & 0xF:X}{lo >> 4:X}{lo & 0xF:X}"


def decode(resp, pos):
    if pos == 0x58:   # 58 N [hi lo status]*N
        n = resp[1] if len(resp) > 1 else 0
        recs = resp[2:]
        return [(dtc_code(recs[i], recs[i + 1]), recs[i + 2])
                for i in range(0, min(len(recs), n * 3) - 2, 3)]
    if pos == 0x59:   # 59 02 mask [hi lo ft status]*
        recs = resp[3:]
        return [(f"{dtc_code(recs[i], recs[i + 1])}-{recs[i + 2]:02X}", recs[i + 3])
                for i in range(0, len(recs) - 3, 4)]
    if pos == 0x43:   # Mode 03: 43 [N] [hi lo]*
        recs = resp[1:]
        if len(recs) % 2:
            recs = recs[1:]      # CAN pone el conteo primero
        return [(dtc_code(recs[i], recs[i + 1]), None)
                for i in range(0, len(recs) - 1, 2) if recs[i] or recs[i + 1]]
    return []


def ask(elm, rx, req, pos):
    lines = elm.cmd(req, timeout=4.0)
    if any("NO DATA" in l or "CAN ERROR" in l for l in lines):
        return "sin respuesta", None
    resp = reassemble(lines, rx)
    if not resp:
        return "sin respuesta valida (" + " | ".join(lines)[:60] + ")", None
    if resp[0] == 0x7F:
        code = resp[2] if len(resp) > 2 else 0
        return f"rechazado: NRC {code:02X} {NRC.get(code, '?')}", ("nrc", code)
    if resp[0] == pos:
        return "OK", decode(resp, pos)
    return "respuesta rara: " + " ".join(f"{b:02X}" for b in resp), None


def main():
    ap = argparse.ArgumentParser(description="Lee DTCs de cada modulo via vLinker (solo lectura).")
    ap.add_argument("--port", required=True, help="COMx del vLinker")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--extra", action="append", default=[], help="TX:RX extra, ej. 743:763")
    a = ap.parse_args()

    mods = list(MODULES) + [(f"0x{e.split(':')[0]}", *e.split(":")) for e in a.extra]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logname = f"dtc_probe_{stamp}.txt"

    with open(logname, "w") as log:
        elm = Elm(a.port, a.baud, log)
        try:
            for c in ["ATZ", "ATE0", "ATL0", "ATS1", "ATH1", "ATSP6", "ATCAF1", "ATST96"]:
                r = elm.cmd(c, timeout=3.0 if c != "ATZ" else 5.0)
                if c == "ATZ":
                    print("Adaptador:", " ".join(r) or "(no contesto - revisa puerto/baud)")
                    time.sleep(0.5)

            summary = []
            for name, tx, rx in mods:
                tried_c0 = False
                print(f"\n=== {name}  ({tx} -> {rx}) ===")
                elm.cmd(f"ATSH {tx}")
                for c in (f"ATFCSH {tx}", "ATFCSD 30 00 00", "ATFCSM 1"):   # flow control a este modulo, en el orden
                    elm.cmd(c)                                              # del CVTz50: FCSM 1 sin FCSH antes = "?"
                elm.cmd(f"ATCRA {rx}")
                worked = None
                for label, req, pos in REQUESTS:
                    status, res = ask(elm, rx, req, pos)
                    silent = status == "sin respuesta" and not tried_c0   # el IPDM dejo de contestar sin sesion (2026-11-19)
                    if (res and res[0] == "nrc" and res[1] in (0x7F, 0x22, 0x7E)) or silent:
                        print(f"  {label}: {status} -> abriendo sesion 10 C0 y repitiendo")
                        print(f"     10 C0 -> {' | '.join(elm.cmd('10 C0')) or 'nada'}")
                        tried_c0 = True
                        status, res = ask(elm, rx, req, pos)
                        label += " (sesion C0)"
                    if status == "OK":
                        codes = res or []
                        print(f"  {label}: OK, {len(codes)} code(s)")
                        for code, st in codes:
                            print(f"     {code}" + (f"   status {st:02X}" if st is not None else ""))
                        worked = worked or label
                    else:
                        print(f"  {label}: {status}")
                if name == "ECM":
                    elm.cmd("ATSH 7DF")
                    elm.cmd("ATFCSH 7E0")     # la respuesta viene de 7E8: flow control a 7E0 (FCSD / FCSM 1 ya puestos)
                    status, res = ask(elm, "7E8", "03", 0x43)
                    print(f"  OBD2 Mode 03: {status}" + (f", {len(res)} code(s)" if status == "OK" else ""))
                    for code, _ in (res or []) if status == "OK" else []:
                        print(f"     {code}")
                summary.append((name, worked))
                if tried_c0:
                    elm.cmd("10 81")  # devolver el modulo a la sesion normal
                elm.cmd("ATAR")   # quitar filtro

            print("\n--- Resumen ---")
            for name, w in summary:
                print(f"  {name}: {w or 'ningun comando funciono'}")
        finally:
            elm.close()
    print(f"\nTodo el trafico quedo en {logname}  (mandamelo si algo sale raro)")


if __name__ == "__main__":
    main()
