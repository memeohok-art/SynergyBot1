#!/usr/bin/env bash
# Daily backup of the Synergy bot's state.
#
#   sudo bash /opt/synergy-bot/backup.sh
#
# Backs up /var/lib/synergy-bot (botdata.json, transcripts) plus the .env.
# Safe to run any time - it takes a consistent copy with tar and never touches
# the live files.
#
# Overridable via environment:
#   DATA_DIR       default /var/lib/synergy-bot
#   ENV_FILE       default /opt/synergy-bot/.env
#   DEST           default /var/backups/synergy-bot
#   KEEP           default 14   (how many local copies to retain)
#   RCLONE_REMOTE  optional rclone target, e.g. "oci:synergy-backups"
#   REMOTE_KEEP    default 30d  (rclone --min-age for remote pruning)

set -euo pipefail

DATA_DIR="${DATA_DIR:-/var/lib/synergy-bot}"
ENV_FILE="${ENV_FILE:-/opt/synergy-bot/.env}"
DEST="${DEST:-/var/backups/synergy-bot}"
KEEP="${KEEP:-14}"
STAMP="$(date -u +%Y-%m-%d_%H%M%S)"
ARCHIVE="$DEST/synergy-$STAMP.tar.gz"

log() { echo "$(date -u +%FT%TZ) $*"; }

mkdir -p "$DEST"
chmod 700 "$DEST"

# Build a relative file list so a restore is just: tar -C / -xzf <archive>
REL=()
if [ -d "$DATA_DIR" ]; then
  REL+=("${DATA_DIR#/}")
fi
if [ -f "$ENV_FILE" ]; then
  REL+=("${ENV_FILE#/}")
fi
if [ "${#REL[@]}" -eq 0 ]; then
  log "ERROR: nothing to back up (no $DATA_DIR and no $ENV_FILE)"
  exit 1
fi

# The archive contains .env, which holds the bot token -> keep it private
umask 077
tar -C / -czf "$ARCHIVE" "${REL[@]}"

# Verify it is actually readable, not just written
if ! tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
  log "ERROR: archive failed verification, removing $ARCHIVE"
  rm -f "$ARCHIVE"
  exit 1
fi

chmod 600 "$ARCHIVE"
SIZE=$(du -h "$ARCHIVE" | cut -f1)
log "wrote $ARCHIVE ($SIZE)"

# Prune local copies, newest first
if [ "$KEEP" -gt 0 ]; then
  ls -1t "$DEST"/synergy-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
  log "local copies kept: $(ls -1 "$DEST"/synergy-*.tar.gz 2>/dev/null | wc -l)"
fi

# Optional off-box copy: a backup on the same VM is not disaster recovery
if [ -n "${RCLONE_REMOTE:-}" ]; then
  if command -v rclone >/dev/null 2>&1; then
    if rclone copy "$ARCHIVE" "$RCLONE_REMOTE" --quiet; then
      log "off-box copy OK -> $RCLONE_REMOTE"
      rclone delete "$RCLONE_REMOTE" --min-age "${REMOTE_KEEP:-30d}" --quiet 2>/dev/null || true
    else
      log "WARN: off-box copy to $RCLONE_REMOTE failed"
    fi
  else
    log "WARN: RCLONE_REMOTE set but rclone is not installed"
  fi
fi
