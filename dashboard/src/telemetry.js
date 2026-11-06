// Telemetry window: swipe left from the main screen (or tap the red warning badge).
// Tabs ALL / ENGINE / CVT. The page tells hud-service which one is open ({cmd:"view"}), so the vLinker
// only reads what is on screen; on the main screen it only reads oil pressure + rpm (1 s) and
// coolant + oil temp (7 s) in the background for the alerts.
// Data in (events from hud.js):  "hud-cvt" -> cvt dict | null (~12/s),  "hud-msg" -> {eng} / {alerts},
//                                 "hud-open" -> the websocket (re)connected.
// Data out: window.hud.send({cmd:"view", view}).   Taps that are not swipes -> "hud-tap" (volume in hud.js).
// ?demo=1 fake data (add &alert=oil|coolant|oiltemp to force an alert), ?page=tele opens on this page.

const Q = new URLSearchParams(location.search);
const DEMO = Q.get("demo") === "1";
const W = () => window.innerWidth;

// ---- thresholds shown on the page (the alerts themselves are decided in cvt_reader.py) ----
const LIM = { coolant: [105, 110], oil: [120, 130], cvt: [90, 110] };
const WEAR_MAX = 200;              // your own limit for the CVT fluid wear bar (Nissan's is 210,000)
const LOCK_TEXT = { open: "Unlocked", engaging: "Engaging", releasing: "Releasing", locked: "Locked", coast: "Locked · coast" };

let E = null, C = null, ALERTS = [];
let PWD = {}, pwTs = 0;                              // power fields (eng_nm, hp, ratio...), from whichever message is newer
const PW = (k) => performance.now() - pwTs < 3000 ? PWD[k] ?? null : null;
const takePower = (d) => { if (d && d.eng_nm !== undefined) { PWD = d; pwTs = performance.now(); } };
let view = "main", tab = "all";
let introPending = true, cvtNullSince = performance.now();

// ---------------------------------------------------------------- DOM
const pageMain = document.getElementById("page-main");
const page = document.getElementById("page-tele");
const pageDsp = document.getElementById("page-dsp");              // audio / DSP page (dsp.js), left of main
const PAGES = [pageMain, page, pageDsp].filter(Boolean);
const pgX = (v) => v === "tele" ? -W() : v === "dsp" ? W() : 0;
page.innerHTML = `
<div class="t-stage">
  <div class="t-head">
    <div class="t-brand"><b>ROGUE</b><span>TELEMETRY</span></div>
    <div class="t-tabs" id="t-tabs">
      <i class="t-tabbg" style="left:0"></i><i class="t-tabbg" style="left:122px"></i><i class="t-tabbg" style="left:244px"></i><i class="t-tabbg" style="left:366px"></i>
      <i class="t-hl" id="t-hl"></i>
      <div class="t-tab on" data-t="all">ALL</div><div class="t-tab" data-t="engine">ENGINE</div><div class="t-tab" data-t="cvt">CVT</div><div class="t-tab" data-t="dyno">DYNO</div>
    </div>
    <div class="t-status"><span>ECM<i class="t-dot" id="t-ecm"></i></span><span>TCM<i class="t-dot" id="t-tcm"></i></span>
      <span class="t-hz" id="t-hz">-- HZ</span><span class="t-clock" id="t-clock">--:--</span></div>
  </div>
  <div class="t-rpm"><div class="t-rpmval"><b id="t-rpm">--</b><small>RPM</small></div>
    <div class="t-leds"><div class="t-segs" id="t-segs"></div>
      <div class="t-ticks"><span>0</span><span>1</span><span>2</span><span>3</span><span>4</span><span>5</span><span class="r">6</span><span class="r">7</span></div></div></div>
  <div class="t-toast" id="t-toast"><div class="ic"><i>!</i></div><div><b id="t-ttl"></b><span id="t-tsub"></span></div><em id="t-tmore"></em></div>
  <div class="t-content" id="t-content"></div>
  <div class="t-introfx"><i class="band"></i><i class="band band2"></i><div class="word"><b>ROGUE</b><span>TELEMETRY</span></div></div>
</div>`;
const $ = (id) => document.getElementById(id);
const content = $("t-content");

