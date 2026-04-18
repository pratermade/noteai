from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid

from pythonjsonlogger import jsonlogger
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Annotated

import aiofiles
import aiosqlite
import httpx
from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
    Query, Request, UploadFile, status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from . import database as db
from . import embeddings, vector_store, wyoming_client
from .chunker import chunk_text
from .models import (
    AttachmentResponse, FOLDERS, NoteCreate, NoteResponse, NoteUpdate,
    ReindexJob, SearchRequest, SearchResult,
)
from .pdf_extractor import ExtractionError as PDFExtractionError, extract as pdf_extract
from .web_extractor import ExtractionError as WebExtractionError, extract_url, get_youtube_video_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_pending_share: dict[str, dict] = {}  # token -> {note_id, expires_at}
_reindex_jobs: dict[str, ReindexJob] = {}
_latest_job_id: str | None = None

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
ATTACHMENT_DIR = Path(settings.attachment_dir)

# Read SW template at import time so the dynamic route can inject the version.
_app_version = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
_sw_template = (FRONTEND_DIR / "service_worker.js").read_text()


# ---------------------------------------------------------------------------
# Startup / lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Logging ──
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    logging.root.setLevel(logging.INFO)
    logging.root.handlers = [handler]
    # Emit DEBUG for the indexing pipeline without drowning in httpx/chromadb internals
    for mod in ("backend.main", "backend.embeddings", "backend.vector_store"):
        logging.getLogger(mod).setLevel(logging.DEBUG)

    # Create attachment dir
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)

    # Remove any stale static manifest.json so the dynamic /manifest.json route
    # always wins (old Docker image layers may contain this file).
    stale_manifest = FRONTEND_DIR / "manifest.json"
    if stale_manifest.exists():
        stale_manifest.unlink()
        logger.info("Removed stale frontend/manifest.json — served dynamically")

    # Init DB
    async with aiosqlite.connect(settings.database_url) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await db.init_db(conn)
        # Migrate legacy free-form folders to "Unfiled"
        placeholders = ",".join("?" * len(FOLDERS))
        await conn.execute(
            f"UPDATE notes SET folder = 'Unfiled' WHERE folder = '' OR folder NOT IN ({placeholders})",
            FOLDERS,
        )
        await conn.commit()
        # Backfill summaries for existing attachments that have extracted text but no summary
        async with conn.execute(
            "SELECT id, extracted_text FROM attachments WHERE summary IS NULL AND extracted_text IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            logger.info("Scheduling LLM summary backfill for %d attachment(s) in background...", len(rows))
            asyncio.create_task(_backfill_summaries([(r[0], r[1]) for r in rows]))
    logger.info("SQLite initialised at %s", settings.database_url)

    # Check ChromaDB
    try:
        await vector_store.get_collection()
        logger.info(
            "ChromaDB connected at %s:%s, collection=%s",
            settings.chroma_host, settings.chroma_port, settings.chroma_collection,
        )
    except Exception as exc:
        logger.warning("ChromaDB not reachable at startup: %s", exc)

    # Check embedding endpoint
    try:
        await embeddings.embed_texts(["ping"])
        logger.info("Embedding endpoint reachable at %s", settings.embedding_base_url)
    except Exception as exc:
        logger.warning("Embedding endpoint not reachable at startup: %s", exc)

    # Check summary endpoint
    if settings.summary_base_url:
        try:
            client = _get_summary_client()
            resp = await client.get(f"{settings.summary_base_url}/v1/models", timeout=5.0)
            logger.info("Summary endpoint reachable at %s (status %s)", settings.summary_base_url, resp.status_code)
        except Exception as exc:
            logger.warning("Summary endpoint not reachable at %s: %s", settings.summary_base_url, exc)
    else:
        logger.info("SUMMARY_BASE_URL not set — summaries will use truncation fallback")

    yield

    await embeddings.close_client()
    if _summary_client:
        await _summary_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="NoterAI", lifespan=lifespan)


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------

async def get_db():
    conn = await db.get_db()
    try:
        yield conn
    finally:
        await conn.close()


