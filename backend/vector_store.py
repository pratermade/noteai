from __future__ import annotations

import logging

import chromadb
from chromadb.config import Settings as ChromaSettings

from .config import settings
from .models import SearchResult

logger = logging.getLogger(__name__)

_client: chromadb.AsyncHttpClient | None = None
_collection = None


def _reset() -> None:
    global _client, _collection
    _collection = None
    _client = None


async def get_collection():
    global _client, _collection
    if _collection is None:
        logger.debug("Connecting to ChromaDB at %s:%s", settings.chroma_host, settings.chroma_port)
        _client = await chromadb.AsyncHttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )
        _collection = await _client.get_or_create_collection(
            name=settings.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )
        logger.debug("ChromaDB collection '%s' ready", settings.chroma_collection)
    return _collection


_UPSERT_BATCH = 100


async def upsert(
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
    ids: list[str],
) -> None:
    logger.debug("Upserting %d vectors to ChromaDB in batches of %d", len(ids), _UPSERT_BATCH)
    try:
        col = await get_collection()
        for i in range(0, len(ids), _UPSERT_BATCH):
            s = slice(i, i + _UPSERT_BATCH)
            batch_ids = ids[s]
            logger.debug(
                "Upserting batch %d–%d (%d vectors)",
                i, min(i + _UPSERT_BATCH, len(ids)), len(batch_ids),
            )
            await col.upsert(
                documents=documents[s],
                embeddings=embeddings[s],
                metadatas=metadatas[s],
                ids=batch_ids,
            )
        logger.debug("Upsert complete: %d vectors", len(ids))
    except Exception:
        logger.warning("ChromaDB upsert failed — resetting collection cache", exc_info=True)
        _reset()
        raise


async def delete_by_note_id(note_id: str) -> None:
    try:
        col = await get_collection()
        results = await col.get(where={"note_id": note_id})
        count = len(results["ids"])
        if count:
            logger.debug("Deleting %d vectors for note_id=%s", count, note_id)
            await col.delete(ids=results["ids"])
        else:
            logger.debug("No vectors found for note_id=%s", note_id)
    except Exception:
        logger.warning("ChromaDB delete_by_note_id failed — resetting collection cache", exc_info=True)
        _reset()
        raise


async def delete_by_attachment_id(attachment_id: str) -> None:
    try:
        col = await get_collection()
        results = await col.get(where={"attachment_id": attachment_id})
        count = len(results["ids"])
        if count:
            logger.debug("Deleting %d vectors for attachment_id=%s", count, attachment_id)
            await col.delete(ids=results["ids"])
        else:
            logger.debug("No vectors found for attachment_id=%s", attachment_id)
    except Exception:
        logger.warning("ChromaDB delete_by_attachment_id failed — resetting collection cache", exc_info=True)
        _reset()
        raise


async def query(
    embedding: list[float],
    n_results: int,
    where: dict | None = None,
) -> list[dict]:
    try:
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
        logger.debug("Query returned %d results", len(items))
        return items
    except Exception:
        logger.warning("ChromaDB query failed — resetting collection cache", exc_info=True)
        _reset()
        raise
