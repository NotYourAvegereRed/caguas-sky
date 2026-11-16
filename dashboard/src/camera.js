// Reverse camera: the factory camera through the USB capture stick (UVC), full screen over everything.
// hud-service sends {"cam": 0 | 1 | 2}: 0 off, 1 warm (P / N: capturing, hidden, so R shows a picture at once),
// 2 on screen (reverse wire or gear R). The RP2040 powers the camera for 1 and 2.
// Needs --use-fake-ui-for-media-stream (kiosk.sh) and a secure origin (localhost): from another PC there is
// no camera, the page just never shows it. Preview on the Pi: ?cam=2. Test in the car without shifting:
// sudo python3 /usr/local/bin/rp2040-link.py test rev 1
const v = document.createElement("video");
v.muted = true; v.playsInline = true;
v.style.cssText = "position:fixed;inset:0;width:100%;height:100%;object-fit:fill;background:#000;z-index:2147483000;visibility:hidden";
document.body.appendChild(v);

let want = 0, opening = false, retry = 0;
async function sync() {
  v.style.visibility = want === 2 ? "visible" : "hidden";      // black until the first frame, never the dashboard
  if (!want) { v.srcObject?.getTracks().forEach((t) => t.stop()); v.srcObject = null; return; }
  if (v.srcObject || opening) return;
  opening = true;
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: true });
    s.getVideoTracks()[0].onended = () => { v.srcObject = null; sync(); };   // stick unplugged / USB hiccup
    v.srcObject = s; await v.play();
  } catch (e) {
    console.warn("camera:", e); clearTimeout(retry); retry = setTimeout(sync, 2000);
  }
  opening = false;
  sync();                                                      // 'want' may have changed while it opened
}
window.addEventListener("hud-cam", (e) => { want = e.detail || 0; sync(); });
const q = Number(new URLSearchParams(location.search).get("cam"));
if (q) { want = q; sync(); }
