# NoterAI

A self-hosted note-keeping PWA with automatic RAG pipeline. Notes, PDFs, and web URLs are chunked, embedded, and stored in a local ChromaDB vector database for semantic search. LLM-generated summaries are shown inline for every note and attachment.

## Features

- **Five note types**: `markdown` (plain notes), `list` (collaborative checklist), `attachment` (PDF/image), `url` (web page), `video` (YouTube — embeds player and indexes transcript)
- **Nine folders** — fixed categories (Unfiled, Reference, Ideas, Todo, Review Later, Projects, Journal, Lists, Archive) to keep notes organized; Archive is excluded from search and RAG by default
- **Collaborative lists** — `list` notes have per-item checkboxes, real-time polling, and per-note sharing with other users; shared lists are visible to collaborators without full account access
- **Journal auto-logging** — every new note (from web UI, Telegram bot, or Android share) is automatically linked in today's journal entry with a timestamped clickable link; a journal entry is created if none exists for the day
- **Voice dictation** — tap the 🎤 button to record a journal entry; the audio is transcribed by a local Whisper server and automatically rewritten into a structured, clinical journal format via the LLM pipeline
- **Semantic search** across notes and attachment content
- **LLM summaries** — auto-generated 50-word summary for every note and attachment
- **RAG chat API** — OpenAI-compatible `/v1/chat/completions` endpoint (port 8084) that answers questions grounded in your notes, with intent-based folder routing (e.g. "what should I work on?" searches only Todo notes) and daily reminder injection
- **Date reminders** — set a due date on any note; the RAG chat surfaces overdue and due-today reminders on every message with direct links; mark done or snooze to a new date from the note editor
- **Next Tasks panel** — sidebar shortcut listing the next 10 notes with due dates ordered soonest first; inline checkboxes mark tasks complete without leaving the view
- **Android PWA** — installable, with share-target support (share URLs, text, PDFs, and images directly from Chrome); shared images can be attached to an existing note or saved as a new one
- **Markdown preview** with edit/preview toggle; internal note links in journal entries open the linked note directly
- **Telegram bot** — conversational RAG over your notes via Telegram; sends scheduled task reminders and journal nudges at configurable times; each user can configure their own bot token so multiple household members run independent bots; credentials and reminder schedules managed entirely from the web UI (no `.env` edits required)

## Prerequisites

