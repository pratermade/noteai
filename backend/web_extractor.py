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


def get_youtube_video_id(url: str) -> str | None:
    """Return the 11-char video ID if url is a YouTube video URL, else None."""
    import re
    m = re.search(
        r'(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})',
        url,
    )
    return m.group(1) if m else None


def _extract_youtube_sync(video_id: str, url: str) -> WebExtractionResult:
    import json
    import urllib.parse
    import urllib.request

    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

    try:
        transcript = YouTubeTranscriptApi().fetch(video_id)
    except TranscriptsDisabled:
        raise ExtractionError(f"Transcripts are disabled for video: {video_id}")
    except NoTranscriptFound:
        raise ExtractionError(f"No transcript found for video: {video_id}")

    text = ' '.join(s.text for s in transcript.snippets).strip()
    if len(text) < 100:
        raise ExtractionError("Transcript too short to be useful")

    # Fetch title via YouTube oEmbed — no API key needed
    title = None
    try:
        oembed = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
        with urllib.request.urlopen(oembed, timeout=5) as resp:
            title = json.loads(resp.read()).get('title')
    except Exception:
        pass  # hostname fallback happens in _web_pipeline

    return WebExtractionResult(text=text, title=title, char_count=len(text))


def _extract_sync(url: str) -> WebExtractionResult:
    video_id = get_youtube_video_id(url)
    if video_id:
        return _extract_youtube_sync(video_id, url)

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
