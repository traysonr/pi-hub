#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SRC_SCRIPT="$REPO_ROOT/scripts/pi-hub-wifi-onboard"
SRC_UNIT="$REPO_ROOT/deploy/systemd/pi-hub-wifi-onboard.service"
SRC_DROPIN="$REPO_ROOT/deploy/systemd/pi-hub.service.d/wifi-onboard.conf"

if [[ ! -f "$SRC_SCRIPT" || ! -f "$SRC_UNIT" || ! -f "$SRC_DROPIN" ]]; then
  echo "Missing source files in repo. Are you running from the pi-hub repo?"
  exit 1
fi

echo "Installing Pi Hub Wi‑Fi onboarding (needs sudo)…"

sudo install -m 0755 "$SRC_SCRIPT" /usr/local/sbin/pi-hub-wifi-onboard

sudo install -d /etc/systemd/system
sudo install -m 0644 "$SRC_UNIT" /etc/systemd/system/pi-hub-wifi-onboard.service

sudo install -d /etc/systemd/system/pi-hub.service.d
sudo install -m 0644 "$SRC_DROPIN" /etc/systemd/system/pi-hub.service.d/wifi-onboard.conf

sudo systemctl daemon-reload
sudo systemctl enable pi-hub-wifi-onboard.service

echo
echo "Done."
echo "To test now (without reboot):"
echo "  sudo systemctl start pi-hub-wifi-onboard.service"
echo
echo "Then reboot to verify it runs before Pi Hub:"
echo "  sudo reboot"

