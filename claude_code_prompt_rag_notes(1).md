# Claude Code prompt — local RAG note-keeping app

## Project overview

Build a self-hosted note-keeping web application with automatic RAG (Retrieval-Augmented Generation)
pipeline integration. When a note is saved, it is automatically chunked, embedded, and stored in a
local ChromaDB vector database so it can be retrieved by a local LLM.

---

## Stack

- **Backend**: Python 3.11+, FastAPI, SQLite (note storage), ChromaDB (vector store, running as a
  separate HTTP service)
- **Embeddings**: Separate local embedding model exposed via an OpenAI-compatible `/v1/embeddings`
  endpoint (configurable base URL)
- **Frontend**: Single-page app using plain HTML + vanilla JS + CSS (no build step, no Node.js
  required). Served as a static mount from FastAPI.
- **Config**: All external service URLs and model names read from a `.env` file via `python-dotenv`

---

## Directory layout to create

```
notes-rag/
├── backend/
│   ├── main.py            # FastAPI app entry point
│   ├── config.py          # Settings loaded from .env
│   ├── database.py        # SQLite note CRUD (use aiosqlite)
│   ├── embeddings.py      # Embedding client (httpx async, OpenAI-compat)
│   ├── vector_store.py    # ChromaDB client wrapper
│   ├── chunker.py         # Text chunking logic
│   ├── pdf_extractor.py   # PyMuPDF text extraction
│   ├── web_extractor.py   # trafilatura webpage text extraction
│   └── models.py          # Pydantic models
├── attachments/           # PDF files stored here (git-ignored)
├── frontend/
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── share.html         # Share target landing page (PWA)
│   ├── share.js           # Share target handler logic
│   ├── manifest.json      # PWA manifest (declares share_target)
│   └── service_worker.js  # Minimal SW required for PWA installability
├── .env.example
├── requirements.txt
└── README.md
```

---

## Configuration (.env.example)

```
# SQLite database file path
DATABASE_URL=./notes.db

# ChromaDB HTTP service
CHROMA_HOST=localhost
CHROMA_PORT=8000
CHROMA_COLLECTION=notes

# Embedding model (OpenAI-compat endpoint)
EMBEDDING_BASE_URL=http://localhost:8080
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_BATCH_SIZE=32

# Chunking
CHUNK_SIZE=512
CHUNK_OVERLAP=64

# Attachments
ATTACHMENT_DIR=./attachments

# PWA / Android share target
# Public-facing base URL of this server (must be HTTPS for PWA install on Android)
# Example with Tailscale: https://myhost.tail12345.ts.net:8443
APP_BASE_URL=https://localhost:8443
```

---

## Data models

### SQLite — `notes` table

| column       | type    | notes                        |
|--------------|---------|------------------------------|
| id           | TEXT    | UUID, primary key            |
| title        | TEXT    | note title                   |
| content      | TEXT    | full markdown content        |
| tags         | TEXT    | JSON array of strings        |
| folder       | TEXT    | folder path string           |
| created_at   | TEXT    | ISO8601                      |
| updated_at   | TEXT    | ISO8601                      |
| indexed_at   | TEXT    | ISO8601, null if not indexed |

### SQLite — `attachments` table

| column        | type    | notes                                          |
|---------------|---------|------------------------------------------------|
| id            | TEXT    | UUID, primary key                              |
| note_id       | TEXT    | FK → notes.id (cascade delete)                |
| filename      | TEXT    | original uploaded filename or page title       |
| source_url    | TEXT    | original URL for web attachments, null for PDF |
| stored_path   | TEXT    | path on disk relative to `ATTACHMENT_DIR`; null for web attachments (text stored inline) |
| mime_type     | TEXT    | `application/pdf` or `text/html`               |
| size_bytes    | INTEGER |                                                |
| page_count    | INTEGER | PDF only; null for web attachments             |
| extracted_text| TEXT    | extracted content stored inline for web attachments; null for PDF (file on disk) |
| extracted_at  | TEXT    | ISO8601, null until text extraction succeeds   |
| indexed_at    | TEXT    | ISO8601, null until vectors are written        |
| extraction_error | TEXT | error message if extraction failed, else null  |
| created_at    | TEXT    | ISO8601                                        |

### Pydantic models

