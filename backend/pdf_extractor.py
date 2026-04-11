from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    pass


@dataclass
class PDFExtractionResult:
    text: str
    page_count: int
    char_count: int
    extraction_warnings: list[str] = field(default_factory=list)


def _extract_sync(path: str) -> PDFExtractionResult:
    import fitz  # pymupdf

    warnings: list[str] = []
    pages_text: list[str] = []

    doc = fitz.open(path)
    page_count = len(doc)

    for page_num, page in enumerate(doc, start=1):
        blocks = page.get_text("blocks")
        # Sort blocks top-to-bottom, then left-to-right
        blocks.sort(key=lambda b: (b[1], b[0]))
        page_text = "\n".join(
            b[4].strip() for b in blocks if isinstance(b[4], str) and b[4].strip()
        )
        if len(page_text) < 20:
            warnings.append(f"Page {page_num} yielded no text (may be scanned)")
        pages_text.append(page_text)

    doc.close()

    full_text = f"\n\n--- Page {{n}} ---\n\n".join(pages_text)
    # Rebuild with actual page numbers
    parts = []
    for i, pt in enumerate(pages_text, start=1):
        if i == 1:
            parts.append(pt)
        else:
            parts.append(f"\n\n--- Page {i} ---\n\n" + pt)
    full_text = "".join(parts)

    # Clean up
    full_text = re.sub(r'\x00', '', full_text)
    full_text = re.sub(r'\n{3,}', '\n\n', full_text)
    full_text = full_text.strip()

    if len(full_text) < 100:
        raise ExtractionError(
            "PDF appears to be scanned or image-only; OCR is not yet supported"
        )

    return PDFExtractionResult(
        text=full_text,
        page_count=page_count,
        char_count=len(full_text),
        extraction_warnings=warnings,
    )


async def extract(path: str) -> PDFExtractionResult:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_sync, path)
