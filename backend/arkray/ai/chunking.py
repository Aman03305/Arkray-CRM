"""Splitting a knowledge document into embeddable chunks.

A chunk is a character range of the document's text (`start`, `end`), cut at a paragraph,
sentence or word boundary where possible, overlapping its neighbour a little so a thought
split across a boundary is still found. The ranges are stored with the vectors; retrieval
slices the *live* document with them (after checking its hash), so chunk text is never
stored.

Bounded: at most MAX_CHUNKS per document (a note is at most 10,000 characters, so a
document fits in about 12 chunks of CHUNK_CHARS).
"""

from __future__ import annotations

from dataclasses import dataclass

CHUNK_CHARS = 1000  # ~200-250 tokens: well inside the model's 512-token window
OVERLAP_CHARS = 150
MAX_CHUNKS = 16
_BREAKS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    start: int
    end: int


def _cut(text: str, start: int, limit: int) -> int:
    """Where to end a chunk that starts at `start` and may not pass `limit`: the last
    natural break in the second half of the window, else the limit itself."""
    if limit >= len(text):
        return len(text)
    window = text[start:limit]
    for separator in _BREAKS:
        at = window.rfind(separator)
        if at >= len(window) // 2:
            return start + at + len(separator)
    return limit


def chunk(text: str) -> list[Chunk]:
    if not text.strip():
        return []
    chunks: list[Chunk] = []
    start = 0
    while start < len(text) and len(chunks) < MAX_CHUNKS:
        end = _cut(text, start, start + CHUNK_CHARS)
        if text[start:end].strip():
            chunks.append(Chunk(index=len(chunks), start=start, end=end))
        if end >= len(text):
            break
        # Overlap, but always advance (and start the next chunk at a word if we can).
        next_start = max(end - OVERLAP_CHARS, start + 1)
        space = text.find(" ", next_start, end)
        start = space + 1 if space != -1 else next_start
    return chunks