- `NoteCreate`: title, content, tags (list[str]), folder (str, default "")
- `NoteUpdate`: same fields, all optional
- `NoteResponse`: all columns + `id`, `created_at`, `updated_at`, `indexed_at`
- `AttachmentResponse`: all `attachments` columns (omit `extracted_text` from list responses
  to keep payload small — only include it in a future detail endpoint if needed)
- `ShareRequest`: title (str), text (str, optional), url (str, optional) — for the JSON
  leg of the share target endpoint
- `SearchRequest`: query (str), n_results (int, default 5), tags (list[str] optional),
  folder (str optional)
- `SearchResult`: note_id, title, folder, tags, score (float), chunk_text (str),
  source_type (Literal["note", "attachment"]), source_label (str — note title, PDF filename,
  or webpage title), source_url (str, optional — populated for web attachments)

---

## Backend — FastAPI routes

### Notes CRUD

| method | path               | description                                 |
|--------|--------------------|---------------------------------------------|
| GET    | /api/notes         | list all notes (filter by tag, folder)      |
| GET    | /api/notes/{id}    | get single note                             |
| POST   | /api/notes         | create note, trigger indexing pipeline      |
| PUT    | /api/notes/{id}    | update note, re-index automatically         |
| DELETE | /api/notes/{id}    | delete note, remove vectors from Chroma     |

### Tags & folders

| method | path               | description                                 |
|--------|--------------------|---------------------------------------------|
| GET    | /api/tags          | list all distinct tags                      |
| GET    | /api/folders       | list all distinct folders                   |

### RAG / search

| method | path               | description                                 |
|--------|--------------------|---------------------------------------------|
| POST   | /api/search        | semantic search, returns ranked chunks      |

### Android share target

| method | path               | description                                          |
|--------|--------------------|------------------------------------------------------|
| POST   | /api/share         | receives share from Android PWA share sheet          |
| GET    | /api/share/pending | frontend polls this after share to get new note id   |

### PDF attachments

| method | path                              | description                                      |
|--------|-----------------------------------|--------------------------------------------------|
| POST   | /api/notes/{id}/attachments       | upload a PDF, trigger extraction + indexing      |
| GET    | /api/notes/{id}/attachments       | list attachments for a note                      |
| GET    | /api/attachments/{att_id}/download| download the original PDF file                   |
| DELETE | /api/attachments/{att_id}         | delete attachment, file on disk, and its vectors |

### Reindex

| method | path                      | description                                      |
|--------|---------------------------|--------------------------------------------------|
| POST   | /api/notes/{id}/reindex   | re-index a single note on demand                 |
| POST   | /api/reindex              | bulk re-index all notes (runs in background)     |
| GET    | /api/reindex/status       | poll progress of an in-flight bulk reindex job   |

### Health

| method | path               | description                                 |
|--------|--------------------|---------------------------------------------|
| GET    | /health            | returns status of db, chroma, embedding svc |

---

---

## PWA setup (Android share sheet integration)

### HTTPS requirement

Android will not offer "Add to Home Screen" (and therefore will not register the app as a
share target) unless the app is served over HTTPS. For local self-hosting, recommended options:

- **Tailscale** (easiest): enable HTTPS on your tailnet (`tailscale cert`), run uvicorn behind
  nginx with the issued cert. The app is then reachable at `https://yourhost.tailXXXX.ts.net`.
- **mkcert** (LAN only): generate a locally-trusted cert for your machine's LAN IP, serve
  uvicorn with `--ssl-keyfile` / `--ssl-certfile`. Works on Android if you install the mkcert
  root CA on the device.

`APP_BASE_URL` in `.env` must be set to the actual HTTPS origin — it is embedded into
`manifest.json` at startup so share_target URLs resolve correctly.

### `frontend/manifest.json`

Generate this file dynamically at startup (write it to the `frontend/` directory from Python
using the `APP_BASE_URL` value) so the share target action URL is always correct:

```json
{
  "name": "Notes RAG",
  "short_name": "Notes",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "#ffffff",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ],
  "share_target": {
    "action": "/share-handler",
    "method": "POST",
    "enctype": "multipart/form-data",
    "params": {
      "title": "title",
      "text":  "text",
      "url":   "url",
      "files": [
        { "name": "file", "accept": ["application/pdf"] }
      ]
    }
  }
}
```

Generate two simple placeholder PNG icons (solid color, no text) and save them to
`frontend/icons/icon-192.png` and `frontend/icons/icon-512.png`. Android requires at
least one maskable or any-purpose icon to offer install.

