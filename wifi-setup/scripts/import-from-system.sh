#!/usr/bin/env bash
set -euo pipefail

# Import current system configs into the project if missing (or with --force to overwrite).
# Usage:
#   ./scripts/import-from-system.sh [--force]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

WFW_UNITS_DEFAULT="captive-portal.service wailo.service"
WFW_UNITS="${WFW_UNITS:-$WFW_UNITS_DEFAULT}"

log() { echo "[import] $*"; }

copy_in() {
  local dest_abs="$1"   # system file
  local src_rel="$2"    # repo-relative
  local src_abs="$PROJECT_DIR/$src_rel"

  if [[ $FORCE -eq 0 && -e "$src_abs" ]]; then
    log "SKIP exists: $src_rel"
    return 0
  fi

  if sudo test -e "$dest_abs"; then
    log "COPY $dest_abs -> $src_rel"
    sudo cp -a "$dest_abs" "$src_abs.tmp"
    sudo chown "$(id -u):$(id -g)" "$src_abs.tmp"
    mv -f "$src_abs.tmp" "$src_abs"
  else
    log "MISS on system (not found): $dest_abs"
  fi
}

while IFS='|' read -r SRC DEST; do
  [[ -z "$SRC" ]] && continue
  copy_in "$DEST" "$SRC"
done < <(
  cat <<'MAP'
hostapd.conf|/etc/hostapd/hostapd.conf
dnsmasq.conf|/etc/dnsmasq.conf
switch-to-client.sh|/usr/local/bin/switch-to-client.sh
wpa_supplicant.conf|/etc/wpa_supplicant/wpa_supplicant.conf
sysctl.conf|/etc/sysctl.conf
wifi-check.sh|/usr/local/bin/wifi-check.sh
hotspot.service|/etc/systemd/system/hotspot.service
start-ap.sh|/usr/local/bin/start-ap.sh
set-wlan0-ip.service|/etc/systemd/system/set-wlan0-ip.service
captive-portal.service|/etc/systemd/system/captive-portal.service
wailo.service|/etc/systemd/system/wailo.service
ignore-wlan0.conf|/etc/NetworkManager/conf.d/ignore-wlan0.conf
set-wlan0-ip.sh|/usr/local/bin/set-wlan0-ip.sh
MAP
)

for unit in $WFW_UNITS; do
  copy_in "/etc/systemd/system/${unit}.d/wait-for-wlan0.conf" "wait-for-wlan0.conf"
done

log "Done. You can now edit files in the repo and run: sudo ./scripts/link.sh"

