#!/usr/bin/env bash
# backup.sh — snapshot the noterai-data volume to timestamped tar.gz files
set -euo pipefail

DATA_VOLUME="noterai-data"
BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")" && pwd)/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CHROMA_OUTFILE="$BACKUP_DIR/chroma-backup-${TIMESTAMP}.tar.gz"
OUTFILE="$BACKUP_DIR/noterai-backup-${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

echo "==> Step 1/2: Backing up Chroma vectors to $CHROMA_OUTFILE ..."
docker run --rm \
    -v "${DATA_VOLUME}:/data:ro" \
    -v "${BACKUP_DIR}:/backup" \
    alpine tar -czf "/backup/chroma-backup-${TIMESTAMP}.tar.gz" -C /data/chroma .

echo "==> Step 2/2: Full volume backup to $OUTFILE ..."
docker run --rm \
    -v "${DATA_VOLUME}:/data:ro" \
    -v "${BACKUP_DIR}:/backup" \
    alpine tar -czf "/backup/noterai-backup-${TIMESTAMP}.tar.gz" -C /data .

echo "==> Done."
echo "    Chroma: $(du -sh "$CHROMA_OUTFILE" | cut -f1)  →  $CHROMA_OUTFILE"
echo "    Full:   $(du -sh "$OUTFILE"        | cut -f1)  →  $OUTFILE"

# Keep only the 10 most recent of each backup type
KEPT=10

prune_backups() {
    local pattern="$1"
    local count
    count=$(ls -1 "$BACKUP_DIR"/${pattern} 2>/dev/null | wc -l)
    if [ "$count" -gt "$KEPT" ]; then
        TO_DELETE=$(ls -1t "$BACKUP_DIR"/${pattern} | tail -n +"$((KEPT + 1))")
        echo "==> Pruning old ${pattern%%-*} backups (keeping $KEPT):"
        echo "$TO_DELETE" | while read -r f; do
            echo "    removing $f"
            rm -f "$f"
        done
    fi
}

prune_backups "chroma-backup-*.tar.gz"
prune_backups "noterai-backup-*.tar.gz"
