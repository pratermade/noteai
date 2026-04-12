from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from .config import settings
from .models import NoteResponse, AttachmentResponse


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            folder TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            indexed_at TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            source_url TEXT,
            stored_path TEXT,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER,
            extracted_text TEXT,
            summary TEXT,
            extracted_at TEXT,
            indexed_at TEXT,
            extraction_error TEXT,
            created_at TEXT NOT NULL
        )
    """)
    await db.commit()

    # Migration: add summary column for databases created before this field existed
    async with db.execute("PRAGMA table_info(attachments)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "summary" not in cols:
        await db.execute("ALTER TABLE attachments ADD COLUMN summary TEXT")
        await db.commit()


def _row_to_note(row: aiosqlite.Row) -> NoteResponse:
    return NoteResponse(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        tags=json.loads(row["tags"]),
        folder=row["folder"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        indexed_at=row["indexed_at"],
    )


def _row_to_attachment(row: aiosqlite.Row) -> AttachmentResponse:
    return AttachmentResponse(
        id=row["id"],
        note_id=row["note_id"],
        filename=row["filename"],
        source_url=row["source_url"],
        stored_path=row["stored_path"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        page_count=row["page_count"],
        summary=row["summary"],
        extracted_at=row["extracted_at"],
        indexed_at=row["indexed_at"],
        extraction_error=row["extraction_error"],
        created_at=row["created_at"],
    )


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_url)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


# Notes

async def create_note(db: aiosqlite.Connection, title: str, content: str,
                      tags: list[str], folder: str) -> NoteResponse:
    note_id = str(uuid.uuid4())
    now = _now()
    await db.execute(
        "INSERT INTO notes (id, title, content, tags, folder, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (note_id, title, content, json.dumps(tags), folder, now, now),
    )
    await db.commit()
    return await get_note(db, note_id)


async def get_note(db: aiosqlite.Connection, note_id: str) -> NoteResponse | None:
    async with db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_note(row) if row else None


async def list_notes(db: aiosqlite.Connection, tag: str | None = None,
                     folder: str | None = None) -> list[NoteResponse]:
    query = "SELECT * FROM notes"
    params: list = []
    conditions = []
    if tag:
        # JSON array contains check
        conditions.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
        params.append(tag)
    if folder is not None:
        conditions.append("folder = ?")
        params.append(folder)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY updated_at DESC"
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_note(r) for r in rows]


async def update_note(db: aiosqlite.Connection, note_id: str, **fields) -> NoteResponse | None:
    if not fields:
        return await get_note(db, note_id)
    fields["updated_at"] = _now()
    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [note_id]
    await db.execute(f"UPDATE notes SET {sets} WHERE id = ?", values)
    await db.commit()
    return await get_note(db, note_id)


async def delete_note(db: aiosqlite.Connection, note_id: str) -> bool:
    cur = await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    await db.commit()
    return cur.rowcount > 0


async def set_note_indexed(db: aiosqlite.Connection, note_id: str) -> None:
    await db.execute("UPDATE notes SET indexed_at = ? WHERE id = ?", (_now(), note_id))
    await db.commit()


async def clear_note_indexed(db: aiosqlite.Connection, note_id: str) -> None:
    await db.execute("UPDATE notes SET indexed_at = NULL WHERE id = ?", (note_id,))
    await db.commit()


async def list_tags(db: aiosqlite.Connection) -> list[str]:
    async with db.execute(
        "SELECT DISTINCT value FROM notes, json_each(notes.tags) ORDER BY value"
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def list_folders(db: aiosqlite.Connection) -> list[str]:
    async with db.execute(
        "SELECT DISTINCT folder FROM notes WHERE folder != '' ORDER BY folder"
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


# Attachments

async def create_attachment(db: aiosqlite.Connection, note_id: str, filename: str,
                             mime_type: str, size_bytes: int,
                             stored_path: str | None = None,
                             source_url: str | None = None) -> AttachmentResponse:
    att_id = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """INSERT INTO attachments
           (id, note_id, filename, source_url, stored_path, mime_type, size_bytes, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (att_id, note_id, filename, source_url, stored_path, mime_type, size_bytes, now),
    )
    await db.commit()
    return await get_attachment(db, att_id)


async def get_attachment(db: aiosqlite.Connection, att_id: str) -> AttachmentResponse | None:
    async with db.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_attachment(row) if row else None


async def list_attachments(db: aiosqlite.Connection, note_id: str) -> list[AttachmentResponse]:
    async with db.execute(
        "SELECT * FROM attachments WHERE note_id = ? ORDER BY created_at DESC", (note_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_attachment(r) for r in rows]


async def update_attachment(db: aiosqlite.Connection, att_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [att_id]
    await db.execute(f"UPDATE attachments SET {sets} WHERE id = ?", values)
    await db.commit()


async def delete_attachment(db: aiosqlite.Connection, att_id: str) -> AttachmentResponse | None:
    att = await get_attachment(db, att_id)
    if att:
        await db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
        await db.commit()
    return att


async def get_attachment_extracted_text(db: aiosqlite.Connection, att_id: str) -> str | None:
    async with db.execute(
        "SELECT extracted_text FROM attachments WHERE id = ?", (att_id,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def list_indexed_attachments(db: aiosqlite.Connection,
                                    note_id: str) -> list[AttachmentResponse]:
    async with db.execute(
        "SELECT * FROM attachments WHERE note_id = ? AND extracted_at IS NOT NULL",
        (note_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_attachment(r) for r in rows]
