#!/usr/bin/env bash
set -euo pipefail

# Symlink project files into their live system locations so edits in-repo take effect immediately.
# Usage:
#   sudo ./scripts/link.sh
# Optional env:
#   WFW_UNITS="captive-portal.service wailo.service"  # services to receive wait-for-wlan0.conf drop-in

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[link] Re-executing with sudo..."
  exec sudo -E bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Default services for the wait-for-wlan0.conf drop-in
WFW_UNITS_DEFAULT="captive-portal.service wailo.service"
WFW_UNITS="${WFW_UNITS:-$WFW_UNITS_DEFAULT}"

timestamp() { date +%Y%m%d-%H%M%S; }

log() { echo "[link] $*"; }

ensure_exec() {
  local rel="$1"
  local src="$PROJECT_DIR/$rel"
  if [[ -f "$src" ]]; then
    chmod 755 "$src" || true
  fi
}

is_placeholder() {
  local src_rel="$1"
  local src_abs="$PROJECT_DIR/$src_rel"
  if [[ -f "$src_abs" ]] && grep -q "WIFI-SETUP-PLACEHOLDER" "$src_abs"; then
    return 0
  fi
  return 1
}

backup_and_link() {
  local src_rel="$1"
  local dest_abs="$2"

  local src_abs="$PROJECT_DIR/$src_rel"
  if [[ ! -e "$src_abs" ]]; then
    log "SKIP missing source: $src_rel"
    return 0
  fi

  if is_placeholder "$src_rel"; then
    log "SKIP placeholder (import first): $src_rel -> $dest_abs"
    return 0
  fi

  mkdir -p "$(dirname "$dest_abs")"

  # Special-case secure copy for sudoers drop-ins (no symlinks; enforce perms)
  if [[ "$dest_abs" == /etc/sudoers.d/* ]]; then
    if [[ -e "$dest_abs" || -L "$dest_abs" ]]; then
      local bak="$dest_abs.bak-$(timestamp)"
      log "BACKUP existing sudoers: $dest_abs -> $bak"
      rm -f "$bak" 2>/dev/null || true
      cp -a "$dest_abs" "$bak" 2>/dev/null || true
      rm -f "$dest_abs"
    fi
    install -o root -g root -m 440 "$src_abs" "$dest_abs"
    log "INSTALLED sudoers: $dest_abs (root:root 440)"
    return 0
  fi

  if [[ -L "$dest_abs" ]]; then
    local current_target
    current_target="$(readlink -f "$dest_abs" || true)"
    if [[ "$current_target" == "$src_abs" ]]; then
      log "OK already linked: $dest_abs -> $src_rel"
      return 0
    fi
    log "REPLACE existing symlink: $dest_abs (was -> $current_target)"
    rm -f "$dest_abs"
  elif [[ -e "$dest_abs" ]]; then
    local bak="$dest_abs.bak-$(timestamp)"
    log "BACKUP existing file: $dest_abs -> $bak"
    mv "$dest_abs" "$bak"
  fi

  ln -s "$src_abs" "$dest_abs"
  log "LINK created: $dest_abs -> $src_rel"
}

# Ensure executable bits for scripts that will be linked into /usr/local/bin
ensure_exec "switch-to-client.sh"
ensure_exec "wifi-check.sh"
ensure_exec "start-ap.sh"
ensure_exec "wifi-monitor.sh"
ensure_exec "portal-apply-creds.sh"
:

# Static mappings: repo-relative | absolute-destination
while IFS='|' read -r SRC DEST; do
  [[ -z "$SRC" ]] && continue
  backup_and_link "$SRC" "$DEST"
done < <(
  cat <<'MAP'
dnsmasq.conf|/etc/dnsmasq.conf
switch-to-client.sh|/usr/local/bin/switch-to-client.sh
wpa_supplicant.conf|/etc/wpa_supplicant/wpa_supplicant.conf
wifi-check.sh|/usr/local/bin/wifi-check.sh
wifi-monitor.sh|/usr/local/bin/wifi-monitor.sh
portal-apply-creds.sh|/usr/local/bin/portal-apply-creds.sh
hotspot.service|/etc/systemd/system/hotspot.service
start-ap.sh|/usr/local/bin/start-ap.sh
captive-portal.service|/etc/systemd/system/captive-portal.service
wailo.service|/etc/systemd/system/wailo.service
ignore-wlan0.conf|/etc/NetworkManager/conf.d/ignore-wlan0.conf
 
sudoers.d/99-wifi-portal|/etc/sudoers.d/99-wifi-portal
MAP
)

# wait-for-wlan0.conf drop-in for each configured unit
for unit in $WFW_UNITS; do :; done

log "Done. Consider running: sudo ./scripts/reload-services.sh"