### `frontend/service_worker.js`

Minimal — only needs to exist and register. No caching strategy required:

```javascript
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
self.addEventListener('fetch', event => {
  event.respondWith(fetch(event.request));
});
```

Register it in `index.html` with:
```html
<script>
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/service_worker.js');
  }
</script>
```

### Share handler route — `GET /share-handler`

The manifest `action` uses `/share-handler`. Android POSTs to this URL, but the PWA
intercepts it before the network request is made (the service worker handles it). However,
since we are using a pass-through service worker (no offline caching), Android will POST
directly to the server.

Return `frontend/share.html` (a static HTML page) as the response to this GET. FastAPI
should serve it from the static mount. The actual POST is handled by `/api/share`.

Add a route: `GET /share-handler` → serve `frontend/share.html`.

---

## Android share target endpoint — `POST /api/share`

Android POSTs `multipart/form-data` to `/share-handler` as defined in the manifest.
However, to keep concerns separate, the manifest `action` should point to `/api/share`
directly (update the manifest template above accordingly).

### What Android sends

| scenario              | `title` field      | `text` field          | `url` field     | `file` field |
|-----------------------|--------------------|-----------------------|-----------------|--------------|
| Share text from app   | app name or empty  | selected text         | —               | —            |
| Share a URL (Chrome)  | page title         | page title or snippet | the URL         | —            |
| Share a PDF file      | filename           | —                     | —               | PDF bytes    |

### Endpoint behaviour

Parse the multipart form. Then branch:

**Case 1 — PDF file present** (`file` field is non-empty and mime type is `application/pdf`):
1. Create a new note: title = `file.filename` (stem, cleaned), content = "", tags = [],
   folder = "shared".
2. Save the PDF to disk using the same logic as `POST /api/notes/{id}/attachments`.
3. Fire background task: PDF extraction + indexing pipeline.
4. Store the new `note_id` in a module-level `_pending_share` dict (keyed by a short
   `share_token` UUID) with a TTL of 5 minutes.
5. Redirect (302) to `/share-handler?token={share_token}`.

**Case 2 — URL present** (`url` field is non-empty):
1. Create a new note: title = `title` field (or the URL hostname if title is empty),
   content = `url` (the raw URL as the note body), tags = [], folder = "shared".
2. Fire background task: fetch the URL and extract its text using `web_extractor.py`, then
   create a web attachment record and index it (see web extraction pipeline below).
3. Same token/redirect flow as Case 1.

**Case 3 — text only** (no file, no URL):
1. Create a new note: title = `title` field (truncated to 80 chars) or "Shared note",
   content = `text` field, tags = [], folder = "shared".
2. Trigger standard note indexing pipeline.
3. Same token/redirect flow.

**Response**: always a 302 redirect to `/share-handler?token={share_token}`. This is
important — Android expects a navigation response from the share target action, not JSON.

---

## Share pending endpoint — `GET /api/share/pending?token={token}`

- Look up `token` in `_pending_share`.
- If found and not expired: return `{"note_id": "...", "status": "ready"}`.
- If not found or expired: return 404.
- Remove the token from the dict after it is successfully retrieved (one-time use).

---

## `frontend/share.html` and `frontend/share.js`

`share.html` is a minimal standalone page (not the main SPA) that handles the post-share
redirect. It should:

1. On load, read `?token=` from the query string.
2. Poll `GET /api/share/pending?token={token}` every 1 second (max 30 attempts).
3. While polling, show a simple "Saving..." spinner with the note title if available.
4. On success (`note_id` returned): redirect to `/?note={note_id}` so the main app
   opens directly to the newly created note.
5. On timeout or error: show a message "Something went wrong — check the main app" with
   a link back to `/`.

The main `app.js` should handle `?note={id}` in the URL on load: if present, open that
note in the editor immediately (as if the user clicked it in the list).

---

## Web extraction pipeline (`web_extractor.py`)

Use `trafilatura` for content extraction. Run in a thread pool executor (synchronous library).

```python
import trafilatura

async def extract_url(url: str) -> WebExtractionResult:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_sync, url)

def _extract_sync(url: str) -> WebExtractionResult:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ExtractionError(f"Could not fetch URL: {url}")
    text = trafilatura.extract(downloaded, include_comments=False,
                               include_tables=True, no_fallback=False)
    if not text or len(text.strip()) < 100:
        raise ExtractionError("Page yielded insufficient text (may be JS-rendered or paywalled)")
    metadata = trafilatura.extract_metadata(downloaded)
    return WebExtractionResult(
        text=text,
        title=metadata.title if metadata else None,
        char_count=len(text),
    )
```

