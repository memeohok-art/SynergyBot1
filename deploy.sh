#!/usr/bin/env bash
# Pull the latest bot code and restart the service.
#   sudo bash deploy.sh

set -euo pipefail
APP_DIR="${APP_DIR:-/opt/synergy-bot}"
SERVICE_NAME="synergy-bot"

cd "$APP_DIR"

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Installing any new requirements"
"$APP_DIR/venv/bin/pip" install --quiet -r requirements.txt

echo "==> Restarting"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl --no-pager --lines=5 status "$SERVICE_NAME" || true

echo
echo "Tailing logs (Ctrl+C to stop):"
journalctl -u "$SERVICE_NAME" -f -n 20
