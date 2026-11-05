#!/usr/bin/env python3
from camilladsp import CamillaClient
import yaml

HOST, PORT = "127.0.0.1", 1234
OUT = "/home/zen/camilladsp/configs/rogue_xover.yml"

# ---------- EDIT THESE (placeholder starting points, not final) ----------
TWEETER_HP = 3500
MID_HP, MID_LP = 500, 3500
MIDBASS_HP, MIDBASS_LP = 80, 500
SUB_LP = 80
SUBSONIC_HP = 25
ORDER = 4
START_GAIN_DB = -10
# ------------------------------------------------------------------------

def lr(kind, freq):
    return {"type": "BiquadCombo",
            "parameters": {"type": kind, "freq": freq, "order": ORDER}}

HP, LP = "LinkwitzRileyHighpass", "LinkwitzRileyLowpass"

filters = {
    "tw_hp": lr(HP, TWEETER_HP),
    "mid_hp": lr(HP, MID_HP), "mid_lp": lr(LP, MID_LP),
    "mb_hp": lr(HP, MIDBASS_HP), "mb_lp": lr(LP, MIDBASS_LP),
    "sub_lp": lr(LP, SUB_LP),
    "sub_subsonic": {"type": "BiquadCombo",
                     "parameters": {"type": "ButterworthHighpass",
                                    "freq": SUBSONIC_HP, "order": 2}},
}
for ch in range(8):
    filters[f"gain_ch{ch}"] = {"type": "Gain",
                               "parameters": {"gain": START_GAIN_DB}}
    filters[f"delay_ch{ch}"] = {"type": "Delay",
                                "parameters": {"delay": 0.0, "unit": "ms",
                                               "subsample": False}}

xover_steps = [
    ([0, 1], ["tw_hp"]),
    ([2, 3], ["mid_hp", "mid_lp"]),
    ([4, 5], ["mb_hp", "mb_lp"]),
    ([6, 7], ["sub_subsonic", "sub_lp"]),
]

client = CamillaClient(HOST, PORT)
client.connect()
cfg = client.config.active()

pipeline = [s for s in cfg["pipeline"] if s.get("type") == "Mixer"]
for chans, names in xover_steps:
    pipeline.append({"type": "Filter", "channels": chans, "names": names})
for ch in range(8):
    pipeline.append({"type": "Filter", "channels": [ch],
                     "names": [f"gain_ch{ch}", f"delay_ch{ch}"]})

cfg["filters"] = {**cfg.get("filters", {}), **filters}
cfg["pipeline"] = pipeline

try:
    client.config.validate(cfg)
    print("Config validated OK")
except Exception as e:
    print("Validation problem:", e)

with open(OUT, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print("Saved to", OUT)
print("Next: CamillaGUI -> Files tab -> load it -> Apply.")
