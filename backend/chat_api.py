from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import aiosqlite
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import embeddings, vector_store
from .chunker import chunk_text
from .config import settings
from .models import FOLDERS

logger = logging.getLogger(__name__)

_DEFAULT_CHARACTER_PROMPT = (
    "You are Alfred, a dry witty butler assistant. "
    "You are helpful but value the user's time, so you keep banter quick and dry."
)

# ---------------------------------------------------------------------------
# Persistent LLM HTTP client (reuses TCP connections across requests)
# ---------------------------------------------------------------------------

_llm_client: httpx.AsyncClient | None = None


def _get_llm_client() -> httpx.AsyncClient:
    global _llm_client
    if _llm_client is None or _llm_client.is_closed:
        _llm_client = httpx.AsyncClient(timeout=120.0)
    return _llm_client


async def close_llm_client() -> None:
    global _llm_client
    if _llm_client and not _llm_client.is_closed:
        await _llm_client.aclose()
        _llm_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_llm_client()
    await embeddings.close_client()


app = FastAPI(title="NoterAI RAG Chat", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_url() -> str:
    base = settings.chat_llm_base_url or settings.summary_base_url
    if not base:
        raise RuntimeError(
            "No LLM configured. Set CHAT_LLM_BASE_URL (or SUMMARY_BASE_URL) in .env"
        )
    return f"{base}/v1/chat/completions"


def _llm_model() -> str:
    return settings.chat_llm_model or settings.summary_model


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if settings.summary_api_key:
        h["Authorization"] = f"Bearer {settings.summary_api_key}"
    return h


def _router_url() -> str | None:
    if not settings.tool_router_base_url:
        return None
    return f"{settings.tool_router_base_url.rstrip('/')}/v1/chat/completions"


def _router_headers() -> dict:
    return {"Content-Type": "application/json"}


async def _get_due_reminders(user_id: str | None = None) -> tuple[str, list[dict]]:
    """Return (system_prompt_text, reminders_list) for due/overdue reminders."""
    today = date.today().isoformat()
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            if user_id:
                async with conn.execute(
                    "SELECT id, title, reminder_at FROM notes "
                    "WHERE user_id = ? AND reminder_at <= ? AND reminder_done = 0 ORDER BY reminder_at",
                    (user_id, today),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    "SELECT id, title, reminder_at FROM notes "
                    "WHERE reminder_at <= ? AND reminder_done = 0 ORDER BY reminder_at",
                    (today,),
                ) as cur:
                    rows = await cur.fetchall()
        if not rows:
            return "", []
        items = [{"id": r["id"], "title": r["title"], "reminder_at": r["reminder_at"]} for r in rows]
        titles = ", ".join(f'"{r["title"]}"' for r in rows)
        system_text = (
            f"## Reminders due today or overdue\n"
            f"The user has {len(items)} pending reminder(s): {titles}. "
            "Please mention these at the start of your response."
        )
        return system_text, items
    except Exception:
        logger.warning("Failed to fetch due reminders", exc_info=True)
        return "", []


async def _get_character_prompt(user_id: str | None = None) -> str:
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            if user_id:
                async with conn.execute(
                    "SELECT value FROM user_settings WHERE user_id = ? AND key = 'character_prompt'",
                    (user_id,),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'character_prompt'"
                ) as cur:
                    row = await cur.fetchone()
            if row and row[0].strip():
                return row[0].strip()
    except Exception:
        pass
    return _DEFAULT_CHARACTER_PROMPT


def _format_reminders_md(reminders: list[dict]) -> str:
    if not reminders:
        return ""
    base = settings.app_base_url.rstrip("/")
    lines = ["", "---", "**Reminders**"]
    for r in reminders:
        link = f"{base}/?note={r['id']}"
        lines.append(f"- [{r['title']}]({link}) — due {r['reminder_at']}")
    return "\n".join(lines)


_CLASSIFY_SYSTEM = (
    "You are a folder classifier. Given a user's query, return a JSON array of the most relevant "
    "folder names from this exact list: " + ", ".join(FOLDERS) + ".\n\n"
    "Return only folders clearly relevant to the query's intent. "
    "Return [] if the query is general or spans many folders.\n\n"
    "Examples:\n"
    "- 'what should I work on / tasks / action items' → [\"Todo\"]\n"
    "- 'how did I feel / past experiences / last week' → [\"Journal\"]\n"
    "- 'how does X work / what is X / specs / facts' → [\"Reference\"]\n"
    "- 'archived / old notes / past projects' → [\"Archive\"]\n"
    "- General or cross-cutting questions → []\n\n"
    "Respond with ONLY a valid JSON array, nothing else."
)


async def _classify_folders(query: str) -> list[str]:
    """Ask the LLM which folders are relevant to this query. Returns [] on any error."""
    try:
        client = _get_llm_client()
        payload = {
            "model": _llm_model(),
            "messages": [
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": query},
            ],
            "max_tokens": 60,
            "temperature": 0.0,
        }
        resp = await client.post(_llm_url(), json=payload, headers=_headers())
        text = resp.json()["choices"][0]["message"]["content"].strip()
        folders = json.loads(text)
        if not isinstance(folders, list):
            return []
        valid = [f for f in folders if f in FOLDERS]
        logger.info("Folder classification for query %r → %s", query[:60], valid)
        return valid
    except Exception:
        logger.warning("Folder classification failed, using default filter", exc_info=True)
        return []


async def _retrieve_context(query: str, user_id: str | None = None) -> tuple[str, list[dict]]:
    """Embed the query, search ChromaDB, return (context_string, numbered_sources)."""
    try:
        emb_result, folders = await asyncio.gather(
            embeddings.embed_texts([query]),
            _classify_folders(query),
        )
        emb = emb_result[0]

        conditions: list[dict] = []
        if user_id:
            conditions.append({"user_id": {"$eq": user_id}})
        if folders:
            conditions.append({"folder": {"$in": folders}})
        else:
            conditions.append({"folder": {"$ne": "Archive"}})

        where: dict | None = {"$and": conditions} if len(conditions) > 1 else (conditions[0] if conditions else None)

        chunks = await vector_store.query(emb, settings.chat_n_results, where=where)
        if not chunks:
            return "", []
        parts = []
        sources: list[dict] = []
        source_index: dict[str, int] = {}  # note_id → 1-based number
        for chunk in chunks:
            meta = chunk["metadata"]
            label = meta.get("source_label") or meta.get("note_id", "Note")
            note_id = meta.get("note_id", "")
            if note_id and note_id not in source_index:
                source_index[note_id] = len(sources) + 1
                sources.append({
                    "note_id": note_id,
                    "label": label,
                    "source_url": meta.get("source_url") or "",
                })
            n = source_index.get(note_id, "?")
            parts.append(f"[{n}] {label}\n{chunk['document']}")
        return "\n\n---\n\n".join(parts), sources
    except Exception:
        logger.warning("RAG retrieval failed", exc_info=True)
        return "", []


def _filter_sources_by_citations(text: str, sources: list[dict]) -> list[dict]:
    """Return only sources whose [N] citation number appears in the response text."""
    cited = {int(n) for n in re.findall(r'\[(\d+)\]', text)}
    return [s for i, s in enumerate(sources, 1) if i in cited]


def _format_sources_md(sources: list[dict]) -> str:
    if not sources:
        return ""
    base = settings.app_base_url.rstrip("/")
    lines = ["", "---", "**Sources**"]
    for s in sources:
        note_link = f"{base}/?note={s['note_id']}"
        line = f"- [{s['label']}]({note_link})"
        if s["source_url"]:
            line += f" · [original]({s['source_url']})"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# List management tools (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_LIST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_list_items",
            "description": "Get all items in a specific list note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list note ID"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_list_item",
            "description": "Add a new item to a list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list note ID"},
                    "content": {"type": "string", "description": "Text of the new item"},
                },
                "required": ["note_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_list_item",
            "description": "Mark a list item as complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list note ID"},
                    "item_id": {"type": "string", "description": "The item ID to mark complete"},
                },
                "required": ["note_id", "item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_list_item",
            "description": "Remove an item from a list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list note ID"},
                    "item_id": {"type": "string", "description": "The item ID to delete"},
                },
                "required": ["note_id", "item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_list",
            "description": "Create a new list note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Name for the new list"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a new markdown note. Use when the user wants to save information, thoughts, or reference material.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Note body in markdown"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "Create a reminder that fires at a specific date/time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short reminder title"},
                    "due_date": {"type": "string", "description": "When the reminder fires — natural language ('tomorrow', 'next Monday', 'in 2 hours') or ISO 8601 datetime"},
                    "content": {"type": "string", "description": "Optional body text for the reminder"},
                },
                "required": ["title", "due_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_journal_entry",
            "description": "Create a journal entry in the Journal folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Journal entry body in markdown"},
                },
                "required": [],
            },
        },
    },
]


_ROUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_list_item",
            "description": "Add an item to an existing list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list to add the item to (e.g. grocery, shopping, todo, chores)"},
                    "content": {"type": "string", "description": "The item to add to the list"},
                },
                "required": ["note_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_list_items",
            "description": "Retrieve all items from a list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list to retrieve items from"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_list_item",
            "description": "Mark an item on a list as complete/done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list the item is on"},
                    "item_id": {"type": "string", "description": "The item to mark as complete (matched via fuzzy search)"},
                },
                "required": ["note_id", "item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_list_item",
            "description": "Remove an item from a list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "The list to remove the item from"},
                    "item_id": {"type": "string", "description": "The item to remove (matched via fuzzy search)"},
                },
                "required": ["note_id", "item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_list",
            "description": "Create a new list, optionally with an initial item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The name of the new list"},
                    "item_id": {"type": "string", "description": "Optional initial item to add to the list"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a new markdown note to save information or thoughts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Note body in markdown"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "Create a reminder that fires at a specific date/time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short reminder title"},
                    "due_date": {"type": "string", "description": "When the reminder fires — natural language ('tomorrow', 'next Monday', 'in 2 hours') or ISO 8601 datetime"},
                    "content": {"type": "string", "description": "Optional body text for the reminder"},
                },
                "required": ["title", "due_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_journal_entry",
            "description": "Create a journal entry in the Journal folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Journal entry body in markdown"},
                },
                "required": [],
            },
        },
    },
]


