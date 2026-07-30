"""Deterministic page-aware scientific text chunking."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .document_parser import ParsedDocument


@dataclass(frozen=True)
class DocumentChunk:
    id: str
    page_start: int
    page_end: int
    section: str
    text: str


def chunk_document(document: ParsedDocument, *, target_characters: int = 5000, overlap_characters: int = 500) -> tuple[DocumentChunk, ...]:
    if target_characters < 500:
        raise ValueError("target_characters must be at least 500.")
    if not 0 <= overlap_characters < target_characters:
        raise ValueError("overlap_characters must be smaller than target_characters.")
    chunks: list[DocumentChunk] = []
    for page in document.pages:
        normalized = "\n".join(line.strip() for line in page.text.splitlines() if line.strip())
        if not normalized:
            continue
        section = _section(normalized)
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + target_characters)
            if end < len(normalized):
                boundary = max(normalized.rfind(". ", start, end), normalized.rfind("\n", start, end))
                if boundary > start + target_characters // 2:
                    end = boundary + 1
            text = normalized[start:end].strip()
            if text:
                chunks.append(DocumentChunk(f"p{page.page}-c{len(chunks) + 1}", page.page, page.page, section, text))
            if end >= len(normalized):
                break
            start = max(start + 1, end - overlap_characters)
    return tuple(chunks)


def _section(text: str) -> str:
    first = text.splitlines()[0][:120]
    if len(first.split()) <= 12 and (first.isupper() or re.match(r"^\d+(?:\.\d+)*\s+", first)):
        return first
    return "body"