`WebExtractionResult`: `text: str`, `title: str | None`, `char_count: int`

### Web attachment indexing pipeline (background task)

After the note is created in the URL share case:

1. Call `web_extractor.extract_url(url)` in executor.
2. Insert a row into `attachments`:
   - `filename` = page title (from metadata) or URL hostname
   - `source_url` = original URL
   - `stored_path` = null (text is stored inline)
   - `mime_type` = `text/html`
   - `extracted_text` = extracted text
   - `extracted_at` = now
3. Pass `extracted_text` through `chunker.py`.
4. Embed and upsert to Chroma with metadata `source_type = "attachment"`,
   `source_label = filename`, `source_url = url`.
5. Update `attachments.indexed_at`.

On extraction failure: set `extraction_error` on the attachment row; do not fail the note.

---

## Updated `vector_store.py` for web attachments

No changes needed to the interface — web attachment chunks use the same
`{attachment_id}_c{chunk_index}` ID pattern and `delete_by_attachment_id` works identically.

---

## PDF attachment endpoints

### Upload — `POST /api/notes/{id}/attachments`

- Accepts `multipart/form-data` with a single `file` field.
- Validate: MIME type must be `application/pdf`; reject anything else with 415.
- Max file size: 50 MB (enforce with a `ContentLengthLimit` dependency or by reading in chunks).
- On receipt:
  1. Generate a UUID for the attachment.
  2. Store the file to `{ATTACHMENT_DIR}/{note_id}/{attachment_id}.pdf` (create dirs as needed).
  3. Insert a row into `attachments` with `extracted_at = null`, `indexed_at = null`.
  4. Return `AttachmentResponse` immediately (202 Accepted).
  5. Fire a background task: extract text → chunk → embed → upsert vectors → update timestamps.
- The stored filename on disk is always the UUID-based path; `filename` column preserves the
  original name for display purposes.

### List — `GET /api/notes/{id}/attachments`

- Returns list of `AttachmentResponse` for the note, ordered by `created_at` descending.
- Include `extracted_at` and `indexed_at` so the frontend can show pipeline status.

### Download — `GET /api/attachments/{att_id}/download`

- Stream the file from disk using FastAPI's `FileResponse`.
- Set `Content-Disposition: attachment; filename="{original_filename}"` so the browser
  prompts a save dialog with the right name.
- Return 404 if attachment row or file on disk is missing.

### Delete — `DELETE /api/attachments/{att_id}`

- Delete the file from disk.
- Delete all Chroma vectors where metadata `attachment_id == att_id`.
- Delete the SQLite row.
- Return 204.
- If the file is missing from disk, still complete the DB/vector cleanup — don't 404.

---

## PDF extraction pipeline (`pdf_extractor.py`)

Use `pymupdf` (imported as `fitz`). Run extraction in a thread pool executor
(`asyncio.get_event_loop().run_in_executor(None, ...)`) since PyMuPDF is synchronous.

### Extraction steps

1. Open the PDF with `fitz.open(path)`.
2. For each page, call `page.get_text("blocks")` to get text blocks with bounding boxes.
3. Sort blocks by vertical position (top-to-bottom), then horizontal (left-to-right).
4. Join block text within a page with `\n`; join pages with `\n\n--- Page {n} ---\n\n` as a
   separator so page boundaries are visible in chunks.
5. Strip null bytes and excessive whitespace.
6. Return a `PDFExtractionResult`:
   - `text: str` — full extracted text
   - `page_count: int`
   - `char_count: int`
   - `extraction_warnings: list[str]` — e.g. "Page 3 yielded no text (may be scanned)"

### Scanned/empty page handling

- If a page yields fewer than 20 characters, add a warning to `extraction_warnings` but
  continue. Do not attempt OCR — that's a future enhancement.
- If the entire document yields fewer than 100 characters total, raise `ExtractionError`
  with message "PDF appears to be scanned or image-only; OCR is not yet supported".
- On `ExtractionError`, set `extracted_at = null` and store the error message in a new
  `extraction_error` TEXT column on the `attachments` table so the UI can display it.

---

## PDF indexing pipeline (background task after upload)

