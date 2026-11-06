// Audio / CamillaDSP page: swipe RIGHT from the main screen (telemetry is the other way).
// Live meters (2 in + 8 out), presets (any ~/camilladsp/configs/*.yml except test_*), bass / treble (shelf filters,
// same ones CamillaGUI's Equalizer moves) and sub level (fader Aux1), status.
// Data in: "hud-dsp" events from hud.js -> {dsp: status | null} (1/s) and {dsp_lv: {in, in_pk, out, out_pk}} (10/s, page open only).
// Data out: window.hud.send({cmd: "dsp_preset", file}) / ({cmd: "dsp_adj", k: "bass"|"treble"|"sub", step: +1 | -1}).   ?demo=1 fake data, ?page=dsp opens here.

const Q = new URLSearchParams(location.search);
const page = document.getElementById("page-dsp");
const CH = [["IN", "L"], ["IN", "R"], ["TW", "L"], ["TW", "R"], ["MID", "L"], ["MID", "R"], ["MB", "L"], ["MB", "R"], ["SUB", ""], ["SUB", "2"]];
const FLOOR = -60, CLIP_DB = -0.5, HOLD_MS = 1500, FALL_DB_S = 20;
const ADJ = [["sub", "SUB"], ["treble", "TREBLE"], ["bass", "BASS"]];   // sub = fader Aux1 (sub outputs only); treble / bass = shelves on the input

page.innerHTML = `
<div class="d-stage">
  <div class="t-head">
    <div class="t-brand"><b>ROGUE</b><span>AUDIO · DSP</span></div>
    <div class="t-status d-status"><span>DSP<i class="t-dot" id="d-dot"></i></span><span id="d-state">--</span>
      <span id="d-rate">-- KHZ</span><span id="d-load">LOAD --</span><span class="d-codec" id="d-codec">--</span></div>
  </div>
  <section class="d-meters">
    <h3>LEVELS <span id="d-clip">CLIPPED 0</span></h3>
    <div class="d-scale">${[0, -6, -12, -24, -36, -48].map((db) => `<span style="bottom:${pct(db)}%">${db}</span>`).join("")}</div>
    <div class="d-bars">${CH.map(([a, b], i) => `${i === 2 ? '<i class="d-gap"></i>' : ""}
      <div class="d-ch"><div class="d-led" data-led="${i}"></div>
        <div class="d-m"><i class="d-cover" data-cover="${i}"></i><i class="d-pk" data-pk="${i}"></i></div>
        <div class="d-lbl">${a}<small>${b}</small></div></div>`).join("")}</div>
  </section>
  <section class="d-side">
    <h3>PRESET</h3><div class="d-presets" id="d-presets"></div>
    <h3>TONE</h3>
    <div class="d-adjs">${ADJ.map(([k, l]) => `<div class="d-adj"><span class="d-adjl">${l}</span>
      <button class="d-btn" data-adj="${k}" data-step="-1" aria-label="${l} down">−</button>
      <div class="d-subv"><b id="d-${k}">--</b><u>dB</u><div class="d-subbar"><i class="c"></i><i class="f" id="d-${k}fill"></i></div></div>
      <button class="d-btn" data-adj="${k}" data-step="1" aria-label="${l} up">+</button></div>`).join("")}</div>
    <div class="d-vol" id="d-vol">VOLUME --</div>
  </section>
</div>`;

function pct(db) { return Math.max(0, Math.min(100, (db - FLOOR) / -FLOOR * 100)); }
const $ = (id) => document.getElementById(id);
const covers = [...page.querySelectorAll("[data-cover]")], pks = [...page.querySelectorAll("[data-pk]")], leds = [...page.querySelectorAll("[data-led]")];
const hold = CH.map(() => ({ db: FLOOR, t: 0, clipT: -1e9 }));
let ST = null, lastLv = performance.now(), presetKey = null;