def resolve_due_date(raw: str) -> str:
    """Convert natural language or ISO date to ISO datetime string."""
    import dateparser
    dp_settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()}
    try:
        parsed = dateparser.parse(raw, languages=["en"], settings=dp_settings)
        if parsed is None:
            stripped = re.sub(r"^(next|this|on|by)\s+", "", raw.strip(), flags=re.I)
            if stripped != raw.strip():
                parsed = dateparser.parse(stripped, languages=["en"], settings=dp_settings)
        if parsed:
            return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return raw


def _extract_date_from_user_message(text: str) -> str | None:
    """Extract and resolve the first date/time phrase found in a user message.

    Used to override unreliable model-generated due_date values.
    """
    import dateparser.search
    dp_settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()}
    try:
        results = dateparser.search.search_dates(text, languages=["en"], settings=dp_settings)
        if results:
            _phrase, parsed_dt = results[0]
            return parsed_dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return None


async def _check_list_access(conn: aiosqlite.Connection, note_id: str,
                              user_id: str) -> bool:
    async with conn.execute(
        "SELECT id FROM notes WHERE id = ? AND note_type = 'list' AND ("
        "  user_id = ? OR id IN (SELECT note_id FROM note_shares WHERE shared_with_user_id = ?)"
        ")",
        (note_id, user_id, user_id),
    ) as cur:
        return await cur.fetchone() is not None


