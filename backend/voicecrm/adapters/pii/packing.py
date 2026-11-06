"""Packs short transcript segments into larger documents for the Azure AI Language PII service.

The service bills per *text record* (1,000 characters; every document is at least one record) and takes at most five
documents of 5,120 characters per request. A transcript segment is about 140 characters, so one document per segment
pays for a whole record per segment and needs a request per five segments. Packing consecutive segments into documents of
a few thousand characters bills what was actually sent and cuts the requests roughly fivefold; entities are mapped back to
the segment they came from, and one that straddles a boundary (a number spoken across two turns) is clipped into both.

``pack_chars=0`` keeps one document per segment (the plain, un-packed behaviour).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from ...ports.pii import PiiSpan

SEPARATOR = "\n"
RECORD_CHARS = 1000
MAX_CHARS_PER_DOCUMENT = 5120
DEFAULT_PACK_CHARS = 4000            # comfortably under the 5,120 limit; a typical 3-minute call fits in one document


@dataclass(frozen=True, slots=True)
class Part:
    """One input text (or one piece of an over-long text) inside a document."""

    index: int          # which input text it came from
    start: int          # [start, end) position in the document
    end: int
    offset: int         # where the piece begins inside its input text (non-zero only for later pieces of a long text)


@dataclass(slots=True)
class Document:
    text: str = ""
    parts: list[Part] = field(default_factory=list)

    def add(self, index: int, offset: int, chunk: str) -> None:
        if self.parts:
            self.text += SEPARATOR
        start = len(self.text)
        self.text += chunk
        self.parts.append(Part(index, start, start + len(chunk), offset))

    @property
    def records(self) -> int:
        return max(1, math.ceil(len(self.text) / RECORD_CHARS))


def split_long(text: str, limit: int) -> list[tuple[int, str]]:
    """(offset, chunk) pieces of at most ``limit`` characters, split at the last whitespace inside the window."""
    if len(text) <= limit:
        return [(0, text)]
    pieces: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + limit // 2, end)
            end = cut + 1 if cut > start else end
        pieces.append((start, text[start:end]))
        start = end
    return pieces


def plan_documents(texts: Sequence[str], *, max_chars: int = MAX_CHARS_PER_DOCUMENT, pack_chars: int = 0) -> list[Document]:
    """Documents covering every non-blank text, in order. Blank texts are skipped; texts longer than ``max_chars`` are split."""
    documents: list[Document] = []
    current = Document()
    for index, text in enumerate(texts):
        if not text or not text.strip():
            continue
        for offset, chunk in split_long(text, max_chars):
            if pack_chars <= 0 or len(chunk) >= pack_chars:
                if current.parts:
                    documents.append(current)
                    current = Document()
                solo = Document()
                solo.add(index, offset, chunk)
                documents.append(solo)
                continue
            if current.parts and len(current.text) + len(SEPARATOR) + len(chunk) > pack_chars:
                documents.append(current)
                current = Document()
            current.add(index, offset, chunk)
    if current.parts:
        documents.append(current)
    return documents


def text_records(texts: Sequence[str], *, max_chars: int = MAX_CHARS_PER_DOCUMENT, pack_chars: int = DEFAULT_PACK_CHARS) -> int:
    """Text records the service would bill for analysing these texts."""
    return sum(document.records for document in plan_documents(texts, max_chars=max_chars, pack_chars=pack_chars))


def distribute(document: Document, spans: Sequence[PiiSpan]) -> list[tuple[int, PiiSpan]]:
    """Maps spans in document coordinates back to (input text index, span in that text's coordinates)."""
    placed: list[tuple[int, PiiSpan]] = []
    for span in spans:
        for part in document.parts:
            lo, hi = max(span.start, part.start), min(span.end, part.end)
            if lo < hi:
                shift = part.offset - part.start
                placed.append((part.index, span.model_copy(update={"start": lo + shift, "end": hi + shift,
                                                                    "text": document.text[lo:hi]})))
    return placed
