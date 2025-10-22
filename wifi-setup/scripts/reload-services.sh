#!/usr/bin/env bash
set -euo pipefail

# Reload/restart services likely affected by config changes.
# Usage: sudo ./scripts/reload-services.sh

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[reload] Re-executing with sudo..."
  exec sudo -E bash "$0" "$@"
fi

log() { echo "[reload] $*"; }

systemctl daemon-reload || true

restart_if_present() {
  local unit="$1"
  if systemctl list-unit-files | awk '{print $1}' | grep -qx "$unit"; then
    log "RESTART $unit"
    systemctl restart "$unit" || true
  else
    # Fall back: try anyway but don't fail the script
    systemctl restart "$unit" >/dev/null 2>&1 || true
  fi
}

# Networking-related services
restart_if_present hostapd.service
restart_if_present dnsmasq.service
restart_if_present hotspot.service
restart_if_present captive-portal.service
restart_if_present wailo.service

# NetworkManager (if its conf changed)
if [[ -e /etc/NetworkManager/conf.d/ignore-wlan0.conf ]]; then
  restart_if_present NetworkManager.service
fi

# Apply sysctl
if [[ -e /etc/sysctl.conf ]]; then
  log "APPLY sysctl from /etc/sysctl.conf"
  sysctl -p /etc/sysctl.conf || true
fi

log "Done."