// ---------------------------------------------------------------- fields
// each field: value from (E, C), unit, decimals, bar {min, max, zones, center}, state(v) -> ok|warn|crit|cool
const W_ = "var(--warn)", R_ = "var(--crit)", C_ = "var(--cool)";
const hiState = ([w, c]) => (v) => v >= c ? "crit" : v >= w ? "warn" : "ok";
const F = {
  coolant: { l: "Coolant", u: "°C", get: () => E?.coolant_c, bar: [40, 120, [[40, 70, C_], [105, 110, W_], [110, 120, R_]]], st: hiState(LIM.coolant) },
  oil:     { l: "Oil temp", u: "°C", get: () => E?.oil_c, bar: [40, 140, [[40, 70, C_], [120, 130, W_], [130, 140, R_]]], st: hiState(LIM.oil) },
  iat:     { l: "Intake air", u: "°C", get: () => E?.iat_c, bar: [0, 80] },
  fuelt:   { l: "Fuel temp", u: "°C", get: () => E?.fuel_c, bar: [0, 80] },
  ecmv:    { l: "Control module", u: "V", d: 2, get: () => E?.ecm_v, bar: [11, 15, [[11, 12, W_], [14.8, 15, W_]]] },
  load:    { l: "Engine load", u: "%", d: 1, get: () => E?.load_pct, bar: [0, 100] },
  stft:    { l: "STFT", u: "%", get: () => E?.stft_pct, sign: true, bar: [-25, 25, [], 0] },
  ltft:    { l: "LTFT", u: "%", get: () => E?.ltft_pct, sign: true, bar: [-25, 25, [], 0] },
  maf:     { l: "MAF", u: "g/s", d: 2, get: () => E?.maf_gs, bar: [0, 150] },
  inj:     { l: "Injector", u: "ms", d: 2, get: () => E?.inj_ms },
  timing:  { l: "Timing", u: "°", get: () => E?.timing_deg },
  exhcam:  { l: "Exh cam", u: "°CA", d: 1, get: () => E?.exhcam_deg },
  vtc:     { l: "Exh VTC", u: "%", d: 1, get: () => E?.vtc_pct },
  cvtt:    { l: "Fluid temp", u: "°C", get: () => C?.temp_c, bar: [20, 130, [[90, 110, W_], [110, 130, R_]]], st: hiState(LIM.cvt) },
  wear:    { l: "Fluid wear", u: `/ ${WEAR_MAX}`, get: () => E?.fluid_wear, bar: [0, WEAR_MAX, [[WEAR_MAX * .75, WEAR_MAX * .9, W_], [WEAR_MAX * .9, WEAR_MAX, R_]]], st: hiState([WEAR_MAX * .75, WEAR_MAX * .9]) },
  thr1: { l: "Throttle 1", u: "V", d: 2, get: () => E?.thr1_v }, thr2: { l: "Throttle 2", u: "V", d: 2, get: () => E?.thr2_v },
  ped1: { l: "Pedal 1", u: "V", d: 2, get: () => E?.ped1_v },    ped2: { l: "Pedal 2", u: "V", d: 2, get: () => E?.ped2_v },
  af:   { l: "A/F B1S1", u: "V", d: 2, get: () => E?.af_v },     ac:   { l: "A/C press", u: "V", d: 2, get: () => E?.ac_v },
  // power (TCM 1213 torque, 1210/1218 ratio): telemetry pages only, every 2nd round. est: true = estimate, value shown as "≈1234"
  engnm:  { l: "Engine torque", u: "Nm", get: () => PW("eng_nm"), bar: [0, 240] },
  hp:     { l: "Power", u: "hp", d: 0, get: () => PW("hp"), bar: [0, 170] },
  wheelnm:{ l: "Wheel torque", u: "Nm", est: true, get: () => PW("wheel_nm"), bar: [0, 3000] },
  whp:    { l: "Wheel power", u: "hp", d: 0, est: true, get: () => PW("whp") },
  ratio:  { l: "Gear ratio", u: "", d: 3, get: () => PW("ratio"),
            sub: () => `target ${fx(PW("ratio_target"), 3)} · ${PW("pri_rpm") ?? "--"} / ${PW("sec_rpm") ?? "--"} rpm`, bar: [0.378, 2.631] },
  // CVT tab tiles
  pri:  { l: "Primary · target", u: "MPa", d: 2, get: () => C?.p_primary_target_mpa, sub: () => `actual ${fx(C?.p_primary_actual_mpa, 2)}`, bar: [0, 6], barv: () => C?.p_primary_actual_mpa },
  sec:  { l: "Secondary · target", u: "MPa", d: 2, get: () => C?.p_secondary_target_mpa, sub: () => `actual ${fx(C?.p_secondary_actual_mpa, 2)}`, bar: [0, 6], barv: () => C?.p_secondary_actual_mpa },
  line: { l: "Line · target", u: "MPa", d: 2, get: () => C?.p_line_target_mpa, sub: () => `gear ${C?.gear ?? "--"} · ${C?.speed_kmh ?? "--"} km/h`, bar: [0, 6] },
  cvtt2:{ l: "Fluid temp", u: "°C", get: () => C?.temp_c, sub: () => "orange 90 · red 110", bar: [20, 130, [[90, 110, W_], [110, 130, R_]]], st: hiState(LIM.cvt) },
  wear2:{ l: "Fluid wear", u: `/ ${WEAR_MAX}`, get: () => E?.fluid_wear, sub: () => "Nissan limit 210,000", bar: [0, WEAR_MAX, [[WEAR_MAX * .75, WEAR_MAX * .9, W_], [WEAR_MAX * .9, WEAR_MAX, R_]]], st: hiState([WEAR_MAX * .75, WEAR_MAX * .9]) },
};
const fx = (v, d = 0) => v == null || Number.isNaN(v) ? "--" : Number(v).toFixed(d);

