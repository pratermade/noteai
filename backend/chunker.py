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
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]


def _hard_split(tokens: list[int], chunk_size: int, chunk_overlap: int) -> list[list[int]]:
    """Slice a token list into chunk_size pieces with overlap."""
    pieces = []
    start = 0
    while start < len(tokens):
        end = start + chunk_size
        pieces.append(tokens[start:end])
        if end >= len(tokens):
            break
        start = end - chunk_overlap
    return pieces


def _char_split(text: str, max_chars: int) -> list[str]:
    """Split text at word boundaries to stay within max_chars. Guaranteed: each piece <= max_chars."""
    if len(text) <= max_chars:
        return [text]
    pieces = []
    while text:
        if len(text) <= max_chars:
            pieces.append(text)
            break
        cut = text.rfind(' ', 0, max_chars)
        if cut <= 0:
            cut = max_chars  # no word boundary — hard cut
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    return [p for p in pieces if p]


def chunk_text(text: str) -> list[dict]:
    """
    Split text into overlapping chunks.
    Returns list of {"text": str, "chunk_index": int}.
    Each chunk is guaranteed to be <= chunk_max_chars characters.
    """
    chunk_size = settings.chunk_size
    chunk_overlap = settings.chunk_overlap
    max_chars = settings.chunk_max_chars

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
                sent_toks = _tokenize(sent)
                sent_len = len(sent_toks)
                if current_len + sent_len > chunk_size and current:
                    segments.append(" ".join(current))
                    current = []
                    current_len = 0
                if sent_len > chunk_size:
                    # Sentence exceeds chunk_size with no splittable boundary — hard-split by tokens
                    if current:
                        segments.append(" ".join(current))
                        current = []
                        current_len = 0
                    for piece in _hard_split(sent_toks, chunk_size, chunk_overlap):
                        segments.append(_detokenize(piece))
                else:
                    current.append(sent)
                    current_len += sent_len
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
        chunk_tokens = (overlap_tokens + seg_tokens) if overlap_tokens else seg_tokens

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

    # Hard-limit: enforce character ceiling (tokenizer-agnostic — catches mismatch between
    # cl100k_base and the embedding model's tokenizer)
    enforced: list[dict] = []
    for ch in chunks:
        if len(ch["text"]) <= max_chars:
            enforced.append({"text": ch["text"], "chunk_index": len(enforced)})
        else:
            for piece in _char_split(ch["text"], max_chars):
                enforced.append({"text": piece, "chunk_index": len(enforced)})

    return enforced
