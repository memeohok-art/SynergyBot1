#!/usr/bin/env bash
# One-shot installer for the Synergy Discord bot on Ubuntu / Debian.
#
#   sudo bash install.sh
#
# Safe to re-run: it updates code, dependencies and the unit file in place.
# Your token in .env and your data in /var/lib/synergy-bot are never touched.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/memeohok-art/SynergyBot1.git}"
APP_DIR="${APP_DIR:-/opt/synergy-bot}"
DATA_DIR="${DATA_DIR:-/var/lib/synergy-bot}"
SERVICE_USER="${SERVICE_USER:-synergy}"
SERVICE_NAME="synergy-bot"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash $0" >&2
  exit 1
fi

bold "==> 1/7  Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates

bold "==> 2/7  Creating service user '$SERVICE_USER'"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "    created"
else
  echo "    already exists"
fi

bold "==> 3/7  Fetching code into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  mkdir -p "$APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

bold "==> 4/7  Creating virtualenv and installing requirements"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

bold "==> 5/7  Preparing data directory $DATA_DIR"
mkdir -p "$DATA_DIR/transcripts"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

bold "==> 6/7  Writing configuration"
NEW_ENV=0
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  NEW_ENV=1
  echo "    created .env from the example"
fi
grep -q '^DATA_FILE='      "$APP_DIR/.env" || echo "DATA_FILE=$DATA_DIR/botdata.json"        >> "$APP_DIR/.env"
grep -q '^TRANSCRIPT_DIR=' "$APP_DIR/.env" || echo "TRANSCRIPT_DIR=$DATA_DIR/transcripts"     >> "$APP_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
echo "    .env is mode 600 (only $SERVICE_USER can read it)"

bold "==> 7/7  Installing the systemd unit"
install -m 644 "$APP_DIR/systemd/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload

echo
if [ "$NEW_ENV" -eq 1 ] || grep -q '^DISCORD_TOKEN=$' "$APP_DIR/.env" 2>/dev/null; then
  # Enabled but deliberately not started yet: without a token it would just
  # crash-loop, and a reboot before the token is set would do the same.
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  bold "Almost there - add your bot token:"
  cat <<EOF

    sudo nano $APP_DIR/.env
    # set:  DISCORD_TOKEN=your_token_here
    # save (Ctrl+O, Enter, Ctrl+X)

    sudo systemctl restart $SERVICE_NAME
    journalctl -u $SERVICE_NAME -f

EOF
  exit 0
fi

bold "Restarting the service"
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"
sleep 4
systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
echo
bold "Done. Follow the log with:"
echo "    journalctl -u $SERVICE_NAME -f"
echo
echo "Look for:  'Logged in as Synergy'  and  'Commands loaded: 98'"
