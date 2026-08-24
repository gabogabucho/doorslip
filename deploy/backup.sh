#!/usr/bin/env bash
#
# Back up the Doorslip database and the welcome desk's key.
#
#   sudo ./backup.sh                 # one copy, now
#   sudo ./backup.sh --keep 30       # and prune to the last 30
#
# Installed as a daily cron job by install.sh.
#
# What this protects, and what it cannot: a dead disk would otherwise take
# every handle, address book and message with it, and there is no identity
# recovery in this protocol (spec §10) — a lost directory is not something the
# people using it can rebuild. It does NOT protect anybody whose own key file
# is lost; that key never existed here and never will.

set -euo pipefail

DB=/var/lib/doorslip/doorslip.db
KEY=/var/lib/doorslip/doorslip.welcome.json
DEST=/var/backups/doorslip
KEEP=14

while [[ $# -gt 0 ]]; do
	case "$1" in
		--keep) KEEP="$2"; shift 2 ;;
		*) echo "unknown option: $1" >&2; exit 1 ;;
	esac
done

# Root-only. A backup holds every message anybody ever sent through here, so
# it is no less sensitive than the live database.
install -d -m 700 "$DEST"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$DEST/doorslip-$STAMP.db"

# `.backup` rather than cp: SQLite is running with WAL, and copying the file
# while a write is in flight yields an archive that looks fine and restores
# short. This takes a consistent snapshot of a live database.
sqlite3 "$DB" ".backup '$TARGET'"
chmod 600 "$TARGET"

if [[ -f "$KEY" ]]; then
	cp "$KEY" "$DEST/welcome-$STAMP.json"
	chmod 600 "$DEST/welcome-$STAMP.json"
fi

# Verify before pruning. An unreadable backup that displaced a good one is
# worse than no backup, because it is discovered on the day it is needed.
if ! sqlite3 "$TARGET" "PRAGMA integrity_check;" | grep -q '^ok$'; then
	echo "backup failed integrity check: $TARGET" >&2
	exit 1
fi

ls -1t "$DEST"/doorslip-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$DEST"/welcome-*.json 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$TARGET ($(du -h "$TARGET" | cut -f1)), keeping $KEEP"
