#!/usr/bin/env bash
#
# Nightly SQLite backup with rotation and an off-host copy hook.
#
# SQLite is a single file and, on a volume, YOU own the backups. Losing the
# volume loses every listing, user and team.
#
# Install (on the Lightsail instance, from the repo root):
#   sudo cp deploy/backup-db.sh /usr/local/bin/marketplace-backup
#   sudo chmod +x /usr/local/bin/marketplace-backup
#   sudo cp deploy/marketplace-backup.service /etc/systemd/system/
#   sudo cp deploy/marketplace-backup.timer   /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now marketplace-backup.timer
#
# Verify:  systemctl list-timers marketplace-backup.timer
#          journalctl -u marketplace-backup.service -n 20

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/marketplace-dashboard}"
DB_PATH="${DB_PATH:-$REPO_DIR/data/marketplace.db}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/data/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

# Optional off-host copy. Set in /etc/default/marketplace-backup, e.g.
#   OFFSITE_CMD='aws s3 cp "$1" s3://my-backups/marketplace/'
OFFSITE_CMD="${OFFSITE_CMD:-}"

log() { printf '%s  %s\n' "$(date -Is)" "$*"; }

if [[ ! -f "$DB_PATH" ]]; then
    log "ERROR: database not found at $DB_PATH"
    exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%dT%H%M%S)"
TARGET="$BACKUP_DIR/marketplace-$STAMP.db"

# Use SQLite's own backup API rather than copying the file. `cp` of a live
# database can capture a torn state; .backup takes a consistent snapshot and
# folds in the WAL. This is the whole reason to prefer it.
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$TARGET'"
else
    # Fall back to the container's Python, which always has sqlite3.
    python3 - "$DB_PATH" "$TARGET" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close(); dst.close()
PY
fi

if [[ ! -s "$TARGET" ]]; then
    log "ERROR: backup is empty: $TARGET"
    exit 1
fi
log "backup written: $TARGET ($(du -h "$TARGET" | cut -f1))"

# Verify the snapshot is actually a usable database before trusting it.
if command -v sqlite3 >/dev/null 2>&1; then
    INTEGRITY="$(sqlite3 "$TARGET" 'PRAGMA integrity_check;')"
    if [[ "$INTEGRITY" != "ok" ]]; then
        log "ERROR: integrity check failed on $TARGET: $INTEGRITY"
        exit 1
    fi
    log "integrity check: ok"
fi

# Optional off-host copy — a backup on the same disk is not a backup.
if [[ -n "$OFFSITE_CMD" ]]; then
    if eval "$OFFSITE_CMD"; then
        log "off-host copy succeeded"
    else
        log "WARNING: off-host copy failed"
    fi
fi

# Rotate.
find "$BACKUP_DIR" -name 'marketplace-*.db' -type f -mtime "+$KEEP_DAYS" -print -delete \
    | while read -r old; do log "pruned $old"; done

log "done"
