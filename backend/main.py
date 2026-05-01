from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid

from pythonjsonlogger import jsonlogger
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone, timedelta
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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .auth import CurrentUser, decode_token, get_current_user, hash_password, verify_password, create_token

_bearer = HTTPBearer(auto_error=False)
from .config import settings
from . import database as db
from . import embeddings, vector_store, wyoming_client
from .chunker import chunk_text
from .models import (
    AttachmentResponse, ChangePasswordRequest, FOLDERS, ListItemCreate, ListItemResponse,
    ListItemUpdate, LoginRequest, NoteCreate, NoteResponse, NoteShareResponse, NoteUpdate,
    ReindexJob, SearchRequest, SearchResult, SettingsPatch, SettingsResponse,
    TokenResponse, UserResponse,
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

_app_version = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
_sw_template = (FRONTEND_DIR / "service_worker.js").read_text()

# Base manifest content (no share_target — injected per-user via /api/manifest)
_BASE_MANIFEST = {
    "name": "NoterAI",
    "short_name": "NoterAI",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#ffffff",
    "theme_color": "#4f46e5",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


# ---------------------------------------------------------------------------
# Startup / lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    logging.root.setLevel(logging.INFO)
    logging.root.handlers = [handler]
    for mod in ("backend.main", "backend.embeddings", "backend.vector_store"):
        logging.getLogger(mod).setLevel(logging.DEBUG)

    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)

    stale_manifest = FRONTEND_DIR / "manifest.json"
    if stale_manifest.exists():
        stale_manifest.unlink()
        logger.info("Removed stale frontend/manifest.json — served dynamically")

    async with aiosqlite.connect(settings.database_url) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await db.init_db(conn)
        placeholders = ",".join("?" * len(FOLDERS))
        await conn.execute(
            f"UPDATE notes SET folder = 'Unfiled' WHERE folder = '' OR folder NOT IN ({placeholders})",
            FOLDERS,
        )
        await conn.commit()
        async with conn.execute(
            "SELECT id, extracted_text FROM attachments WHERE summary IS NULL AND extracted_text IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            logger.info("Scheduling LLM summary backfill for %d attachment(s) in background...", len(rows))
            asyncio.create_task(_backfill_summaries([(r[0], r[1]) for r in rows]))
    logger.info("SQLite initialised at %s", settings.database_url)

    try:
        await vector_store.get_collection()
        logger.info("ChromaDB connected at %s:%s, collection=%s",
                    settings.chroma_host, settings.chroma_port, settings.chroma_collection)
    except Exception as exc:
        logger.warning("ChromaDB not reachable at startup: %s", exc)

    try:
        await embeddings.embed_texts(["ping"])
        logger.info("Embedding endpoint reachable at %s", settings.embedding_base_url)
    except Exception as exc:
        logger.warning("Embedding endpoint not reachable at startup: %s", exc)

    if settings.summary_base_url:
        try:
            client = _get_summary_client()
            resp = await client.get(f"{settings.summary_base_url}/v1/models", timeout=5.0)
            logger.info("Summary endpoint reachable at %s (status %s)",
                        settings.summary_base_url, resp.status_code)
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
                      tags: list[str], folder: str, user_id: str = "") -> None:
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
                    "user_id": user_id,
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
                             source_label: str, source_url: str | None = None,
                             user_id: str = "") -> None:
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
                    "user_id": user_id,
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
                         original_filename: str, user_id: str = "") -> None:
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
        await _index_attachment(att_id, note_id, result.text, original_filename, user_id=user_id)
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


async def _web_pipeline(att_id: str, note_id: str, url: str, user_id: str = "") -> None:
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
        await _index_attachment(att_id, note_id, result.text, label, source_url=url, user_id=user_id)
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


async def _journal_pipeline(note_id: str, user_id: str = "") -> None:
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            note = await db.get_note(conn, note_id)
            if not note or not note.content.strip():
                return
            title = note.title
            if not title or title.lower() in ("untitled", "shared note"):
                title = f"Journal — {datetime.now(timezone.utc).strftime('%B %-d, %Y')}"
            await db.update_note(conn, note_id, **{"title": title})
        await vector_store.delete_by_note_id(note_id)
        await _index_note(note_id, title, note.content, note.tags, "Journal", user_id=user_id)
        logger.info("Journal pipeline complete for note %s", note_id)
    except Exception:
        logger.warning("Journal pipeline failed for note %s", note_id, exc_info=True)


async def _backfill_summaries(rows: list[tuple[str, str]]) -> None:
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_summary(text: str, max_chars: int = 200) -> str:
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
            {"role": "system",
             "content": "Summarize the following content in 50 words or fewer. Be concise and factual."},
            {"role": "user", "content": text[:8000]},
        ],
        "max_tokens": 80,
    }
    try:
        client = _get_summary_client()
        resp = await client.post(f"{settings.summary_base_url}/v1/chat/completions",
                                 json=payload, headers=headers)
        if resp.status_code != 200:
            logger.warning("Summary LLM returned %s — falling back to truncation: %s",
                           resp.status_code, resp.text[:200])
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
        resp = await client.post(f"{settings.summary_base_url}/v1/chat/completions",
                                 json=payload, headers=headers)
        if resp.status_code != 200:
            return text
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.warning("Journal rewrite LLM call failed — returning original", exc_info=True)
        return text


