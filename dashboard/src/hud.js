// HUD: CVT (2x2: primary, secondary, fluid temp, torque converter) + now playing with album art (right). Panels cover the screen.
// Data contract (JSON, same shape for demo and live):
//   { cvt:   { p_primary_target_mpa, p_primary_actual_mpa,
//              p_secondary_target_mpa, p_secondary_actual_mpa,
//              temp_c, lockup: "open"|"engaging"|"releasing"|"locked"|"coast", slip_rpm,
//              tcc_a (torque converter clutch solenoid current, A) },
//     media: { status: "playing"|"paused"|"stopped", title, artist, art, position_ms, duration_ms, codec } }
//   media.art = image URL or data URI. Missing art -> placeholder.
//   vol = { db, mute, min_db, max_db } (CamillaDSP main volume) | null; page sends {cmd:"vol", db}.
// Live source: hud-service on ws://<host>:8766 pushes {"media": ...} about once a second and
// {"cvt": ...} ~12 times a second (vLinker, read-only; null = no adapter / car off),
// and receives {cmd: "toggle"|"next"|"prev"}.
// Smoothing: data arrives ~12 Hz, needles are drawn at 60 fps easing toward the newest value
// (interpolation, not averaging, so shift/crank pressure peaks still show). Tune SMOOTH_TAU.
// Console helper for testing: hud.setCvt({...}), hud.setMedia({...}).
// Telemetry page (swipe left) lives in telemetry.js; this file feeds it with "hud-cvt" / "hud-msg" events.

// ---- Gauge colors ----------------------------------------------------------
// Each gauge arc takes its color from a list of [value, color] stops and blends
// smoothly between them. Edit the stops here. The ones marked (placeholder) are
// not from you: only the points you gave are fixed.
const BLUE = "#4da6ff", GREEN = "#7ce3c4", YELLOW = "#ffd54a",
      ORANGE = "#ff9a3c", RED = "#ff4d3d", VOLCANIC = "#ff2a12", DEEP_RED = "#d0121e";
const PRESSURE_STOPS = [          // MPa (full scale 6.0, % of the arc in brackets)
  [0.0, BLUE],   [0.8, BLUE],     // 0.0-0.8  (0-13%):  pure blue
  [1.2, GREEN],                   // 0.8-1.2  (13-20%): blue -> green
  [2.8, GREEN],                   // 1.2-2.8  (20-47%): solid green
  [3.2, YELLOW],                  // 2.8-3.2  (47-53%): green -> yellow, 3.0 = 50%
  [4.0, YELLOW],                  // 3.2-4.0  (53-67%): solid yellow
  [4.4, DEEP_RED],                // 4.0-4.4  (67-73%): yellow -> red
  [6.0, DEEP_RED]                 // 4.4-6.0  (73-100%): deep red
];
const TEMP_STOPS = [              // °C
  [20, BLUE], [45, BLUE],         // (placeholder) cold = blue
  [60, GREEN],                    // (placeholder) normal = green
  [90, GREEN],                    // 90: starts turning orange
  [95, YELLOW], [100, ORANGE],
  [110, RED],                     // 110: red
  [130, VOLCANIC]                 // 130: limit, volcanic red
];

// Pulley labels confirmed against CVTz50 on 2026-10-26 (PRI = primary, SEC = secondary).
const CONFIG = {
  // Live by default (CVT + media from hud-service). ?demo=1: fake data, only to judge the layout.
  demo: new URLSearchParams(location.search).get("demo") === "1",
  ws: `ws://${location.hostname}:8766`,
  // key = big number + arc; tgt = the TCM's target (white notch + gap band); peak = "gold" | "dim" | none
  gauges: [
    { key: "p_primary_actual_mpa",   tgt: "p_primary_target_mpa",   peak: "gold", label: "Primary",    sub: "pressure · actual vs target", unit: "MPa", min: 0,  max: 6,   dec: 2, stops: PRESSURE_STOPS },
    { key: "p_secondary_actual_mpa", tgt: "p_secondary_target_mpa", peak: "gold", label: "Secondary",  sub: "pressure · actual vs target", unit: "MPa", min: 0,  max: 6,   dec: 2, stops: PRESSURE_STOPS },
    { key: "temp_c",                                                peak: "dim",  label: "Fluid temp", sub: "CVT",                         unit: "°C",  min: 20, max: 130, dec: 0, stops: TEMP_STOPS }
  ]
};

const SMOOTH_TAU = 0.09;   // s; bigger = smoother but slower needles, 0 = no smoothing
const PEAK_MIN_MPA = 2.25;   // gold peak line only when the held pressure peak is above this
const PEAK_HOLD_MS = 5500;   // peak holds this long, then fades
const PEAK_FADE_MS = 600;    // fade time, then it resets to the live value

