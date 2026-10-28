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
echo "==> Waiting for the bot to come up"
sleep 8

echo
echo "==> Boot lines:"
journalctl -u "$SERVICE_NAME" --no-pager -n 80 \
  | grep -E "Logged in|Commands loaded|Synced|Members intent|Data file|Traceback|Error|error" \
  || echo "    (no matching lines yet - check: journalctl -u $SERVICE_NAME -n 50)"

echo
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "Service is active. Expected: 'Commands loaded: 99' and 'Synced 98 slash commands'."
else
  echo "Service is NOT active. Investigate with: journalctl -u $SERVICE_NAME -n 50"
  exit 1
fi