function barHtml(b) {
  if (!b) return "";
  const [min, max, zones = [], center] = b, p = (x) => Math.max(0, Math.min(100, (x - min) / (max - min) * 100));
  return `<div class="t-bar">${zones.map(([a, z, c]) => `<i class="z" style="left:${p(a)}%;width:${p(z) - p(a)}%;background:${c}"></i>`).join("")}<i class="f"></i>${center != null ? `<i class="c" style="left:${p(center)}%"></i>` : ""}</div>`;
}
const item = (k, cls = "") => `<div class="t-item ${cls}" data-f="${k}"><div class="t-row"><span class="t-lbl">${F[k].l}</span><span class="t-val"><b>--</b><u>${F[k].u}</u></span></div>${barHtml(F[k].bar)}</div>`;
const tile = (k, i, inner) => `<div class="t-tile" style="--i:${i}" data-f="${k}"><div class="t-lbl">${F[k]?.l ?? ""}</div>${inner ?? `<div class="t-val"><b>--</b><u>${F[k].u}</u></div><div class="t-sub"></div>${barHtml(F[k].bar)}`}</div>`;

const colThermal = (i) => `<div class="t-col" style="--i:${i}"><h3>THERMAL</h3>${item("coolant")}${item("oil")}${item("iat", "sm")}${item("fuelt", "sm")}</div>`;
const colOil = (i) => `<div class="t-col" style="--i:${i}"><h3>OIL · POWER</h3>
  <div class="t-hero" id="t-eop"><span class="t-tag" id="t-eoptag">--</span><div class="t-lbl">Oil pressure</div>
    <div class="t-val"><b id="t-psi">--</b><u>PSI</u></div><div class="t-bar"><i class="f" id="t-eopbar"></i></div>
    <div class="t-sub"><span id="t-eopmv">-- mV</span><span id="t-eopmin">min -- mV</span></div></div>
  <div class="t-pair">${item("engnm", "sm")}${item("hp", "sm")}</div>
  <div class="t-pair">${item("wheelnm", "sm")}${item("load", "sm")}</div></div>`;
const colFuel = (i) => `<div class="t-col" style="--i:${i}"><h3>FUEL · AIR</h3>
  <div class="t-pair">${item("stft", "sm")}${item("ltft", "sm")}</div>${item("maf", "sm")}
  <div class="t-pair">${item("inj", "sm")}${item("timing", "sm")}${item("exhcam", "sm")}${item("vtc", "sm")}</div></div>`;
const colCvt = (i) => `<div class="t-col" style="--i:${i}"><h3>CVT</h3>${item("cvtt")}
  <table class="t-ptab"><tr><th>PRESSURE · MPA</th><th>TGT</th><th>ACT</th></tr>
    <tr><td>PRIMARY</td><td class="n" data-c="p_primary_target_mpa">--</td><td class="n" data-c="p_primary_actual_mpa">--</td></tr>
    <tr><td>SECONDARY</td><td class="n" data-c="p_secondary_target_mpa">--</td><td class="n" data-c="p_secondary_actual_mpa">--</td></tr>
    <tr><td>RATIO</td><td class="n" data-p="ratio_target">--</td><td class="n" data-p="ratio">--</td></tr></table>
  <div class="t-row" style="margin:12px 0 14px;align-items:center"><span class="t-chip" data-lockchip>--</span>
    <span class="t-mini t-lockinfo">SLIP <b data-slip>--</b><br>TCC <b data-tcc>--</b></span></div>
  ${item("wear", "sm")}</div>`;
const strip = (i) => `<div class="t-strip" style="--i:${i}">${["ecmv", "thr1", "thr2", "ped1", "ped2", "af", "ac"].map((k) =>
  `<div data-f="${k}"><div class="t-lbl">${F[k].l}</div><div class="t-val"><b>--</b><u>${F[k].u}</u></div></div>`).join("")}</div>`;

