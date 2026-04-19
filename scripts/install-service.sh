#!/usr/bin/env bash
# Install the pi-hub systemd service so the web server starts automatically
# on boot and restarts on failure.
#
# Usage:
#   ./scripts/install-service.sh           # install + enable + start
#   ./scripts/install-service.sh --uninstall
set -euo pipefail

UNIT_NAME="pi-hub.service"
SRC_UNIT="$(cd "$(dirname "$0")" && pwd)/${UNIT_NAME}"
DEST_UNIT="/etc/systemd/system/${UNIT_NAME}"

if [ "${1:-}" = "--uninstall" ]; then
  echo "Stopping and removing ${UNIT_NAME}..."
  sudo systemctl disable --now "${UNIT_NAME}" || true
  sudo rm -f "${DEST_UNIT}"
  sudo systemctl daemon-reload
  echo "Uninstalled."
  exit 0
fi

if [ ! -f "${SRC_UNIT}" ]; then
  echo "Unit file not found at ${SRC_UNIT}" >&2
  exit 1
fi

echo "Installing ${UNIT_NAME} -> ${DEST_UNIT}"
sudo install -m 0644 "${SRC_UNIT}" "${DEST_UNIT}"

echo "Reloading systemd..."
sudo systemctl daemon-reload

echo "Enabling and starting ${UNIT_NAME}..."
sudo systemctl enable --now "${UNIT_NAME}"

echo
echo "Status:"
systemctl --no-pager status "${UNIT_NAME}" | head -n 15 || true

echo
echo "Done. Useful commands:"
echo "  sudo systemctl status pi-hub"
echo "  sudo systemctl restart pi-hub"
echo "  sudo systemctl stop pi-hub"
echo "  journalctl -u pi-hub -f"
