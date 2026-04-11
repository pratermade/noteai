from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    pass


@dataclass
class WebExtractionResult:
    text: str
    title: str | None
    char_count: int


def _extract_sync(url: str) -> WebExtractionResult:
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ExtractionError(f"Could not fetch URL: {url}")

    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )
    if not text or len(text.strip()) < 100:
        raise ExtractionError(
            "Page yielded insufficient text (may be JS-rendered or paywalled)"
        )

    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata else None

    return WebExtractionResult(
        text=text,
        title=title,
        char_count=len(text),
    )


async def extract_url(url: str) -> WebExtractionResult:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_sync, url)
