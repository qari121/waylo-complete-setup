#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Usage: sudo portal-apply-creds.sh "SSID" "PSK"
# Appends a network block to /etc/wpa_supplicant/wpa_supplicant.conf
# then switches the device from AP to client mode.

IFACE=wlan0
CFG=/etc/wpa_supplicant/wpa_supplicant.conf

SSID=${1:-}
PSK=${2:-}

if [[ -z "$SSID" || -z "$PSK" ]]; then
  echo "usage: sudo $(basename "$0") \"SSID\" \"PSK\"" >&2
  exit 2
fi

# Basic sanitization (no embedded newlines)
if [[ "$SSID" == *$'\n'* || "$PSK" == *$'\n'* ]]; then
  echo "SSID/PSK must be single-line" >&2
  exit 2
fi

echo "[portal] adding/updating network for SSID: $SSID"

tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT

# Remove any existing block(s) for this SSID
awk -v ssid="$SSID" '
  # Enter a network block when seeing either "network={" or "network = {" (with optional leading spaces)
  /^[[:space:]]*network([[:space:]]*=[[:space:]]*|[[:space:]]+)\{/ { inblk=1; buf=$0; next }
  inblk {
    buf = buf "\n" $0
    if ($0 ~ /^\}/) {
      inblk=0
      if (buf ~ "ssid=\"" ssid "\"") { next } else { print buf }
      buf=""
    }
    next
  }
  { print }
' "$CFG" > "$tmpfile"

# Append the single desired block
cat >>"$tmpfile" <<EOF

network={
    ssid="${SSID}"
    psk="${PSK}"
    key_mgmt=WPA-PSK
}
EOF

cp "$tmpfile" "$CFG"

echo "[portal] credentials saved → asking monitor to switch"
# Clear force flag if present so monitor does not re-open AP
rm -f /tmp/force_ap 2>/dev/null || true
# Signal the monitor to switch without restart
date > /tmp/creds_ready

