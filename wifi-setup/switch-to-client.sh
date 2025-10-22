#!/usr/bin/env bash
# switch-to-client.sh ─ invoked when the user submits Wi-Fi credentials
# ───────────────────────────────────────────────────────────────
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOG=/tmp/client-switch.log
IFACE=wlan0
CFG=/etc/wpa_supplicant/wpa_supplicant.conf
AP_SSID="OrangePi-AP"
AP_PSK="orangepi123"

exec &> >(tee -a "$LOG")
echo "=== PORTAL → CLIENT  $(date) ==="

echo "[PORTAL] stopping portal & hotspot…"
# Stop AP pieces first; do NOT stop the portal here (portal unit may be our parent)
systemctl stop hotspot.service 2>/dev/null || true
pkill hostapd   2>/dev/null
pkill dnsmasq   2>/dev/null

# re-configure interface         
ip link set "$IFACE" down        || true
ip addr flush dev "$IFACE"       || true
ip route flush dev "$IFACE"       || true
iw dev "$IFACE" set type managed || true
ip link set "$IFACE" up          || true

echo "[PORTAL] starting wpa_supplicant…"
killall wpa_supplicant 2>/dev/null
rm -rf /var/run/wpa_supplicant* 2>/dev/null || true
sleep 1
wpa_supplicant -B -i "$IFACE" -c "$CFG" -f /tmp/wpa_supplicant.log -d

echo "[Portal] waiting for wifi to associate.."
for i in {1..15}; do
	if wpa_cli -i "$IFACE" status 2>/dev/null | \
		grep -q '^wpa_state=COMPLETED'; then
			echo "[Portal] associated with $(wpa_cli -i "$IFACE" status \
				| grep '^ssid=' | cut -d= -f2)"
			break
		fi
		sleep 1
done

echo "[PORTAL] requesting DHCP lease…"
dhclient -r "$IFACE"
# Run DHCP in background and wait for it to complete
dhclient -4 -v "$IFACE" &
DHCP_PID=$!

# Wait for DHCP to complete (max 30 seconds)
echo "[PORTAL] waiting for DHCP to complete..."
for i in {1..30}; do
    if ! kill -0 $DHCP_PID 2>/dev/null; then
        echo "[PORTAL] DHCP completed"
        break
    fi
    sleep 1
done

# Kill DHCP if it's still running
kill $DHCP_PID 2>/dev/null || true

# Wait a bit more for interface to settle
sleep 3

# Check both IP and connectivity
if ip -4 addr show "$IFACE" | grep -q 'inet '; then
    # Verify we're connected to the intended network
    CURRENT_SSID=$(wpa_cli -i "$IFACE" status 2>/dev/null | grep '^ssid=' | cut -d= -f2)
    if [[ -n "$CURRENT_SSID" ]]; then
        echo "[PORTAL] connected to network: $CURRENT_SSID"
        
        # Test actual connectivity to 8.8.8.8
        if ping -I "$IFACE" -c1 -W2 8.8.8.8 >/dev/null 2>&1; then
            echo "[PORTAL] connected with internet access!"
            # Now we can safely stop the portal
            systemctl stop captive-portal.service 2>/dev/null || true
            exit 0
        else
            echo "[PORTAL] has IP but no internet -> reverting to hotspot"
            /usr/local/bin/start-ap.sh "$IFACE" "$AP_SSID" "$AP_PSK"
            systemctl start captive-portal.service
            exit 1
        fi
    else
        echo "[PORTAL] no SSID detected -> reverting to hotspot"
        /usr/local/bin/start-ap.sh "$IFACE" "$AP_SSID" "$AP_PSK"
        systemctl start captive-portal.service
        exit 1
    fi
else
    echo "[PORTAL] connection failed -> reverting to hotspot"
    /usr/local/bin/start-ap.sh "$IFACE" "$AP_SSID" "$AP_PSK"
    systemctl start captive-portal.service
    exit 1
fi