// ---- status (1/s) ----
function setStatus(st) {
  ST = st;
  const run = st?.state === "Running";
  $("d-dot").classList.toggle("on", run);
  $("d-state").textContent = st?.state ? st.state.toUpperCase() : "NO DSP";
  $("d-rate").textContent = st?.rate ? `${(st.rate / 1000).toFixed(1)} KHZ` : "-- KHZ";
  $("d-load").textContent = `LOAD ${st?.load ?? "--"}%`;
  $("d-codec").textContent = st?.codec ? String(st.codec).toUpperCase() : "BT --";
  $("d-clip").textContent = `CLIPPED ${st?.clipped ?? 0}`;
  $("d-clip").classList.toggle("bad", (st?.clipped ?? 0) > 0);
  const pkey = JSON.stringify([st?.presets, st?.config]);
  if (pkey !== presetKey) {                 // rebuild only on change: a rebuild under a finger would swallow the tap
    presetKey = pkey;
    $("d-presets").innerHTML = (st?.presets ?? []).map((p) =>
      `<button class="d-preset${p.file === st.config ? " on" : ""}" data-file="${p.file}">${p.title}</button>`).join("");
  }
  for (const [k] of ADJ) {
    const v = st?.adj?.[k], [lo, hi] = st?.adj_range?.[k] ?? [-12, 12], p = (x) => (x - lo) / (hi - lo) * 100, f = $(`d-${k}fill`);
    $(`d-${k}`).textContent = v == null ? "--" : `${v > 0 ? "+" : ""}${v.toFixed(0)}`;
    if (v == null) f.style.width = "0";
    else { f.style.left = `${Math.min(p(0), p(v))}%`; f.style.width = `${Math.abs(p(v) - p(0))}%`; }
  }
  const v = st?.vol;
  $("d-vol").textContent = !v ? "VOLUME --" : v.mute ? "MUTED" : `VOLUME ${v.db.toFixed(1)} dB`;
}

// ---- levels (10/s): RMS bar, peak hold that falls, clip LED held 2 s ----
function setLevels(lv) {
  const now = performance.now(), dt = (now - lastLv) / 1000; lastLv = now;
  const rms = [...(lv?.in ?? []), ...(lv?.out ?? [])], pk = [...(lv?.in_pk ?? []), ...(lv?.out_pk ?? [])];
  CH.forEach((_, i) => {
    const r = rms[i] ?? FLOOR, p = pk[i] ?? FLOOR, h = hold[i];
    covers[i].style.transform = `scaleY(${1 - pct(r) / 100})`;
    if (p >= h.db) { h.db = p; h.t = now; }
    else if (now - h.t > HOLD_MS) h.db = Math.max(FLOOR, h.db - FALL_DB_S * dt);
    pks[i].style.bottom = `${pct(h.db)}%`;
    if (p >= CLIP_DB) h.clipT = now;
    leds[i].classList.toggle("on", now - h.clipT < 2000);
  });
}

page.addEventListener("click", (e) => {
  const pre = e.target.closest(".d-preset"), adj = e.target.closest("[data-adj]");
  if (pre && !pre.classList.contains("on")) { window.hud?.send?.({ cmd: "dsp_preset", file: pre.dataset.file }); pre.classList.add("pending"); }
  if (adj) window.hud?.send?.({ cmd: "dsp_adj", k: adj.dataset.adj, step: Number(adj.dataset.step) });
});
window.addEventListener("hud-dsp", (ev) => {
  const d = ev.detail;
  if ("dsp" in d) setStatus(d.dsp);
  if ("dsp_lv" in d) setLevels(d.dsp_lv);
});
setStatus(null); setLevels(null);

// ---- demo (?demo=1): music-ish levels, presets and sub buttons work locally ----
if (Q.get("demo") === "1") {
  const st = { state: "Running", config: "rogue_xover.yml", rate: 48000, load: 0.6, clipped: 0, codec: "ldac",
               adj: { bass: 0, treble: 0, sub: 0 }, adj_range: { bass: [-12, 12], treble: [-12, 12], sub: [-12, 12] },
               vol: { db: -25, mute: false }, presets: [{ file: "rogue_xover.yml", title: "Normal" }, { file: "bass_plus.yml", title: "Bass +" }, { file: "night.yml", title: "Night" }] };
  window.hud = window.hud || {};
  const send0 = window.hud.send;
  window.hud.send = (c) => {
    if (c?.cmd === "dsp_preset") st.config = c.file;
    else if (c?.cmd === "dsp_adj") st.adj[c.k] = Math.max(-12, Math.min(12, st.adj[c.k] + c.step));
    else return send0?.(c);
    setStatus(st);
  };
  setStatus(st);
  const t0 = performance.now();
  setInterval(() => {
    const t = (performance.now() - t0) / 1000, beat = Math.max(0, Math.sin(t * 7.3)) ** 6;
    const lvl = (base, k, ph) => base + 6 * Math.sin(t * k + ph) + 10 * beat;
    const out = [lvl(-30, 3.1, 0), lvl(-31, 2.9, 1), lvl(-22, 2.3, 2), lvl(-23, 2.1, 3), lvl(-16, 1.7, 4), lvl(-17, 1.9, 5),
                 lvl(-12, 1.1, 6) + 4 * beat, lvl(-12, 1.1, 6) + 4 * beat];
    const inn = [lvl(-14, 2, 0), lvl(-15, 2.2, 1)];
    const pk = (x) => Math.min(-0.2, x + 4 + 3 * Math.random());     // clips now and then, not all the time
    setLevels({ in: inn, in_pk: inn.map(pk), out, out_pk: out.map(pk) });
  }, 100);
  setInterval(() => setStatus(st), 1000);
}