// Peak hold: jumps to any new max at once, holds PEAK_HOLD_MS, fades over PEAK_FADE_MS, then restarts from the live value.
// update() returns { v, a } (a = opacity 0..1) or null when there is no value.
class PeakHold {
  v = null; t = 0;
  update(x, now) {
    if (x == null) return (this.v = null);
    if (this.v == null || x >= this.v || now - this.t > PEAK_HOLD_MS + PEAK_FADE_MS) { this.v = x; this.t = now; }
    const age = now - this.t;
    return { v: this.v, a: age <= PEAK_HOLD_MS ? 1 : 1 - (age - PEAK_HOLD_MS) / PEAK_FADE_MS };
  }
}

const hex2rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
function colorAt(stops, v) {
  if (v <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) {
    const [v1, c1] = stops[i];
    if (v <= v1) {
      const [v0, c0] = stops[i - 1];
      const f = (v - v0) / (v1 - v0), a = hex2rgb(c0), b = hex2rgb(c1);
      return `rgb(${a.map((x, k) => Math.round(x + (b[k] - x) * f)).join(",")})`;
    }
  }
  return stops[stops.length - 1][1];
}

const ICON = {
  prev: '<svg viewBox="0 0 24 24" preserveAspectRatio="none"><path d="M6 6h2v12H6zM20 6v12L9.5 12z"/></svg>',
  next: '<svg viewBox="0 0 24 24" preserveAspectRatio="none"><path d="M16 6h2v12h-2zM4 6l10.5 6L4 18z"/></svg>',
  play: '<svg viewBox="0 0 24 24" preserveAspectRatio="none"><path d="M7 5v14l12-7z"/></svg>',
  pause:'<svg viewBox="0 0 24 24" preserveAspectRatio="none"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
  speaker:'<svg viewBox="0 0 24 32" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2" y="1.5" width="20" height="29" rx="2"/><circle cx="12" cy="8.5" r="2.4"/><circle cx="12" cy="20.5" r="5.8"/><circle cx="12" cy="20.5" r="1.6"/></svg>'
};

const $ = (id) => document.getElementById(id);
if (new URLSearchParams(location.search).get("bodies") === "0") document.body.classList.add("hud-hide-bodies");
const root = document.getElementById("hud");
root.innerHTML = `
  <section class="panel panel--np" id="panel-np">
    <div class="panel__head"><span>Now playing</span><span class="tag" id="tag-codec">BT</span></div>
    <div class="np">
      <div class="np__art"><img id="np-art" alt=""></div>
      <div class="np__text"><div class="np__title" id="np-title"></div><div class="np__artist" id="np-artist"></div></div>
      <div class="np__prog"><div class="np__bar"><div id="np-fill"></div></div>
        <div class="np__times"><span id="np-pos">0:00</span><span id="np-dur">0:00</span></div></div>
      <div class="np__controls">
        <button class="btn" id="b-prev" aria-label="Previous">${ICON.prev}</button>
        <button class="btn btn--main" id="b-play" aria-label="Play/Pause">${ICON.play}</button>
        <button class="btn" id="b-next" aria-label="Next">${ICON.next}</button>
      </div>
    </div>
  </section>
  <section class="panel" id="panel-cvt">
    <div class="panel__head"><span>CVT<span class="cvt-hz" id="cvt-hz"></span></span><span class="tag tag--demo" id="tag-cvt" hidden>DEMO</span></div>
    <div class="cvt" id="cvt"></div>
  </section>`;

// ---- engine-start animation (main screen) ----
// Standby (panels dimmed a little) while the car is on but the engine is not running; when rpm stays above
// WAKE_RPM for a moment the panels wake up: they brighten and lift, a soft sheen crosses the glass, the album
// cover comes into focus and the gauges climb to their values. Once per trip: it re-arms after the engine has
// been off (rpm <= WAKE_RPM or no OBD) for 60 s. No OBD data for 15 s (bench, car off) = no standby dimming.
const WAKE_RPM = 500;
const WAKE = { armed: true, k: 1, t0: 0, DELAY: 350, D: 1100, above: 0, offSince: performance.now(), nullSince: performance.now() };
function wakeCheck(c) {
  const now = performance.now(), rpm = c?.rpm;
  if (c == null) {
    WAKE.above = 0; WAKE.nullSince ??= now; WAKE.offSince ??= now;
    if (now - WAKE.nullSince > 15000) root.classList.remove("hud--standby");
  } else {
    WAKE.nullSince = null;
    if (rpm != null && rpm > WAKE_RPM) {
      WAKE.offSince = null;
      if (WAKE.armed) { WAKE.above ||= now; if (now - WAKE.above > 800) startWake(); }
    } else {
      WAKE.above = 0; WAKE.offSince ??= now;
    }
  }
  if (!WAKE.armed && WAKE.offSince != null && now - WAKE.offSince > 60000) WAKE.armed = true;   // new trip
  if (WAKE.armed && c != null && !(rpm > WAKE_RPM)) root.classList.add("hud--standby");
}
// ---- welcome sweep (welcome.js): every gauge goes to full scale and back down to its value, like a car's
// cluster at start. Used once per boot by the welcome animation; holdWake() keeps the normal wake out of the way.
const SWEEP = { on: false, t0: 0, DELAY: 250, D: 1700 };
if (new URLSearchParams(location.search).has("welcome")) WAKE.armed = false;   // preview: the welcome drives it
const easeIO = (x) => x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
function sweepFrac(fReal, now = performance.now()) {
  const x = Math.max(0, Math.min(1, (now - SWEEP.t0 - SWEEP.DELAY) / SWEEP.D));
  return x < .5 ? easeIO(x / .5) : 1 + (fReal - 1) * (1 - Math.pow(1 - (x - .5) / .5, 3));
}
function holdWake() { WAKE.armed = false; WAKE.k = 1; root.classList.remove("hud--standby", "hud--wake"); }
function startSweep() { holdWake(); SWEEP.t0 = performance.now(); SWEEP.on = true; }
function startWake() {
  WAKE.armed = false; WAKE.above = 0; WAKE.t0 = performance.now(); WAKE.k = 0;
  root.classList.remove("hud--standby", "hud--wake");
  void root.offsetWidth;                                   // restart the CSS animations
  root.classList.add("hud--wake");
  setTimeout(() => root.classList.remove("hud--wake"), 2400);
}

