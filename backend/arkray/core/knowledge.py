"""Knowledge documents: what a CRM record contributes to Ask Arkray's semantic index.

Each owning module decides which of its records are worth embedding and which of their
fields may be (`knowledge_documents` in leads, pipeline and activities selectors), the way
each decides what global search matches. The `ai` module only chunks, embeds, stores and
retrieves; it never reads another module's tables.

What a document may contain: descriptive text a salesperson wrote about the record (names,
titles, descriptions, note bodies, an opportunity's lost reason). Never contact data
(email, phone numbers, address lines), meeting links (they often carry passcodes),
passwords, tokens, sessions, audit data or anything from the identity module.

A document is derived data. Its `content_hash` identifies the exact text that was
embedded, so retrieval can tell whether the live record still says the same thing
(docs/rag-architecture.md#query-path-defence-in-depth).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

# Bump when the way documents are composed changes: every stored chunk then no longer
# matches its source and is re-embedded by reconciliation.
DOCUMENT_FORMAT = "1"


class SourceType(StrEnum):
    LEAD = "lead"
    OPPORTUNITY = "opportunity"
    TASK = "task"
    MEETING = "meeting"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    source_type: SourceType
    source_id: UUID
    owner_id: UUID  # who may see it now (the record's owner): retrieval's pre-filter
    lead_id: UUID
    opportunity_id: UUID | None
    label: str  # a short display name: a lead's name, a title, "Note"
    text: str  # the embeddable text, composed by `compose`
    occurred_at: datetime  # the record's own date: created, due, starts
    updated_at: datetime

    @property
    def content_hash(self) -> str:
        return content_hash(self.source_type, self.text)


def content_hash(source_type: str, text: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"{DOCUMENT_FORMAT}\x1f{source_type}\x1f".encode())
    digest.update(text.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def compose(*fields: tuple[str, str]) -> str:
    """Labelled lines ("Title: ...") for the fields that have a value, in order. Labels
    tell the embedding model (and later the LLM) what each piece of text is."""
    lines = []
    for label, value in fields:
        text = " ".join(value.split()) if "\n" not in value else value.strip()
        if text:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)
