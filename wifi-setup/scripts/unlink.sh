#!/usr/bin/env bash
set -euo pipefail

# Remove symlinks created by link.sh and restore latest backups if present.
# Usage: sudo ./scripts/unlink.sh

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[unlink] Re-executing with sudo..."
  exec sudo -E bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

WFW_UNITS_DEFAULT="captive-portal.service"
WFW_UNITS="${WFW_UNITS:-$WFW_UNITS_DEFAULT}"

timestamp() { date +%Y%m%d-%H%M%S; }

log() { echo "[unlink] $*"; }

restore_backup_if_any() {
  local path="$1"
  local latest
  latest=$(ls -1t "${path}.bak-"* 2>/dev/null | head -n1 || true)
  if [[ -n "$latest" && -e "$latest" ]]; then
    log "RESTORE $latest -> $path"
    mv -f "$latest" "$path"
  fi
}

remove_link() {
  local src_rel="$1"
  local dest_abs="$2"
  local src_abs="$PROJECT_DIR/$src_rel"

  if [[ -L "$dest_abs" ]]; then
    local current_target
    current_target="$(readlink -f "$dest_abs" || true)"
    if [[ "$current_target" == "$src_abs" ]]; then
      log "UNLINK $dest_abs"
      rm -f "$dest_abs"
      restore_backup_if_any "$dest_abs"
      return 0
    fi
  fi
  log "SKIP not our link: $dest_abs"
}

while IFS='|' read -r SRC DEST; do
  [[ -z "$SRC" ]] && continue
  remove_link "$SRC" "$DEST"
done < <(
  cat <<'MAP'
dnsmasq.conf|/etc/dnsmasq.conf
switch-to-client.sh|/usr/local/bin/switch-to-client.sh
wpa_supplicant.conf|/etc/wpa_supplicant/wpa_supplicant.conf
wifi-check.sh|/usr/local/bin/wifi-check.sh
hotspot.service|/etc/systemd/system/hotspot.service
start-ap.sh|/usr/local/bin/start-ap.sh
captive-portal.service|/etc/systemd/system/captive-portal.service
ignore-wlan0.conf|/etc/NetworkManager/conf.d/ignore-wlan0.conf
 
MAP
)

for unit in $WFW_UNITS; do :; done

log "Done. You may want: sudo ./scripts/reload-services.sh"

