from __future__ import annotations

import logging

import httpx
from tenacity import (
    before_sleep_log, retry, retry_if_exception_type,
    stop_after_attempt, wait_exponential,
)

from .config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=120.0)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


class EmbeddingError(Exception):
    pass


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _embed_batch(texts: list[str], batch_label: str = "") -> list[list[float]]:
    client = get_client()
    url = f"{settings.embedding_base_url}/v1/embeddings"
    payload = {"model": settings.embedding_model, "input": texts}
    logger.debug("Embedding batch %s: %d texts → %s", batch_label, len(texts), url)
    resp = await client.post(url, json=payload)
    if resp.status_code != 200:
        raise EmbeddingError(
            f"Embedding endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    items = sorted(data["data"], key=lambda x: x["index"])
    logger.debug("Embedding batch %s complete: %d vectors returned", batch_label, len(items))
    return [item["embedding"] for item in items]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batching as needed."""
    if not texts:
        return []
    batch_size = settings.embedding_batch_size
    n_batches = (len(texts) + batch_size - 1) // batch_size
    logger.debug("embed_texts: %d texts, batch_size=%d, %d batch(es)", len(texts), batch_size, n_batches)
    results: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        batch_num = i // batch_size + 1
        embeddings = await _embed_batch(batch, batch_label=f"{batch_num}/{n_batches}")
        results.extend(embeddings)
    return results