const LOCK_TILE = `<div class="t-val" style="font-size:60px"><b data-lockbig>--</b></div><div class="t-sub">slip <span data-slip>--</span> rpm · TCC <span data-tcc>--</span></div><div class="t-bar"><i class="f" data-tccbar></i></div>`;
const VIEWS = {
  all: () => `<div class="t-grid" style="grid-template-columns:1fr 1fr 1fr 1.08fr">${colThermal(0)}${colOil(1)}${colFuel(2)}${colCvt(3)}</div>${strip(4)}`,
  engine: () => `<div class="t-grid" style="grid-template-columns:1fr 1fr 1fr">${colThermal(0)}${colOil(1)}${colFuel(2)}</div>${strip(3)}`,
  cvt: () => `<div class="t-tiles">${tile("pri", 0)}${tile("sec", 1)}${tile("cvtt2", 2)}
    ${tile("lock", 3, LOCK_TILE)}
    ${tile("wear2", 4)}${tile("ratio", 5)}</div>`,
  dyno: () => `<div class="t-tiles">${tile("dyno", 0, `<div class="t-val"><b id="dy-time">--</b><u>s</u></div>
    <div class="t-sub" id="dy-sub">--</div><div class="t-sub" id="dy-best" style="margin-top:8px"></div>`)}
    ${tile("hp", 1)}${tile("engnm", 2)}${tile("sec", 3)}${tile("lock", 4, LOCK_TILE)}${tile("peak", 5, `<div class="t-peaks"><div><div class="t-lbl">Power</div><div class="t-val"><b id="pk-hp">--</b><u>hp</u></div></div><div><div class="t-lbl">Torque</div><div class="t-val"><b id="pk-nm">--</b><u>Nm</u></div></div><div><div class="t-lbl">SEC actual</div><div class="t-val"><b id="pk-sec">--</b><u>MPa</u></div></div><div><div class="t-lbl">PRI actual</div><div class="t-val"><b id="pk-pri">--</b><u>MPa</u></div></div></div><div class="t-sub" id="pk-sub">no run yet</div>`)}</div>`
};
F.dyno = { l: "0-60 mph" };
F.peak = { l: "Last run · peaks" };
F.lock = { l: "Torque converter" };

// ---------------------------------------------------------------- render / update
let fieldEls = [];
function render() {
  content.innerHTML = VIEWS[tab]();
  fieldEls = [...content.querySelectorAll("[data-f]")].filter((el) => F[el.dataset.f]?.get).map((el) => ({
    el, f: F[el.dataset.f], b: el.querySelector(".t-val b"), fill: el.querySelector(".t-bar .f"), sub: el.querySelector(".t-sub")
  }));
  update();
}
function setFill(fill, v, bar) {
  if (!fill || !bar) return;
  const [min, max, , center] = bar, p = (x) => Math.max(0, Math.min(100, (x - min) / (max - min) * 100));
  if (v == null) { fill.style.width = "0"; return; }
  if (center != null) { const c = p(center), q = p(v); fill.style.left = `${Math.min(c, q)}%`; fill.style.width = `${Math.abs(q - c)}%`; }
  else fill.style.width = `${p(v)}%`;
}
function update() {
  if (page.classList.contains("pg-off")) return;     // off-screen: no DOM work
  for (const o of fieldEls) {
    const v = o.f.get();
    o.b.textContent = v == null ? "--" : (o.f.est ? "≈" : "") + (o.f.sign && v > 0 ? "+" : "") + fx(v, o.f.d ?? 0);
    const st = v == null ? "ok" : o.f.st ? o.f.st(v) : "ok";
    o.el.classList.toggle("st-warn", st === "warn"); o.el.classList.toggle("st-crit", st === "crit");
    setFill(o.fill, o.f.barv ? o.f.barv() : v, o.f.bar);
    if (o.sub && o.f.sub) o.sub.textContent = o.f.sub();
  }
  for (const td of content.querySelectorAll("[data-c]")) td.textContent = fx(C?.[td.dataset.c], 2);
  for (const td of content.querySelectorAll("[data-p]")) td.textContent = fx(PW(td.dataset.p), 3);
  const lock = C?.lockup, slip = C?.slip_rpm, tcc = C?.tcc_a;
  for (const el of content.querySelectorAll("[data-lockchip]")) { el.textContent = LOCK_TEXT[lock] || "--"; el.dataset.lock = lock || ""; }
  for (const el of content.querySelectorAll("[data-lockbig]")) el.textContent = (LOCK_TEXT[lock] || "--").replace(" · coast", "");
  for (const el of content.querySelectorAll("[data-slip]")) el.textContent = slip == null ? "--" : Math.abs(slip) >= 127 ? `${slip > 0 ? ">+" : "<"}${slip}` : Math.round(slip);
  for (const el of content.querySelectorAll("[data-tcc]")) el.textContent = tcc == null ? "--" : `${tcc.toFixed(2)}A`;
  for (const el of content.querySelectorAll("[data-tccbar]")) el.style.width = `${Math.min(100, (tcc || 0) / 0.7 * 100)}%`;
  // oil pressure hero
  const hero = $("t-eop");
  if (hero) {
    const mv = E?.eop_mv, crit = E?.eop_crit_mv, warn = E?.eop_warn_mv;
    const alert = ALERTS.some((a) => a.id === "oil_pressure");
    const st = mv == null ? "ok" : alert || (crit && mv <= crit) ? "crit" : warn && mv <= warn ? "warn" : "ok";
    hero.className = `t-hero st-${st}`;
    $("t-eoptag").textContent = mv == null ? "--" : st === "crit" ? "LOW" : st === "warn" ? "WATCH" : "NORMAL";
    $("t-psi").textContent = fx(E?.eop_psi, 1);
    $("t-eopmv").textContent = `${mv ?? "--"} mV`;
    $("t-eopmin").textContent = `min ${crit ?? "--"} mV`;
    $("t-eopbar").style.width = mv == null ? "0" : `${Math.max(0, Math.min(100, (mv - 500) / 3000 * 100))}%`;
  }
  // status line
  $("t-ecm").classList.toggle("on", !!E); $("t-tcm").classList.toggle("on", !!C);
  const hz = tab === "cvt" || tab === "dyno" ? E?.cvt_hz : E?.eng_hz;
  $("t-hz").textContent = hz == null ? "-- HZ" : `${hz.toFixed(1)} HZ`;
  rpmTarget = engineFresh() ? E.rpm : C?.rpm ?? E?.rpm ?? null;
  if (tab === "dyno") drawDyno();
}
let engTs = 0;
const engineFresh = () => E?.rpm != null && performance.now() - engTs < 2000 && tab !== "cvt";

