"""Self-check for the reverse camera decision in hud_service.py (Display.update_cam).
Run:  ~/hud-venv/bin/python dashboard/service/cam_test.py"""
import hud_service as h

d = h.Display.__new__(h.Display)
d.cam, d.cam_t, d.rev_t, d.r_since = 0, -1e9, -1e9, None
step = lambda gear, rev, now: (d.update_cam(gear, rev, now), d.cam)
assert step(None, None, 0) == (False, 0)          # no OBD, no RP2040: off
assert step("P", 0, 1) == (True, 1)               # P: warm
assert step("R", 0, 2) == (False, 1)              # gear R before the wire: warm, waiting CAM_R_CONFIRM_S
assert step("R", 1, 2.25) == (False, 1)
assert step("R", 1, 2.5) == (True, 2)             # R held 0.5 s: on screen
assert step("R", 1, 3) == (False, 2)
assert step("N", 1, 4) == (False, 2)              # wire still on
assert step("D", 0, 5) == (False, 2)              # D: still on screen CAM_SHOW_S after the wire went off
assert step("D", 0, 4 + h.CAM_SHOW_S) == (True, 1)   # then warm
assert step("D", 0, 4 + h.CAM_SHOW_S + h.CAM_HOLD_S) == (True, 0)
assert step(None, 1, 30) == (True, 1)             # no OBD: the reverse wire alone (warm first)
assert step(None, 1, 30.5) == (True, 2)           # ...after 0.5 s too
d.cam, d.cam_t, d.rev_t, d.r_since = 0, -1e9, -1e9, None
assert step("P", 0, 100) == (True, 1)             # P -> D through R and N: never on screen
assert step("R", 1, 100.25) == (False, 1)
assert step("N", 0, 100.5) == (False, 1)
assert step("D", 0, 100.75) == (False, 1)
assert step("R", 1, 101) == (False, 1)            # R again: the 0.5 s count starts over
assert step("R", 1, 101.5) == (True, 2)
print("cam_test: PASS")
