from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from . import database as db, embeddings, vector_store
from .chunker import chunk_text
from .config import settings

logger = logging.getLogger(__name__)


async def _index_journal(journal_id: str, journal_title: str, content: str, user_id: str) -> None:
    await vector_store.delete_by_note_id(journal_id)
    chunks = chunk_text(content)
    if not chunks:
        return
    texts = [c["text"] for c in chunks]
    embs = await embeddings.embed_texts(texts)
    ids = [f"{journal_id}_{c['chunk_index']}" for c in chunks]
    metas = [
        {
            "note_id": journal_id,
            "user_id": user_id,
            "chunk_index": c["chunk_index"],
            "title": journal_title,
            "tags": "[]",
            "folder": "Journal",
            "source_type": "note",
            "source_label": journal_title,
        }
        for c in chunks
    ]
    await vector_store.upsert(texts, embs, metas, ids)
    now_s = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.database_url) as conn:
        await conn.execute("UPDATE notes SET indexed_at = ? WHERE id = ?", (now_s, journal_id))
        await conn.commit()


async def journal_log_note(note_id: str, note_title: str, note_folder: str, user_id: str) -> None:
    """Append a timestamped reference to a newly-created note in today's journal entry."""
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = 'server_timezone'",
                (user_id,),
            ) as cur:
                tz_row = await cur.fetchone()
        tz_str = (tz_row[0].strip() if tz_row and tz_row[0] else "") or os.environ.get("TZ", "")
        try:
            tz = ZoneInfo(tz_str) if tz_str else None
        except ZoneInfoNotFoundError:
            tz = None

        now = datetime.now(tz) if tz else datetime.now(timezone.utc)
        journal_title = f"Journal — {now.strftime('%B %-d, %Y')}"
        time_str = now.strftime("%H:%M")
        new_line = f"- {time_str} — Added [**{note_title}**](#note/{note_id}) *({note_folder})*"

        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            async with conn.execute(
                "SELECT id, content FROM notes WHERE user_id = ? AND folder = 'Journal' AND title = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (user_id, journal_title),
            ) as cur:
                row = await cur.fetchone()

            if row:
                journal_id = row["id"]
                new_content = (row["content"] or "").rstrip() + "\n" + new_line
                await conn.execute(
                    "UPDATE notes SET content = ?, updated_at = ? WHERE id = ?",
                    (new_content, datetime.now(timezone.utc).isoformat(), journal_id),
                )
                await conn.commit()
                await _index_journal(journal_id, journal_title, new_content, user_id)
            else:
                initial_content = f"## Notes\n\n{new_line}"
                journal_note = await db.create_note(
                    conn, user_id, journal_title, initial_content, [], "Journal"
                )
                await _index_journal(journal_note.id, journal_title, initial_content, user_id)

        logger.info("Journal log: note %s (%r) recorded for user %s", note_id, note_title, user_id)
    except Exception:
        logger.warning("Journal log failed for note %s", note_id, exc_info=True)