Chroma document IDs for PDF chunks follow the pattern `{attachment_id}_p{page}_c{chunk_index}`
so they are fully independent from note content chunks and idempotent on re-index.

Chunk metadata must include:
- `note_id` — so note-scoped search filters still work
- `attachment_id`
- `source_type: "attachment"`
- `source_label` — the original filename
- `chunk_index`
- `page_hint` — the page number the chunk came from (best-effort, based on `--- Page N ---`
  separators in the extracted text)

Steps:
1. Run `pdf_extractor.extract(stored_path)` in executor.
2. Update `attachments.page_count` and `attachments.extracted_at` in SQLite.
3. Pass extracted text through the existing `chunker.py` (same `CHUNK_SIZE` / `CHUNK_OVERLAP`).
4. Embed and upsert to Chroma.
5. Update `attachments.indexed_at`.

On re-index (triggered by `POST /api/notes/{id}/reindex` or bulk reindex): repeat from step 1.
Old vectors are deleted by `attachment_id` before re-chunking.

---

## Updated `vector_store.py`

Add a `delete_by_attachment_id(attachment_id: str)` method alongside the existing
`delete_by_note_id`. Implement identically — filter by metadata then delete.

---

## Updated semantic search

`SearchResult` gains two new fields:
- `source_type: Literal["note", "attachment"]` — read from Chroma chunk metadata
- `source_label: str` — note title (for `source_type == "note"`) or original PDF filename
  (for `source_type == "attachment"`)

When `source_type == "attachment"`, also populate `attachment_id` in the result so the
frontend can render a download link directly from the search result card.

---

## Reindex scope for attachments

`POST /api/reindex` bulk job must also re-index attachments. For each note in scope:
1. Re-index the note's own content as before.
2. For each attachment belonging to that note where `extracted_at` is non-null, re-run
   the PDF indexing pipeline.
3. Attachments where `extracted_at` is null (failed extraction) are skipped with a warning.

Update the job progress object to include `attachments_completed` and `attachments_failed`
counts alongside the existing note counts.

---

## Reindex endpoints

### Single note — `POST /api/notes/{id}/reindex`

- Returns 404 if note does not exist.
- Clears existing Chroma vectors for the note, then re-runs the full indexing pipeline
  synchronously (not as a background task — the caller is explicitly asking to wait).
- Resets `indexed_at = null` at the start of the request, sets it on completion.
- Returns `NoteResponse` with the updated `indexed_at`.
- Useful after changing `CHUNK_SIZE` / `CHUNK_OVERLAP` in `.env` and restarting the server.

### Bulk reindex — `POST /api/reindex`

- Accepts an optional JSON body: `{"folder": "...", "tag": "..."}` to scope the job to a
  subset of notes. If body is empty or omitted, all notes are reindexed.
- Immediately returns a job object:
  ```json
  {
    "job_id": "<uuid>",
    "status": "running",
    "total": 42,
    "completed": 0,
    "failed": 0,
    "started_at": "<iso8601>"
  }
  ```
- The actual work runs as an `asyncio` background task. Process notes sequentially (not
  concurrently) to avoid overwhelming the embedding server.
- For each note: clear old vectors, re-chunk, re-embed, upsert, update `indexed_at`.
  If a single note fails, log the error, increment `failed`, and continue — do not abort
  the whole job.
- Store job state in a module-level dict keyed by `job_id` (in-memory is fine; this is a
  single-process local app).

### Bulk reindex status — `GET /api/reindex/status`

- Returns the most recent job object (or 404 if no job has ever been run).
- Optionally accept `?job_id=<uuid>` to retrieve a specific job.
- `status` values: `"running"`, `"completed"`, `"completed_with_errors"`, `"failed"`
- Final job object includes `finished_at` and a `errors` list of
  `{"note_id": "...", "title": "...", "error": "..."}` for any failed notes.

### Frontend additions for reindex

- In the note editor toolbar, add a small "reindex" icon button next to the "saved/indexed"
  badge. Clicking it calls `POST /api/notes/{id}/reindex`, shows a spinner, then refreshes
  the badge.
- In the sidebar footer (or a settings panel), add a "reindex all" button that calls
  `POST /api/reindex`, then polls `GET /api/reindex/status` every 3 seconds and shows a
  progress bar: `{completed} / {total} notes indexed`. Disappears when status is
  `completed` or `completed_with_errors`. If `completed_with_errors`, show a warning toast
  listing the failed note titles.

---

