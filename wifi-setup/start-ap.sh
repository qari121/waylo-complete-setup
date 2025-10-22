##############################################################################
# /usr/local/bin/start-ap.sh    (chmod +x)
##############################################################################
#!/usr/bin/env bash
# Usage: start-ap.sh <iface> <ssid> <psk>
IFACE=${1:-wlan0}
SSID=${2:-OrangePi-AP}
PSK=${3:-orangepi123}

echo "[HOTSPOT] Bringing up fallback AP $SSID…"

cat >/tmp/hostapd.conf <<EOF
interface=$IFACE
driver=nl80211
ssid=$SSID
hw_mode=g
channel=6
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=$PSK
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ieee80211n=1
country_code=US
EOF

ip link set "$IFACE" down || true
ip addr flush dev "$IFACE" || true
iw dev "$IFACE" set type __ap 2>/dev/null || true
ip link set "$IFACE" up || true
ip addr add 192.168.4.1/24 dev "$IFACE" || true

pkill -x hostapd 2>/dev/null || true
hostapd /tmp/hostapd.conf -B

systemctl restart dnsmasq   # serves DHCP 192.168.4.10-50
##############################################################################
# END start-ap.sh
##############################################################################
