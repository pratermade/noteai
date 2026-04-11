#!/bin/sh
set -e

mkdir -p /data/attachments /data/chroma

# Start ChromaDB in the background
chroma run --path /data/chroma --host 127.0.0.1 --port "${CHROMA_PORT:-8006}" &
CHROMA_PID=$!

# Wait for ChromaDB to be ready
echo "Waiting for ChromaDB..."
until curl -sf "http://127.0.0.1:${CHROMA_PORT:-8006}/api/v2/version" > /dev/null 2>&1; do
    sleep 1
done
echo "ChromaDB ready."

# Forward signals to children so the container stops cleanly
trap "kill $CHROMA_PID" TERM INT

exec uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "${APP_PORT:-8889}" \
    --log-level info
