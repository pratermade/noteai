from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlparse

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile

from backend.core import database as db
from backend.core import wyoming_client
from backend.core.auth import CurrentUser, get_current_user
from backend.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_db():
    conn = await db.get_db()
    try:
        yield conn
    finally:
        await conn.close()


DB = Annotated[aiosqlite.Connection, Depends(_get_db)]


@router.post("/journal/dictate", status_code=201)
async def dictate_journal(
    audio: UploadFile,
    conn: DB,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
):
    audio_bytes = await audio.read()
    logger.info("dictate: received %d bytes, content_type=%s", len(audio_bytes), audio.content_type)
    try:
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
    from backend.core.main import _journal_pipeline
    await _journal_pipeline(note.id, current_user["id"])
    return {"id": note.id}
