import { renderBackground } from "./widgets/background.js";

// Preview only: ?speed=1200 runs the day 1200x faster (18 h in ~54 s).
const speed = Number(new URLSearchParams(location.search).get("speed"));
if (speed > 1) {
  globalThis.__SKY_SPEED = speed;
  globalThis.__SKY_T0 = Date.now();
  document.getElementById("background-mount").classList.add("sky-fast");
}

// ---- Edit these to taste -------------------------------------------------
const CONFIG = {
  location: { latitude: 18.234, longitude: -66.049, name: "Caguas, PR" },
  locality: "coastal",      // auto | urban | coastal | tropical | lake | mountain | forest | meadow | desert | polar
  motion: new URLSearchParams(location.search).get("motion") || "still",   // still: calm cost ~1 core (101% vs 4% CPU, Nov 5); ?motion=calm to preview
  atmosphere: "balanced",   // soft | balanced | vivid
  readability: "high"       // minimal | standard | high
};
// --------------------------------------------------------------------------

const settings = {
  background: {
    enabled: true,
    kind: "animated",
    motion: CONFIG.motion,
    atmosphere: CONFIG.atmosphere,
    readability: CONFIG.readability
  },
  appearance: { locality: CONFIG.locality },
  weather: { location: CONFIG.location, units: "fahrenheit" }
};

renderBackground(document.getElementById("background-mount"), settings, async () => {})
  .catch((err) => console.error("[sky] background failed", err));
