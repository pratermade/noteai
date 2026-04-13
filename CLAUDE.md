# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Self-hosted note-keeping web app with automatic RAG pipeline. Notes are chunked, embedded, and stored in ChromaDB for semantic search. See `claude_code_prompt_rag_notes(1).md` for the full spec.

## Commands

```bash
# Start ChromaDB HTTP service (required before running the app)
chroma run --path ./chroma_data

# Start the app
uvicorn backend.main:app --reload

# Start with HTTPS (required for Android PWA)
uvicorn backend.main:app --ssl-keyfile key.pem --ssl-certfile cert.pem --port 8443
```

## Stack

- **Backend**: FastAPI + aiosqlite (notes) + ChromaDB HTTP client (vectors)
- **Embeddings**: OpenAI-compatible `/v1/embeddings` endpoint called directly via `httpx` (no `openai` SDK)
- **Frontend**: Plain HTML + vanilla JS + CSS, served as static mount from FastAPI (no build step)
- **Config**: `.env` via `pydantic-settings`

## Directory Layout

```
backend/
  main.py          # FastAPI app, lifespan, route registration
  config.py        # Settings from .env
  database.py      # SQLite CRUD (aiosqlite)
  embeddings.py    # httpx async embedding client with tenacity retry
  vector_store.py  # ChromaDB AsyncHttpClient wrapper
  chunker.py       # tiktoken-based paragraph/sentence chunker
  pdf_extractor.py # PyMuPDF text extraction (run in executor)
  web_extractor.py # trafilatura URL extraction (run in executor)
  models.py        # Pydantic models
attachments/       # PDF files on disk — git-ignored
frontend/
  index.html / app.js / style.css
  share.html / share.js   # Post-share redirect handler
  manifest.json           # Written at startup (not static) — embeds APP_BASE_URL
  service_worker.js       # Minimal pass-through SW for PWA installability
  icons/                  # icon-192.png, icon-512.png
```

## Architecture Notes

### Async rules
- All code uses `async def` throughout — no threading or concurrent.futures
- **PyMuPDF** (`import fitz`) is synchronous — always call via `loop.run_in_executor(None, ...)`
- **trafilatura** is synchronous — same rule, always via `run_in_executor`

### Indexing pipeline
Triggered as a FastAPI `BackgroundTask` on note create/update:
1. Chunk via `chunker.py` (tiktoken cl100k_base, paragraph → sentence splits, overlap applied)
2. Embed in batches via `embeddings.py` (POST to `{EMBEDDING_BASE_URL}/v1/embeddings`)
3. Upsert to Chroma with `{note_id}_{chunk_index}` IDs (idempotent)
4. Update `indexed_at` in SQLite

On note update: delete old Chroma vectors by `note_id`, then re-index.
On note delete: delete all Chroma vectors by `note_id`.

### PDF pipeline
Background task after upload. Chroma IDs use `{attachment_id}_p{page}_c{chunk_index}`.
Stored on disk as `{ATTACHMENT_DIR}/{note_id}/{attachment_id}.pdf` — never use original filename on disk.

### Web attachment pipeline
After URL share: `trafilatura.fetch_url` + `trafilatura.extract` in executor, store text inline in `attachments.extracted_text`, index with `source_type="attachment"`.

### manifest.json
Written to disk at startup in the `lifespan` block (not a static file) so `APP_BASE_URL` is embedded correctly. Use synchronous write before server starts accepting requests.

### Android share target
- Share POST → `/api/share` → creates note/attachment as background task → stores token in `_pending_share` dict → 302 redirect to `/share-handler?token=...`
- Must be 302 (not 307) — Android PWA runtime requires standard redirect
- `_pending_share` tokens are in-memory, 5-minute TTL, purged lazily on access
- HTTPS is required for Android PWA install and share target registration

### ChromaDB IDs
- Note chunks: `{note_id}_{chunk_index}`
- PDF chunks: `{attachment_id}_p{page}_c{chunk_index}`
- Both are idempotent on re-index

## Key Constraints

- No LangChain, LlamaIndex, or any LLM framework — pipeline implemented directly
- No `openai` Python SDK — use raw `httpx` for embedding endpoint calls
- No authentication — single-user local app
- No Jinja2 templating — frontend is pure static files
- `pymupdf` is imported as `fitz` (`import fitz`)
- Use `uuid.uuid4()` for all IDs
- Target Python 3.11+; use `asyncio.TaskGroup` and other 3.11 stdlib features where appropriate
- If embedding service or ChromaDB is unreachable: save note to SQLite anyway, set `indexed_at = null`, log warning — never fail the create/update request

## Configuration (.env)

```
DATABASE_URL=./notes.db
CHROMA_HOST=localhost
CHROMA_PORT=8000
CHROMA_COLLECTION=notes
EMBEDDING_BASE_URL=http://localhost:8080
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_BATCH_SIZE=32
CHUNK_SIZE=512
CHUNK_OVERLAP=64
ATTACHMENT_DIR=./attachments
APP_BASE_URL=https://localhost:8443
SUMMARY_BASE_URL=http://localhost:11434  # OpenAI-compatible chat completions URL; omit to use truncation fallback
SUMMARY_MODEL=gpt-4o-mini
SUMMARY_API_KEY=                         # optional Bearer token for hosted APIs
```