// ---------------------------------------------------------------- RPM shift lights (eased at 60 fps)
const N = 40, segs = $("t-segs");
segs.innerHTML = Array.from({ length: N }, (_, i) => { const r = (i + 1) / N * 7000; return `<i class="t-seg ${r > 6000 ? "r" : r > 5000 ? "y" : ""}"></i>`; }).join("");
const segEls = [...segs.children];
let rpmTarget = null, rpmShown = null, sweepUntil = 0, lastLit = -1, last = performance.now();
function drawRpm(r) {
  $("t-rpm").textContent = r == null ? "--" : Math.round(r);
  const lit = r == null ? 0 : segEls.filter((_, i) => (i + 1) / N * 7000 <= r + 90).length;
  if (lit !== lastLit) { segEls.forEach((s, i) => s.classList.toggle("on", i < lit)); lastLit = lit; }
}
function frame(now) {
  const dt = Math.min(.1, (now - last) / 1000); last = now;
  if (view === "tele" && now > sweepUntil) {
    if (rpmTarget == null) { if (rpmShown != null) { rpmShown = null; drawRpm(null); } }
    else { rpmShown = rpmShown == null ? rpmTarget : rpmShown + (rpmTarget - rpmShown) * (1 - Math.exp(-dt / .12)); drawRpm(rpmShown); }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
function sweep() {                                     // 0 -> 7000 -> current, like a cluster at key-on
  const t0 = performance.now(), D = 1000; sweepUntil = t0 + D;
  (function f(t) { const k = Math.min(1, (t - t0) / D), end = rpmTarget ?? 0;
    drawRpm(k < .5 ? 7000 * Math.sin(k * Math.PI) : end + (7000 - end) * Math.sin(k * Math.PI)); rpmShown = end;
    if (k < 1) requestAnimationFrame(f); })(t0);
}

// ---------------------------------------------------------------- dyno: 0-60 mph timer (for show), locked while the CVT is hot
const DYNO_MAX_C = 82, KMH60 = 96.56, MOVE_KMH = 1, RUN_MAX_MS = 30000;
// one step of the timer. s = {st, t0, last}; st: locked | wait (moving, stop to arm) | ready (stopped) | run | done
function dynoStep(s, now, kmh, tempC) {
  if (tempC == null || tempC > DYNO_MAX_C) return { ...s, st: "locked" };   // no temp = no dyno
  if (kmh == null) return { ...s, st: "wait" };
  if (s.st === "run") {
    if (kmh >= KMH60) return { ...s, st: "done", last: (now - s.t0) / 1000 };
    if (kmh < MOVE_KMH) return { ...s, st: "ready" };                     // stopped again: re-arm
    return now - s.t0 > RUN_MAX_MS ? { ...s, st: "wait" } : s;           // too slow: give up
  }
  if (kmh < MOVE_KMH) return { ...s, st: "ready" };
  if (s.st === "ready") return { ...s, st: "run", t0: now };              // first sample moving: clock starts
  return s.st === "done" ? s : { ...s, st: "wait" };
}
let DY = { st: "wait", t0: 0, last: null }, dyBest = null, PK = null;
try { dyBest = Number(localStorage.getItem("dyno-best")) || null; } catch {}
function dynoTick() {
  const prev = DY.st;
  if (view !== "tele" || tab !== "dyno") { if (prev !== "wait") DY = { ...DY, st: "wait" }; return; }   // only records on the Dyno tab
  DY = dynoStep(DY, performance.now(), C?.speed_kmh, C?.temp_c);
  if (DY.st === "run" && prev !== "run") PK = { hp: null, rpm: null, nm: null, sec: null, pri: null };
  if (PK && (DY.st === "run" || prev === "run")) {
    const hp = PW("hp"), max = (k, v) => { if (v != null && (PK[k] == null || v > PK[k])) PK[k] = v; };
    if (hp != null && (PK.hp == null || hp > PK.hp)) { PK.hp = hp; PK.rpm = E?.rpm ?? C?.rpm ?? null; }
    max("nm", PW("eng_nm")); max("sec", C?.p_secondary_actual_mpa); max("pri", C?.p_primary_actual_mpa);
  }
  if (prev === "run" && DY.st === "done" && (dyBest == null || DY.last < dyBest)) {
    dyBest = DY.last; try { localStorage.setItem("dyno-best", String(dyBest)); } catch {}
  }
}
function drawDyno() {
  const t = $("dy-time"); if (!t) return;
  const mph = C?.speed_kmh == null ? "--" : Math.round(C.speed_kmh / 1.609), temp = C?.temp_c;
  const [v, msg] = {
    locked: [DY.last, temp == null ? "LOCKED · no CVT temp" : `LOCKED · CVT ${Math.round(temp)} °C · under ${DYNO_MAX_C} to run`],
    wait:   [DY.last, `STOP TO ARM · ${mph} mph`],
    ready:  [DY.last, "READY · starts when you move"],
    run:    [(performance.now() - DY.t0) / 1000, `RUNNING · ${mph} mph`],
    done:   [DY.last, "DONE · stop to arm again"],
  }[DY.st];
  t.textContent = v == null ? "--" : v.toFixed(DY.st === "run" ? 1 : 2);
  $("dy-sub").textContent = msg;
  $("dy-best").textContent = `best ${dyBest == null ? "--" : dyBest.toFixed(2)} s`;
  t.closest(".t-tile").classList.toggle("st-crit", DY.st === "locked");
  $("pk-hp").textContent = fx(PK?.hp); $("pk-nm").textContent = fx(PK?.nm);
  $("pk-sec").textContent = fx(PK?.sec, 2); $("pk-pri").textContent = fx(PK?.pri, 2);
  $("pk-sub").textContent = !PK ? "no run yet" : `peak power @ ${PK.rpm == null ? "--" : Math.round(PK.rpm).toLocaleString("en-US")} rpm${DY.st === "run" ? " · live" : ""}`;
}

// ---------------------------------------------------------------- alerts (banner here, badge on the main screen)
const ALERT_TEXT = {
  oil_pressure: (a) => ["OIL PRESSURE LOW", `${(a.mv / 1000).toFixed(2)} V · ${a.psi} PSI @ ${a.rpm} RPM · CHECK OIL NOW`],
  coolant: (a) => ["ENGINE OVERHEATING", `COOLANT ${a.value} °C · PULL OVER SAFELY`],
  oil_temp: (a) => ["OIL TEMP HIGH", `${a.value} °C · EASE OFF`],
};
const panelCvt = document.getElementById("panel-cvt");
const badge = document.createElement("div");
badge.className = "t-badge"; badge.id = "t-badge"; badge.setAttribute("role", "button"); badge.setAttribute("aria-label", "Warning");
badge.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 3 L22.5 21 H1.5 Z" fill="none" stroke="#fff" stroke-width="2.4" stroke-linejoin="round"/><rect x="10.8" y="9" width="2.4" height="6.5" rx="1" fill="#fff"/><circle cx="12" cy="18" r="1.35" fill="#fff"/></svg>`;
badge.addEventListener("click", () => go("tele"));
panelCvt?.appendChild(badge);
// same fillet as the panel: badge sits INSET px from the panel's corner, so its radius = panel radius - INSET (concentric)
function placeBadge() {
  if (!panelCvt) return;
  const INSET = 8, r = parseFloat(getComputedStyle(panelCvt).borderTopRightRadius) || 22;
  badge.style.top = `${INSET}px`; badge.style.right = `${INSET}px`;
  badge.style.setProperty("--t-badge-r", `${Math.max(6, r - INSET)}px`);
}
placeBadge();
function setAlerts(list) {
  ALERTS = Array.isArray(list) ? list : [];
  const on = ALERTS.length > 0;
  badge.classList.toggle("on", on); panelCvt?.classList.toggle("t-warn", on);
  page.classList.toggle("t-alert", on);
  if (on) {
    const a = ALERTS[0], [t, s] = (ALERT_TEXT[a.id] || (() => ["WARNING", ""]))(a);
    $("t-ttl").textContent = t; $("t-tsub").textContent = s;
    $("t-tmore").textContent = ALERTS.length > 1 ? `+${ALERTS.length - 1} MORE` : "";
  }
  update();
}

// ---------------------------------------------------------------- tabs
const hl = $("t-hl");
function setTab(t) {
  if (!VIEWS[t]) return;
  const idx = ["all", "engine", "cvt", "dyno"].indexOf(t);
  hl.style.left = `${idx * 122}px`;
  document.querySelectorAll(".t-tab").forEach((e) => e.classList.toggle("on", e.dataset.t === t));
  if (t === tab) return;
  tab = t; sendView();
  content.classList.add("out");
  setTimeout(() => {
    render(); content.classList.remove("out"); content.classList.add("pre");
    requestAnimationFrame(() => requestAnimationFrame(() => content.classList.remove("pre")));
  }, 170);
}
$("t-tabs").addEventListener("click", (e) => { const t = e.target.closest(".t-tab"); if (t) setTab(t.dataset.t); });
const sendView = () => window.hud?.send?.({ cmd: "view", view: view === "tele" ? tab : view });

// ---------------------------------------------------------------- pages + swipe
let offset = 0;                                      // px, 0 = main, -W = telemetry, +W = DSP
const setPg = (px) => { offset = px; document.body.style.setProperty("--pg", `${px}px`); };
let hideT = 0;
function go(v) {
  const was = view; view = v;
  document.body.dataset.view = v;
  clearTimeout(hideT);
  PAGES.forEach((p) => p.classList.remove("pg-off"));
  setPg(pgX(v));
  const shown = v === "tele" ? page : v === "dsp" ? pageDsp : pageMain;
  hideT = setTimeout(() => PAGES.forEach((p) => p !== shown && p.classList.add("pg-off")), 480);
  if (v === "tele" && was !== "tele") {
    update();
    if (introPending) {
      introPending = false;
      page.classList.remove("t-intro"); page.classList.add("t-pre");
      setTimeout(() => {
        page.classList.remove("t-pre"); render(); page.classList.add("t-intro");
        setTimeout(sweep, 1150);
        setTimeout(() => page.classList.remove("t-intro"), 3000);
      }, 380);
    }
  }
  if (v !== was) sendView();
}
window.addEventListener("resize", () => { document.body.classList.add("pg-drag"); setPg(pgX(view)); requestAnimationFrame(() => document.body.classList.remove("pg-drag")); });

let g = null, suppressClick = false;
document.addEventListener("pointerdown", (e) => {
  // only a finger that starts ON the volume pill is a volume drag; anywhere else a swipe still works
  g = { x: e.clientX, y: e.clientY, t: performance.now(), target: e.target, drag: false,
        noSwipe: !!e.target.closest(".vol__pill"), base: offset };
}, true);
document.addEventListener("pointermove", (e) => {
  if (!g || g.noSwipe) return;
  const dx = e.clientX - g.x, dy = e.clientY - g.y;
  if (!g.drag && Math.abs(dx) > 14 && Math.abs(dx) > Math.abs(dy) * 1.3) {
    g.drag = true; g.x0 = e.clientX;
    window.dispatchEvent(new Event("hud-hidevol"));    // volume open? hide it fast, the swipe wins
    document.body.classList.add("pg-drag");
    PAGES.forEach((p) => p.classList.remove("pg-off")); clearTimeout(hideT);
    update();
  }
  if (g.drag) {
    let p = g.base + (e.clientX - g.x0);
    const lo = view === "dsp" ? 0 : -W(), hi = view === "tele" ? 0 : pageDsp ? W() : 0;   // one page per swipe
    if (p > hi) p = hi + (p - hi) * .25; if (p < lo) p = lo + (p - lo) * .25;           // rubber band at the ends
    setPg(p);
  }
}, true);
function endGesture(e) {
  if (!g) return;
  const dx = e.clientX - g.x, dy = e.clientY - g.y, dt = Math.max(1, performance.now() - g.t);
  if (g.drag) {
    document.body.classList.remove("pg-drag");
    const v = dx / dt, far = Math.abs(dx) > W() * .22, fast = Math.abs(v) > .45;
    const swipe = far || fast;
    if (view === "main" && dx < 0 && swipe) go("tele");
    else if (view === "main" && dx > 0 && swipe && pageDsp) go("dsp");
    else if ((view === "tele" && dx > 0 || view === "dsp" && dx < 0) && swipe) go("main");
    else go(view);                                   // snap back
    suppressClick = true; setTimeout(() => (suppressClick = false), 80);
  } else if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
    window.dispatchEvent(new CustomEvent("hud-tap", { detail: { x: e.clientX, y: e.clientY, target: g.target, view } }));
  }
  g = null;
}
document.addEventListener("pointerup", endGesture, true);
document.addEventListener("pointercancel", (e) => { if (g?.drag) { document.body.classList.remove("pg-drag"); go(view); } g = null; }, true);
document.addEventListener("click", (e) => { if (suppressClick) { e.stopPropagation(); e.preventDefault(); } }, true);

// ---------------------------------------------------------------- data in
window.addEventListener("hud-cvt", (ev) => {
  const d = ev.detail;
  if (d == null) { if (C != null) cvtNullSince = performance.now(); C = null; }
  else {
    if (C == null && performance.now() - cvtNullSince > 60000) introPending = true;   // car was off a while: new trip
    C = d;
    takePower(d);
    dynoTick();
  }
  update();
});
window.addEventListener("hud-msg", (ev) => {
  const d = ev.detail;
  if ("eng" in d) { E = d.eng; if (E) { engTs = performance.now(); takePower(E); } update(); }
  if ("alerts" in d) setAlerts(d.alerts);
});
window.addEventListener("hud-open", sendView);

// clock
const clock = () => { $("t-clock").textContent = new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).replace(/\s?[AP]M/, ""); };
clock(); setInterval(clock, 10000);