def _hostname(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or url


def _purge_expired_shares() -> None:
    now = datetime.now(timezone.utc)
    expired = [t for t, v in _pending_share.items() if v["expires_at"] < now]
    for t in expired:
        entry = _pending_share.pop(t)
        if entry.get("tmp_path"):
            Path(entry["tmp_path"]).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, conn: DB):
    user = await db.get_user_by_username(conn, body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_token(user["id"])
    return TokenResponse(token=token, username=user["username"])


@app.get("/api/auth/me", response_model=UserResponse)
async def me(current_user: CurrentUser):
    return UserResponse(**current_user)


@app.get("/api/users")
async def list_users(conn: DB, current_user: CurrentUser):
    users = await db.list_users(conn)
    return [{"id": u["id"], "username": u["username"]} for u in users]


@app.post("/api/auth/change-password", status_code=204)
async def change_password(body: ChangePasswordRequest, current_user: CurrentUser, conn: DB):
    user = await db.get_user_by_username(conn, current_user["username"])
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new_hash = hash_password(body.new_password)
    await conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (new_hash, current_user["id"]),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Notes CRUD
# ---------------------------------------------------------------------------

@app.get("/api/notes", response_model=list[NoteResponse])
async def list_notes(
    conn: DB,
    current_user: CurrentUser,
    tag: str | None = Query(default=None),
    folder: str | None = Query(default=None),
):
    return await db.list_notes(conn, user_id=current_user["id"], tag=tag, folder=folder)


@app.get("/api/notes/{note_id}", response_model=NoteResponse)
async def get_note(note_id: str, conn: DB, current_user: CurrentUser):
    note = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.post("/api/notes", response_model=NoteResponse, status_code=201)
async def create_note(body: NoteCreate, conn: DB, background_tasks: BackgroundTasks,
                      current_user: CurrentUser):
    note = await db.create_note(conn, current_user["id"], body.title, body.content,
                                body.tags, body.folder, note_type=body.note_type,
                                reminder_at=body.reminder_at)
    if body.note_type == "list":
        await db.set_note_indexed(conn, note.id)
    elif note.folder == "Journal":
        background_tasks.add_task(_journal_pipeline, note.id, current_user["id"])
    else:
        background_tasks.add_task(
            _index_note, note.id, note.title, note.content, note.tags, note.folder,
            current_user["id"]
        )
    return note


@app.put("/api/notes/{note_id}", response_model=NoteResponse)
async def update_note(note_id: str, body: NoteUpdate, conn: DB,
                      background_tasks: BackgroundTasks, current_user: CurrentUser):
    existing = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not existing:
        raise HTTPException(status_code=404, detail="Note not found")
    raw = body.model_dump(exclude_unset=True)
    fields = {k: v for k, v in raw.items() if v is not None or k == "reminder_at"}
    # For list notes, access already verified via get_note (supports shared); skip user_id filter
    update_uid = None if existing.note_type == "list" else current_user["id"]
    note = await db.update_note(conn, note_id, user_id=update_uid, **fields)
    if note.note_type == "list":
        await db.set_note_indexed(conn, note_id)
    else:
        await db.clear_note_indexed(conn, note_id)
        try:
            await vector_store.delete_by_note_id(note_id)
        except Exception as exc:
            logger.warning("Could not delete old vectors for note %s: %s", note_id, exc)
        if note.folder == "Journal":
            background_tasks.add_task(_journal_pipeline, note.id, current_user["id"])
        else:
            background_tasks.add_task(
                _index_note, note.id, note.title, note.content, note.tags, note.folder,
                current_user["id"]
            )
    return note


@app.delete("/api/notes/{note_id}", status_code=204)
async def delete_note(note_id: str, conn: DB, current_user: CurrentUser):
    note = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
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
    note_dir = ATTACHMENT_DIR / note_id
    if note_dir.exists():
        shutil.rmtree(note_dir, ignore_errors=True)
    try:
        await vector_store.delete_by_note_id(note_id)
    except Exception as exc:
        logger.warning("Could not delete vectors for note %s: %s", note_id, exc)
    await db.delete_note(conn, note_id, user_id=current_user["id"])


# ---------------------------------------------------------------------------
# Tags & Folders
# ---------------------------------------------------------------------------

@app.get("/api/tags")
async def list_tags(conn: DB, current_user: CurrentUser):
    return await db.list_tags(conn, user_id=current_user["id"])


@app.get("/api/tasks", response_model=list[NoteResponse])
async def list_tasks(conn: DB, current_user: CurrentUser):
    return await db.list_next_tasks(conn, user_id=current_user["id"])


@app.get("/api/version")
async def get_version():
    return {"version": _app_version}


@app.get("/api/folders")
async def list_folders(conn: DB, current_user: CurrentUser):
    return await db.list_folders(conn)


@app.post("/api/journal/dictate", status_code=201)
async def dictate_journal(
    audio: UploadFile,
    conn: DB,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
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

    note = await db.create_note(conn, current_user["id"], "Untitled", transcript, [], "Journal")
    await _journal_pipeline(note.id, current_user["id"])
    return {"id": note.id}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.post("/api/search", response_model=list[SearchResult])
async def search(body: SearchRequest, conn: DB, current_user: CurrentUser):
    try:
        emb = await embeddings.embed_texts([body.query])
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Embedding service unavailable: {exc}")

    conditions = [{"user_id": {"$eq": current_user["id"]}}]
    if body.tags:
        conditions.append({"$or": [{"tags": {"$contains": t}} for t in body.tags]})
    if body.folder:
        conditions.append({"folder": {"$eq": body.folder}})
    else:
        conditions.append({"folder": {"$ne": "Archive"}})

    where = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    try:
        raw = await vector_store.query(emb[0], body.n_results, where)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Vector store unavailable: {exc}")

    results: list[SearchResult] = []
    for item in raw:
        meta = item["metadata"]
        note_id = meta.get("note_id", "")
        note = await db.get_note(conn, note_id, user_id=current_user["id"])
        if not note:
            continue
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
# List items
# ---------------------------------------------------------------------------

async def _require_note_access(conn: aiosqlite.Connection, note_id: str,
                                user_id: str) -> NoteResponse:
    note = await db.get_note(conn, note_id, user_id=user_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.note_type != "list":
        raise HTTPException(status_code=400, detail="Note is not a list")
    return note


@app.get("/api/notes/{note_id}/items", response_model=list[ListItemResponse])
async def get_list_items(note_id: str, conn: DB, current_user: CurrentUser):
    await _require_note_access(conn, note_id, current_user["id"])
    return await db.get_list_items(conn, note_id)


@app.post("/api/notes/{note_id}/items", response_model=ListItemResponse, status_code=201)
async def create_list_item(note_id: str, body: ListItemCreate, conn: DB,
                            current_user: CurrentUser):
    await _require_note_access(conn, note_id, current_user["id"])
    return await db.create_list_item(conn, note_id, body.content, body.position)


@app.put("/api/notes/{note_id}/items/{item_id}", response_model=ListItemResponse)
async def update_list_item(note_id: str, item_id: str, body: ListItemUpdate,
                            conn: DB, current_user: CurrentUser):
    await _require_note_access(conn, note_id, current_user["id"])
    fields = body.model_dump(exclude_unset=True)
    item = await db.update_list_item(conn, item_id, note_id, **fields)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.delete("/api/notes/{note_id}/items/{item_id}", status_code=204)
async def delete_list_item(note_id: str, item_id: str, conn: DB,
                            current_user: CurrentUser):
    await _require_note_access(conn, note_id, current_user["id"])
    deleted = await db.delete_list_item(conn, item_id, note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Item not found")


# ---------------------------------------------------------------------------
# Note sharing
# ---------------------------------------------------------------------------

async def _require_note_owner(conn: aiosqlite.Connection, note_id: str,
                               user_id: str) -> NoteResponse:
    note = await db.get_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.note_type != "list":
        raise HTTPException(status_code=400, detail="Only list notes can be shared")
    # Check ownership directly (not via share)
    async with conn.execute("SELECT id FROM notes WHERE id = ? AND user_id = ?",
                            (note_id, user_id)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Only the note owner can manage sharing")
    return note


@app.get("/api/notes/{note_id}/shares", response_model=list[NoteShareResponse])
async def get_note_shares(note_id: str, conn: DB, current_user: CurrentUser):
    note = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not note or note.note_type != "list":
        raise HTTPException(status_code=404, detail="Note not found")
    return await db.list_note_shares(conn, note_id)


@app.post("/api/notes/{note_id}/shares/{user_id}", response_model=NoteShareResponse,
          status_code=201)
async def share_note(note_id: str, user_id: str, conn: DB, current_user: CurrentUser):
    await _require_note_owner(conn, note_id, current_user["id"])
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="Cannot share a note with yourself")
    target = await db.get_user_by_id(conn, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    share = await db.create_note_share(conn, note_id, user_id)
    if not share:
        raise HTTPException(status_code=409, detail="Already shared with this user")
    return share


@app.delete("/api/notes/{note_id}/shares/{user_id}", status_code=204)
async def unshare_note(note_id: str, user_id: str, conn: DB, current_user: CurrentUser):
    await _require_note_owner(conn, note_id, current_user["id"])
    deleted = await db.delete_note_share(conn, note_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Share not found")


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


@app.post("/api/notes/{note_id}/attachments", response_model=AttachmentResponse, status_code=202)
async def upload_attachment(
    note_id: str,
    conn: DB,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    note = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    mime = file.content_type or ""
    if mime != "application/pdf" and not mime.startswith("image/"):
        raise HTTPException(status_code=415, detail="Only PDF and image files are accepted")

    existing = await db.list_attachments(conn, note_id)
    if mime == "application/pdf" and any(a.mime_type == "application/pdf" for a in existing):
        raise HTTPException(status_code=400, detail="This note already has a PDF attachment.")

    MAX_SIZE = 50 * 1024 * 1024
    contents = await file.read(MAX_SIZE + 1)
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit")

    att_id = str(uuid.uuid4())
    note_dir = ATTACHMENT_DIR / note_id
    note_dir.mkdir(parents=True, exist_ok=True)
    ext = _IMAGE_EXT.get(mime, ".pdf")
    stored_filename = f"{att_id}{ext}"
    stored_path = Path(note_id) / stored_filename
    full_path = ATTACHMENT_DIR / stored_path

    async with aiofiles.open(full_path, "wb") as f:
        await f.write(contents)

    original_filename = file.filename or f"attachment{ext}"
    att = await db.create_attachment(
        conn, note_id=note_id, filename=original_filename,
        mime_type=mime, size_bytes=len(contents),
        stored_path=str(stored_path),
    )
    if mime == "application/pdf":
        background_tasks.add_task(
            _pdf_pipeline, att.id, note_id, str(full_path), original_filename, current_user["id"]
        )
    return att


@app.get("/api/notes/{note_id}/attachments", response_model=list[AttachmentResponse])
async def list_attachments(note_id: str, conn: DB, current_user: CurrentUser):
    note = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return await db.list_attachments(conn, note_id)


@app.get("/api/attachments/{att_id}/download")
async def download_attachment(
    att_id: str,
    conn: DB,
    token: str | None = Query(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    from .auth import decode_token
    raw = (credentials.credentials if credentials else None) or token
    if not raw:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = decode_token(raw)
    async with conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=401, detail="User not found")

    att = await db.get_attachment(conn, att_id)
    if not att or not att.stored_path:
        raise HTTPException(status_code=404, detail="Attachment not found")
    note = await db.get_note(conn, att.note_id, user_id=user_id)
    if not note:
        raise HTTPException(status_code=404, detail="Attachment not found")
    full_path = ATTACHMENT_DIR / att.stored_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    is_image = bool(att.mime_type and att.mime_type.startswith("image/"))
    disposition = "inline" if is_image else f'attachment; filename="{att.filename}"'
    return FileResponse(
        path=str(full_path),
        media_type=att.mime_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@app.delete("/api/attachments/{att_id}", status_code=204)
async def delete_attachment(att_id: str, conn: DB, current_user: CurrentUser):
    att = await db.get_attachment(conn, att_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    note = await db.get_note(conn, att.note_id, user_id=current_user["id"])
    if not note:
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
async def reindex_note(note_id: str, conn: DB, background_tasks: BackgroundTasks,
                       current_user: CurrentUser):
    note = await db.get_note(conn, note_id, user_id=current_user["id"])
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await db.clear_note_indexed(conn, note_id)
    try:
        await vector_store.delete_by_note_id(note_id)
    except Exception as exc:
        logger.warning("Could not delete old vectors: %s", exc)

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
                    _pdf_pipeline, att.id, note_id,
                    str(ATTACHMENT_DIR / att.stored_path), att.filename, current_user["id"]
                )
            elif att.mime_type in ("text/html", "video/youtube") and att.source_url:
                background_tasks.add_task(_web_pipeline, att.id, note_id, att.source_url,
                                          current_user["id"])

    await _index_note(note.id, note.title, note.content, note.tags, note.folder,
                      current_user["id"])
    return await db.get_note(conn, note_id, user_id=current_user["id"])


@app.post("/api/reindex", response_model=ReindexJob)
async def bulk_reindex(
    conn: DB,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    folder: str | None = None,
    tag: str | None = None,
):
    notes = await db.list_notes(conn, user_id=current_user["id"], tag=tag, folder=folder)
    job_id = str(uuid.uuid4())
    job = ReindexJob(
        job_id=job_id, status="running", total=len(notes),
        completed=0, failed=0, started_at=_now(),
    )
    _reindex_jobs[job_id] = job
    global _latest_job_id
    _latest_job_id = job_id

    note_snapshots = [(n.id, n.title, n.content, n.tags, n.folder) for n in notes]
    background_tasks.add_task(_bulk_reindex_task, job_id, note_snapshots, current_user["id"])
    return job


async def _bulk_reindex_task(job_id: str, notes: list[tuple], user_id: str) -> None:
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
            await _index_note(note_id, title, content, tags, folder, user_id)

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
                        await _pdf_pipeline(att.id, note_id, full_path, att.filename, user_id)
                    elif att.mime_type in ("text/html", "video/youtube") and att.source_url:
                        await _web_pipeline(att.id, note_id, att.source_url, user_id)
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
async def reindex_status(current_user: CurrentUser, job_id: str | None = Query(default=None)):
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
# Settings
# ---------------------------------------------------------------------------

_DEFAULT_REMINDER_TIMES = "08:00,14:00"
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_DEFAULT_CHARACTER_PROMPT = (
    "You are Alfred, a dry witty butler assistant. "
    "You are helpful but value the user's time, so you keep banter quick and dry."
)


def _parse_times(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


async def _read_reminder_times(conn, user_id: str) -> list[str]:
    raw = await db.get_user_setting(conn, user_id, "reminder_times")
    if raw is not None:
        return _parse_times(raw)
    # Migrate legacy hour-only setting
    legacy = await db.get_user_setting(conn, user_id, "reminder_hours")
    if legacy:
        migrated = ",".join(f"{int(h.strip()):02d}:00" for h in legacy.split(",") if h.strip())
        await db.set_user_setting(conn, user_id, "reminder_times", migrated)
        return _parse_times(migrated)
    return _parse_times(_DEFAULT_REMINDER_TIMES)


async def _build_settings_response(conn, user_id: str) -> SettingsResponse:
    async def _get_u(key: str, default: str = "") -> str:
        val = await db.get_user_setting(conn, user_id, key)
        return val if val is not None else default

    async def _get_g(key: str, env_key: str = "", default: str = "") -> str:
        val = await db.get_setting(conn, key)
        if val is not None:
            return val
        return os.environ.get(env_key, default) if env_key else default

    times = await _read_reminder_times(conn, user_id)

    async def _get_tg(key: str, env_key: str = "", default: str = "") -> str:
        """Per-user Telegram setting, falling back to legacy global then env."""
        val = await db.get_user_setting(conn, user_id, key)
        if val is not None:
            return val
        val = await db.get_setting(conn, key)  # legacy global migration fallback
        if val is not None:
            return val
        return os.environ.get(env_key, default) if env_key else default

    allowed_raw = await _get_tg("telegram_allowed_users", "TELEGRAM_ALLOWED_USERS", "")
    allowed = [int(u.strip()) for u in allowed_raw.split(",") if u.strip()]

    chat_id_raw = await _get_tg("telegram_reminder_chat_id", "TELEGRAM_REMINDER_CHAT_ID", "0")
    chat_id = int(chat_id_raw) if chat_id_raw.strip() else 0

    max_hist_raw = await _get_tg("telegram_max_history", "TELEGRAM_MAX_HISTORY", "20")
    max_hist = int(max_hist_raw) if max_hist_raw.strip() else 20

    journal_raw = await db.get_user_setting(conn, user_id, "journal_reminder_times")
    journal_times = _parse_times(journal_raw) if journal_raw else []

    tz = await _get_u("server_timezone") or os.environ.get("TZ", "")

    return SettingsResponse(
        server_timezone=tz,
        reminder_times=times,
        journal_reminder_times=journal_times,
        telegram_bot_token=await _get_tg("telegram_bot_token", "TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_users=allowed,
        telegram_reminder_chat_id=chat_id,
        telegram_rag_url=await _get_tg("telegram_rag_url", "TELEGRAM_RAG_URL", "http://localhost:8084"),
        telegram_rag_model=await _get_tg("telegram_rag_model", "TELEGRAM_RAG_MODEL", "noterai-rag"),
        telegram_max_history=max_hist,
        character_prompt=await _get_u("character_prompt"),
        telegram_bot_user_id=await _get_g("bot_user_id"),
        telegram_user_id=await _get_tg("telegram_user_id"),
    )


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings(conn: DB, current_user: CurrentUser):
    return await _build_settings_response(conn, current_user["id"])


def _validate_times(times: list[str], label: str) -> None:
    for t in times:
        m = _TIME_RE.fullmatch(t)
        if not m:
            raise HTTPException(status_code=422, detail=f"{label}: invalid time '{t}' — use HH:MM")
        h, mn = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mn <= 59):
            raise HTTPException(status_code=422, detail=f"{label}: time '{t}' out of range")


@app.patch("/api/settings", response_model=SettingsResponse)
async def update_settings(body: SettingsPatch, conn: DB, current_user: CurrentUser):
    uid = current_user["id"]
    # Per-user settings
    if body.server_timezone is not None:
        await db.set_user_setting(conn, uid, "server_timezone", body.server_timezone)
    if body.reminder_times is not None:
        _validate_times(body.reminder_times, "Reminder times")
        await db.set_user_setting(conn, uid, "reminder_times", ",".join(body.reminder_times))
    if body.journal_reminder_times is not None:
        _validate_times(body.journal_reminder_times, "Journal reminder times")
        await db.set_user_setting(conn, uid, "journal_reminder_times",
                                  ",".join(body.journal_reminder_times))
    if body.character_prompt is not None:
        await db.set_user_setting(conn, uid, "character_prompt", body.character_prompt)
    # Per-user Telegram settings
    if body.telegram_bot_token is not None:
        await db.set_user_setting(conn, uid, "telegram_bot_token", body.telegram_bot_token)
    if body.telegram_allowed_users is not None:
        await db.set_user_setting(conn, uid, "telegram_allowed_users",
                                  ",".join(str(u) for u in body.telegram_allowed_users))
    if body.telegram_reminder_chat_id is not None:
        await db.set_user_setting(conn, uid, "telegram_reminder_chat_id",
                                  str(body.telegram_reminder_chat_id))
    if body.telegram_rag_url is not None:
        await db.set_user_setting(conn, uid, "telegram_rag_url", body.telegram_rag_url)
    if body.telegram_rag_model is not None:
        await db.set_user_setting(conn, uid, "telegram_rag_model", body.telegram_rag_model)
    if body.telegram_max_history is not None:
        await db.set_user_setting(conn, uid, "telegram_max_history",
                                  str(body.telegram_max_history))
    if body.telegram_bot_user_id is not None:
        user = await db.get_user_by_id(conn, body.telegram_bot_user_id)
        if not user:
            raise HTTPException(status_code=422, detail="telegram_bot_user_id: user not found")
        await db.set_setting(conn, "bot_user_id", body.telegram_bot_user_id)
    if body.telegram_user_id is not None:
        await db.set_user_setting(conn, uid, "telegram_user_id", body.telegram_user_id)
    return await _build_settings_response(conn, uid)


@app.post("/api/settings/test-telegram")
async def test_telegram(conn: DB, current_user: CurrentUser):
    uid = current_user["id"]
    token = await db.get_user_setting(conn, uid, "telegram_bot_token") or await db.get_setting(conn, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id_raw = await db.get_user_setting(conn, uid, "telegram_reminder_chat_id") or await db.get_setting(conn, "telegram_reminder_chat_id") or os.environ.get("TELEGRAM_REMINDER_CHAT_ID", "0")
    chat_id = int(chat_id_raw) if chat_id_raw.strip() else 0

    if not token:
        raise HTTPException(status_code=400, detail="Bot token is not configured")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Reminder chat ID is not configured")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "✅ NoterAI test — Telegram connection is working!"},
        )

    if resp.status_code != 200:
        detail = resp.json().get("description", "unknown error")
        raise HTTPException(status_code=502, detail=f"Telegram API error: {detail}")

    return {"ok": True}


@app.post("/api/settings/test-task-reminder")
async def test_task_reminder(conn: DB, current_user: CurrentUser):
    uid = current_user["id"]
    token = await db.get_user_setting(conn, uid, "telegram_bot_token") or await db.get_setting(conn, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id_raw = await db.get_user_setting(conn, uid, "telegram_reminder_chat_id") or await db.get_setting(conn, "telegram_reminder_chat_id") or os.environ.get("TELEGRAM_REMINDER_CHAT_ID", "0")
    chat_id = int(chat_id_raw) if chat_id_raw.strip() else 0

    if not token:
        raise HTTPException(status_code=400, detail="Bot token is not configured")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Reminder chat ID is not configured")

    char_prompt = await db.get_user_setting(conn, current_user["id"], "character_prompt") or _DEFAULT_CHARACTER_PROMPT
    today = date.today().isoformat()
    async with conn.execute(
        "SELECT title, reminder_at FROM notes"
        " WHERE user_id = ? AND reminder_at <= ? AND reminder_done = 0 ORDER BY reminder_at",
        (current_user["id"], today),
    ) as cur:
        rows = await cur.fetchall()

    if not settings.summary_base_url:
        if not rows:
            text = "Nothing due today. Well done."
        else:
            task_lines = [f"- {'Overdue' if r['reminder_at'] < today else 'Due today'} ({r['reminder_at']}): {r['title']}" for r in rows]
            text = "Due tasks:\n" + "\n".join(task_lines)
    elif not rows:
        messages = [
            {"role": "system", "content": (
                f"{char_prompt} The user has no overdue or due tasks today — they are all caught up. "
                "Send a brief, genuine well-done (1-2 sentences). Warm but understated. No bullet points."
            )},
            {"role": "user", "content": "I have no tasks due today."},
        ]
        async with httpx.AsyncClient(timeout=30.0) as client:
            llm_resp = await client.post(
                f"{settings.summary_base_url}/v1/chat/completions",
                json={"model": settings.summary_model, "messages": messages, "stream": False},
            )
            if llm_resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"LLM API error: {llm_resp.text}")
            text = llm_resp.json()["choices"][0]["message"]["content"]
    else:
        task_lines = [f"- {'Overdue' if r['reminder_at'] < today else 'Due today'} ({r['reminder_at']}): {r['title']}" for r in rows]
        task_list = "Here are my due and overdue tasks:\n" + "\n".join(task_lines)
        messages = [
            {"role": "system", "content": (
                f"{char_prompt} You are sending a proactive scheduled reminder via Telegram. "
                "Summarize the provided due tasks — helpful, brief, and a touch wry. "
                "Be a little opinionated about what they should tackle first. No bullet points."
            )},
            {"role": "user", "content": task_list},
        ]
        async with httpx.AsyncClient(timeout=60.0) as client:
            llm_resp = await client.post(
                f"{settings.summary_base_url}/v1/chat/completions",
                json={"model": settings.summary_model, "messages": messages, "stream": False},
            )
            if llm_resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"LLM API error: {llm_resp.text}")
            text = llm_resp.json()["choices"][0]["message"]["content"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        tg_resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    if tg_resp.status_code != 200:
        detail = tg_resp.json().get("description", "unknown error")
        raise HTTPException(status_code=502, detail=f"Telegram API error: {detail}")

    return {"ok": True}


@app.post("/api/settings/test-journal-reminder")
async def test_journal_reminder(conn: DB, current_user: CurrentUser):
    uid = current_user["id"]
    token = await db.get_user_setting(conn, uid, "telegram_bot_token") or await db.get_setting(conn, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id_raw = await db.get_user_setting(conn, uid, "telegram_reminder_chat_id") or await db.get_setting(conn, "telegram_reminder_chat_id") or os.environ.get("TELEGRAM_REMINDER_CHAT_ID", "0")
    chat_id = int(chat_id_raw) if chat_id_raw.strip() else 0

    if not token:
        raise HTTPException(status_code=400, detail="Bot token is not configured")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Reminder chat ID is not configured")
    if not settings.summary_base_url:
        raise HTTPException(status_code=400, detail="SUMMARY_BASE_URL is not configured")

    char_prompt = await db.get_user_setting(conn, current_user["id"], "character_prompt") or _DEFAULT_CHARACTER_PROMPT
    today = date.today().isoformat()
    async with conn.execute(
        "SELECT COUNT(*) FROM notes WHERE user_id = ? AND folder = 'Journal' AND substr(created_at, 1, 10) = ?",
        (current_user["id"], today),
    ) as cur:
        row = await cur.fetchone()
    has_entry = (row[0] if row else 0) > 0

    if has_entry:
        messages = [
            {"role": "system", "content": (
                f"{char_prompt} The user has already written their journal entry today. "
                "Send a brief, genuine well-done (1-2 sentences). Warm but understated. No bullet points."
            )},
            {"role": "user", "content": "I've written my journal entry for today."},
        ]
    else:
        messages = [
            {"role": "system", "content": (
                f"{char_prompt} The user has not written a journal entry today. "
                "Write a short nudge (2-3 sentences, under 100 words) encouraging them to "
                "take a few minutes and reflect on their day. Helpful, brief, and a touch wry. No bullet points."
            )},
            {"role": "user", "content": "Remind me to write my journal entry for today."},
        ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        llm_resp = await client.post(
            f"{settings.summary_base_url}/v1/chat/completions",
            json={"model": settings.summary_model, "messages": messages, "stream": False},
        )
        if llm_resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"LLM API error: {llm_resp.text}")
        text = llm_resp.json()["choices"][0]["message"]["content"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        tg_resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    if tg_resp.status_code != 200:
        detail = tg_resp.json().get("description", "unknown error")
        raise HTTPException(status_code=502, detail=f"Telegram API error: {detail}")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Share target (unauthenticated — identified by per-user share_key)
# ---------------------------------------------------------------------------

async def _get_share_user_id(conn: aiosqlite.Connection,
                              share_key: str | None) -> str | None:
    if share_key:
        user = await db.get_user_by_share_key(conn, share_key)
        if user:
            return user["id"]
    # Fall back to first user
    user = await db.get_first_user(conn)
    return user["id"] if user else None


@app.post("/api/share")
async def share(
    background_tasks: BackgroundTasks,
    title: str = Form(default=""),
    text: str = Form(default=""),
    url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    key: str | None = Query(default=None),
):
    token = str(uuid.uuid4())[:8]
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)

    async with aiosqlite.connect(settings.database_url) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")

        user_id = await _get_share_user_id(conn, key)
        if not user_id:
            return RedirectResponse(url="/share-handler?error=no_user", status_code=302)

        if file and file.filename:
            mime = file.content_type or "application/pdf"
            stem = Path(file.filename).stem.replace("_", " ").replace("-", " ")
            note = await db.create_note(conn, user_id, stem, "", [], "shared", note_type='attachment')
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
                conn, note_id=note.id, filename=file.filename,
                mime_type=mime, size_bytes=len(contents),
                stored_path=str(stored_path),
            )
            background_tasks.add_task(
                _pdf_pipeline, att.id, note.id, str(full_path), file.filename, user_id
            )
        elif url or text.strip().startswith(('http://', 'https://')):
            resolved_url = url or text.strip()
            hostname = _hostname(resolved_url)
            note_title = title.strip() or hostname
            is_youtube = get_youtube_video_id(resolved_url) is not None
            note_type = 'video' if is_youtube else 'url'
            mime_type = 'video/youtube' if is_youtube else 'text/html'
            note = await db.create_note(conn, user_id, note_title, resolved_url, [], "shared",
                                        note_type=note_type)
            att = await db.create_attachment(
                conn, note_id=note.id, filename=hostname,
                mime_type=mime_type, size_bytes=0, source_url=resolved_url,
            )
            background_tasks.add_task(_web_pipeline, att.id, note.id, resolved_url, user_id)
        else:
            note_title = title[:80].strip() or "Shared note"
            note = await db.create_note(conn, user_id, note_title, text, [], "shared")
            background_tasks.add_task(
                _index_note, note.id, note.title, note.content, note.tags, note.folder, user_id
            )

    _purge_expired_shares()
    _pending_share[token] = {"note_id": note.id, "expires_at": expires}

    return RedirectResponse(url=f"/share-handler?token={token}", status_code=302)


@app.get("/api/share/pending")
async def share_pending(token: str = Query(...)):
    _purge_expired_shares()
    entry = _pending_share.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Token not found or expired")
    note_id = entry["note_id"]
    del _pending_share[token]
    return {"note_id": note_id, "status": "ready"}


@app.post("/api/share/finalize")
async def share_finalize(
    request: Request,
    conn: DB,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    body = await request.json()
    token = body.get("token")
    action = body.get("action")
    target_note_id = body.get("note_id")

    _purge_expired_shares()
    entry = _pending_share.get(token)
    if not entry or entry.get("type") != "image_pending":
        raise HTTPException(status_code=404, detail="Token not found or expired")
    if entry["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden")

    att_id    = entry["att_id"]
    filename  = entry["filename"]
    mime_type = entry["mime_type"]
    tmp_path  = Path(entry["tmp_path"])
    ext       = _IMAGE_EXT.get(mime_type, ".bin")

    if action == "new":
        stem = Path(filename).stem.replace("_", " ").replace("-", " ")
        note = await db.create_note(conn, current_user["id"], stem, "", [], "shared",
                                    note_type="attachment")
        note_id = note.id
    elif action == "attach":
        if not target_note_id:
            raise HTTPException(status_code=400, detail="note_id required for attach")
        note = await db.get_note(conn, target_note_id, user_id=current_user["id"])
        if not note:
            raise HTTPException(status_code=404, detail="Note not found")
        note_id = target_note_id
    else:
        raise HTTPException(status_code=400, detail="action must be 'new' or 'attach'")

    note_dir = ATTACHMENT_DIR / note_id
    note_dir.mkdir(parents=True, exist_ok=True)
    dest_path = note_dir / f"{att_id}{ext}"
    tmp_path.rename(dest_path)
    stored_path = str(Path(note_id) / f"{att_id}{ext}")

    att = await db.create_attachment(
        conn, note_id=note_id, filename=filename,
        mime_type=mime_type, size_bytes=dest_path.stat().st_size,
        stored_path=stored_path,
    )

    del _pending_share[token]
    return {"note_id": note_id, "att_id": att.id}


@app.get("/share-handler")
async def share_handler():
    return FileResponse(str(FRONTEND_DIR / "share.html"))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    result: dict = {}
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("SELECT 1")
        result["sqlite"] = "ok"
    except Exception as exc:
        result["sqlite"] = f"error: {exc}"
    try:
        await vector_store.get_collection()
        result["chroma"] = "ok"
    except Exception as exc:
        result["chroma"] = f"error: {exc}"
    try:
        await embeddings.embed_texts(["ping"])
        result["embedding"] = "ok"
    except Exception as exc:
        result["embedding"] = f"error: {exc}"
    overall = "ok" if all(v == "ok" for v in result.values()) else "degraded"
    result["status"] = overall
    return result


# ---------------------------------------------------------------------------
# Dynamic routes — service_worker.js and manifest.json
# (must be defined before the StaticFiles mount)
# ---------------------------------------------------------------------------

@app.get("/service_worker.js", include_in_schema=False)
async def service_worker():
    return Response(
        content=_sw_template.replace("'__APP_VERSION__'", f"'{_app_version}'"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


def _build_user_manifest(share_key: str) -> dict:
    share_url = f"{settings.app_base_url}/api/share?key={share_key}"
    manifest_data = dict(_BASE_MANIFEST)
    manifest_data["share_target"] = {
        "action": share_url,
        "method": "POST",
        "enctype": "multipart/form-data",
        "params": {
            "title": "title",
            "text": "text",
            "url": "url",
            "files": [{"name": "file", "accept": ["application/pdf"]}],
        },
    }
    return manifest_data


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    """Base manifest — no share_target. Used for PWA install prompt before login."""
    return JSONResponse(content=_BASE_MANIFEST, media_type="application/manifest+json")


@app.get("/manifest/{share_key}.json", include_in_schema=False)
async def user_manifest_by_key(share_key: str, conn: DB):
    """Stable per-user manifest with share_target. Safe to set as <link rel=manifest> after login."""
    user = await db.get_user_by_share_key(conn, share_key)
    if not user:
        raise HTTPException(status_code=404)
    return JSONResponse(content=_build_user_manifest(share_key), media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/manifest", include_in_schema=False)
async def api_manifest(current_user: CurrentUser, conn: DB):
    """Returns the current user's share_key so the frontend can build their manifest URL."""
    user = await db.get_user_by_id(conn, current_user["id"])
    if not user:
        raise HTTPException(status_code=404)
    return {"share_key": user["share_key"]}


# ---------------------------------------------------------------------------
# Static frontend (must be last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
