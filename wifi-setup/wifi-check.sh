#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
 
IFACE=wlan0
CFG=/etc/wpa_supplicant/wpa_supplicant.conf
LOG=/tmp/wifi-check.log
MAX_WAIT=60
AP_SSID="Orangepi-AP"
AP_PSK="orangepi123"


exec > >(tee -a "$LOG") 2>&1
echo "=== BOOT Fallback Script === $(date)"

systemctl stop captive-portal.service || true
pkill -x hostapd 2>/dev/null || true
pkill -x dnsmasq 2>/dev/null || true

ip link set "$IFACE" down || true
ip addr flush dev "$IFACE" || true
ip link set "$IFACE" up    || true
iw dev "$IFACE" set type managed 2>/dev/null || true


echo "[BOOT] Starting wpa_supplicant..."

pkill -x wpa_supplicant 2>/dev/null || true
rm -rf /var/run/wpa_supplicant*
dhclient -r "$IFACE" 2>/dev/null || true

if ! wpa_supplicant -B -i "$IFACE" -c "$CFG"; then
	echo "[BOOT] wpa_supplicant failed --> falling back to AP"
    /usr/local/bin/start-ap.sh "$IFACE" "$AP_SSID" "$AP_PSK"
    systemctl start captive-portal.service
    exit 0
fi

echo "[BOOT] Requesting DHCP lease..."
dhclient -1 -v "$IFACE" || true


for ((i=0; i<MAX_WAIT; i++)); do
	if ip -4 addr show "$IFACE" | grep -q 'inet '; then
		echo "[BOOT] WIFI connected IP: $(hostname -I)"
		exit 0
	fi
	sleep 1
done

echo "[BOOT] No IP after ${MAX_WAIT}s -> starting fallback AP.."
"/usr/local/bin/start-ap.sh" "$IFACE" "$AP_SSID" "$AP_PSK"
systemctl start captive-portal.service
exit 0