// ---------------------------------------------------------------- demo (?demo=1)
if (DEMO) {
  const forced = Q.get("alert"), t0 = performance.now();
  setInterval(() => {
    const t = (performance.now() - t0) / 1000, rpm = 650 + Math.max(0, 1500 * Math.sin(t / 4)) | 0;
    const low = forced === "oil";
    E = { rpm, coolant_c: forced === "coolant" ? 111 : 86, oil_c: forced === "oiltemp" ? 131 : 81, iat_c: 52, fuel_c: 34,
          eop_mv: low ? 920 : Math.round(1480 + rpm * .5), maf_gs: +(2.2 + rpm / 400).toFixed(2), load_pct: 21.1, timing_deg: 9,
          stft_pct: Math.round(3 * Math.sin(t)), ltft_pct: -5, inj_ms: 2.5, ecm_v: 13.6, exhcam_deg: 0, vtc_pct: 0,
          thr1_v: .7, thr2_v: .7, ped1_v: .77, ped2_v: .77, af_v: 2.18, ac_v: 1.54, fluid_wear: 109, eng_hz: 3.2, cvt_hz: 11.8,
          eng_nm: Math.round(25 + rpm / 12), ratio: +(2.2 - rpm / 1800).toFixed(3), ratio_target: +(2.18 - rpm / 1800).toFixed(3),
          pri_rpm: rpm, sec_rpm: Math.round(rpm / Math.max(.4, 2.2 - rpm / 1800)), tc_factor: 1 };
    E.hp = +Math.max(0, E.eng_nm * rpm / 7121).toFixed(1); E.wheel_nm = Math.round(E.eng_nm * E.ratio * 5.65 * .9);
    E.whp = +(E.wheel_nm * E.sec_rpm / 5.65 / 7121).toFixed(1); takePower(E);
    const k = Math.min(4, rpm / 1000); E.eop_warn_mv = Math.round(Math.max(1200, (9 * k / 20.7 + .77) * 1000)); E.eop_crit_mv = Math.round(Math.max(1000, (6 * k / 20.7 + .77) * 1000));
    E.eop_psi = +Math.max(0, (E.eop_mv / 1000 - .77) * 20.7).toFixed(1);
    engTs = performance.now(); update();
  }, 300);
  const A = { oil: [{ id: "oil_pressure", mv: 920, psi: 3.1, rpm: 780, min_mv: 1000 }], coolant: [{ id: "coolant", value: 111 }], oiltemp: [{ id: "oil_temp", value: 131 }] };
  setTimeout(() => setAlerts(A[forced] || []), 50);
}

render();
document.body.dataset.view = "main";
pageMain.classList.remove("pg-off"); page.classList.add("pg-off"); pageDsp?.classList.add("pg-off");
if (Q.get("page") === "tele") { introPending = Q.get("intro") === "1"; go("tele"); }
if (Q.get("page") === "dsp") go("dsp");
window.tele = { go, setTab, setAlerts };
