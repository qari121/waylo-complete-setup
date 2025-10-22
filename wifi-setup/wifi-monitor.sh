#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/wifi-monitor.log
IFACE=wlan0
CFG=/etc/wpa_supplicant/wpa_supplicant.conf
AP_SSID="OrangePi-AP"
AP_PSK="orangepi123"
CHECK_HOST="8.8.8.8"
CONNECT_WAIT_SECS=15
SLEEP_BETWEEN_CHECKS=5
AP_ACTIVE=0

exec > >(tee -a "$LOG") 2>&1
echo "=== WIFI MONITOR START $(date) ==="

if [[ -f /tmp/force_ap ]]; then
  echo "[MON] /tmp/force_ap present → forcing AP"
  has_known_networks() { return 1; }
fi

ensure_managed_mode() {
  ip link set "$IFACE" down || true
  ip addr flush dev "$IFACE" || true
  iw dev "$IFACE" set type managed 2>/dev/null || true
  ip link set "$IFACE" up || true
  # Avoid power saving disconnects
  iw dev "$IFACE" set power_save off 2>/dev/null || true
}




start_complete_startup_sequence() {
    if [[ -f /home/orangepi/Waylo_AI/complete_startup_sequence.sh ]]; then
        echo "[MON] Network connected - starting complete startup sequence"
        # Run in background to avoid blocking the monitor
        /home/orangepi/Waylo_AI/complete_startup_sequence.sh &
    fi
}

stop_ap_stack_if_running() {
  pkill -x hostapd 2>/dev/null || true
  pkill -x dnsmasq 2>/dev/null || true
  systemctl stop captive-portal.service 2>/dev/null || true
}



start_client_stack() {
  echo "[MON] starting client stack"
  pkill -x hostapd 2>/dev/null || true
  pkill -x dnsmasq 2>/dev/null || true
  systemctl stop captive-portal.service 2>/dev/null || true
  ensure_managed_mode
  # Ensure no leftover dhclient/avahi compete for the interface
  pkill -f "dhclient .*${IFACE}" 2>/dev/null || true
  pkill -x avahi-autoipd 2>/dev/null || true
  pkill -x wpa_supplicant 2>/dev/null || true
  rm -rf /var/run/wpa_supplicant*
  # Log wpa_supplicant to help diagnose dropouts
  wpa_supplicant -B -i "$IFACE" -c "$CFG" -f /tmp/wpa_supplicant.log -d
  # Run DHCP and keep it running to renew leases
  dhclient -r "$IFACE" 2>/dev/null || true
  dhclient -4 -v "$IFACE" &
}

has_ip() {
  ip -4 addr show "$IFACE" | grep -q 'inet '
}

has_connectivity() {
  ping -I "$IFACE" -c1 -W2 "$CHECK_HOST" >/dev/null 2>&1
}

# Detect at least one network block (handles both "network={" and "network = {" forms)
has_known_networks() {
  grep -Eq '^[[:space:]]*network([[:space:]]*=[[:space:]]*|[[:space:]]+)\{' "$CFG" 2>/dev/null
}

# Check if networks are actually reachable (prevents infinite loops)
are_networks_reachable() {
    # If scan tools are missing, allow a switch attempt only when creds were just submitted
    if ! command -v iwlist >/dev/null 2>&1; then
        [[ -f /tmp/creds_ready ]] && return 0 || return 1
    fi

    if [ ! -f "$CFG" ]; then
        return 1
    fi

    # Extract SSIDs using sed; handles embedded apostrophes because SSIDs are double-quoted
    local ssids
    ssids=$(grep -E '^[[:space:]]*ssid="[^"]+"' "$CFG" | sed -E 's/^[[:space:]]*ssid="([^"]+)".*/\1/')
    if [ -z "$ssids" ]; then
        return 1
    fi

    # Try a few scans in case the interface is not ready yet
    local attempt scan
    for attempt in 1 2 3; do
        scan=$(iwlist "$IFACE" scan 2>/dev/null)
        while IFS= read -r ssid; do
            if echo "$scan" | grep -F "\"$ssid\"" >/dev/null; then
                return 0
            fi
        done <<< "$ssids"
        sleep 2
    done

    return 1
}

start_ap_stack() {
  echo "[MON] bringing up AP + portal"
  pkill -x wpa_supplicant 2>/dev/null || true
  dhclient -r "$IFACE" 2>/dev/null || true
  pkill -f "dhclient .*${IFACE}" 2>/dev/null || true
  pkill -x avahi-autoipd 2>/dev/null || true
  ip link set "$IFACE" down || true
  ip addr flush dev "$IFACE" || true
  # Skip waiting for avahi-autoipd - go straight to AP
  /usr/local/bin/start-ap.sh "$IFACE" "$AP_SSID" "$AP_PSK"
  systemctl start captive-portal.service || true
  # Ensure app depending on internet is stopped while AP is active
  stop_wailo_if_enabled
  AP_ACTIVE=1
  # Do not clear cooldown here; it gates client retries while AP is up
}