## Indexing pipeline (triggered on create/update)

Implement this as an async background task (FastAPI `BackgroundTasks`) so the HTTP response
returns immediately while indexing happens behind the scenes.

Steps:
1. Retrieve full note content from SQLite
2. Split into chunks using `chunker.py`:
   - Chunk by paragraph first, then by token count (target `CHUNK_SIZE` tokens, `CHUNK_OVERLAP`
     overlap)
   - Each chunk carries metadata: `note_id`, `chunk_index`, `title`, `tags`, `folder`
3. Send chunks to embedding model in batches (`EMBEDDING_BATCH_SIZE`)
4. Upsert vectors to ChromaDB collection using `note_id_chunk_index` as the document ID
   (so re-indexing is idempotent)
5. On success, update `indexed_at` timestamp in SQLite

On note delete: delete all Chroma documents where metadata `note_id == id`.

On note update: delete old vectors first (by `note_id`), then re-index fresh.

---

## Chunker (`chunker.py`)

- Use `tiktoken` (cl100k_base encoding) to count tokens
- Split on double newlines first (paragraphs), then merge short paragraphs, then split long
  paragraphs at sentence boundaries if they exceed `CHUNK_SIZE`
- Apply overlap by prepending the last `CHUNK_OVERLAP` tokens of the previous chunk
- Return `list[dict]` with keys: `text`, `chunk_index`

---

## Embedding client (`embeddings.py`)

- Async `httpx.AsyncClient`
- POST to `{EMBEDDING_BASE_URL}/v1/embeddings` with `{"model": EMBEDDING_MODEL, "input": [...]}`
- Parse `data[i].embedding` from response
- Raise a clear exception with status code if the request fails
- Implement simple retry (3 attempts, exponential backoff) using `tenacity`

---

## ChromaDB client (`vector_store.py`)

- Use `chromadb.AsyncHttpClient` pointed at `CHROMA_HOST:CHROMA_PORT`
- Get-or-create collection named `CHROMA_COLLECTION` with cosine distance
- Methods needed:
  - `upsert(documents: list[str], embeddings: list[list[float]], metadatas: list[dict], ids: list[str])`
  - `delete_by_note_id(note_id: str)` — query by metadata filter then delete
  - `query(embedding: list[float], n_results: int, where: dict | None) -> list[SearchResult]`

---

## Semantic search endpoint

`POST /api/search` with `SearchRequest` body:

1. Embed the query string using the embedding client
2. Build optional Chroma `where` filter from `tags` and `folder` if provided
3. Query Chroma for top `n_results` chunks
4. For each result, fetch the full note from SQLite to populate title/tags/folder
5. Return list of `SearchResult` sorted by score descending

---

## Frontend

Keep it functional and clean — this is a personal tool, not a product demo.

### Layout

Two-column layout (sidebar + main):
- Left sidebar: folder tree, tag list (clickable to filter), "New note" button
- Main area: note list (when no note open) or note editor

### Note editor

- Title input at the top
- Textarea for markdown content (raw markdown is fine, no preview needed)
- Tag input: comma-separated, rendered as removable chips below the field
- Folder input: text field (e.g. "work/projects")
- "Save" button — calls PUT or POST, shows a small status indicator ("saving..." → "saved" →
  "indexed" as the background task completes)
- Indexing status: poll `GET /api/notes/{id}` every 2 seconds after save until `indexed_at`
  is non-null, then show "indexed" badge

### Search panel

- Search input at the top of the note list panel
- On input (debounced 400ms), call `POST /api/search` and display results as cards showing
  title, folder, tags, relevance score, and the matched chunk snippet
- Results are clickable — clicking opens the full note in the editor

### Attachments panel (in note editor)

Below the note editor textarea, add a collapsible "Attachments" section:

- A drag-and-drop upload zone (also has a "choose file" fallback). Accept only `application/pdf`.
  On drop/select, immediately POST to `/api/notes/{id}/attachments`. Show an upload progress
  indicator (use `XMLHttpRequest` with `upload.onprogress` — `fetch` doesn't expose upload
  progress).
- Each attachment renders as a row showing: PDF icon, original filename, file size, page count
  (once extracted), and a status badge that cycles through:
  `uploading` → `extracting` → `indexing` → `indexed` (or `extraction failed` in red).
- Poll `GET /api/notes/{id}/attachments` every 2 seconds while any attachment has
  `indexed_at == null`, same pattern as note indexing status.
