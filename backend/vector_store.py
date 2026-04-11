from __future__ import annotations

import logging

import chromadb
from chromadb.config import Settings as ChromaSettings

from .config import settings
from .models import SearchResult

logger = logging.getLogger(__name__)

_client: chromadb.AsyncHttpClient | None = None
_collection = None


async def get_collection():
    global _client, _collection
    if _collection is None:
        _client = await chromadb.AsyncHttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )
        _collection = await _client.get_or_create_collection(
            name=settings.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


async def upsert(
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
    ids: list[str],
) -> None:
    col = await get_collection()
    await col.upsert(
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
        ids=ids,
    )


async def delete_by_note_id(note_id: str) -> None:
    col = await get_collection()
    results = await col.get(where={"note_id": note_id})
    if results["ids"]:
        await col.delete(ids=results["ids"])


async def delete_by_attachment_id(attachment_id: str) -> None:
    col = await get_collection()
    results = await col.get(where={"attachment_id": attachment_id})
    if results["ids"]:
        await col.delete(ids=results["ids"])


async def query(
    embedding: list[float],
    n_results: int,
    where: dict | None = None,
) -> list[dict]:
    col = await get_collection()
    kwargs: dict = {
        "query_embeddings": [embedding],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where
    results = await col.query(**kwargs)
    if not results["ids"] or not results["ids"][0]:
        return []
    items = []
    for doc, meta, dist, doc_id in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
        results["ids"][0],
    ):
        items.append({
            "id": doc_id,
            "document": doc,
            "metadata": meta,
            "distance": dist,
        })
    return items
