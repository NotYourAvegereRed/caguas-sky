// Welcome animation: plays on the first engine start of each boot, together with the music fade-in.
// Calm style: the live sky comes up out of black, "Good evening, Zen" + date/time, then the two glass
// panels slide in from the sides and the CVT gauges sweep full scale and settle on their values.
// In the car the RP2040 raises the real backlight from 0 at the same moment (firmware v0.9).
// Preview: ?demo=1&welcome=1 (once) or ?demo=1&welcome=loop (every 9 s). ?name=... overrides the name,
// ?hour=8 shows the greeting for that hour (morning 5-11, afternoon 12-17, evening 18-4).
// Live: hud-service sends {"welcome": {name, greet}} on the first RUN of each boot and {"bye": {name, greet}}
// at shutdown (greet = the full text for the hour, from ~/.config/hud/display.json). The bye fades the panels,
// shows "Good night, Zen" / "See you, Zen" and holds it while rp2040-link fades the backlight to 0.
// Preview of the bye: ?demo=1&bye=1 (once) or ?demo=1&bye=loop.
const Q = new URLSearchParams(location.search);
const DEFAULTS = { name: "Zen", greet: { morning: "Good morning", afternoon: "Good afternoon", evening: "Good evening" } };
const T = { reveal: 2700, end: 5200 };          // ms: panels come in at reveal, overlay gone at end

const el = document.createElement("div");
el.id = "welcome"; el.setAttribute("aria-hidden", "true");
el.innerHTML = `<div class="wl__veil"></div><div class="wl__black"></div>
  <div class="wl__txt"><div class="wl__inner">
    <div class="wl__greet"></div><div class="wl__name"></div><div class="wl__sub"></div>
  </div></div>`;
document.body.appendChild(el);
const hudRoot = document.getElementById("hud");

function greeting(d, g) {
  const h = d.getHours();
  return h >= 5 && h < 12 ? g.morning : h >= 12 && h < 18 ? g.afternoon : g.evening;
}

let playing = false;
function play(opts = {}) {
  if (playing) return false;
  playing = true;
  const name = opts.name || Q.get("name") || DEFAULTS.name;
  const now = new Date();
  if (Q.has("hour")) now.setHours(Number(Q.get("hour")), 0);   // preview: ?hour=8 / 14 / 21
  const text = typeof opts.greet === "string" ? opts.greet : greeting(now, { ...DEFAULTS.greet, ...(opts.greet || {}) });
  el.classList.remove("bye");
  el.querySelector(".wl__greet").textContent = `${text},`;
  el.querySelector(".wl__name").textContent = name;
  el.querySelector(".wl__sub").textContent =
    now.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }) + "   ·   " +
    now.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  hudRoot.classList.remove("hud--wl-in", "hud--standby", "hud--wake");
  hudRoot.classList.add("hud--wl-pre");
  window.hud?.holdWake?.();
  el.classList.remove("on"); void el.offsetWidth; el.classList.add("on");      // restart the CSS timeline
  el.classList.remove("hold");                                                 // the black layer fades out from here
  setTimeout(() => {
    hudRoot.classList.remove("hud--wl-pre"); void hudRoot.offsetWidth;
    hudRoot.classList.add("hud--wl-in");
    window.hud?.sweep?.();
  }, T.reveal);
  setTimeout(() => { hudRoot.classList.remove("hud--wl-in"); el.classList.remove("on"); playing = false; }, T.end);
  return true;
}
// ---- bye: panels fade out and "Good night, Zen" stays on screen; then rp2040-link has the RP2040 fade the
// real backlight to black over 2.5 s (the page itself never goes black, so the fade is the screen's own) ----
function bye(opts = {}) {
  const now = new Date();
  if (Q.has("hour")) now.setHours(Number(Q.get("hour")), 0);
  const h = now.getHours();
  const text = typeof opts.greet === "string" ? opts.greet : (h >= 18 || h < 5 ? "Good night" : "See you");
  playing = true;
  el.querySelector(".wl__greet").textContent = `${text},`;
  el.querySelector(".wl__name").textContent = opts.name || Q.get("name") || DEFAULTS.name;
  el.querySelector(".wl__sub").textContent = "";
  hudRoot.classList.remove("hud--wl-pre", "hud--wl-in", "hud--wake");
  hudRoot.classList.add("hud--bye");
  el.classList.remove("on", "bye", "hold"); void el.offsetWidth; el.classList.add("on", "bye");
}
// back to normal after a bye: key ON again before ACC ends (and the preview loop)
function unbye() {
  if (!el.classList.contains("bye")) return;
  hudRoot.classList.remove("hud--bye"); el.classList.remove("on", "bye"); playing = false;
}
// hold: pure black over everything (key ON, waiting for the first welcome). The backlight is already on,
// so the welcome rises out of real black.
function hold(on) { if (!playing) el.classList.toggle("hold", !!on); }
window.welcome = { play, bye, unbye, hold, get playing() { return playing; } };
if (window.hud?.isHeld?.()) hold(true);   // the hold message arrived before this file loaded

const byeMode = Q.get("bye");
if (byeMode === "1") setTimeout(bye, 1500);
else if (byeMode === "loop") { const go = () => { bye(); setTimeout(unbye, 6000); }; setTimeout(go, 1500); setInterval(go, 9000); }
const mode = Q.get("welcome");
if (mode === "1") setTimeout(play, 700);
else if (mode === "loop") { setTimeout(play, 700); setInterval(play, 9000); }