async def _tool_get_list_items(note_id: str, user_id: str) -> str:
    logger.debug("_tool_get_list_items note_id=%s user=%s", note_id, user_id)
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            if not await _check_list_access(conn, note_id, user_id):
                logger.warning("_tool_get_list_items: access denied note_id=%s user=%s", note_id, user_id)
                return json.dumps({"error": "Note not found or access denied"})
            async with conn.execute(
                "SELECT id, content, completed, position FROM list_items"
                " WHERE note_id = ? ORDER BY position ASC, created_at ASC",
                (note_id,),
            ) as cur:
                rows = await cur.fetchall()
        items = [
            {"id": r["id"], "content": r["content"],
             "completed": bool(r["completed"]), "position": r["position"]}
            for r in rows
        ]
        return json.dumps({"items": items})
    except Exception as exc:
        logger.error("_tool_get_list_items failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _tool_add_list_item(note_id: str, content: str, user_id: str) -> str:
    logger.debug("_tool_add_list_item note_id=%s content=%r user=%s", note_id, content, user_id)
    try:
        now = datetime.now(timezone.utc).isoformat()
        item_id = str(uuid.uuid4())
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            if user_id and not await _check_list_access(conn, note_id, user_id):
                logger.warning("_tool_add_list_item: access denied note_id=%s user=%s", note_id, user_id)
                return json.dumps({"error": "Note not found or access denied"})
            async with conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM list_items WHERE note_id = ?",
                (note_id,),
            ) as cur:
                row = await cur.fetchone()
                position = row[0] if row else 0
            await conn.execute(
                "INSERT INTO list_items (id, note_id, content, completed, position, created_at)"
                " VALUES (?,?,?,0,?,?)",
                (item_id, note_id, content, position, now),
            )
            await conn.execute(
                "UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id)
            )
            await conn.commit()
        return json.dumps({"item_id": item_id, "content": content, "position": position})
    except Exception as exc:
        logger.error("_tool_add_list_item failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _tool_complete_list_item(note_id: str, item_id: str, user_id: str) -> str:
    logger.debug("_tool_complete_list_item note_id=%s item_id=%s user=%s", note_id, item_id, user_id)
    try:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            if not await _check_list_access(conn, note_id, user_id):
                logger.warning("_tool_complete_list_item: access denied note_id=%s user=%s", note_id, user_id)
                return json.dumps({"error": "Note not found or access denied"})
            cur = await conn.execute(
                "UPDATE list_items SET completed = 1 WHERE id = ? AND note_id = ?",
                (item_id, note_id),
            )
            if cur.rowcount == 0:
                logger.warning("_tool_complete_list_item: item not found item_id=%s note_id=%s", item_id, note_id)
                return json.dumps({"error": "Item not found"})
            await conn.execute(
                "UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id)
            )
            await conn.commit()
        return json.dumps({"success": True, "item_id": item_id})
    except Exception as exc:
        logger.error("_tool_complete_list_item failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _tool_delete_list_item(note_id: str, item_id: str, user_id: str) -> str:
    logger.debug("_tool_delete_list_item note_id=%s item_id=%s user=%s", note_id, item_id, user_id)
    try:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            if not await _check_list_access(conn, note_id, user_id):
                logger.warning("_tool_delete_list_item: access denied note_id=%s user=%s", note_id, user_id)
                return json.dumps({"error": "Note not found or access denied"})
            cur = await conn.execute(
                "DELETE FROM list_items WHERE id = ? AND note_id = ?",
                (item_id, note_id),
            )
            if cur.rowcount == 0:
                logger.warning("_tool_delete_list_item: item not found item_id=%s note_id=%s", item_id, note_id)
                return json.dumps({"error": "Item not found"})
            await conn.execute(
                "UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id)
            )
            await conn.commit()
        return json.dumps({"success": True, "item_id": item_id})
    except Exception as exc:
        logger.error("_tool_delete_list_item failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _tool_create_list(title: str, user_id: str, item_id: str | None = None) -> str:
    logger.debug("_tool_create_list title=%r item_id=%r user=%s", title, item_id, user_id)
    try:
        note_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute(
                "INSERT INTO notes"
                " (id, user_id, title, content, tags, folder, created_at, updated_at, note_type, indexed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (note_id, user_id, title, "", "[]", "Lists", now, now, "list", now),
            )
            if item_id:
                item_uuid = str(uuid.uuid4())
                await conn.execute(
                    "INSERT INTO list_items (id, note_id, content, completed, position, created_at)"
                    " VALUES (?,?,?,0,0,?)",
                    (item_uuid, note_id, item_id, now),
                )
            await conn.commit()
        result = {"note_id": note_id, "title": title}
        if item_id:
            result["item_id"] = item_uuid  # type: ignore[assignment]
        return json.dumps(result)
    except Exception as exc:
        logger.error("_tool_create_list failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _index_note_inline(note_id: str, title: str, content: str,
                              user_id: str, folder: str,
                              tags: list[str] | None = None) -> None:
    if not content.strip():
        return
    try:
        chunks = chunk_text(content)
        if not chunks:
            return
        texts = [c["text"] for c in chunks]
        embs = await embeddings.embed_texts(texts)
        ids = [f"{note_id}_{c['chunk_index']}" for c in chunks]
        metas = [
            {
                "note_id": note_id,
                "user_id": user_id,
                "chunk_index": c["chunk_index"],
                "title": title,
                "tags": json.dumps(tags or []),
                "folder": folder,
                "source_type": "note",
                "source_label": title,
            }
            for c in chunks
        ]
        await vector_store.upsert(texts, embs, metas, ids)
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("UPDATE notes SET indexed_at = ? WHERE id = ?", (now, note_id))
            await conn.commit()
    except Exception:
        logger.warning("_index_note_inline failed for note %s", note_id, exc_info=True)


def _title_from_content(content: str) -> str:
    for line in content.splitlines():
        stripped = line.lstrip("#").strip()
        if stripped:
            return stripped[:60]
    return "Untitled"


async def _tool_create_note(content: str | None, user_id: str) -> str:
    logger.debug("_tool_create_note user=%s", user_id)
    if content is None:
        return json.dumps({"needs_input": "content", "prompt": "What should the note say?"})
    try:
        note_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        title = _title_from_content(content)
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute(
                "INSERT INTO notes"
                " (id, user_id, title, content, tags, folder, created_at, updated_at, note_type)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (note_id, user_id, title, content, "[]", "Unfiled", now, now, "markdown"),
            )
            await conn.commit()
        await _index_note_inline(note_id, title, content, user_id, "Unfiled")
        return json.dumps({"note_id": note_id, "title": title})
    except Exception as exc:
        logger.error("_tool_create_note failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _tool_create_reminder(title: str, due_date: str, content: str, user_id: str) -> str:
    logger.debug("_tool_create_reminder title=%r due_date=%r user=%s", title, due_date, user_id)
    try:
        note_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute(
                "INSERT INTO notes"
                " (id, user_id, title, content, tags, folder, created_at, updated_at, note_type, reminder_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (note_id, user_id, title, content, "[]", "Unfiled", now, now, "markdown", due_date),
            )
            await conn.commit()
        if content:
            await _index_note_inline(note_id, title, content, user_id, "Unfiled")
        return json.dumps({"note_id": note_id, "title": title, "reminder_at": due_date})
    except Exception as exc:
        logger.error("_tool_create_reminder failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _tool_create_journal_entry(content: str | None, user_id: str) -> str:
    logger.debug("_tool_create_journal_entry user=%s", user_id)
    if content is None:
        return json.dumps({"needs_input": "content", "prompt": "What would you like to journal?"})
    try:
        note_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        title = _title_from_content(content)
        async with aiosqlite.connect(settings.database_url) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute(
                "INSERT INTO notes"
                " (id, user_id, title, content, tags, folder, created_at, updated_at, note_type)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (note_id, user_id, title, content, "[]", "Journal", now, now, "markdown"),
            )
            await conn.commit()
        if content:
            await _index_note_inline(note_id, title, content, user_id, "Journal")
        return json.dumps({"note_id": note_id, "title": title})
    except Exception as exc:
        logger.error("_tool_create_journal_entry failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


async def _execute_tool(name: str, arguments: dict, user_id: str | None) -> str:
    if name in ("get_list_items", "add_list_item", "complete_list_item",
                "delete_list_item", "create_list"):
        if not user_id:
            return json.dumps({"error": "No user context"})
    if name == "get_list_items":
        return await _tool_get_list_items(arguments.get("note_id", ""), user_id)  # type: ignore[arg-type]
    if name == "add_list_item":
        return await _tool_add_list_item(
            arguments.get("note_id", ""), arguments.get("content", ""), user_id  # type: ignore[arg-type]
        )
    if name == "complete_list_item":
        return await _tool_complete_list_item(
            arguments.get("note_id", ""), arguments.get("item_id", ""), user_id  # type: ignore[arg-type]
        )
    if name == "delete_list_item":
        return await _tool_delete_list_item(
            arguments.get("note_id", ""), arguments.get("item_id", ""), user_id  # type: ignore[arg-type]
        )
    if name == "create_list":
        return await _tool_create_list(arguments.get("title", ""), user_id, arguments.get("item_id"))  # type: ignore[arg-type]
    if name == "create_note":
        if not user_id:
            return json.dumps({"error": "No user context"})
        return await _tool_create_note(arguments.get("content"), user_id)
    if name == "create_reminder":
        if not user_id:
            return json.dumps({"error": "No user context"})
        return await _tool_create_reminder(
            arguments.get("title", ""),
            resolve_due_date(arguments.get("due_date", "")),
            arguments.get("content", ""), user_id,
        )
    if name == "create_journal_entry":
        if not user_id:
            return json.dumps({"error": "No user context"})
        return await _tool_create_journal_entry(arguments.get("content"), user_id)
    return json.dumps({"error": f"Unknown tool: {name}"})


async def _resolve_list_name(name_or_id: str, user_id: str) -> str | None:
    """Return the list UUID for a given name or UUID. UUID match is tried first."""
    async with aiosqlite.connect(settings.database_url) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id FROM notes WHERE id = ? AND note_type = 'list' AND ("
            "  user_id = ? OR id IN (SELECT note_id FROM note_shares WHERE shared_with_user_id = ?)"
            ")",
            (name_or_id, user_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["id"]
        async with conn.execute(
            "SELECT id FROM notes WHERE note_type = 'list'"
            " AND title LIKE ? COLLATE NOCASE"
            " AND (user_id = ? OR id IN (SELECT note_id FROM note_shares WHERE shared_with_user_id = ?))"
            " ORDER BY updated_at DESC LIMIT 1",
            (f"%{name_or_id}%", user_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row["id"] if row else None


async def _resolve_item_fuzzy(note_id: str, query: str) -> str | None:
    """Return an item UUID by substring content match, or exact UUID match as fallback."""
    async with aiosqlite.connect(settings.database_url) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id FROM list_items WHERE note_id = ? AND content LIKE ? COLLATE NOCASE LIMIT 1",
            (note_id, f"%{query}%"),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["id"]
        async with conn.execute(
            "SELECT id FROM list_items WHERE id = ? AND note_id = ?",
            (query, note_id),
        ) as cur:
            row = await cur.fetchone()
            return row["id"] if row else None


async def _execute_router_tool(name: str, arguments: dict, user_id: str | None) -> str:
    """Execute a router tool call, resolving list names and fuzzy item references to UUIDs."""
    if not user_id:
        return json.dumps({"error": "No user context"})

    raw_note_id = arguments.get("note_id", "")

    if name in ("get_list_items", "add_list_item", "complete_list_item", "delete_list_item"):
        note_id = await _resolve_list_name(raw_note_id, user_id)
        if not note_id:
            return json.dumps({"error": f"List not found: {raw_note_id!r}"})

        if name == "get_list_items":
            return await _tool_get_list_items(note_id, user_id)

        if name == "add_list_item":
            return await _tool_add_list_item(note_id, arguments.get("content", ""), user_id)

        raw_item = arguments.get("item_id", "")
        item_id = await _resolve_item_fuzzy(note_id, raw_item)
        if not item_id:
            return json.dumps({"error": f"Item not found: {raw_item!r}"})

        if name == "complete_list_item":
            return await _tool_complete_list_item(note_id, item_id, user_id)
        if name == "delete_list_item":
            return await _tool_delete_list_item(note_id, item_id, user_id)

    if name == "create_list":
        return await _tool_create_list(arguments.get("title", ""), user_id, arguments.get("item_id"))
    if name == "create_note":
        return await _tool_create_note(arguments.get("content"), user_id)
    if name == "create_reminder":
        return await _tool_create_reminder(
            arguments.get("title", ""),
            resolve_due_date(arguments.get("due_date", "")),
            arguments.get("content", ""), user_id,
        )
    if name == "create_journal_entry":
        return await _tool_create_journal_entry(arguments.get("content"), user_id)

    return json.dumps({"error": f"Unknown tool: {name}"})


def _append_log_line(path: str, line: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


async def _log_router_interaction(
    router_messages: list[dict],
    rounds: list[dict],
    final_response: str | None,
    user_id: str | None,
) -> None:
    if not settings.tool_router_log:
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "messages": router_messages,
        "tool_rounds": rounds,
        "final_response": final_response,
    }
    line = json.dumps(entry) + "\n"
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _append_log_line, settings.tool_router_log, line)
    except Exception as exc:
        logger.warning("Router log write failed: %s", exc)


def _build_router_messages(original: list[dict]) -> list[dict]:
    non_system = [m for m in original if m.get("role") != "system"]
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%A, %B %-d %Y, %H:%M UTC")
    system_msg = {
        "role": "system",
        "content": (
            f"Current date and time: {now_str}. "
            "For due_date you MUST copy the user's exact words verbatim — "
            "do NOT compute or convert the date yourself. "
            "Examples: user says 'friday' → pass 'friday'; "
            "'next thursday' → pass 'next thursday'; "
            "'tomorrow morning' → pass 'tomorrow morning'. "
            "The system resolves the phrase automatically."
        ),
    }
    return [system_msg] + non_system[-settings.tool_router_context_messages:]


def _parse_xml_tool_calls(content: str) -> list[dict] | None:
    """Parse <tool_call> XML blocks that some models emit instead of structured tool_calls."""
    import re
    calls = []
    for block in re.finditer(r'<tool_call>(.*?)</tool_call>', content, re.DOTALL):
        text = block.group(1).strip()
        # JSON format: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
        try:
            obj = json.loads(text)
            if "name" in obj:
                calls.append({
                    "id": f"xml_{len(calls)}",
                    "type": "function",
                    "function": {
                        "name": obj["name"],
                        "arguments": json.dumps(obj.get("arguments", {})),
                    },
                })
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        # Parameter format: <function=name><parameter=key>value</parameter>...</function>
        fn_m = re.search(r'<function=(\w+)>(.*?)</function>', text, re.DOTALL)
        if fn_m:
            args = {
                pm.group(1): pm.group(2).strip()
                for pm in re.finditer(r'<parameter=(\w+)>(.*?)</parameter>', fn_m.group(2), re.DOTALL)
            }
            calls.append({
                "id": f"xml_{len(calls)}",
                "type": "function",
                "function": {"name": fn_m.group(1), "arguments": json.dumps(args)},
            })
    return calls or None


async def _run_tool_router(
    raw_messages: list[dict],
    user_id: str | None,
) -> tuple[list[dict], list[dict]] | None:
    """
    Pre-pass: call the Qwen3 router to handle all tool calls.
    Returns (augmented_messages, rounds_log) on success, or None on any
    failure (triggers fallback to existing agentic loop).
    rounds_log entries: {"round": N, "tool_calls": [...], "tool_results": [...]}
    """
    url = _router_url()
    if not url:
        return None

    router_msgs = _build_router_messages(raw_messages)
    accumulated: list[dict] = []
    rounds_log: list[dict] = []

    for _round in range(3):
        payload = {
            "model": settings.tool_router_model,
            "messages": router_msgs + accumulated,
            "tools": _ROUTER_TOOLS,
            "tool_choice": "auto",
            "temperature": 0.0,
        }
        try:
            client = _get_llm_client()
            resp = await client.post(url, json=payload, headers=_router_headers(), timeout=30.0)
        except httpx.TimeoutException:
            logger.warning("Tool router timed out on round %d, falling back", _round + 1)
            return None
        except httpx.RequestError as exc:
            logger.warning("Tool router request error: %s, falling back", exc)
            return None

        if resp.status_code != 200:
            logger.warning("Tool router HTTP %d on round %d, falling back", resp.status_code, _round + 1)
            return None

        try:
            data = resp.json()
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("Tool router malformed response: %s, falling back", exc)
            return None

        if not tool_calls:
            xml_calls = _parse_xml_tool_calls(message.get("content") or "")
            if xml_calls:
                import re as _re
                clean = _re.sub(r'<tool_call>.*?</tool_call>', '', message.get("content", ""),
                                flags=_re.DOTALL).strip()
                message = {**message, "content": clean or None, "tool_calls": xml_calls}
                tool_calls = xml_calls
                logger.info("Router [round %d]: parsed %d XML tool call(s)", _round + 1, len(xml_calls))
            else:
                return raw_messages + accumulated, rounds_log

        round_entry: dict = {"round": _round + 1, "tool_calls": [], "tool_results": []}
        accumulated.append(message)
        for tc in tool_calls:
            try:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"].get("arguments", "{}"))
            except (KeyError, json.JSONDecodeError) as exc:
                logger.warning("Tool router bad tool call: %s, falling back", exc)
                return None
            # For create_reminder, override due_date by extracting directly from the
            # user's message — the router model often computes wrong date phrases.
            if fn_name == "create_reminder":
                last_user = next(
                    (m["content"] for m in reversed(router_msgs) if m.get("role") == "user"),
                    "",
                )
                extracted = _extract_date_from_user_message(last_user)
                if extracted:
                    fn_args["due_date"] = extracted
            logger.info("Router [round %d]: %s args=%s user=%s", _round + 1, fn_name, fn_args, user_id)
            result = await _execute_router_tool(fn_name, fn_args, user_id)
            round_entry["tool_calls"].append({"name": fn_name, "arguments": fn_args})
            round_entry["tool_results"].append(result)
            accumulated.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })
        rounds_log.append(round_entry)

    return raw_messages + accumulated, rounds_log


def _build_messages(original: list[dict], context: str, reminders: str = "", character_prompt: str = "") -> list[dict]:
    now = datetime.now(timezone.utc).strftime("%A, %B %-d %Y, %H:%M UTC")
    char = character_prompt or _DEFAULT_CHARACTER_PROMPT
    system = (
        f"Current date and time: {now}\n\n"
        f"{char}\n"
        "I only want to discuss my notes if I ask about them. "
        "Use the context below — excerpts from their notes — to answer accurately. "
        "If the notes don't contain relevant information, say so and answer from general knowledge. "
        "If the answer from the notes is incomplete, say so and expand the thoughts from general knowledge."
    )
    if reminders:
        system += f"\n\n{reminders}"
    if context:
        system += (
            "\n\nWhen using information from the notes below, cite them inline as [1], [2], etc. "
            "Only cite a source if you actually used it in your answer.\n\n"
            f"## Relevant notes\n\n{context}"
        )
    # If the caller included a system message, append it as additional instructions.
    caller_system = next((m["content"] for m in original if m["role"] == "system"), None)
    if caller_system:
        system += f"\n\n{caller_system}"
    msgs = [{"role": "system", "content": system}]
    for m in original:
        if m["role"] != "system":
            msgs.append(m)
    return msgs


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "noterai-rag",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "noterai",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Chat completions
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "noterai-rag"
    messages: list[Message]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    skip_reminders: bool = False
    user_id: str | None = None


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest):
    user_msgs = [m for m in body.messages if m.role == "user"]
    query = user_msgs[-1].content if user_msgs else ""

    async def _no_context():
        return "", []

    async def _no_reminders():
        return "", []

    (context, sources), (reminders_text, reminders_list), character_prompt = await asyncio.gather(
        _retrieve_context(query, body.user_id) if query else _no_context(),
        _no_reminders() if body.skip_reminders else _get_due_reminders(body.user_id),
        _get_character_prompt(body.user_id),
    )
    reminders_md = _format_reminders_md(reminders_list)
    messages = _build_messages([m.model_dump() for m in body.messages], context, reminders_text, character_prompt)

    payload: dict = {"model": _llm_model(), "messages": messages}
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        payload["temperature"] = body.temperature

    if body.stream:
        payload["stream"] = True

        _stream_router_msgs: list[dict] = []
        _stream_rounds_log: list[dict] = []
        if body.user_id and settings.tool_router_base_url:
            raw_messages = [m.model_dump() for m in body.messages]
            router_result = await _run_tool_router(raw_messages, body.user_id)
            if router_result is not None:
                augmented, _stream_rounds_log = router_result
                _stream_router_msgs = _build_router_messages(raw_messages)
                payload["messages"] = _build_messages(augmented, context, reminders_text, character_prompt)

        async def _stream():
            buf = b""
            response_text = ""
            client = _get_llm_client()
            async with client.stream("POST", _llm_url(), json=payload, headers=_headers()) as resp:
                async for raw in resp.aiter_bytes():
                    buf += raw
                    lines = buf.split(b"\n")
                    buf = lines[-1]
                    for line in lines[:-1]:
                        if line.strip() == b"data: [DONE]":
                            used_sources = _filter_sources_by_citations(response_text, sources)
                            footer = _format_sources_md(used_sources) + reminders_md
                            if footer:
                                src_chunk = json.dumps({
                                    "id": "chatcmpl-src",
                                    "object": "chat.completion.chunk",
                                    "choices": [{"index": 0, "delta": {"content": footer}, "finish_reason": None}],
                                })
                                yield f"data: {src_chunk}\n\n".encode()
                            if _stream_rounds_log:
                                asyncio.create_task(_log_router_interaction(
                                    _stream_router_msgs, _stream_rounds_log,
                                    response_text or None, body.user_id,
                                ))
                            yield b"data: [DONE]\n\n"
                        else:
                            if line.startswith(b"data: "):
                                try:
                                    chunk_data = json.loads(line[6:])
                                    content = chunk_data["choices"][0]["delta"].get("content", "")
                                    if content:
                                        response_text += content
                                except Exception:
                                    pass
                            yield line + b"\n"
            if buf.strip():
                yield buf

        return StreamingResponse(_stream(), media_type="text/event-stream")

    client = _get_llm_client()

    # Tool-router pre-pass: small model handles tool decisions, main LLM only generates
    if body.user_id and settings.tool_router_base_url:
        raw_messages = [m.model_dump() for m in body.messages]
        router_result = await _run_tool_router(raw_messages, body.user_id)
        if router_result is not None:
            augmented, rounds_log = router_result
            payload["messages"] = _build_messages(augmented, context, reminders_text, character_prompt)
            resp = await client.post(_llm_url(), json=payload, headers=_headers())
            data = resp.json()
            response_text: str | None = None
            try:
                response_text = data["choices"][0]["message"].get("content") or ""
                used_sources = _filter_sources_by_citations(response_text, sources)
                footer = _format_sources_md(used_sources) + reminders_md
                if footer:
                    data["choices"][0]["message"]["content"] = response_text + footer
            except (KeyError, IndexError, TypeError):
                pass
            asyncio.create_task(_log_router_interaction(
                _build_router_messages(raw_messages), rounds_log, response_text, body.user_id,
            ))
            return JSONResponse(content=data, status_code=resp.status_code)

    # Fallback: existing agentic loop (used when router is not configured or fails)
    # Add list tools when we have a user context
    if body.user_id:
        payload["tools"] = _LIST_TOOLS
        payload["tool_choice"] = "auto"

    # Tool-calling loop (max 5 rounds to prevent runaway)
    for _round in range(5):
        resp = await client.post(_llm_url(), json=payload, headers=_headers())
        data = resp.json()
        if resp.status_code != 200:
            break
        try:
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
        except (KeyError, IndexError, TypeError):
            break

        if not tool_calls:
            # Final text response
            try:
                response_text = message.get("content") or ""
                used_sources = _filter_sources_by_citations(response_text, sources)
                footer = _format_sources_md(used_sources) + reminders_md
                if footer:
                    data["choices"][0]["message"]["content"] = response_text + footer
            except (KeyError, IndexError, TypeError):
                pass
            break

        # Execute tool calls and feed results back
        payload["messages"].append(message)
        for tc in tool_calls:
            try:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"].get("arguments", "{}"))
            except (KeyError, json.JSONDecodeError):
                fn_name, fn_args = "unknown", {}
            logger.info("Tool call [round %d]: %s args=%s user=%s", _round + 1, fn_name, fn_args, body.user_id)
            result = await _execute_tool(fn_name, fn_args, body.user_id)
            try:
                result_data = json.loads(result)
                logger.info("Tool result [%s]: %s", fn_name, result_data)
            except Exception:
                logger.info("Tool result [%s]: %s", fn_name, result)
            payload["messages"].append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

    return JSONResponse(content=data, status_code=resp.status_code)
