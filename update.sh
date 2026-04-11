#!/usr/bin/env bash
# update.sh — rebuild and restart the noterai container
set -euo pipefail

CONTAINER_NAME="noterai"
IMAGE_NAME="noterai:dev"
DATA_VOLUME="noterai-data"
APP_PORT="${APP_PORT:-8889}"
APP_BASE_URL="${APP_BASE_URL:-http://localhost:$APP_PORT}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. Migrate data from old containers before rebuilding ───────────────────
# Containers using an old noterai image may have data in their writable layer
# (notes.db, attachments, chroma) if the volume was mounted at the wrong path.
# Copy that data into the named volume now, while we can still exec into them.
docker volume create "$DATA_VOLUME" > /dev/null 2>&1 || true

volume_has_db() {
    docker run --rm -v "${DATA_VOLUME}:/data" alpine \
        sh -c "test -f /data/notes.db && echo yes || echo no" 2>/dev/null
}

if [ "$(volume_has_db)" != "yes" ]; then
    echo "==> Volume has no notes.db; checking existing containers for data to migrate..."
    # Match by image name (works even after rebuild since .Image stores what was passed to `run`)
    OLD_WITH_DATA=$(docker ps -a --format '{{.ID}} {{.Image}}' \
        | awk '/noterai/{print $1}' \
        | while read -r cid; do
            has=$(docker exec "$cid" sh -c "test -f /data/notes.db && echo yes || echo no" 2>/dev/null || echo no)
            [ "$has" = "yes" ] && echo "$cid"
          done | head -1)
    if [ -n "$OLD_WITH_DATA" ]; then
        echo "    Migrating data from container $OLD_WITH_DATA..."
        docker exec "$OLD_WITH_DATA" tar -C /data -czf - . \
            | docker run --rm -i -v "${DATA_VOLUME}:/data" alpine tar -xzf - -C /data
        echo "    Migration done."
    else
        echo "    No existing data found; starting fresh."
    fi
fi

# ── 2. Build ────────────────────────────────────────────────────────────────
echo "==> Building $IMAGE_NAME..."
docker build -t "$IMAGE_NAME" .

# ── 3. Stop and remove all noterai containers ────────────────────────────────
OLD_CONTAINERS=$(docker ps -a --format '{{.ID}} {{.Image}}' | awk '/noterai/{print $1}')
if [ -n "$OLD_CONTAINERS" ]; then
    echo "==> Stopping old container(s)..."
    docker stop $OLD_CONTAINERS 2>/dev/null || true
    docker rm $OLD_CONTAINERS 2>/dev/null || true
fi
# Also remove canonical name if it exists under a different image
if docker inspect "$CONTAINER_NAME" > /dev/null 2>&1; then
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
    docker rm "$CONTAINER_NAME"
fi

# ── 4. Start ────────────────────────────────────────────────────────────────
echo "==> Starting $CONTAINER_NAME on port $APP_PORT..."
docker run -d \
    --name "$CONTAINER_NAME" \
    --network=host \
    --restart=unless-stopped \
    -v "${DATA_VOLUME}:/data" \
    -e APP_PORT="$APP_PORT" \
    -e APP_BASE_URL="$APP_BASE_URL" \
    "$IMAGE_NAME"

echo "==> Waiting for app to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${APP_PORT}/health" > /dev/null 2>&1; then
        echo "==> Ready. Health: $(curl -s http://127.0.0.1:${APP_PORT}/health)"
        echo "==> noterai running at http://localhost:$APP_PORT"
        exit 0
    fi
    sleep 1
done
echo "==> Container started but health check timed out. Check logs:"
echo "    docker logs $CONTAINER_NAME"