// ---- CVT tiles ----
const cvtHost = $("cvt");
const gaugeEls = {};
for (const g of CONFIG.gauges) {
  const el = document.createElement("div");
  el.className = "cell";
  // marks (target, peak) are drawn at angle 0 and turned into place with rotate(); x 104..116 = r 44..56 from the center, across the track
  el.innerHTML = `
    <div class="gauge__dial">
      <svg viewBox="0 0 120 120" preserveAspectRatio="none" aria-hidden="true">
        <circle class="gauge__track" cx="60" cy="60" r="50" pathLength="100" stroke-dasharray="75 100" transform="rotate(135 60 60)"/>
        ${g.tgt ? '<circle class="gauge__gap" cx="60" cy="60" r="50" pathLength="100" stroke-dasharray="0 100" transform="rotate(135 60 60)"/>' : ""}
        <circle class="gauge__value" cx="60" cy="60" r="50" pathLength="100" stroke-dasharray="0 100" transform="rotate(135 60 60)"/>
        ${g.peak ? `<g class="gauge__peak gauge__peak--${g.peak}" opacity="0"><line class="pk-glow" x1="104" y1="60" x2="116" y2="60"/><line class="pk-core" x1="104" y1="60" x2="116" y2="60"/></g>` : ""}
        ${g.tgt ? '<g class="gauge__tgt" opacity="0"><line x1="105" y1="60" x2="115" y2="60"/><polygon points="117.5,60 123.5,56.5 123.5,63.5"/></g>' : ""}
      </svg>
      <div class="gauge__num"><b>--</b><i>${g.unit}</i><small class="gauge__sub"></small></div>
    </div>
    <div class="cell__label">${g.label}<small>${g.sub}</small></div>`;
  cvtHost.appendChild(el);
  gaugeEls[g.key] = { el, arc: el.querySelector(".gauge__value"), num: el.querySelector("b"), cfg: g,
    gap: el.querySelector(".gauge__gap"), tgtEl: el.querySelector(".gauge__tgt"), pkEl: el.querySelector(".gauge__peak"),
    subEl: el.querySelector(".gauge__sub"), tgt: null, peak: g.peak ? new PeakHold() : null, raw: null };
}
const angleOf = (cfg, v) => 135 + 270 * Math.max(0, Math.min(1, (v - cfg.min) / (cfg.max - cfg.min)));
const fmtD = (x, dec) => (x < 0 ? "−" : "+") + Math.abs(x).toFixed(dec);   // real minus sign, like the mockup
// target notch + gap band + peak line + the small line under the number. Hidden during the wake / welcome sweeps.
// Only writes to the DOM when something visible changed (same rule as drawGauge).
function drawMarks(o, now) {
  const { cfg } = o, set = (k, el, attr, val) => { if (o[k] !== val) { el.setAttribute(attr, val); o[k] = val; } };
  const anim = WAKE.k < 1 || SWEEP.on || o.shown == null;
  if (o.tgtEl) {
    const ok = !anim && o.tgt != null;
    set("_tO", o.tgtEl, "opacity", ok ? "1" : "0"); set("_gO", o.gap, "opacity", ok ? ".35" : "0");
    if (ok) {
      const fA = 75 * (angleOf(cfg, o.shown) - 135) / 270, fT = 75 * (angleOf(cfg, o.tgt) - 135) / 270;
      set("_tR", o.tgtEl, "transform", `rotate(${angleOf(cfg, o.tgt).toFixed(1)} 60 60)`);
      set("_gD", o.gap, "stroke-dasharray", `${Math.abs(fA - fT).toFixed(2)} 100`);
      set("_gF", o.gap, "stroke-dashoffset", (-Math.min(fA, fT)).toFixed(2));
    }
  }
  const p = o.peak?.update(o.raw, now);
  if (o.pkEl) {
    const show = !anim && p && (cfg.peak === "dim" || p.v > PEAK_MIN_MPA);
    set("_pO", o.pkEl, "opacity", show ? p.a.toFixed(2) : "0");
    if (show) set("_pR", o.pkEl, "transform", `rotate(${angleOf(cfg, p.v).toFixed(1)} 60 60)`);
  }
  const sub = o.shown == null ? ""
    : cfg.tgt ? (o.tgt == null ? "" : `T ${o.tgt.toFixed(cfg.dec)} · Δ ${fmtD(o.shown - o.tgt, cfg.dec)}`)
    : p ? `P = ${p.v.toFixed(cfg.dec)}` : "";
  if (sub !== o._sub) { o.subEl.textContent = sub; o._sub = sub; }
}
// lockup tile: arc = TCC solenoid current (0..TCC_MAX_A), colored by state; chip = state; small line = slip
const TCC_MAX_A = 0.7;   // full arc = the max the TCM commands on this car (0.70 A in the logs)
const lockEl = document.createElement("div");
lockEl.className = "cell"; lockEl.dataset.lock = "open";
lockEl.innerHTML = `
  <div class="gauge__dial lock__dial">
    <svg viewBox="0 0 120 120" preserveAspectRatio="none" aria-hidden="true">
      <circle class="gauge__track" cx="60" cy="60" r="50" pathLength="100" stroke-dasharray="75 100" transform="rotate(135 60 60)"/>
      <circle class="gauge__value lock__arc" id="lock-arc" cx="60" cy="60" r="50" pathLength="100" stroke-dasharray="0 100" transform="rotate(135 60 60)"/>
    </svg>
    <div class="gauge__num lock__in">
      <span class="lock__chip" id="lock-chip">--</span>
      <b id="lock-amps">--</b><i>A</i>
      <span class="lock__coast" id="lock-coast">coast</span>
    </div>
  </div>
  <div class="cell__label">Torque converter<small id="lock-slip">slip -- rpm</small></div>`;