- Each row has a download button (links to `/api/attachments/{att_id}/download`) and a
  delete button (calls `DELETE /api/attachments/{att_id}` with a confirmation prompt,
  then removes the row from the UI).

### Search result cards — attachment results

When a search result has `source_type == "attachment"`, render the card with:
- A PDF icon badge instead of the note icon
- `source_label` (filename) as the subtitle below the note title
- A small "download PDF" link using the `attachment_id`
- The chunk snippet and relevance score as usual

### Filtering

- Clicking a tag in the sidebar calls `GET /api/notes?tag=X`
- Clicking a folder in the sidebar calls `GET /api/notes?folder=X`
- A "clear filter" control resets to all notes

---

## Error handling

- Backend: return structured JSON errors `{"detail": "message", "code": "ERROR_CODE"}` for all
  4xx/5xx responses
- If embedding service is unreachable, save the note to SQLite anyway and set `indexed_at = null`;
  log a warning. Do not fail the create/update request.
- If ChromaDB is unreachable, same behaviour — note is saved, indexing is deferred.
- Frontend: display a non-blocking toast notification for errors; never silently swallow them.

---

## Startup behaviour

On app startup (`lifespan` context manager):
1. Create SQLite tables if they don't exist
2. Ensure ChromaDB collection exists
3. Log connection status for all three services (SQLite, Chroma, embedding endpoint)

---

## README

Write a `README.md` covering:
1. Prerequisites (Python 3.11+, ChromaDB running as HTTP service, a local embedding model server)
2. Install steps (`pip install -r requirements.txt`)
3. `.env` configuration reference
4. How to start ChromaDB as an HTTP service (`chroma run --path ./chroma_data`)
5. How to start the FastAPI app (`uvicorn backend.main:app --reload`)
6. How the RAG pipeline works (one paragraph)
7. HTTPS setup options: Tailscale (recommended) and mkcert (LAN); explain why HTTPS is
   required for the Android PWA share target
8. How to install the PWA on Android: open the app URL in Chrome → three-dot menu →
   "Add to Home Screen" → the app will then appear in the Android share sheet
9. Example curl commands for create, search, and the share endpoint

---

## requirements.txt (include at minimum)

```
fastapi
uvicorn[standard]
aiosqlite
aiofiles
chromadb
httpx
tenacity
tiktoken
python-dotenv
pydantic-settings
pymupdf
trafilatura
```

---

## Constraints and notes for Claude Code

- Do not use LangChain, LlamaIndex, or any other LLM framework — implement the pipeline directly
- Do not use `openai` Python SDK — call the embedding endpoint raw with `httpx` (keeps the
  dependency footprint small and works with any OpenAI-compat server)
- All async — use `async def` throughout; no `threading` or `concurrent.futures`
- No authentication required — this is a single-user local app
- Write the frontend as static files only — no Jinja2 templating, no SSR
- Use `uuid.uuid4()` for note IDs
- Target Python 3.11+; use `tomllib`, `asyncio.TaskGroup`, and other 3.11 stdlib features where
  appropriate
- `pymupdf` is imported as `fitz` — use `import fitz`, not `import pymupdf`
- Run all PyMuPDF calls inside `loop.run_in_executor(None, ...)` — it is synchronous and will
  block the event loop if called directly in an `async def`
- The `attachments/` directory must be created at startup if it doesn't exist; add it to
  `.gitignore` in the README instructions
- Stored PDF paths on disk use `{ATTACHMENT_DIR}/{note_id}/{attachment_id}.pdf` — never use
  the original filename as the on-disk name (avoids path traversal and collision issues)
- `manifest.json` must be written to disk at startup (not hardcoded as a static file) so
  `APP_BASE_URL` is correctly embedded; use `aiofiles` or a synchronous write in the
  `lifespan` startup block before the server begins accepting requests
- The share handler redirect must be a 302 (not 307) — Android's PWA runtime expects a
  standard redirect after a share POST
- `_pending_share` tokens are in-memory only; they expire after 5 minutes and are purged
  lazily on access — no background cleanup task needed
- `trafilatura` is synchronous; always call it via `run_in_executor` — same rule as PyMuPDF
- Do not follow redirects blindly in `trafilatura.fetch_url` to URLs outside the original
  domain — pass `no_ssl_validation=False` (default) to respect cert errors
- Add `aiofiles` to requirements.txt (needed for async manifest write at startup)
