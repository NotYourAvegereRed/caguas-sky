#!/usr/bin/env python3
"""Patches ~/sky/service/hud_service.py so the screen level comes from the light sensor (/dev/shm/caguas-lux,
'pct') when it is fresh, and from ILL + night_pct when it is not. Only touches two spots; backup in
hud_service.py.bak-lux. Run once: python3 patch_hud_lux.py"""
import os, shutil, sys
P = os.path.expanduser("~/sky/service/hud_service.py")
s = open(P).read()
if "LUX_FILE" in s:
    sys.exit("already patched, nothing to do")
A_OLD = 'LIGHTS_FILE = os.environ.get("HUD_LIGHTS_FILE", "/dev/shm/caguas-lights")\n'
A_NEW = A_OLD + 'LUX_FILE = os.environ.get("HUD_LUX_FILE", "/dev/shm/caguas-lux")      # light_reader.py: screen level from the VEML7700\n'
B_OLD = '''    def level(self):
        return self.cfg["night_pct"] if self.night else 100
'''
B_NEW = '''    def level(self):
        # light sensor first (light_reader.py, fresh '-test' file wins); stale -> ILL + night_pct as before
        pct = self.read_fresh(LUX_FILE, "pct", lambda v: isinstance(v, (int, float)) and 0 <= v <= 100)
        if pct is not None:
            return int(pct)
        return self.cfg["night_pct"] if self.night else 100
'''
for old, what in ((A_OLD, "LIGHTS_FILE line"), (B_OLD, "level() method")):
    if s.count(old) != 1:
        sys.exit(f"could not find the {what} exactly once: nothing changed. Send Claude your hud_service.py")
shutil.copy2(P, P + ".bak-lux")
s = s.replace(A_OLD, A_NEW).replace(B_OLD, B_NEW)
open(P, "w").write(s)
print(f"patched {P} (backup {P}.bak-lux)")
