#!/usr/bin/env bash
# Auto-connect the Pi to the phone over Bluetooth.
# Fast retries right after boot / after a disconnect, slow retries when the phone isn't around.
# "Connected" means PipeWire has the phone's audio node, not just a Bluetooth link.
set -u

PHONE_MAC="${PHONE_MAC:-38:8A:06:B1:84:E1}"
MAC_US="${PHONE_MAC//:/_}"
DEV_PATH="/org/bluez/hci0/dev_${MAC_US}"
A2DP_SOURCE_UUID="0000110a-0000-1000-8000-00805f9b34fb"   # phone's A2DP source (music) profile

FAST_INTERVAL=0.5      # seconds between attempts after boot / disconnect
FAST_WINDOW=120      # how long to keep retrying fast
SLOW_INTERVAL=20     # retry interval after the fast window (phone not around)
CHECK_INTERVAL=10    # how often to check while connected
CONNECT_TIMEOUT=8    # max time a single connect attempt may take
AUDIO_WAIT=1         # seconds to wait for the audio node after an attempt

log() { echo "[phone-autoconnect] $*"; }
uptime_s() { cut -d' ' -f1 /proc/uptime; }

wait_for_adapter() {
  for _ in $(seq 1 60); do
    if bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then return 0; fi
    bluetoothctl power on >/dev/null 2>&1
    sleep 1
  done
  return 1
}

audio_up() { pw-cli ls Node 2>/dev/null | grep -q "bluez_input.${MAC_US}"; }
link_up()  { bluetoothctl info "$PHONE_MAC" 2>/dev/null | grep -q "Connected: yes"; }

wait_for_audio() {
  for _ in $(seq 1 $((AUDIO_WAIT * 4))); do
    audio_up && return 0
    sleep 0.25
  done
  return 1
}

try_connect() {
  if link_up; then
    # Linked (e.g. only the calls profile) but no music: ask for A2DP specifically
    timeout "$CONNECT_TIMEOUT" busctl --system call org.bluez "$DEV_PATH" \
      org.bluez.Device1 ConnectProfile s "$A2DP_SOURCE_UUID" >/dev/null 2>&1
  else
    timeout "$CONNECT_TIMEOUT" bluetoothctl connect "$PHONE_MAC" >/dev/null 2>&1
  fi
  wait_for_audio
}

wait_for_adapter || { log "Bluetooth adapter never powered on"; exit 1; }
bluetoothctl trust "$PHONE_MAC" >/dev/null 2>&1

# Wait (max 3 s) for PipeWire's Bluetooth side before the first attempt
for _ in $(seq 1 12); do
  pw-cli ls Node 2>/dev/null | grep -q bluez_midi.server && break
  sleep 0.25
done
log "Target phone: $PHONE_MAC (adapter ready at $(uptime_s)s)"

start=$(date +%s)
was_up=0
while true; do
  if audio_up; then
    if [ "$was_up" -eq 0 ]; then
      log "Audio connected at $(uptime_s)s since boot"
      was_up=1
    fi
    sleep "$CHECK_INTERVAL"
    continue
  fi

  if [ "$was_up" -eq 1 ]; then
    log "Audio lost at $(uptime_s)s, retrying"
    was_up=0
    start=$(date +%s)
  fi

  try_connect && continue

  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -lt "$FAST_WINDOW" ]; then
    sleep "$FAST_INTERVAL"
  else
    sleep "$SLOW_INTERVAL"
  fi
done