cvtHost.appendChild(lockEl);

// target = newest value from the data source; shown = what is drawn (eases toward target)
// Each new sample starts a straight-line move from where the needle is now to the new value, lasting as long as
// the gap since the previous sample (~70 ms live at 15 Hz, 250 ms in the demo): constant speed, so the needle
// never stops and restarts between samples (what the old CSS transition did, without animating twice).
function retarget(o, v, now = performance.now()) {
  const gap = o.lastT == null ? 70 : now - o.lastT;
  o.lastT = now; o.from = o.shown; o.target = v; o.t0 = now; o.dur = Math.max(30, Math.min(450, gap * 1.2));   // a bit longer than the gap: no stop if the next sample is late
}
function stepTo(o, now) {
  if (o.dur == null) { o.shown = o.target; return; }
  const x = Math.min(1, (now - o.t0) / o.dur);
  o.shown = o.from + (o.target - o.from) * x;
}
function setGauge(key, v) {
  const o = gaugeEls[key]; if (!o) return;
  o.raw = v == null || Number.isNaN(v) ? null : v;   // the peak follows raw samples, not the eased needle
  if (v == null || Number.isNaN(v)) { o.target = o.shown = null; o.lastT = o.dur = null; drawGauge(o, null); return; }
  if (o.shown == null || SMOOTH_TAU <= 0) { o.shown = o.target = v; o.dur = null; o.lastT = performance.now(); drawGauge(o, v); return; }
  retarget(o, v);
}
// Drawing only touches the DOM when something visible changes (arc in 0.1 steps, the shown digits, the color):
// on the Pi every DOM write means a repaint on the GPU, and the needles move all the time while driving.
function drawGauge(o, v) {
  if (v == null) {
    if (o.txt !== "--") { o.num.textContent = "--"; o.arc.style.opacity = 0; o.el.style.removeProperty("--lvl"); }
    o.txt = "--"; o.dash = o.col = null; return;
  }
  if (o.txt === "--" || o.txt == null) o.arc.style.opacity = 1;
  const { min, max, dec, stops } = o.cfg;
  if (WAKE.k < 1) v = min + (v - min) * WAKE.k;          // engine-start sweep: climbs from the bottom
  let f = Math.max(0, Math.min(1, (v - min) / (max - min)));
  if (SWEEP.on) { f = sweepFrac(f); v = min + f * (max - min); }   // welcome: full scale and back
  const dash = `${(75 * f).toFixed(2)} 100`, txt = v.toFixed(dec), col = colorAt(stops, Number(txt));
  if (dash !== o.dash) { o.arc.setAttribute("stroke-dasharray", dash); o.dash = dash; }
  if (txt !== o.txt) { o.num.textContent = txt; o.txt = txt; }
  if (col !== o.col) { o.el.style.setProperty("--lvl", col); o.col = col; }
}
// coast is shown as "Locked" + a small "coast" line, the way cvt_probe.py shows "LOCKED (coast)"
const LOCK_TEXT = { open: "Unlocked", engaging: "Engaging", releasing: "Releasing", locked: "Locked", coast: "Locked" };
const slipS = { target: null, shown: null };
const ampS = { target: null, shown: null };
function drawSlip(v) {
  const r = v == null ? null : Math.round(v);
  const txt = r == null ? "slip -- rpm"
    : Math.abs(r) >= 127 ? `slip ${r > 0 ? ">+" : "<"}${r} rpm`   // the byte saturates at ±127: real slip is bigger
    : `slip ${r} rpm`;
  if (txt !== slipS.txt) { $("lock-slip").textContent = txt; slipS.txt = txt; }
}
function drawAmps(a) {
  const arc = $("lock-arc");
  if (a == null) { $("lock-amps").textContent = "--"; arc.style.opacity = 0; ampS.dash = ampS.txt = null; return; }
  arc.style.opacity = 1;
  if (WAKE.k < 1) a *= WAKE.k;
  if (SWEEP.on) a = TCC_MAX_A * sweepFrac(Math.max(0, Math.min(1, a / TCC_MAX_A)));
  const dash = `${(75 * Math.max(0, Math.min(1, a / TCC_MAX_A))).toFixed(2)} 100`, txt = a.toFixed(2);
  if (dash !== ampS.dash) { arc.setAttribute("stroke-dasharray", dash); ampS.dash = dash; }
  if (txt !== ampS.txt) { $("lock-amps").textContent = txt; ampS.txt = txt; }
}
function setLockup(state, slip, amps) {
  const ok = state in LOCK_TEXT;
  lockEl.dataset.lock = ok ? state : "open";
  $("lock-chip").textContent = ok ? LOCK_TEXT[state] : "--";
  for (const [s, v, draw] of [[slipS, slip, drawSlip], [ampS, amps, drawAmps]]) {
    if (v == null || Number.isNaN(v)) { s.target = s.shown = null; s.lastT = s.dur = null; draw(null); continue; }
    if (s.shown == null || SMOOTH_TAU <= 0) { s.shown = s.target = v; s.dur = null; s.lastT = performance.now(); draw(v); continue; }
    retarget(s, v);
  }
}
// easing loop: the values ease every frame; each needle is drawn at an adaptive rate (15-60 fps by speed, see due())
let lastFrame = performance.now(), lastDraw = 0;
const DRAW_FPS = Number(new URLSearchParams(location.search).get("fps")) || 60;   // ?fps=45 / 60 to compare
const DRAW_MS = 1000 / DRAW_FPS - 1;
// [needle speed in full scales per second, draw fps]: first row whose speed is reached wins.
// Picked so a needle never jumps more than ~1 px per draw (the 176 px dial's arc is ~415 px long).
const ADAPT_FPS = [[0.072, 60], [0.036, 30], [0, 15]];
function due(o, span, cap, now) {
  const moving = o.dur != null && now - o.t0 < o.dur;
  const speed = moving ? Math.abs(o.target - o.from) / span / (o.dur / 1000) : 0;
  const fps = Math.min(cap, ADAPT_FPS.find(([s]) => speed >= s)[1]);
  if (now - (o.lastDraw ?? -1e9) < 1000 / fps - 1) return false;
  o.lastDraw = now; return true;
}
function tick(now) {
  const dt = Math.min(0.1, (now - lastFrame) / 1000); lastFrame = now;
  const paint = now - lastDraw >= DRAW_MS;
  if (paint) lastDraw = now;
  const k = SMOOTH_TAU > 0 ? 1 - Math.exp(-dt / SMOOTH_TAU) : 1;
  if (WAKE.k < 1) {                                       // engine-start sweep in progress: redraw every frame
    const x = Math.max(0, Math.min(1, (now - WAKE.t0 - WAKE.DELAY) / WAKE.D));
    WAKE.k = x >= 1 ? 1 : 1 - Math.pow(1 - x, 3);         // ease-out
    if (paint || WAKE.k >= 1) {
      for (const o of Object.values(gaugeEls)) if (o.shown != null) drawGauge(o, o.shown);
      if (ampS.shown != null) drawAmps(ampS.shown);
    }
  }
  if (SWEEP.on) {                                         // welcome sweep in progress
    if (paint) {
      for (const o of Object.values(gaugeEls)) if (o.shown != null) drawGauge(o, o.shown);
      if (ampS.shown != null) drawAmps(ampS.shown);
    }
    if (now - SWEEP.t0 > SWEEP.DELAY + SWEEP.D) {
      SWEEP.on = false;
      for (const o of Object.values(gaugeEls)) if (o.shown != null) drawGauge(o, o.shown);
      if (ampS.shown != null) drawAmps(ampS.shown);
    }
  }
  // adaptive: each gauge picks its own draw rate from how fast its needle is moving (ADAPT_FPS), capped by ?fps=
  for (const o of Object.values(gaugeEls)) {
    if (o.target != null && o.shown != null) stepTo(o, now);
    if (!due(o, o.cfg.max - o.cfg.min, DRAW_FPS, now)) continue;
    if (o.shown != null && o.shown !== o.drawnAt) { drawGauge(o, o.shown); o.drawnAt = o.shown; }
    drawMarks(o, now);
  }
  for (const [s, draw, span] of [[slipS, drawSlip, 254], [ampS, drawAmps, TCC_MAX_A]]) {
    if (s.target == null || s.shown == null) continue;
    stepTo(s, now);
    if (due(s, span, DRAW_FPS, now) && s.shown !== s.drawnAt) { draw(s.shown); s.drawnAt = s.shown; }
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
function setCvt(c) {
  window.dispatchEvent(new CustomEvent("hud-cvt", { detail: c ?? null }));   // telemetry page (telemetry.js)
  wakeCheck(c);
  $("cvt-hz").textContent = c?.hz ? `${Math.round(c.hz)} Hz` : "";      // CVT readings per second
  for (const g of CONFIG.gauges) {
    setGauge(g.key, c?.[g.key]);
    if (g.tgt) { const t = c?.[g.tgt]; gaugeEls[g.key].tgt = t == null || Number.isNaN(t) ? null : t; }
  }
  setLockup(c?.lockup, c?.slip_rpm, c?.tcc_a);
}

// ---- now playing ----
const fmt = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const PLACEHOLDER_ART = "data:image/svg+xml;utf8," + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><rect width="200" height="200" fill="#1c2036"/><path d="M118 52v70.5a22 22 0 1 1-12-19.6V72l-40 8v52.5a22 22 0 1 1-12-19.6V66z" fill="#3a4170"/></svg>`);
let sendCmd = () => {};
$("b-prev").onclick = () => sendCmd("prev");
$("b-next").onclick = () => sendCmd("next");
$("b-play").onclick = () => sendCmd("toggle");
const artImg = $("np-art");
artImg.onerror = () => { if (artImg.src !== PLACEHOLDER_ART) artImg.src = PLACEHOLDER_ART; };
let lastArt = null;
function setMedia(m) {
  const playing = m && m.status === "playing";
  $("b-play").innerHTML = playing ? ICON.pause : ICON.play;
  $("tag-codec").textContent = m?.codec ? `BT · ${m.codec}` : "BT";
  const art = m?.art || PLACEHOLDER_ART;
  if (art !== lastArt) { artImg.src = art; lastArt = art; }
  $("np-title").textContent = m?.title || "Nothing playing";
  $("np-artist").textContent = m?.title ? (m.artist || "") : "";
  const d = m?.duration_ms || 0, p = m?.position_ms || 0;
  $("np-fill").style.width = d ? `${Math.min(100, (p / d) * 100)}%` : "0%";
  $("np-pos").textContent = fmt(p); $("np-dur").textContent = fmt(d);
}

// ---- volume: tap the LEFT half (not on a button) -> One UI-style pill, left of the album cover ----
// Shows 0-100 like the phone; underneath it is CamillaDSP's main volume, linear in dB from min_db to max_db.
// Look: blurred lilac / teal / silver blobs drifting through the WHOLE pill; the empty (top) part is the same
// field darkened, the fill line is feathered, white glow around. Blobs move with transform only (cheap on the Pi)
// and only while the pill is open (display:none otherwise, so it costs nothing when hidden).
const volEl = document.createElement("div");
volEl.className = "vol__pill"; volEl.id = "vol-pill"; volEl.style.display = "none";
volEl.innerHTML = `
    <div class="vol__field"><i class="vb vb--a"></i><i class="vb vb--b"></i><i class="vb vb--c"></i><i class="vb vb--d"></i><i class="vb vb--e"></i></div>
    <div class="vol__shade" id="vol-shade"></div>
    <div class="vol__grain"></div>
    <div class="vol__top"><span class="vol__num" id="vol-num">--</span></div>`;
$("panel-np").appendChild(volEl);
const VOL = { db: null, mute: false, min: -60, max: 0, dragging: false, lastUser: -1e9, hideT: 0, sendT: 0, pending: null };
const volFrac = (db) => Math.max(0, Math.min(1, (db - VOL.min) / (VOL.max - VOL.min)));
function drawVol() {
  const f = VOL.db == null || VOL.mute ? 0 : volFrac(VOL.db);
  $("vol-shade").style.height = `${((1 - f) * 100).toFixed(1)}%`;
  $("vol-num").textContent = VOL.db == null ? "--" : VOL.mute ? "0" : Math.round(f * 100);
}
// place it in the space left of the cover, from the cover's top to the bottom of the artist line
function placeVol() {
  const panel = $("panel-np").getBoundingClientRect();
  const art = document.querySelector(".np__art").getBoundingClientRect();
  const text = document.querySelector(".np__text").getBoundingClientRect();
  const w = 76, gap = art.left - panel.left;
  volEl.style.width = `${w}px`;
  volEl.style.left = `${Math.max(8, (gap - w) / 2)}px`;
  volEl.style.top = `${art.top - panel.top}px`;
  volEl.style.height = `${Math.max(200, text.bottom - art.top)}px`;
}
const isOpen = () => volEl.classList.contains("vol--on");
function showVol() {
  if (VOL.db == null) return;                                  // no CamillaDSP: nothing to control
  if (volEl.style.display === "none") { placeVol(); volEl.style.display = "block"; }
  requestAnimationFrame(() => volEl.classList.add("vol--on"));
  clearTimeout(VOL.hideT); VOL.hideT = setTimeout(hideVol, 3000);
}
function hideVol() {
  if (VOL.dragging) { VOL.hideT = setTimeout(hideVol, 1000); return; }
  clearTimeout(VOL.hideT);
  volEl.classList.remove("vol--on");
  setTimeout(() => { if (!isOpen()) volEl.style.display = "none"; }, 250);
}
function sendVol(db) {           // ~16 updates/s while dragging; CamillaDSP ramps between them
  VOL.pending = db;
  if (VOL.sendT) return;
  VOL.sendT = setTimeout(() => { VOL.sendT = 0; if (VOL.pending != null) { sendCmd({ cmd: "vol", db: VOL.pending }); VOL.pending = null; } }, 60);
}
function volFromPointer(e) {
  const r = volEl.getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (r.bottom - e.clientY) / r.height));
  VOL.db = Math.round((VOL.min + f * (VOL.max - VOL.min)) * 2) / 2;   // 0.5 dB steps
  VOL.mute = false;
  VOL.lastUser = performance.now(); drawVol(); sendVol(VOL.db); showVol();
}
volEl.addEventListener("pointerdown", (e) => { VOL.dragging = true; volEl.setPointerCapture?.(e.pointerId); volFromPointer(e); });
volEl.addEventListener("pointermove", (e) => { if (VOL.dragging) volFromPointer(e); });
for (const ev of ["pointerup", "pointercancel"]) volEl.addEventListener(ev, () => { VOL.dragging = false; VOL.lastUser = performance.now(); });
// A tap (finger down and up without moving, so swipes to the telemetry page never open it) on the left half of
// the main screen opens it, unless it was a button; if it is already open, a tap outside it closes it.
// "hud-tap" comes from the swipe handler in telemetry.js.
window.addEventListener("hud-tap", (ev) => {
  const { x, target, view } = ev.detail;
  if (view === "tele" || volEl.contains(target)) return;
  if (isOpen()) { hideVol(); return; }
  if (!target.closest("button, .t-badge") && x < window.innerWidth / 2) showVol();
});
window.addEventListener("resize", () => { if (volEl.style.display !== "none") placeVol(); });
// a swipe to the telemetry page started while the pill was open: close it quickly (telemetry.js)
window.addEventListener("hud-hidevol", () => {
  if (!isOpen()) return;
  volEl.classList.add("vol--quick"); VOL.dragging = false; hideVol();
  setTimeout(() => volEl.classList.remove("vol--quick"), 200);
});
function setVol(v) {
  // ignore the service's echo while (and just after) the finger is on the slider
  if (VOL.dragging || performance.now() - VOL.lastUser < 700) return;
  if (v) { VOL.db = v.db; VOL.mute = v.mute; VOL.min = v.min_db ?? VOL.min; VOL.max = v.max_db ?? VOL.max; }
  else { VOL.db = null; if (volEl.style.display !== "none") hideVol(); }
  drawVol();
}
setVol(null);

// ---- demo source (fake data, only to judge the layout) ----
function demoArt(h1, h2) {
  return "data:image/svg+xml;utf8," + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${h1} 70% 55%)"/><stop offset="1" stop-color="hsl(${h2} 65% 28%)"/></linearGradient></defs><rect width="300" height="300" fill="url(#g)"/><circle cx="210" cy="90" r="70" fill="hsl(${h1} 80% 75% / .35)"/><circle cx="90" cy="220" r="90" fill="hsl(${h2} 70% 45% / .45)"/></svg>`);
}
function demoCvt(t) {
  const thr = (Math.sin(t / 3.1) + 1) / 2;
  const tp = 0.9 + 5.0 * thr * (0.8 + 0.2 * Math.sin(t / 1.7));
  const ts = 0.7 + 3.8 * thr * (0.8 + 0.2 * Math.cos(t / 2.3));
  const lag = (x, k) => Math.max(0, x - k * (1 - thr));  // actual trails target a bit
  const ph = Math.floor(t / 5) % 5;                      // unlocked -> engaging -> locked -> coast -> releasing
  const inPh = (t % 5) / 5;
  return {
    p_primary_target_mpa: tp, p_primary_actual_mpa: lag(tp, 0.3),
    p_secondary_target_mpa: ts, p_secondary_actual_mpa: lag(ts, 0.2),
    temp_c: 88 + 28 * Math.sin(t / 22),                  // sweeps ~60 to ~116 so you can see the colors
    lockup: ["open", "engaging", "locked", "coast", "releasing"][ph],
    slip_rpm: [127, 127 - 90 * inPh, -3, -4, -127][ph] + (ph === 2 || ph === 3 ? 2 * Math.sin(t) : 0),
    tcc_a: [0, 0.35 + 0.1 * inPh, 0.62, 0.42, 0.42 * (1 - inPh)][ph],
    rpm: new URLSearchParams(location.search).get("startup") === "1" && t < 2.5 ? 0 : 750,
    // 20 s launch loop for the Dyno tab: stopped 3 s, 0 -> ~110 km/h in ~9 s, back down
    speed_kmh: Math.round(t % 20 < 3 ? 0 : t % 20 < 12 ? 110 * Math.min(1, ((t % 20 - 3) / 9) ** 0.8) : 110 * (20 - t % 20) / 8),
    gear: "D",
    hz: 15.4
  };
}

