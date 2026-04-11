from __future__ import annotations

import re

import tiktoken

from .config import settings

_enc = tiktoken.get_encoding("cl100k_base")


def _tokenize(text: str) -> list[int]:
    return _enc.encode(text)


def _detokenize(tokens: list[int]) -> str:
    return _enc.decode(tokens)


def _split_sentences(text: str) -> list[str]:
    # Simple sentence splitter on punctuation followed by whitespace
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]


def chunk_text(text: str) -> list[dict]:
    """
    Split text into overlapping chunks.
    Returns list of {"text": str, "chunk_index": int}.
    """
    chunk_size = settings.chunk_size
    chunk_overlap = settings.chunk_overlap

    # Split on double newlines (paragraphs)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

    # Merge very short paragraphs and split very long ones
    segments: list[str] = []
    for para in paragraphs:
        tokens = _tokenize(para)
        if len(tokens) <= chunk_size:
            segments.append(para)
        else:
            # Split long paragraph at sentence boundaries
            sentences = _split_sentences(para)
            current: list[str] = []
            current_len = 0
            for sent in sentences:
                sent_tokens = len(_tokenize(sent))
                if current_len + sent_tokens > chunk_size and current:
                    segments.append(" ".join(current))
                    current = []
                    current_len = 0
                current.append(sent)
                current_len += sent_tokens
            if current:
                segments.append(" ".join(current))

    # Build chunks with overlap
    chunks: list[dict] = []
    overlap_tokens: list[int] = []

    for seg in segments:
        seg_tokens = _tokenize(seg)

        # If overlap + segment fits in one chunk, accumulate
        combined = overlap_tokens + seg_tokens
        if len(combined) <= chunk_size:
            overlap_tokens = combined
            continue

        # Flush accumulated overlap + segment as a chunk
        if overlap_tokens:
            chunk_tokens = overlap_tokens + seg_tokens
        else:
            chunk_tokens = seg_tokens

        # Emit chunks of chunk_size from chunk_tokens
        start = 0
        while start < len(chunk_tokens):
            end = start + chunk_size
            piece = chunk_tokens[start:end]
            chunks.append({"text": _detokenize(piece), "chunk_index": len(chunks)})
            if end >= len(chunk_tokens):
                break
            start = end - chunk_overlap

        # Carry overlap forward
        overlap_tokens = chunk_tokens[-chunk_overlap:] if chunk_overlap else []

    # Flush any remaining accumulated tokens
    if overlap_tokens and (not chunks or _detokenize(overlap_tokens) != chunks[-1]["text"]):
        chunks.append({"text": _detokenize(overlap_tokens), "chunk_index": len(chunks)})

    # If text was too short for any chunk to be emitted, emit as single chunk
    if not chunks and text.strip():
        chunks.append({"text": text.strip(), "chunk_index": 0})

    return chunks