stop_ap_stack_if_running() {
  pkill -x hostapd 2>/dev/null || true
  pkill -x dnsmasq 2>/dev/null || true
  systemctl stop captive-portal.service 2>/dev/null || true
  stop_wailo_if_enabled
  AP_ACTIVE=0
}

# Initial behavior: if no known networks configured, go straight to AP
if ! has_known_networks; then
  echo "[MON] no known networks in $CFG → starting AP"
  start_ap_stack
else
  # Ensure interface is ready for scanning on first boot before deciding
  ensure_managed_mode
  # Only try client immediately if at least one configured SSID is in range or creds were just submitted
  if are_networks_reachable || [[ -f /tmp/creds_ready ]]; then
    echo "[MON] trying to connect to known networks..."
    start_client_stack

    # Wait for connection with proper success tracking
    connection_success=false
    for i in $(seq 1 "$CONNECT_WAIT_SECS"); do
      if has_ip; then
        echo "[MON] connected (initial)."
        rm -f /tmp/client_connect_failed 2>/dev/null || true
        rm -f /tmp/creds_ready 2>/dev/null || true
        connection_success=true
        if has_connectivity; then
          start_wailo_if_enabled
        else
          stop_wailo_if_enabled
        fi
        break
      fi
      sleep 1
    done

    if [[ "$connection_success" == "false" ]]; then
      echo "[MON] initial connect failed after ${CONNECT_WAIT_SECS}s → starting AP mode"
      pkill -x avahi-autoipd 2>/dev/null || true
      start_ap_stack
    fi
  else
    echo "[MON] no configured SSIDs in range → starting AP"
    start_ap_stack
  fi
fi

# Recalculate AP state from system (survives restarts or external changes)
detect_ap_active() {
  if pidof hostapd >/dev/null 2>&1; then
    AP_ACTIVE=1
    return
  fi
  if iw dev "$IFACE" info 2>/dev/null | grep -q "type AP"; then
    AP_ACTIVE=1
    return
  fi
  AP_ACTIVE=0
}

# Continuous monitor loop
while true; do
  detect_ap_active
  if [[ $AP_ACTIVE -eq 1 ]]; then
    # In AP mode: switch only if (new creds submitted) OR (known network is in range and not cooling down)
    if has_known_networks && { [[ -f /tmp/creds_ready ]] || { are_networks_reachable && [[ ! -f /tmp/ap_cooldown ]]; }; }; then
      echo "[MON] credentials present while AP active → switching to client"
      stop_ap_stack_if_running
      start_client_stack
      for i in $(seq 1 "$CONNECT_WAIT_SECS"); do
        if has_ip; then
          echo "[MON] client connected after credentials."
          rm -f /tmp/client_connect_failed 2>/dev/null || true
          rm -f /tmp/ap_cooldown /tmp/creds_ready 2>/dev/null || true
          if has_connectivity; then
            start_complete_startup_sequence
          else
            # Don't start anything if no internet
          fi
          break
        fi
        sleep 1
      done
      if ! has_ip; then
        echo "[MON] client connect failed → marking as failed and staying in AP mode"
        echo "$(date): client connect failed" > /tmp/client_connect_failed
        # Set cooldown to prevent immediate retry
        echo "$(date): AP cooldown started" > /tmp/ap_cooldown
        rm -f /tmp/creds_ready 2>/dev/null || true
        start_ap_stack
      fi
    fi
    sleep "$SLEEP_BETWEEN_CHECKS"
    continue
  fi

  # Client mode path - if we have connectivity, stay in client mode
  if has_ip; then
    stop_ap_stack_if_running
    if has_connectivity; then
      start_complete_startup_sequence
    else
      # Don't start anything if no internet
    fi
    sleep "$SLEEP_BETWEEN_CHECKS"
    continue
  fi

  # If there are no known networks, stay in AP until user provides creds
  if ! has_known_networks; then
    if [[ $AP_ACTIVE -eq 0 ]]; then
      echo "[MON] no known networks → ensuring AP is up"
      start_ap_stack
    fi
    sleep "$SLEEP_BETWEEN_CHECKS"
    continue
  fi

  # No IP while in client path → no need to stop anything

  # We have known networks but no connectivity - this is the key fix!
  # Try to reconnect, and if that fails, start AP mode
  echo "[MON] known networks exist but no connectivity → trying to reconnect"
  # Kill any avahi-autoipd that might be running before attempting reconnect
  pkill -x avahi-autoipd 2>/dev/null || true
  start_client_stack
  for i in $(seq 1 "$CONNECT_WAIT_SECS"); do
    if has_ip; then
      echo "[MON] reconnected."
      if has_connectivity; then
          start_complete_startup_sequence
      else
          # Don't start anything if no internet
      fi
      break
    fi
    sleep 1
  done
  if ! has_ip; then
    echo "[MON] reconnect failed → starting AP mode"
    # Kill any avahi-autoipd that might be running and go straight to AP
    pkill -x avahi-autoipd 2>/dev/null || true
    start_ap_stack
  fi
  sleep "$SLEEP_BETWEEN_CHECKS"
done



