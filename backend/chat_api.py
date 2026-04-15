from __future__ import annotations

import logging
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import embeddings, vector_store
from .config import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="NoterAI RAG Chat")


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


async def _retrieve_context(query: str) -> str:
    """Embed the query, search ChromaDB, return formatted context string."""
    try:
        emb = await embeddings.embed_texts([query])
        chunks = await vector_store.query(emb[0], settings.chat_n_results)
        if not chunks:
            return ""
        parts = []
        for chunk in chunks:
            meta = chunk["metadata"]
            label = meta.get("source_label") or meta.get("note_id", "Note")
            parts.append(f"[{label}]\n{chunk['document']}")
        return "\n\n---\n\n".join(parts)
    except Exception:
        logger.warning("RAG retrieval failed", exc_info=True)
        return ""


def _build_messages(original: list[dict], context: str) -> list[dict]:
    system = (
        "You are a helpful assistant with access to the user's personal notes. "
        "Use the context below — excerpts from their notes — to answer accurately. "
        "If the notes don't contain relevant information, say so and answer from general knowledge."
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
    context = await _retrieve_context(query) if query else ""

    messages = _build_messages([m.model_dump() for m in body.messages], context)

    payload: dict = {"model": _llm_model(), "messages": messages}
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        payload["temperature"] = body.temperature
    if body.stream:
        payload["stream"] = True

    if body.stream:
        async def _stream():
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", _llm_url(), json=payload, headers=_headers()
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(_stream(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(_llm_url(), json=payload, headers=_headers())
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
