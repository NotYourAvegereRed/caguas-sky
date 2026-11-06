#!/bin/bash
# Caguas-sky kiosk: modo 1024x600 (EDID null) + chromium con auto-restart
LOG=/tmp/kiosk.log
exec >>"$LOG" 2>&1
echo "=== kiosk $(date) ==="

# 1) esperar output HDMI y forzar modo
for i in $(seq 1 40); do
  OUT=$(wlr-randr 2>/dev/null | awk '/^HDMI/{print $1; exit}')
  [ -n "$OUT" ] && break; sleep 0.5
done
echo "output: $OUT"
# EDID already prefers 1024x600@59.82; forcing a custom 60.00 Hz re-syncs the Santek (black 10-15 s).
# Only switch if the native mode did not come up (no EDID read).
wlr-randr | grep -q "1024x600 px.*current" || wlr-randr --output "$OUT" --mode 1024x600

# 2) esperar el server del dashboard (max 60s)
URL=http://localhost:8080/index.html
for i in $(seq 1 60); do
  curl -s -o /dev/null "$URL" && break; sleep 1
done

# 3) chromium; si se cae, vuelve a abrir
CHR=$(command -v chromium || command -v chromium-browser)
PREF=~/.config/chromium/Default/Preferences
rm -rf ~/.cache/chromium/Default/Cache ~/.cache/chromium/Default/"Code Cache"   # dashboard siempre fresco
while true; do
  # evita la burbuja "Chromium no se cerró correctamente"
  [ -f "$PREF" ] && sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[A-Za-z]*"/"exit_type":"Normal"/' "$PREF"
  "$CHR" --ozone-platform=wayland --kiosk --noerrdialogs --disable-infobars \
    --no-first-run --password-store=basic --overscroll-history-navigation=0 \
    --disable-pinch --disable-session-crashed-bubble --check-for-update-interval=31536000 \
    "$URL"
  echo "chromium salió ($?), reabriendo en 2s"; sleep 2
done
