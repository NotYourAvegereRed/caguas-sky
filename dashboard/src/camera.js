// camera.js: backup camera page. hud.js fires "hud-cam" from hud_service: 0 off, 1 warm (P / N: the capture is
// open but hidden, so R shows a picture at once), 2 on screen (REV wire or gear R, + 8 s). A failed open is
// retried every 2 s while wanted. Preview on a laptop: ?cam=2 (uses its webcam), ?cam=1 warm.

// Guides, in a 1000 x 600 box stretched over the picture. CALIBRATE IN THE CAR: put cones (or boxes) at
// 0.5 m, 1 m and 2 m behind the bumper, at the car's width, and move these numbers until the lines sit on them.
const GUIDE = {
  near: { y: 600, l: 210, r: 790 },   // bottom of the screen (bumper)
  far:  { y: 300, l: 405, r: 595 },   // end of the side lines (~2 m)
  marks: [ { y: 515, c: "#ff3b30" },  // ~0.5 m: red
           { y: 420, c: "#ffd60a" },  // ~1 m:   yellow
           { y: 300, c: "#34c759" } ],// ~2 m:   green
  tick: 34,                           // length of the cross marks
};
const SKIP = /rp1|pisp|unicam|bcm2835|hevc/i;   // Pi 5 internal video nodes, never the capture card

const box = document.createElement("div");
box.id = "cam"; box.hidden = true;
box.innerHTML = `<video class="cam__v" muted playsinline autoplay></video>
  <svg class="cam__g" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true"></svg>
  <div class="cam__msg" hidden></div>`;
document.body.appendChild(box);
const video = box.querySelector("video"), svg = box.querySelector("svg"), msg = box.querySelector(".cam__msg");

// side lines take the color of the zone they cross; cross marks at each distance
const xAt = (side, y) => {
  const k = (GUIDE.near.y - y) / (GUIDE.near.y - GUIDE.far.y);
  return GUIDE.near[side] + (GUIDE.far[side] - GUIDE.near[side]) * k;
};
let guides = "", y0 = GUIDE.near.y;
for (const m of GUIDE.marks) {
  for (const s of ["l", "r"]) guides += `<line x1="${xAt(s, y0)}" y1="${y0}" x2="${xAt(s, m.y)}" y2="${m.y}" stroke="${m.c}"/>`;
  guides += `<line x1="${xAt("l", m.y)}" y1="${m.y}" x2="${xAt("l", m.y) + GUIDE.tick}" y2="${m.y}" stroke="${m.c}"/>`;
  guides += `<line x1="${xAt("r", m.y)}" y1="${m.y}" x2="${xAt("r", m.y) - GUIDE.tick}" y2="${m.y}" stroke="${m.c}"/>`;
  y0 = m.y;
}
svg.innerHTML = guides;

let stream = null, want = 0, devId = null, opening = false, retry = 0;

async function pickDevice() {
  const all = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput");
  return all.find(d => d.label && !SKIP.test(d.label))?.deviceId ?? null;
}

async function open() {
  if (stream || opening || !want) return;
  opening = true; clearTimeout(retry);
  try {
    if (!devId) {   // labels only show after the first permission; the kiosk grants it on its own
      const probe = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      probe.getTracks().forEach(t => t.stop());
      devId = await pickDevice();
    }
    const s = await navigator.mediaDevices.getUserMedia({ audio: false,
      video: { ...(devId ? { deviceId: { exact: devId } } : {}), width: { ideal: 720 }, height: { ideal: 480 } } });
    if (!want) s.getTracks().forEach(t => t.stop());   // camera off while it was opening
    else {
      stream = s; video.srcObject = s; msg.hidden = true;
      s.getVideoTracks()[0].onended = () => { close(); msg.textContent = "Capture card desconectada"; msg.hidden = false; open(); };
    }
  } catch (e) {
    devId = null;
    msg.textContent = "Sin señal de la cámara"; msg.hidden = false;   // from Chromium: the capture card did not open
    console.warn("camera:", e);
    retry = setTimeout(open, 2000);
  }
  opening = false;
}

function close() {
  stream?.getTracks().forEach(t => t.stop());
  stream = null; video.srcObject = null;
}

function show(v) {
  v = v === true ? 2 : Number(v) || 0;
  if (v === want) return;
  const was = want; want = v;
  if (v) open(); else { clearTimeout(retry); close(); }
  if (v === 2) {
    box.hidden = false;
    requestAnimationFrame(() => box.classList.add("cam--on"));
  } else if (was === 2) {
    box.classList.remove("cam--on");
    setTimeout(() => { if (want !== 2) box.hidden = true; }, 350);
  }
}

window.addEventListener("hud-cam", e => show(e.detail));
const q = new URLSearchParams(location.search).get("cam");
if (q) show(Number(q));
window.camera = { show };
