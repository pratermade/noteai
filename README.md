# NoterAI

A self-hosted note-keeping PWA with automatic RAG pipeline. Notes, PDFs, and web URLs are chunked, embedded, and stored in a local ChromaDB vector database for semantic search. LLM-generated summaries are shown inline for every note and attachment.

## Features

- **Four note types**: `markdown` (plain notes), `attachment` (PDF), `url` (web page), `video` (YouTube — embeds player and indexes transcript)
- **Nine folders** — fixed categories (Unfiled, Reference, Ideas, Todo, Review Later, Project, Journal, Resources, Archive) to keep notes organized; Archive is excluded from search and RAG by default
- **Semantic search** across notes and attachment content
- **LLM summaries** — auto-generated 50-word summary for every note and attachment
- **RAG chat API** — OpenAI-compatible `/v1/chat/completions` endpoint (port 8084) that answers questions grounded in your notes, with intent-based folder routing (e.g. "what should I work on?" searches only Todo notes) and daily reminder injection
- **Date reminders** — set a due date on any note; the RAG chat surfaces overdue and due-today reminders on every message with direct links; mark done or snooze to a new date from the note editor
- **Next Tasks panel** — sidebar shortcut listing the next 10 notes with due dates ordered soonest first; inline checkboxes mark tasks complete without leaving the view
- **Android PWA** — installable, with share-target support (share URLs, text, and PDFs directly from Chrome)
- **Markdown preview** with edit/preview toggle

## Prerequisites