function startDemo() {
  $("tag-cvt").hidden = false;
  const tracks = [
    { title: "Song title goes here", artist: "Artist name", duration_ms: 214000, art: demoArt(200, 280) },
    { title: "A much longer song title that should be cut with an ellipsis at the edge", artist: "Another artist, Featured artist", duration_ms: 187000, art: demoArt(20, 330) },
    { title: "Short", artist: "Solo", duration_ms: 243000, art: demoArt(140, 220) },
    { title: "Track without cover art", artist: "Unknown", duration_ms: 201000, art: null }
  ];
  let ti = 0, pos = 62000, status = "playing", t0 = performance.now();
  setVol({ db: -18, mute: false, min_db: -60, max_db: 0 });
  sendCmd = (cmd) => {
    if (cmd?.cmd === "vol") return;          // demo: the slider just moves locally
    if (cmd === "next") { ti = (ti + 1) % tracks.length; pos = 0; }
    else if (cmd === "prev") { ti = (ti + tracks.length - 1) % tracks.length; pos = 0; }
    else if (cmd === "toggle") status = status === "playing" ? "paused" : "playing";
    draw();
  };
  function draw() {
    setCvt(demoCvt((performance.now() - t0) / 1000));
    setMedia({ ...tracks[ti], status, position_ms: pos, codec: "LDAC" });
  }
  setInterval(() => { if (status === "playing") { pos += 250; if (pos > tracks[ti].duration_ms) { ti = (ti + 1) % tracks.length; pos = 0; } } draw(); }, 250);
  draw();
}

