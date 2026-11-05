#!/bin/sh
# wifi-prefer-home: si el Pi esta en el hotspot (Kenepa.M) y una red de la casa se ve bien, se cambia.
# NetworkManager no deja una conexion que funciona por otra de mas prioridad; esto lo hace.
# Lo corre wifi-prefer-home.timer cada 2 min. MIN = senal minima (0-100) para no cambiar con la casa lejos.
MIN=50
nmcli -t -f NAME,DEVICE connection show --active | grep -qx "Hotspot:wlan0" || exit 0
list=$(nmcli -t -f SSID,SIGNAL device wifi list --rescan yes)
for s in Kenepa.4 Kenepa.3; do
  sig=$(echo "$list" | awk -F: -v s="$s" "\$1==s && \$2>m {m=\$2} END {print m+0}")
  if [ "$sig" -ge "$MIN" ]; then echo "Hotspot -> $s (senal $sig)"; exec nmcli connection up "$s"; fi
done
