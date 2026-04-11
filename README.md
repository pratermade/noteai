# Notes RAG

A self-hosted note-keeping app with automatic RAG pipeline. Every note (and PDF/URL attachment) is chunked, embedded, and stored in a local ChromaDB vector database for semantic search.

## Prerequisites

- Python 3.11+
- A running **ChromaDB** HTTP service
- A local **embedding model server** exposing an OpenAI-compatible `/v1/embeddings` endpoint (e.g. [Ollama](https://ollama.com), [llama.cpp server](https://github.com/ggerganov/llama.cpp), [text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference))

## Install

```bash
git clone <repo>
cd notes-rag
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
# Edit .env to match your setup
```

Key settings:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `./notes.db` | SQLite file path |
| `CHROMA_HOST` | `localhost` | ChromaDB host |
| `CHROMA_PORT` | `8000` | ChromaDB port |
| `EMBEDDING_BASE_URL` | `http://localhost:8080` | Embedding server URL |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Model name sent to embedding server |
| `APP_BASE_URL` | `https://localhost:8443` | Public HTTPS URL (required for Android PWA) |

## Start ChromaDB

```bash
pip install chromadb   # if not already installed globally
chroma run --path ./chroma_data
```

ChromaDB listens on port 8000 by default.

## Start the app

```bash
uvicorn backend.main:app --reload
```

With HTTPS (required for Android PWA):

```bash
uvicorn backend.main:app --ssl-keyfile key.pem --ssl-certfile cert.pem --host 0.0.0.0 --port 8443
```

## How the RAG pipeline works

When a note is saved, the backend splits the content into overlapping token-count chunks (using tiktoken), sends each chunk in batches to the embedding server, and upserts the resulting vectors into ChromaDB with metadata (note ID, tags, folder). Semantic search embeds the query with the same model and retrieves the closest chunks from Chroma, then fetches full note details from SQLite to populate the result cards. PDF and web attachments go through the same pipeline — their extracted text is chunked and indexed separately, with results attributed back to the parent note.

## HTTPS setup for Android PWA

Android only registers an app as a share target and offers "Add to Home Screen" when the app is served over **HTTPS**.

### Option 1 — Tailscale (recommended)

1. Install Tailscale on your server and enable HTTPS: `tailscale cert <hostname>`
2. Configure nginx (or uvicorn directly) with the issued certificate.
3. Set `APP_BASE_URL=https://yourhost.tail12345.ts.net` in `.env`.

### Option 2 — mkcert (LAN only)

```bash
mkcert -install
mkcert <your-lan-ip>
uvicorn backend.main:app --ssl-keyfile <your-lan-ip>-key.pem --ssl-certfile <your-lan-ip>.pem --host 0.0.0.0 --port 8443
```

Install the mkcert root CA on your Android device to trust the certificate.

## Install on Android

1. Open the app URL in Chrome on your Android device.
2. Tap the three-dot menu → **Add to Home Screen**.
3. Once installed, the app appears in the Android share sheet. You can share URLs, text, and PDF files directly into Notes RAG.

## curl examples

**Create a note:**
```bash
curl -X POST http://localhost:8000/api/notes \
  -H 'Content-Type: application/json' \
  -d '{"title":"My note","content":"Some content here","tags":["work"],"folder":"projects"}'
```

**Semantic search:**
```bash
curl -X POST http://localhost:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"content about projects","n_results":5}'
```

**Share endpoint (simulate Android share):**
```bash
curl -X POST http://localhost:8000/api/share \
  -F 'title=Interesting article' \
  -F 'url=https://example.com/article'
```

**Upload a PDF:**
```bash
curl -X POST http://localhost:8000/api/notes/<note-id>/attachments \
  -F 'file=@/path/to/document.pdf'
```

**Health check:**
```bash
curl http://localhost:8000/health
```

## Notes

- The `attachments/` directory and `chroma_data/` are git-ignored — back them up separately if needed.
- Reindex all notes after changing `CHUNK_SIZE` or `CHUNK_OVERLAP` using the "Reindex all notes" button in the sidebar or `POST /api/reindex`.
- OCR for scanned PDFs is not yet supported.