DB = Annotated[aiosqlite.Connection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Indexing pipelines
# ---------------------------------------------------------------------------

async def _index_note(note_id: str, title: str, content: str,
                      tags: list[str], folder: str) -> None:
    if not content.strip():
        logger.debug("Skipping index for note %s — empty content", note_id)
        return
    logger.info("Indexing note %s (%d chars) folder=%s", note_id, len(content), folder)
    try:
        chunks = chunk_text(content)
        if not chunks:
            logger.warning("No chunks produced for note %s — skipping", note_id)
            return
        n = len(chunks)
        batch_size = settings.index_batch_size
        logger.debug("Note %s: %d chunk(s), index_batch_size=%d", note_id, n, batch_size)
        for i in range(0, n, batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c["text"] for c in batch]
            embs = await embeddings.embed_texts(texts)
            ids = [f"{note_id}_{c['chunk_index']}" for c in batch]
            metas = [
                {
                    "note_id": note_id,
                    "chunk_index": c["chunk_index"],
                    "title": title,
                    "tags": json.dumps(tags),
                    "folder": folder,
                    "source_type": "note",
                    "source_label": title,
                }
                for c in batch
            ]
            await vector_store.upsert(texts, embs, metas, ids)
            logger.debug("Note %s: upserted chunks %d–%d / %d", note_id, i, i + len(batch), n)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.set_note_indexed(conn, note_id)
            summary = await _llm_summary(content)
            await db.set_note_summary(conn, note_id, summary)
        logger.info("Indexed note %s: %d chunk(s)", note_id, n)
    except Exception:
        logger.error("Failed to index note %s", note_id, exc_info=True)


async def _index_attachment(att_id: str, note_id: str, text: str,
                             source_label: str, source_url: str | None = None) -> None:
    logger.info("Indexing attachment %s for note %s (%d chars)", att_id, note_id, len(text))
    try:
        chunks = chunk_text(text)
        if not chunks:
            logger.warning("No chunks produced for attachment %s — skipping", att_id)
            return
        n = len(chunks)
        batch_size = settings.index_batch_size
        logger.info("Attachment %s: %d chunk(s), index_batch_size=%d", att_id, n, batch_size)
        for i in range(0, n, batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c["text"] for c in batch]
            embs = await embeddings.embed_texts(texts)
            ids = [f"{att_id}_c{c['chunk_index']}" for c in batch]
            metas = [
                {
                    "note_id": note_id,
                    "attachment_id": att_id,
                    "chunk_index": c["chunk_index"],
                    "source_type": "attachment",
                    "source_label": source_label,
                    "source_url": source_url or "",
                }
                for c in batch
            ]
            await vector_store.upsert(texts, embs, metas, ids)
            logger.info("Attachment %s: upserted batch %d–%d / %d", att_id, i, i + len(batch), n)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(conn, att_id, indexed_at=_now())
        logger.info("Indexed attachment %s: %d chunk(s) total", att_id, n)
    except Exception:
        logger.error("Failed to index attachment %s for note %s", att_id, note_id, exc_info=True)


async def _pdf_pipeline(att_id: str, note_id: str, stored_path: str,
                         original_filename: str) -> None:
    try:
        result = await pdf_extract(stored_path)
        summary = await _llm_summary(result.text)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(
                conn, att_id,
                page_count=result.page_count,
                extracted_at=_now(),
                extracted_text=result.text,
                summary=summary,
            )
            await db.set_note_type(conn, note_id, 'attachment')
        await _index_attachment(att_id, note_id, result.text, original_filename)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.set_note_indexed(conn, note_id)
    except PDFExtractionError as exc:
        logger.error("PDF extraction failed", extra={"attachment_id": att_id, "note_id": note_id}, exc_info=True)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(conn, att_id, extraction_error=str(exc))
    except Exception as exc:
        logger.error("Unexpected error in PDF pipeline", extra={"attachment_id": att_id, "note_id": note_id}, exc_info=True)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(conn, att_id, extraction_error=str(exc))


async def _web_pipeline(att_id: str, note_id: str, url: str) -> None:
    try:
        result = await extract_url(url)
        label = result.title or _hostname(url)
        summary = await _llm_summary(result.text)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(
                conn, att_id,
                filename=label,
                extracted_text=result.text,
                summary=summary,
                extracted_at=_now(),
                size_bytes=result.char_count,
            )
            note_type = 'video' if get_youtube_video_id(url) else 'url'
            await db.set_note_type(conn, note_id, note_type)
        await _index_attachment(att_id, note_id, result.text, label, source_url=url)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.set_note_indexed(conn, note_id)
    except WebExtractionError as exc:
        logger.error("Web extraction failed", extra={"attachment_id": att_id, "note_id": note_id}, exc_info=True)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(conn, att_id, extraction_error=str(exc))
    except Exception as exc:
        logger.error("Unexpected error in web pipeline", extra={"attachment_id": att_id, "note_id": note_id}, exc_info=True)
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await db.update_attachment(conn, att_id, extraction_error=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_summary(text: str, max_chars: int = 200) -> str:
    """Fallback: extract a short summary by truncating to a sentence boundary."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        pos = truncated.rfind(sep, max_chars // 2)
        if pos != -1:
            return truncated[: pos + 1]
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        return truncated[:last_space] + "\u2026"
    return truncated + "\u2026"


_summary_client: httpx.AsyncClient | None = None


def _get_summary_client() -> httpx.AsyncClient:
    global _summary_client
    if _summary_client is None:
        _summary_client = httpx.AsyncClient(timeout=60.0)
    return _summary_client


async def _llm_summary(text: str) -> str:
    """Generate a ~150-word summary via LLM, falling back to truncation if unavailable."""
    if not settings.summary_base_url:
        logger.info("No SUMMARY_BASE_URL configured — using truncation fallback")
        return _truncate_summary(text)
    logger.info("Requesting LLM summary from %s (model=%s, text=%d chars)",
                settings.summary_base_url, settings.summary_model, len(text))
    headers = {"Content-Type": "application/json"}
    if settings.summary_api_key:
        headers["Authorization"] = f"Bearer {settings.summary_api_key}"
    payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": "Summarize the following content in 50 words or fewer. Be concise and factual.",
            },
            {"role": "user", "content": text[:8000]},
        ],
        "max_tokens": 80,
    }
    try:
        client = _get_summary_client()
        resp = await client.post(
            f"{settings.summary_base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning(
                "Summary LLM returned %s — falling back to truncation: %s",
                resp.status_code, resp.text[:200],
            )
            return _truncate_summary(text)
        result = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("LLM summary generated (%d chars)", len(result))
        return result
    except Exception:
        logger.warning("Summary LLM call failed — falling back to truncation", exc_info=True)
        return _truncate_summary(text)


async def _llm_journal_rewrite(text: str) -> str:
    if not settings.summary_base_url:
        return text
    headers = {"Content-Type": "application/json"}
    if settings.summary_api_key:
        headers["Authorization"] = f"Bearer {settings.summary_api_key}"
    payload = {
        "model": settings.summary_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a clinical note-taking assistant. Transform the following rough "
                    "voice transcript into a structured journal entry using this exact format:\n\n"
                    "**Time:** [time of day if mentioned, e.g. Morning / Afternoon / Evening]\n\n"
                    "**Activities**\n"
                    "* [bullet point per distinct activity or event]\n\n"
                    "Rules: fix grammar, remove filler words and repetitions, one bullet per activity. "
                    "Use plain direct language — no metaphors, emotional color, or reflective commentary. "
                    "Keep faithful to the original, do not invent details. "
                    "Start directly with the formatted entry, no preamble."
                ),
            },
            {"role": "user", "content": text[:12000]},
        ],
        "max_tokens": 1500,
    }
    try:
        client = _get_summary_client()
        resp = await client.post(
            f"{settings.summary_base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            return text
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.warning("Journal rewrite LLM call failed — returning original", exc_info=True)
        return text


async def _journal_pipeline(note_id: str) -> None:
    """Rewrite note content as a clean journal entry when saved to the Journal folder."""
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            note = await db.get_note(conn, note_id)
            if not note or not note.content.strip():
                return
            rewritten = await _llm_journal_rewrite(note.content)
            title = note.title
            if not title or title.lower() in ("untitled", "shared note"):
                title = f"Journal — {datetime.now(timezone.utc).strftime('%B %-d, %Y')}"
            await db.update_note(conn, note_id, title=title, content=rewritten)
        await vector_store.delete_by_note_id(note_id)
        await _index_note(note_id, title, rewritten, note.tags, "Journal")
        logger.info("Journal rewrite complete for note %s", note_id)
    except Exception:
        logger.warning("Journal pipeline failed for note %s", note_id, exc_info=True)


async def _backfill_summaries(rows: list[tuple[str, str]]) -> None:
    """Generate LLM summaries for attachments that have extracted text but no summary."""
    logger.info("Starting LLM summary backfill for %d attachment(s)...", len(rows))
    completed = 0
    for att_id, extracted_text in rows:
        try:
            summary = await _llm_summary(extracted_text)
            async with aiosqlite.connect(settings.database_url) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA foreign_keys = ON")
                await conn.execute("UPDATE attachments SET summary = ? WHERE id = ?", (summary, att_id))
                await conn.commit()
            completed += 1
        except Exception:
            logger.warning("Summary backfill failed for attachment %s", att_id, exc_info=True)
    logger.info("Summary backfill complete: %d/%d succeeded", completed, len(rows))


def _hostname(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or url


def _purge_expired_shares() -> None:
    now = datetime.now(timezone.utc)
    expired = [t for t, v in _pending_share.items() if v["expires_at"] < now]
    for t in expired:
        del _pending_share[t]


# ---------------------------------------------------------------------------
# Notes CRUD
# ---------------------------------------------------------------------------

@app.get("/api/notes", response_model=list[NoteResponse])
async def list_notes(
    conn: DB,
    tag: str | None = Query(default=None),
    folder: str | None = Query(default=None),
):
    return await db.list_notes(conn, tag=tag, folder=folder)


@app.get("/api/notes/{note_id}", response_model=NoteResponse)
async def get_note(note_id: str, conn: DB):
    note = await db.get_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.post("/api/notes", response_model=NoteResponse, status_code=201)
async def create_note(body: NoteCreate, conn: DB, background_tasks: BackgroundTasks):
    note = await db.create_note(conn, body.title, body.content, body.tags, body.folder,
                                reminder_at=body.reminder_at)
    if note.folder == "Journal":
        background_tasks.add_task(_journal_pipeline, note.id)
    else:
        background_tasks.add_task(
            _index_note, note.id, note.title, note.content, note.tags, note.folder
        )
    return note


@app.put("/api/notes/{note_id}", response_model=NoteResponse)
async def update_note(note_id: str, body: NoteUpdate, conn: DB,
                      background_tasks: BackgroundTasks):
    existing = await db.get_note(conn, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Note not found")
    raw = body.model_dump(exclude_unset=True)
    # Allow reminder_at=None to clear it, but drop other None values (means "don't change")
    fields = {k: v for k, v in raw.items() if v is not None or k == "reminder_at"}
    note = await db.update_note(conn, note_id, **fields)
    # Clear indexed_at and re-index
    await db.clear_note_indexed(conn, note_id)
    try:
        await vector_store.delete_by_note_id(note_id)
    except Exception as exc:
        logger.warning("Could not delete old vectors for note %s: %s", note_id, exc)
    if note.folder == "Journal":
        background_tasks.add_task(_journal_pipeline, note.id)
    else:
        background_tasks.add_task(
            _index_note, note.id, note.title, note.content, note.tags, note.folder
        )
    return note


@app.delete("/api/notes/{note_id}", status_code=204)
async def delete_note(note_id: str, conn: DB):
    note = await db.get_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    # Delete attachment files
    atts = await db.list_attachments(conn, note_id)
    for att in atts:
        if att.stored_path:
            path = ATTACHMENT_DIR / att.stored_path
            if path.exists():
                path.unlink(missing_ok=True)
        try:
            await vector_store.delete_by_attachment_id(att.id)
        except Exception:
            logger.warning("Could not delete vectors for attachment %s", att.id, exc_info=True)
    # Clean up note dir
    note_dir = ATTACHMENT_DIR / note_id
    if note_dir.exists():
        shutil.rmtree(note_dir, ignore_errors=True)
    try:
        await vector_store.delete_by_note_id(note_id)
    except Exception as exc:
        logger.warning("Could not delete vectors for note %s: %s", note_id, exc)
    await db.delete_note(conn, note_id)


# ---------------------------------------------------------------------------
# Tags & Folders
# ---------------------------------------------------------------------------

@app.get("/api/tags")
async def list_tags(conn: DB):
    return await db.list_tags(conn)


@app.get("/api/tasks", response_model=list[NoteResponse])
async def list_tasks(conn: DB):
    return await db.list_next_tasks(conn)


@app.get("/api/version")
async def get_version():
    return {"version": _app_version}


@app.get("/api/folders")
async def list_folders(conn: DB):
    return await db.list_folders(conn)


@app.post("/api/journal/dictate", status_code=201)
async def dictate_journal(
    audio: UploadFile,
    conn: DB,
    background_tasks: BackgroundTasks,
):
    audio_bytes = await audio.read()
    logger.info("dictate: received %d bytes, content_type=%s", len(audio_bytes), audio.content_type)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(settings.whisper_base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 10300
        logger.info("dictate: connecting to Wyoming at %s:%d", host, port)
        transcript = (await wyoming_client.transcribe(audio_bytes, host, port)).strip()
        logger.info("dictate: transcript=%r", transcript[:100] if transcript else "")
    except Exception as exc:
        logger.warning("dictate: failed — %s", exc)
        raise HTTPException(status_code=502, detail=f"Whisper transcription failed: {exc}")

    if not transcript:
        raise HTTPException(status_code=422, detail="Transcription returned empty text")

    note_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO notes (id, title, content, tags, folder, created_at, updated_at, note_type) VALUES (?,?,?,?,?,?,?,?)",
        (note_id, "Untitled", transcript, json.dumps([]), "Journal", now, now, "markdown"),
    )
    await conn.commit()
    await _journal_pipeline(note_id)
    return {"id": note_id}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.post("/api/search", response_model=list[SearchResult])
async def search(body: SearchRequest, conn: DB):
    try:
        emb = await embeddings.embed_texts([body.query])
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Embedding service unavailable: {exc}")

    where: dict | None = None
    conditions = []
    if body.tags:
        # Chroma $in operator for any matching tag (stored as JSON string)
        # We search per-tag and merge, or use a simpler approach
        conditions.append({"$or": [{"tags": {"$contains": t}} for t in body.tags]})
    if body.folder:
        conditions.append({"folder": {"$eq": body.folder}})
    else:
        conditions.append({"folder": {"$ne": "Archive"}})

    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    try:
        raw = await vector_store.query(emb[0], body.n_results, where)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Vector store unavailable: {exc}")

    results: list[SearchResult] = []
    for item in raw:
        meta = item["metadata"]
        note_id = meta.get("note_id", "")
        note = await db.get_note(conn, note_id)
        if not note:
            continue
        # cosine distance → similarity score
        score = 1.0 - item["distance"]
        att_id = meta.get("attachment_id") or None
        att_summary: str | None = None
        if att_id:
            att = await db.get_attachment(conn, att_id)
            if att:
                att_summary = att.summary
        results.append(SearchResult(
            note_id=note_id,
            title=note.title,
            folder=note.folder,
            tags=note.tags,
            score=round(score, 4),
            chunk_text=item["document"],
            source_type=meta.get("source_type", "note"),
            source_label=meta.get("source_label", note.title),
            source_url=meta.get("source_url") or None,
            attachment_id=att_id,
            attachment_summary=att_summary,
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@app.post("/api/notes/{note_id}/attachments", response_model=AttachmentResponse,
          status_code=202)
async def upload_attachment(
    note_id: str,
    conn: DB,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    note = await db.get_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    existing = await db.list_attachments(conn, note_id)
    if existing:
        raise HTTPException(status_code=400, detail="This note already has an attachment. Create a new note for additional content.")

    if file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted")

    MAX_SIZE = 50 * 1024 * 1024
    contents = await file.read(MAX_SIZE + 1)
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit")

    att_id = str(uuid.uuid4())
    note_dir = ATTACHMENT_DIR / note_id
    note_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{att_id}.pdf"
    stored_path = Path(note_id) / stored_filename
    full_path = ATTACHMENT_DIR / stored_path

    async with aiofiles.open(full_path, "wb") as f:
        await f.write(contents)

    original_filename = file.filename or "attachment.pdf"
    att = await db.create_attachment(
        conn,
        note_id=note_id,
        filename=original_filename,
        mime_type="application/pdf",
        size_bytes=len(contents),
        stored_path=str(stored_path),
    )
    background_tasks.add_task(
        _pdf_pipeline, att.id, note_id, str(full_path), original_filename
    )
    return att


@app.get("/api/notes/{note_id}/attachments", response_model=list[AttachmentResponse])
async def list_attachments(note_id: str, conn: DB):
    note = await db.get_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return await db.list_attachments(conn, note_id)


@app.get("/api/attachments/{att_id}/download")
async def download_attachment(att_id: str, conn: DB):
    att = await db.get_attachment(conn, att_id)
    if not att or not att.stored_path:
        raise HTTPException(status_code=404, detail="Attachment not found")
    full_path = ATTACHMENT_DIR / att.stored_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(
        path=str(full_path),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{att.filename}"'},
    )


@app.delete("/api/attachments/{att_id}", status_code=204)
async def delete_attachment(att_id: str, conn: DB):
    att = await db.get_attachment(conn, att_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if att.stored_path:
        full_path = ATTACHMENT_DIR / att.stored_path
        full_path.unlink(missing_ok=True)
    try:
        await vector_store.delete_by_attachment_id(att_id)
    except Exception as exc:
        logger.warning("Could not delete vectors for attachment %s: %s", att_id, exc)
    await db.delete_attachment(conn, att_id)


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------

@app.post("/api/notes/{note_id}/reindex", response_model=NoteResponse)
async def reindex_note(note_id: str, conn: DB, background_tasks: BackgroundTasks):
    note = await db.get_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await db.clear_note_indexed(conn, note_id)
    try:
        await vector_store.delete_by_note_id(note_id)
    except Exception as exc:
        logger.warning("Could not delete old vectors: %s", exc)

    # Derive and set note_type from existing attachments synchronously so the
    # returned note has the correct type before the frontend poll re-fetches it.
    atts = await db.list_attachments(conn, note_id)
    if atts:
        if any(a.mime_type == "application/pdf" for a in atts):
            await db.set_note_type(conn, note_id, 'attachment')
        elif any(a.mime_type == "video/youtube" for a in atts):
            await db.set_note_type(conn, note_id, 'video')
        elif any(a.mime_type == "text/html" for a in atts):
            await db.set_note_type(conn, note_id, 'url')
        for att in atts:
            if att.mime_type == "application/pdf" and att.stored_path:
                background_tasks.add_task(
                    _pdf_pipeline, att.id, note_id, str(ATTACHMENT_DIR / att.stored_path), att.filename
                )
            elif att.mime_type in ("text/html", "video/youtube") and att.source_url:
                background_tasks.add_task(_web_pipeline, att.id, note_id, att.source_url)

    await _index_note(note.id, note.title, note.content, note.tags, note.folder)
    return await db.get_note(conn, note_id)


@app.post("/api/reindex", response_model=ReindexJob)
async def bulk_reindex(
    conn: DB,
    background_tasks: BackgroundTasks,
    folder: str | None = None,
    tag: str | None = None,
):
    notes = await db.list_notes(conn, tag=tag, folder=folder)
    job_id = str(uuid.uuid4())
    job = ReindexJob(
        job_id=job_id,
        status="running",
        total=len(notes),
        completed=0,
        failed=0,
        started_at=_now(),
    )
    _reindex_jobs[job_id] = job
    global _latest_job_id
    _latest_job_id = job_id

    note_snapshots = [
        (n.id, n.title, n.content, n.tags, n.folder) for n in notes
    ]

    background_tasks.add_task(_bulk_reindex_task, job_id, note_snapshots)
    return job


async def _bulk_reindex_task(job_id: str, notes: list[tuple]) -> None:
    job = _reindex_jobs[job_id]
    for note_id, title, content, tags, folder in notes:
        try:
            async with aiosqlite.connect(settings.database_url) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA foreign_keys = ON")
                await db.clear_note_indexed(conn, note_id)
            try:
                await vector_store.delete_by_note_id(note_id)
            except Exception:
                logger.warning("Could not delete old vectors for note %s", note_id, exc_info=True)
            await _index_note(note_id, title, content, tags, folder)

            # Re-index attachments — derive note_type from all attachments first,
            # then re-run pipelines for those that have already been extracted.
            async with aiosqlite.connect(settings.database_url) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA foreign_keys = ON")
                all_atts = await db.list_attachments(conn, note_id)
                if any(a.mime_type == "application/pdf" for a in all_atts):
                    await db.set_note_type(conn, note_id, 'attachment')
                elif any(a.mime_type == "video/youtube" for a in all_atts):
                    await db.set_note_type(conn, note_id, 'video')
                elif any(a.mime_type == "text/html" for a in all_atts):
                    await db.set_note_type(conn, note_id, 'url')
                atts = await db.list_indexed_attachments(conn, note_id)

            for att in atts:
                try:
                    await vector_store.delete_by_attachment_id(att.id)
                    if att.mime_type == "application/pdf" and att.stored_path:
                        full_path = str(ATTACHMENT_DIR / att.stored_path)
                        await _pdf_pipeline(att.id, note_id, full_path, att.filename)
                    elif att.mime_type in ("text/html", "video/youtube") and att.source_url:
                        await _web_pipeline(att.id, note_id, att.source_url)
                    job.attachments_completed += 1
                except Exception as exc:
                    logger.warning("Attachment reindex failed %s: %s", att.id, exc)
                    job.attachments_failed += 1

            job.completed += 1
        except Exception as exc:
            logger.warning("Note reindex failed %s: %s", note_id, exc)
            job.failed += 1
            job.errors.append({"note_id": note_id, "title": title, "error": str(exc)})

    job.finished_at = _now()
    job.status = "completed_with_errors" if job.failed else "completed"


@app.get("/api/reindex/status", response_model=ReindexJob)
async def reindex_status(job_id: str | None = Query(default=None)):
    if job_id:
        job = _reindex_jobs.get(job_id)
    elif _latest_job_id:
        job = _reindex_jobs.get(_latest_job_id)
    else:
        job = None
    if not job:
        raise HTTPException(status_code=404, detail="No reindex job found")
    return job


# ---------------------------------------------------------------------------
# Share target
# ---------------------------------------------------------------------------

@app.post("/api/share")
async def share(
    background_tasks: BackgroundTasks,
    title: str = Form(default=""),
    text: str = Form(default=""),
    url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
):
    token = str(uuid.uuid4())[:8]
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)

    async with aiosqlite.connect(settings.database_url) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")

        if file and file.filename:
            # Case 1: PDF file
            stem = Path(file.filename).stem.replace("_", " ").replace("-", " ")
            note = await db.create_note(conn, stem, "", [], "shared", note_type='attachment')
            contents = await file.read()
            att_id = str(uuid.uuid4())
            note_dir = ATTACHMENT_DIR / note.id
            note_dir.mkdir(parents=True, exist_ok=True)
            stored_filename = f"{att_id}.pdf"
            stored_path = Path(note.id) / stored_filename
            full_path = ATTACHMENT_DIR / stored_path
            async with aiofiles.open(full_path, "wb") as f:
                await f.write(contents)
            att = await db.create_attachment(
                conn,
                note_id=note.id,
                filename=file.filename,
                mime_type="application/pdf",
                size_bytes=len(contents),
                stored_path=str(stored_path),
            )
            background_tasks.add_task(
                _pdf_pipeline, att.id, note.id, str(full_path), file.filename
            )
        elif url or text.strip().startswith(('http://', 'https://')):
            # Case 2: URL share — either url field or text field contains a URL
            # (some Android apps route the URL through the text field)
            resolved_url = url or text.strip()
            hostname = _hostname(resolved_url)
            note_title = title.strip() or hostname
            is_youtube = get_youtube_video_id(resolved_url) is not None
            note_type = 'video' if is_youtube else 'url'
            mime_type = 'video/youtube' if is_youtube else 'text/html'
            note = await db.create_note(conn, note_title, resolved_url, [], "shared", note_type=note_type)
            att = await db.create_attachment(
                conn,
                note_id=note.id,
                filename=hostname,
                mime_type=mime_type,
                size_bytes=0,
                source_url=resolved_url,
            )
            background_tasks.add_task(_web_pipeline, att.id, note.id, resolved_url)
        else:
            # Case 3: text only
            note_title = title[:80].strip() or "Shared note"
            note = await db.create_note(conn, note_title, text, [], "shared")
            background_tasks.add_task(
                _index_note, note.id, note.title, note.content, note.tags, note.folder
            )

    _purge_expired_shares()
    _pending_share[token] = {"note_id": note.id, "expires_at": expires}

    return RedirectResponse(
        url=f"/share-handler?token={token}", status_code=302
    )


@app.get("/api/share/pending")
async def share_pending(token: str = Query(...)):
    _purge_expired_shares()
    entry = _pending_share.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Token not found or expired")
    note_id = entry["note_id"]
    del _pending_share[token]
    return {"note_id": note_id, "status": "ready"}


@app.get("/share-handler")
async def share_handler():
    return FileResponse(str(FRONTEND_DIR / "share.html"))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    result: dict = {}

    # SQLite
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("SELECT 1")
        result["sqlite"] = "ok"
    except Exception as exc:
        result["sqlite"] = f"error: {exc}"

    # ChromaDB
    try:
        await vector_store.get_collection()
        result["chroma"] = "ok"
    except Exception as exc:
        result["chroma"] = f"error: {exc}"

    # Embedding
    try:
        await embeddings.embed_texts(["ping"])
        result["embedding"] = "ok"
    except Exception as exc:
        result["embedding"] = f"error: {exc}"

    overall = "ok" if all(v == "ok" for v in result.values()) else "degraded"
    result["status"] = overall
    return result


# ---------------------------------------------------------------------------
# service_worker.js — served dynamically so APP_VERSION is injected at startup
# manifest.json — served dynamically so APP_BASE_URL is always current
# (both must be defined before the StaticFiles mount)
# ---------------------------------------------------------------------------

@app.get("/service_worker.js", include_in_schema=False)
async def service_worker():
    return Response(
        content=_sw_template.replace("'__APP_VERSION__'", f"'{_app_version}'"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )

@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    return JSONResponse(
        content={
            "name": "NoterAI",
            "short_name": "NoterAI",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#4f46e5",
            "icons": [
                {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
            "share_target": {
                "action": f"{settings.app_base_url}/api/share",
                "method": "POST",
                "enctype": "multipart/form-data",
                "params": {
                    "title": "title",
                    "text": "text",
                    "url": "url",
                    "files": [{"name": "file", "accept": ["application/pdf"]}],
                },
            },
        },
        media_type="application/manifest+json",
    )


# ---------------------------------------------------------------------------
# Static frontend (must be last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