- Python 3.11+
- **ffmpeg** — required for voice dictation audio conversion (WebM/Opus → PCM)
- A running **ChromaDB** HTTP service
- A local **embedding model server** exposing an OpenAI-compatible `/v1/embeddings` endpoint (e.g. [Ollama](https://ollama.com), [llama.cpp](https://github.com/ggerganov/llama.cpp), [text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference))
- _(Optional)_ An OpenAI-compatible `/v1/chat/completions` endpoint for LLM summaries (e.g. Ollama). Without it, summaries fall back to text truncation.
- _(Optional)_ A **Wyoming faster-whisper** server for voice dictation (e.g. `wyoming-faster-whisper --uri tcp://0.0.0.0:10300 --model base`). Without it, the 🎤 dictation button will not function.

## Docker (recommended)

```bash
cp .env.example .env   # configure settings
docker compose up -d
```

The app listens on `https://0.0.0.0:8443` by default (HTTPS required for Android PWA).

## First-time setup: creating users

NoterAI requires at least one user account. On a fresh install, run:

```bash
# Create the admin user and migrate any existing notes/settings to it
docker exec -it noterai python -m backend.create_user --username admin --migrate
# You will be prompted to set a password
```

To add more users:

```bash
docker exec -it noterai python -m backend.create_user --username alice
```

To reset a forgotten password:

```bash
docker exec noterai python -c "
import asyncio, aiosqlite, bcrypt
async def reset():
    async with aiosqlite.connect('/data/notes.db') as db:
        h = bcrypt.hashpw(b'newpassword', bcrypt.gensalt()).decode()
        await db.execute(\"UPDATE users SET password_hash=? WHERE username='admin'\", (h,))
        await db.commit()
        print('Password reset.')
asyncio.run(reset())
"
```

After creating users, open the app in your browser — you will be prompted to log in.

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
| `WHISPER_BASE_URL` | `http://localhost:10300` | Wyoming faster-whisper TCP server address used for voice dictation |
| `JWT_SECRET` | _(required)_ | Secret key for signing JWT auth tokens — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `JWT_EXPIRY_DAYS` | `30` | How many days a login token remains valid |

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

## Voice Dictation (Journal)

Tap the 🎤 button in the search bar to record a voice journal entry. When you tap again to stop, the audio is:

1. Sent to the backend as a WebM/Opus blob
2. Converted to 16 kHz mono PCM via ffmpeg
3. Transcribed by the Wyoming faster-whisper TCP server (`WHISPER_BASE_URL`)
4. Rewritten by the LLM into a structured journal entry with **Time** and **Activities** bullet points
5. Saved as a new note in the Journal folder and opened automatically

The Wyoming server must be running and reachable at `WHISPER_BASE_URL` (default `http://localhost:10300`). Example launch:

```bash
wyoming-faster-whisper --uri tcp://0.0.0.0:10300 --model base --language en
```

If the recording is too short or produces no audio, the frontend shows an error immediately without sending a request.

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

1. **Log in first** — the share target is registered per-user. You must be logged in before installing so Chrome captures the correct per-user manifest.
2. Open the app URL in Chrome on your Android device.
3. Tap the three-dot menu → **Add to Home Screen**.
4. Once installed, NoterAI appears in the Android share sheet — you can share URLs, text, PDFs, and images directly from any app. When sharing an image you'll be prompted to attach it to an existing note or create a new one.

> **Note — share target not appearing?** If you installed the PWA before logging in (or before the multi-user update), the registered manifest has no `share_target`. Uninstall the PWA from Android (long-press icon → Uninstall), log in via Chrome, then reinstall. Chrome must read the manifest *at install time* to register the share target; updating the installed app's manifest after the fact is unreliable across Chrome versions.

## Telegram Bot

NoterAI includes an optional Telegram bot that connects to the RAG chat API so you can query your notes from your phone. Each NoterAI user can configure their own independent bot token — multiple household members can each have a separate bot that searches only their own notes.

### Setup

1. Create a bot via [@BotFather](https://t.me/botfather) and copy the token.
2. Open **Settings** in the web UI and fill in the **Telegram Bot** section:
   - **Bot Token** — token from BotFather (each user sets their own)
   - **Allowed User IDs** — your Telegram numeric user ID (use `/chatid` in the bot once it's running, or check [@userinfobot](https://t.me/userinfobot))
   - **Reminder Chat ID** — the chat where reminders are sent (usually your own user ID)
   - **My Telegram User ID** — links your Telegram account to your NoterAI account so the bot saves notes and journal entries under your user
   - **RAG API URL** — URL of the RAG chat service (default `http://localhost:8084`)
3. Click **Test Connection** to verify the token is valid, then **Save Settings**.
4. Restart the container (`bash update.sh`) to pick up the new credentials.

### Bot commands

| Command | Description |
|---|---|
| `/start` | Show help |
| `/clear` | Reset conversation history |
| `/status` | Check RAG API health |
| `/chatid` | Show your Telegram user ID |
| `/remind` | Trigger a task reminder immediately (for testing) |

### Chat shortcuts

Beyond normal RAG questions, the bot recognizes two keyword prefixes:

- **`remember <text>`** — saves the text as a new Reference note immediately, without an LLM round-trip. Also logs the note in today's journal.
- **`lookup <query>`** — skips folder classification and searches all non-Archive notes directly.

### Scheduled reminders

Configure reminder times in **Settings → Telegram Reminders**. At each time, the bot queries the RAG API for overdue/due-today tasks and sends a concise summary to the reminder chat.

Configure journal check times in **Settings → Journal Reminders**. At each time, the bot checks whether a journal entry exists for today. If not, it sends a friendly AI-generated nudge to write one.

Times are interpreted in the **Server Timezone** configured under **Settings → System**. Set it to your local IANA timezone (e.g. `America/Chicago`) so reminders fire at the correct local time. The scheduler re-reads the database every 30 minutes, so reminder time changes take effect without a restart; credential changes require a restart.

## API examples

```bash
BASE=http://localhost:8889

# Authenticate and capture token
TOKEN=$(curl -s -X POST $BASE/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"changeme"}' | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Create a note
curl -X POST $BASE/api/notes \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"My note","content":"Some content","tags":["work"],"folder":"Reference"}'

# Semantic search
curl -X POST $BASE/api/search \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"content about projects","n_results":5}'

# Simulate Android URL share (uses share_key, no JWT needed)
curl -X POST "$BASE/api/share?key=<your-share-key>" \
  -F 'title=Interesting article' \
  -F 'url=https://example.com/article'

# Upload a PDF attachment
curl -X POST $BASE/api/notes/<note-id>/attachments \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@/path/to/document.pdf'

# Re-index a single note
curl -X POST $BASE/api/notes/<note-id>/reindex \
  -H "Authorization: Bearer $TOKEN"

# Re-index all notes
curl -X POST $BASE/api/reindex \
  -H "Authorization: Bearer $TOKEN"

# Health check (unauthenticated)
curl $BASE/health
```

## Notes

- `attachments/` and `chroma_data/` are git-ignored — back them up separately.
- After changing `CHUNK_SIZE`, `CHUNK_OVERLAP`, or `CHUNK_MAX_CHARS`, use "Reindex all notes" in the sidebar or `POST /api/reindex` to rebuild the vector index.
- If your embedding server has a small physical batch size (e.g. llama.cpp with `--ubatch-size 512`), lower `CHUNK_MAX_CHARS` (e.g. `400`) to avoid token overflow errors.
- OCR for scanned PDFs is not supported.