// ---- live source: hud-service on ws://<host>:8766 ----
function startLive() {
  setCvt(null); setMedia(null);
  const connect = () => {
    const ws = new WebSocket(CONFIG.ws);
    ws.onopen = () => window.dispatchEvent(new Event("hud-open"));
    ws.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if ("cvt" in d) {
          setCvt(d.cvt);
          const tag = $("tag-cvt");
          tag.textContent = "NO OBD"; tag.hidden = d.cvt != null;   // shown while there is no CVT data
        }
        if ("media" in d) setMedia(d.media);
        if ("vol" in d) setVol(d.vol);
        if ("eng" in d || "alerts" in d) window.dispatchEvent(new CustomEvent("hud-msg", { detail: d }));
        if ("dsp" in d || "dsp_lv" in d) window.dispatchEvent(new CustomEvent("hud-dsp", { detail: d }));   // dsp.js
        if ("welcomed" in d) setWelcomed(d.welcomed);
        if ("car" in d || "trip" in d) window.dispatchEvent(new CustomEvent("hud-trip", { detail: d }));
        if ("car" in d && d.car && !["PARK", "SHUTDOWN"].includes(d.car)) window.welcome?.unbye();   // came back
        if (d.welcome) window.welcome?.play(d.welcome);
        if (d.bye) window.welcome?.bye(d.bye);
      } catch {}
    };
    ws.onclose = () => {
      setMedia(null); setCvt(null); setVol(null);
      window.dispatchEvent(new CustomEvent("hud-msg", { detail: { eng: null, alerts: [] } }));
      window.dispatchEvent(new CustomEvent("hud-dsp", { detail: { dsp: null } }));
      setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
    sendCmd = (cmd) => { if (ws.readyState === 1) ws.send(JSON.stringify(typeof cmd === "object" ? cmd : { cmd })); };
  };
  connect();
}

// ---- welcome hold: until hud-service says the welcome ran ({"welcomed": true}), the normal engine-start
// animation stays off (the welcome replaces it on the first engine start of each boot).
let welcomeHold = false;
function setWelcomed(w) {
  if (w === false && !welcomeHold) { welcomeHold = true; holdWake(); window.welcome?.hold(true); }   // black until the welcome
  else if (w === true && welcomeHold) { welcomeHold = false; window.welcome?.hold(false); if (!window.welcome?.playing) WAKE.armed = true; }
}

CONFIG.demo ? startDemo() : startLive();

window.hud = { setCvt, setMedia, setVol, holdWake, sweep: startSweep, isHeld: () => welcomeHold, send: (c) => sendCmd(c) };
