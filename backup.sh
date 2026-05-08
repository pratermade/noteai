#!/usr/bin/env bash
# backup.sh — snapshot the noterai-data volume to a timestamped tar.gz
set -euo pipefail

DATA_VOLUME="noterai-data"
BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")" && pwd)/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTFILE="$BACKUP_DIR/noterai-backup-${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

echo "==> Backing up volume '$DATA_VOLUME' to $OUTFILE ..."
docker run --rm \
    -v "${DATA_VOLUME}:/data:ro" \
    -v "${BACKUP_DIR}:/backup" \
    alpine tar -czf "/backup/noterai-backup-${TIMESTAMP}.tar.gz" -C /data .

echo "==> Done. $(du -sh "$OUTFILE" | cut -f1) written to $OUTFILE"

# Keep only the 10 most recent backups
KEPT=10
COUNT=$(ls -1 "$BACKUP_DIR"/noterai-backup-*.tar.gz 2>/dev/null | wc -l)
if [ "$COUNT" -gt "$KEPT" ]; then
    TO_DELETE=$(ls -1t "$BACKUP_DIR"/noterai-backup-*.tar.gz | tail -n +"$((KEPT + 1))")
    echo "==> Pruning old backups (keeping $KEPT):"
    echo "$TO_DELETE" | while read -r f; do
        echo "    removing $f"
        rm -f "$f"
    done
fi
