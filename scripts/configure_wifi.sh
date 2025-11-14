#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo $0 <ssid> <psk> [country])" >&2
  exit 1
fi

SSID=${1:-}
PSK=${2:-}
COUNTRY=${3:-GB}

if [[ -z "$SSID" || -z "$PSK" ]]; then
  echo "Usage: sudo $0 <ssid> <password> [country_code]" >&2
  exit 1
fi

WPA_CONF=/etc/wpa_supplicant/wpa_supplicant.conf
BACKUP="${WPA_CONF}.$(date +%Y%m%d%H%M%S).bak"

echo "Backing up ${WPA_CONF} to ${BACKUP}" >&2
cp "$WPA_CONF" "$BACKUP" 2>/dev/null || true

cat > "$WPA_CONF" <<CONFIG
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=${COUNTRY}

network={
    ssid="${SSID}"
    psk="${PSK}"
}
CONFIG

chmod 600 "$WPA_CONF"

rfkill unblock wifi || true
wpa_cli -i wlan0 reconfigure >/dev/null 2>&1 || systemctl restart wpa_supplicant.service || true

cat <<MSG
Wi-Fi configuration updated.
If the interface was down, reboot with 'sudo reboot' to apply settings.
MSG
