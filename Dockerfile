FROM python:3.12-slim

# System deps for PyMuPDF and trafilatura
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmupdf-dev \
    curl \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install local CA certificates
COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Append local CA certs to certifi's bundle so httpx trusts them (no-op if certs/ is empty)
RUN python3 -c "import certifi; print(certifi.where())" | \
    xargs -I{} sh -c 'ls /usr/local/share/ca-certificates/*.crt 2>/dev/null && cat /usr/local/share/ca-certificates/*.crt >> {} || true'

# Copy application code
COPY backend/ backend/
COPY plugins/ plugins/
COPY frontend/ frontend/

# Runtime data lives in a mounted volume
VOLUME ["/data"]

# ChromaDB runs internally on 8006; chat API default port is 8084
EXPOSE 8889 8084

ENV DATABASE_URL=/data/notes.db \
    CHROMA_HOST=localhost \
    CHROMA_PORT=8006 \
    CHROMA_COLLECTION=notes \
    ATTACHMENT_DIR=/data/attachments \
    EMBEDDING_BASE_URL=http://127.0.0.1:8082 \
    EMBEDDING_MODEL=nomic-embed-text \
    EMBEDDING_BATCH_SIZE=32 \
    CHUNK_SIZE=350 \
    CHUNK_OVERLAP=40 \
    APP_PORT=8889

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
