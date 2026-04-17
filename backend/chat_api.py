from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import aiosqlite
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import embeddings, vector_store
from .config import settings
from .models import FOLDERS

logger = logging.getLogger(__name__)

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


_last_reminder_date: str | None = None


async def _get_daily_reminders() -> tuple[str, list[dict]]:
    """Return (system_prompt_text, reminders_list) for due reminders on first chat of the day."""
    global _last_reminder_date
    today = date.today().isoformat()
    if today == _last_reminder_date:
        return "", []
    _last_reminder_date = today
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
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
        logger.warning("Failed to fetch daily reminders", exc_info=True)
        return "", []


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
    "- 'my projects / project status' → [\"Project\"]\n"
    "- 'ideas / brainstorm' → [\"Ideas\"]\n"
    "- 'things to read / links to revisit' → [\"Review Later\"]\n"
    "- 'tools / templates / assets' → [\"Resources\"]\n"
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


async def _retrieve_context(query: str) -> tuple[str, list[dict]]:
    """Embed the query, search ChromaDB, return (context_string, numbered_sources)."""
    try:
        emb_result, folders = await asyncio.gather(
            embeddings.embed_texts([query]),
            _classify_folders(query),
        )
        emb = emb_result[0]

        if folders:
            where: dict | None = {"folder": {"$in": folders}}
        else:
            where = {"folder": {"$ne": "Archive"}}

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


def _build_messages(original: list[dict], context: str, reminders: str = "") -> list[dict]:
    now = datetime.now(timezone.utc).strftime("%A, %B %-d %Y, %H:%M UTC")
    system = (
        f"Current date and time: {now}\n\n"
        "You are a helpful butler/assistant with access to the user's personal notes. "
        "You are like Alfred from batman, very helpful but also you appreciate that my time is important so banter is kept quick and dry"
        "I only want to discuss my notes if I ask about them"
        "Use the context below — excerpts from their notes — to answer accurately. "
        "If the notes don't contain relevant information, say so and answer from general knowledge."
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


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest):
    # RAG: embed the last user message to retrieve relevant note chunks
    user_msgs = [m for m in body.messages if m.role == "user"]
    query = user_msgs[-1].content if user_msgs else ""

    async def _no_context():
        return "", []

    (context, sources), (reminders_text, reminders_list) = await asyncio.gather(
        _retrieve_context(query) if query else _no_context(),
        _get_daily_reminders(),
    )
    reminders_md = _format_reminders_md(reminders_list)
    messages = _build_messages([m.model_dump() for m in body.messages], context, reminders_text)

    payload: dict = {"model": _llm_model(), "messages": messages}
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        payload["temperature"] = body.temperature

    if body.stream:
        payload["stream"] = True

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
    resp = await client.post(_llm_url(), json=payload, headers=_headers())
    data = resp.json()
    if resp.status_code == 200:
        try:
            response_text = data["choices"][0]["message"]["content"]
            used_sources = _filter_sources_by_citations(response_text, sources)
            footer = _format_sources_md(used_sources) + reminders_md
            if footer:
                data["choices"][0]["message"]["content"] += footer
        except (KeyError, IndexError, TypeError):
            pass
    return JSONResponse(content=data, status_code=resp.status_code)
