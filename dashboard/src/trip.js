// Drive summary: while the engine is off (car EXT / PARK) the CVT panel shows the last trip instead of the
// gauges; back to the gauges as soon as the key goes ON again. hud-service sends {"car": state},
// {"trip": {...}, "units": "imperial" | "metric"} (see hud_service.py). Preview: ?demo=1&trip=1.
const Q = new URLSearchParams(location.search);
const panel = document.getElementById("panel-cvt");
const box = document.createElement("div");
box.className = "trip"; box.setAttribute("aria-live", "polite");
box.innerHTML = `
  <div class="panel__head"><span>Last drive</span><span class="trip__when" id="trip-when"></span></div>
  <div class="trip__hero">
    <div><b id="trip-dur">--</b><small>driving</small></div>
    <div><b id="trip-dist">--</b><small id="trip-dist-u">mi</small></div>
  </div>
  <div class="trip__avg" id="trip-avg"></div>
  <div class="trip__grid">
    <div><small>CVT max</small><b id="trip-cvtmax">--</b></div>
    <div><small>CVT avg</small><b id="trip-cvtavg">--</b></div>
    <div><small>Oil max</small><b id="trip-oil">--</b></div>
    <div><small>Coolant max</small><b id="trip-cool">--</b></div>
    <div><small>Max rpm</small><b id="trip-rpm">--</b></div>
    <div><small>Alerts</small><b id="trip-alerts">--</b></div>
  </div>`;
panel.appendChild(box);
const $ = (id) => document.getElementById(id);

let car = null, trip = null, units = "imperial";
const OFF_STATES = ["EXT", "PARK"];
const t12 = (hm) => {                       // "13:05" -> "1:05 PM"
  const [h, m] = (hm || "").split(":").map(Number);
  return Number.isFinite(h) ? `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}` : "";
};
const dur = (min) => min < 60 ? `${Math.round(min)} min` : `${Math.floor(min / 60)} h ${String(Math.round(min % 60)).padStart(2, "0")}`;
const temp = (c) => c == null ? "--" : `${Math.round(c)}°C`;

function draw() {
  const show = !!trip && OFF_STATES.includes(car);
  panel.classList.toggle("panel--trip", show);
  if (!trip) return;
  const imp = units !== "metric", k = imp ? 0.621371 : 1;
  $("trip-when").textContent = `${t12(trip.start)} – ${t12(trip.end)}`;
  $("trip-dur").textContent = dur(trip.duration_min ?? 0);
  $("trip-dist").textContent = ((trip.distance_km ?? 0) * k).toFixed(1);
  $("trip-dist-u").textContent = imp ? "miles" : "km";
  $("trip-avg").textContent = `avg ${Math.round((trip.avg_kmh ?? 0) * k)} ${imp ? "mph" : "km/h"}  ·  top ${Math.round((trip.max_kmh ?? 0) * k)} ${imp ? "mph" : "km/h"}`;
  $("trip-cvtmax").textContent = temp(trip.cvt_max_c);
  $("trip-cvtavg").textContent = temp(trip.cvt_avg_c);
  $("trip-oil").textContent = temp(trip.oil_max_c);
  $("trip-cool").textContent = temp(trip.coolant_max_c);
  $("trip-rpm").textContent = trip.rpm_max ? trip.rpm_max.toLocaleString("en-US") : "--";
  $("trip-alerts").textContent = trip.alerts ?? "--";
  box.classList.toggle("trip--alert", (trip.alerts ?? 0) > 0);
}

window.addEventListener("hud-trip", (ev) => {
  const d = ev.detail;
  if ("units" in d && d.units) units = d.units;
  if ("trip" in d) trip = d.trip;
  if ("car" in d) car = d.car;
  draw();
});
window.trip = { set: (t, c = "EXT", u = "imperial") => { trip = t; car = c; units = u; draw(); } };

if (Q.get("trip") === "1") {
  setTimeout(() => window.trip.set({ start: "13:05", end: "13:29", duration_min: 24.3, distance_km: 19.6, avg_kmh: 48.4,
    max_kmh: 112, cvt_max_c: 86, cvt_avg_c: 78.4, oil_max_c: 97, coolant_max_c: 91, rpm_max: 4850, alerts: 0 }), 600);
}
