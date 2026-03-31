#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# backup_tasks.sh — hourly backup of data.json with change detection
#
# Structure:
#   ../backups/
#     2026/
#       04-April/
#         2026-04-01_14-00.json
#         2026-04-01_15-00.json
#         ...
#
# Cron (runs every hour):
#   0 * * * * /path/to/backup_tasks.sh >> /path/to/backup_tasks.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/data.json"
BACKUP_ROOT="$(dirname "$SCRIPT_DIR")/backups"

# ── Sanity check ─────────────────────────────────────────────────────────────
if [[ ! -f "$SOURCE" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP — data.json not found at $SOURCE"
  exit 0
fi

# ── Build destination path ───────────────────────────────────────────────────
YEAR=$(date '+%Y')
MONTH=$(date '+%m-%B')          # e.g. 04-April
TIMESTAMP=$(date '+%Y-%m-%d_%H-%M')
DEST_DIR="$BACKUP_ROOT/$YEAR/$MONTH"
DEST_FILE="$DEST_DIR/$TIMESTAMP.json"

# ── Change detection via md5 ─────────────────────────────────────────────────
SOURCE_MD5=$(md5sum "$SOURCE" | awk '{print $1}')

# Find the most recent backup by filename (names encode timestamp lexicographically).
# Avoid sorting by mtime — file copies/touches would give wrong results.
LATEST=""
if [[ -d "$BACKUP_ROOT" ]]; then
  LATEST=$(find "$BACKUP_ROOT" -name '*.json' -print 2>/dev/null | sort | tail -1)
fi

if [[ -n "$LATEST" ]]; then
  LATEST_MD5=$(md5sum "$LATEST" | awk '{print $1}')
  if [[ "$SOURCE_MD5" == "$LATEST_MD5" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP — no changes since last backup ($LATEST)"
    exit 0
  fi
fi

# ── Write backup (only now do we create the directory) ───────────────────────
mkdir -p "$DEST_DIR"

# ── Write backup ─────────────────────────────────────────────────────────────
cp "$SOURCE" "$DEST_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] OK   — backed up to $DEST_FILE (md5: $SOURCE_MD5)"