- Python 3.11+
- A running **ChromaDB** HTTP service
- A local **embedding model server** exposing an OpenAI-compatible `/v1/embeddings` endpoint (e.g. [Ollama](https://ollama.com), [llama.cpp](https://github.com/ggerganov/llama.cpp), [text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference))
- _(Optional)_ An OpenAI-compatible `/v1/chat/completions` endpoint for LLM summaries (e.g. Ollama). Without it, summaries fall back to text truncation.

## Docker (recommended)

```bash
cp .env.example .env   # configure settings
docker compose up -d
```

The app listens on `https://0.0.0.0:8443` by default (HTTPS required for Android PWA).

## Manual install

```bash
git clone <repo>
cd noterai
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start ChromaDB
chroma run --path ./chroma_data

# Start the app (plain HTTP)
uvicorn backend.main:app --reload

# Start with HTTPS (required for Android PWA)
uvicorn backend.main:app --ssl-keyfile key.pem --ssl-certfile cert.pem --host 0.0.0.0 --port 8443
```

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `./notes.db` | SQLite file path |
| `CHROMA_HOST` | `localhost` | ChromaDB host |
| `CHROMA_PORT` | `8000` | ChromaDB port |
| `EMBEDDING_BASE_URL` | `http://localhost:8080` | Embedding server URL |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Model name |
| `EMBEDDING_BATCH_SIZE` | `32` | Embedding batch size |
| `CHUNK_SIZE` | `350` | Tokens per chunk (tiktoken cl100k_base) |
| `CHUNK_OVERLAP` | `64` | Overlap between chunks |
| `CHUNK_MAX_CHARS` | `500` | Hard character ceiling per chunk — guards against tokenizer mismatch between cl100k_base and the embedding model; lower if your embedding server has a small context window |
| `INDEX_BATCH_SIZE` | `200` | Chunks embedded and upserted per round-trip — limits peak memory for large PDFs |
| `ATTACHMENT_DIR` | `./attachments` | PDF storage directory |
| `APP_BASE_URL` | `https://localhost:8443` | Public HTTPS URL (used in Android manifest) |
| `SUMMARY_BASE_URL` | _(unset)_ | OpenAI-compatible chat completions base URL for LLM summaries |
| `SUMMARY_MODEL` | `gpt-4o-mini` | Model name for summaries |
| `SUMMARY_API_KEY` | _(unset)_ | Bearer token for hosted summary APIs |
| `CHAT_LLM_BASE_URL` | _(falls back to `SUMMARY_BASE_URL`)_ | LLM base URL for the RAG chat API |
| `CHAT_LLM_MODEL` | _(falls back to `SUMMARY_MODEL`)_ | Model name for the RAG chat API |
| `CHAT_N_RESULTS` | `8` | Note chunks injected as RAG context per chat request |
| `CHAT_PORT` | `8084` | Port for the RAG chat API service |

## How the RAG pipeline works

When a note is saved, the backend splits the content into overlapping token chunks (tiktoken `cl100k_base`), sends batches to the embedding server, and upserts vectors into ChromaDB with metadata (note ID, tags, folder).

**PDF attachments** are extracted with PyMuPDF, chunked, and indexed with IDs in the form `{attachment_id}_c{chunk}`. Large PDFs are processed in batches of `INDEX_BATCH_SIZE` chunks so partial progress is saved to ChromaDB even if a batch fails.

**URL attachments** (from the Android share target) are fetched with a browser User-Agent and extracted with trafilatura, then indexed as attachment chunks attributed back to the parent note. **Reddit URLs** (including mobile `/s/` share links) use Reddit's public JSON API instead of HTML scraping for reliable extraction.

**Video notes** fetch the YouTube transcript via the `youtube-transcript-api` library, index it as chunks, and embed an inline YouTube player above the note content.

Semantic search embeds the query with the same model, retrieves the closest chunks from Chroma, and hydrates the result cards from SQLite.

## RAG Chat API

NoterAI exposes an OpenAI-compatible chat completions endpoint on port 8084 (`CHAT_PORT`). Point any OpenAI-compatible client at it using model name `noterai-rag`:

```bash
# Start the chat API alongside the main app
uvicorn backend.chat_api:app --port 8084

# Example request
curl http://localhost:8084/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"noterai-rag","messages":[{"role":"user","content":"What did I write about Rust?"}]}'
```

Each request:
1. Runs a fast LLM call to classify which folder(s) best match the query intent, then scopes the vector search accordingly (falls back to all non-Archive folders for general questions)
2. Embeds the last user message and retrieves the closest matching note chunks from ChromaDB
3. Injects the chunks as numbered context; the LLM cites `[1]`, `[2]` etc. inline and only cited sources appear in the footer
4. Prepends any overdue or due-today reminders to the system prompt on every request

Streaming (`"stream": true`) is supported. The chat API shares the same embedding and vector store configuration as the main app.

## HTTPS for Android PWA

Android only registers share targets and allows "Add to Home Screen" over HTTPS.

### Option 1 — Tailscale (recommended)

```bash
tailscale cert <hostname>
# Configure nginx or uvicorn with the issued cert
# Set APP_BASE_URL=https://yourhost.tail12345.ts.net in .env
```

### Option 2 — mkcert (LAN only)

```bash
mkcert -install
mkcert <your-lan-ip>
uvicorn backend.main:app \
  --ssl-keyfile <your-lan-ip>-key.pem \
  --ssl-certfile <your-lan-ip>.pem \
  --host 0.0.0.0 --port 8443
```

Install the mkcert root CA on your Android device to trust the certificate.

## Install on Android

1. Open the app URL in Chrome on your Android device.
2. Tap the three-dot menu → **Add to Home Screen**.
3. Once installed, NoterAI appears in the Android share sheet — you can share URLs, text, and PDFs directly from any app.

## API examples

```bash
BASE=http://localhost:8889

# Create a note
curl -X POST $BASE/api/notes \
  -H 'Content-Type: application/json' \
  -d '{"title":"My note","content":"Some content","tags":["work"],"folder":"Reference"}'

# Semantic search
curl -X POST $BASE/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"content about projects","n_results":5}'

# Simulate Android URL share
curl -X POST $BASE/api/share \
  -F 'title=Interesting article' \
  -F 'url=https://example.com/article'

# Upload a PDF attachment
curl -X POST $BASE/api/notes/<note-id>/attachments \
  -F 'file=@/path/to/document.pdf'

# Re-index a single note
curl -X POST $BASE/api/notes/<note-id>/reindex

# Re-index all notes
curl -X POST $BASE/api/reindex

# Health check
curl $BASE/health
```

## Notes

- `attachments/` and `chroma_data/` are git-ignored — back them up separately.
- After changing `CHUNK_SIZE`, `CHUNK_OVERLAP`, or `CHUNK_MAX_CHARS`, use "Reindex all notes" in the sidebar or `POST /api/reindex` to rebuild the vector index.
- If your embedding server has a small physical batch size (e.g. llama.cpp with `--ubatch-size 512`), lower `CHUNK_MAX_CHARS` (e.g. `400`) to avoid token overflow errors.
- OCR for scanned PDFs is not supported.
