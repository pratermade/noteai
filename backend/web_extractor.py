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


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fetch_html(url: str) -> tuple[str, str]:
    """Fetch URL with a browser user-agent. Returns (html, final_url)."""
    import re
    import requests

    # Reddit's new UI is React-rendered; old.reddit.com serves plain HTML
    fetch_url = re.sub(r'https?://(www\.)?reddit\.com', 'https://old.reddit.com', url)

    try:
        resp = requests.get(fetch_url, headers=_BROWSER_HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp.text, resp.url
    except requests.RequestException as exc:
        raise ExtractionError(f"Could not fetch URL: {url} — {exc}")


def _is_reddit_url(url: str) -> bool:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return host.endswith("reddit.com")


def _extract_reddit_sync(url: str) -> WebExtractionResult:
    import re
    import requests

    # Resolve share links (/s/ format from mobile) to canonical /comments/ URL
    resolved = url
    if '/s/' in url:
        try:
            head = requests.head(url, headers=_BROWSER_HEADERS, timeout=15, allow_redirects=True)
            resolved = head.url
        except Exception as exc:
            raise ExtractionError(f"Could not resolve Reddit share URL: {exc}")

    # Extract post ID — Reddit JSON API requires /comments/{id}.json without slug
    m = re.search(r'/comments/([a-z0-9]+)', resolved, re.I)
    if not m:
        raise ExtractionError(f"Could not parse Reddit post URL: {url}")
    json_url = f"https://www.reddit.com/comments/{m.group(1)}.json"

    try:
        resp = requests.get(json_url, headers=_BROWSER_HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise ExtractionError(f"Reddit JSON fetch failed: {exc}")

    try:
        post = data[0]["data"]["children"][0]["data"]
        title = post.get("title", "")
        selftext = post.get("selftext", "").strip()

        comments = []
        for child in data[1]["data"]["children"][:20]:
            body = child.get("data", {}).get("body", "").strip()
            if body and body not in ("[deleted]", "[removed]"):
                comments.append(body)

        parts = [title]
        if selftext:
            parts.append(selftext)
        if comments:
            parts.append("\n\n".join(comments))

        text = "\n\n".join(parts).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ExtractionError(f"Reddit JSON parse failed: {exc}")

    if len(text) < 50:
        raise ExtractionError("Reddit post yielded insufficient text")

    return WebExtractionResult(text=text, title=title, char_count=len(text))


def _extract_sync(url: str) -> WebExtractionResult:
    video_id = get_youtube_video_id(url)
    if video_id:
        return _extract_youtube_sync(video_id, url)

    if _is_reddit_url(url):
        return _extract_reddit_sync(url)

    import trafilatura

    html, final_url = _fetch_html(url)
    if not html:
        raise ExtractionError(f"Could not fetch URL: {url}")

    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        url=final_url,
    )
    if not text or len(text.strip()) < 100:
        raise ExtractionError(
            "Page yielded insufficient text (may be JS-rendered or paywalled)"
        )

    metadata = trafilatura.extract_metadata(html)
    title = metadata.title if metadata else None

    return WebExtractionResult(
        text=text,
        title=title,
        char_count=len(text),
    )


async def extract_url(url: str) -> WebExtractionResult:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_sync, url)
