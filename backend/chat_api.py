from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import embeddings, vector_store
from .config import settings

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


async def _retrieve_context(query: str) -> tuple[str, list[dict]]:
    """Embed the query, search ChromaDB, return (context_string, deduplicated_sources)."""
    try:
        emb = await embeddings.embed_texts([query])
        chunks = await vector_store.query(emb[0], settings.chat_n_results)
        if not chunks:
            return "", []
        parts = []
        seen_notes: dict[str, dict] = {}  # note_id → source entry, deduplicated
        for chunk in chunks:
            meta = chunk["metadata"]
            label = meta.get("source_label") or meta.get("note_id", "Note")
            note_id = meta.get("note_id", "")
            parts.append(f"[{label}]\n{chunk['document']}")
            if note_id and note_id not in seen_notes:
                seen_notes[note_id] = {
                    "note_id": note_id,
                    "label": label,
                    "source_url": meta.get("source_url") or "",
                }
        return "\n\n---\n\n".join(parts), list(seen_notes.values())
    except Exception:
        logger.warning("RAG retrieval failed", exc_info=True)
        return "", []


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


def _build_messages(original: list[dict], context: str) -> list[dict]:
    system = (
        "You are a helpful assistant with access to the user's personal notes. "
        "You are a midieval librarian scholar (a bit cartoony) that is happy to help anyone that wants to learn, using your library"
        "Use the context below — excerpts from their notes — to answer accurately. "
        "If the notes don't contain relevant information, say so and answer from general knowledge."
        "If the answer from the notes is incomplete, say so and expand the thoughts from general knowledge."
    )
    if context:
        system += f"\n\n## Relevant notes\n\n{context}"
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
    context, sources = await _retrieve_context(query) if query else ("", [])
    sources_md = _format_sources_md(sources)

    messages = _build_messages([m.model_dump() for m in body.messages], context)

    payload: dict = {"model": _llm_model(), "messages": messages}
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        payload["temperature"] = body.temperature

    if body.stream:
        payload["stream"] = True

        async def _stream():
            buf = b""
            client = _get_llm_client()
            async with client.stream("POST", _llm_url(), json=payload, headers=_headers()) as resp:
                async for raw in resp.aiter_bytes():
                    buf += raw
                    lines = buf.split(b"\n")
                    buf = lines[-1]  # keep incomplete line in buffer
                    for line in lines[:-1]:
                        if line.strip() == b"data: [DONE]":
                            if sources_md:
                                src_chunk = json.dumps({
                                    "id": "chatcmpl-src",
                                    "object": "chat.completion.chunk",
                                    "choices": [{"index": 0, "delta": {"content": sources_md}, "finish_reason": None}],
                                })
                                yield f"data: {src_chunk}\n\n".encode()
                            yield b"data: [DONE]\n\n"
                        else:
                            yield line + b"\n"
            if buf.strip():
                yield buf

        return StreamingResponse(_stream(), media_type="text/event-stream")

    client = _get_llm_client()
    resp = await client.post(_llm_url(), json=payload, headers=_headers())
    data = resp.json()
    if sources_md and resp.status_code == 200:
        try:
            data["choices"][0]["message"]["content"] += sources_md
        except (KeyError, IndexError, TypeError):
            pass
    return JSONResponse(content=data, status_code=resp.status_code)
