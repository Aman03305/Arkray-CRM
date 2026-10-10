"""Assembling a typed answer (docs/rag-architecture.md#hallucination-protection).

An answer is data, not markup:

    {"blocks": [{"type": "paragraph" | "bullet", "parts": [{"text": ..., "bold"?: true}
                                                            | {"ref": "lead:<uuid>"}]}],
     "facts": [...], "sources": [...], "citations": [...], "notices": [...],
     "provenance": {"mode": ..., "tools": [...], "grounded": ...}}

- **Rendering is safe by construction.** The narrative is parsed here into paragraphs,
  bullets, bold runs and record references; everything else is plain text. No HTML, no
  links, no images: the browser renders text nodes, and builds a link only from a `ref`,
  inside the current workspace.
- **Only real records are links.** A `[[kind:id]]` the model wrote becomes a reference only
  if a tool returned that record in this question (so the caller may see it); any other is
  dropped.
- **Numeric grounding.** Every number in the narrative (amounts, counts, dates, times,
  percentages, lakh/crore figures) must appear in this question's tool results or in the
  question itself. Otherwise the narrative is discarded and the facts the tools returned are
  shown with a notice: a model that invents a figure loses its words, not the user's trust.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from arkray.core.text import without_refused

from .formatting import user_text
from .tools import Citation, Fact, RecordRef, ToolContext

# Case-insensitive: "[[Lead:...]]" is a reference too (shown if visible, else dropped), never
# literal text (Phase 9 review).
REF_PATTERN = re.compile(
    r"\[\[(lead|opportunity|task|meeting|note):([0-9a-fA-F-]{36})\]\]", re.IGNORECASE
)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_LINK = re.compile(r"!?\[([^\]\[]*)\]\([^)]*\)")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_NUMBER = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|crores?|cr|l|k|million|mn|m|%)?(?![\w])",
    re.IGNORECASE,
)
# "Rs.99,00,000", "INR 99,00,000": the currency word is not part of the number.
_CURRENCY = re.compile(r"(?:\bRs\.?|\bINR)\s*", re.IGNORECASE)
_BOLD_REF = re.compile(r"\*\*\s*(\[\[[^\]]+\]\])\s*\*\*")
_MULTIPLIERS = {
    "lakh": Decimal(100_000),
    "lakhs": Decimal(100_000),
    "l": Decimal(100_000),
    "crore": Decimal(10_000_000),
    "crores": Decimal(10_000_000),
    "cr": Decimal(10_000_000),
    "k": Decimal(1_000),
    "million": Decimal(1_000_000),
    "mn": Decimal(1_000_000),
    "m": Decimal(1_000_000),
}
ALWAYS_ALLOWED = frozenset({Decimal(0), Decimal(1)})
# Numbers written as words count too ("seventeen overdue tasks").
NUMBER_WORDS = {
    word: Decimal(value)
    for value, word in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ]
    )
} | {
    "thirty": Decimal(30),
    "forty": Decimal(40),
    "fifty": Decimal(50),
    "sixty": Decimal(60),
    "seventy": Decimal(70),
    "eighty": Decimal(80),
    "ninety": Decimal(90),
    "hundred": Decimal(100),
    "dozen": Decimal(12),
}
_WORD = re.compile(r"[A-Za-z]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# An ISO timestamp's UTC offset ("+05:30") states no fact: leave it out of what's allowed.
_ISO_OFFSET = re.compile(r"(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:\d{2}|Z)$")
MAX_NARRATIVE_CHARS = 6000


# --- numbers -------------------------------------------------------------------------------------
def _value(digits: str, unit: str | None) -> Decimal | None:
    try:
        number = Decimal(digits.replace(",", ""))
    except InvalidOperation:
        return None
    if unit and unit.lower() in _MULTIPLIERS:
        number *= _MULTIPLIERS[unit.lower()]
    return number.normalize()


def numbers_in(text: str) -> set[Decimal]:
    """The numbers a text states: digits (with lakh/crore/k/million and % units) and number
    words. Record references and UUIDs are not numbers."""
    text = _CURRENCY.sub(" ", _UUID.sub(" ", REF_PATTERN.sub(" ", text)))
    found: set[Decimal] = set()
    for match in _NUMBER.finditer(text):
        value = _value(match.group(1), match.group(2))
        if value is not None:
            found.add(value)
    for word in _WORD.findall(text):
        if word.lower() in NUMBER_WORDS:
            found.add(NUMBER_WORDS[word.lower()].normalize())
    return found


_SKIPPED_KEYS = frozenset({"ref", "id", "relevance"})


def _strings(value: Any) -> Iterable[str]:
    """The texts of a tool result whose numbers are facts: never ids or references, and an
    ISO timestamp without its UTC offset."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in _SKIPPED_KEYS:
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, bool) or value is None:
        return
    else:
        text = str(value)
        yield _ISO_OFFSET.sub(r"\1", text) if "T" in text else text


def allowed_numbers(tool_results: Iterable[str], *texts_given: str) -> set[Decimal]:
    """Every number stated by this question's tool results (and the question). Dates and
    times are stated both as ISO values and as display forms, so "3 Oct", "10:30" and
    "2026-10-03" all check out against the same fact; ids, references and UTC offsets are
    not facts and allow nothing."""
    allowed: set[Decimal] = set(ALWAYS_ALLOWED)
    texts = list(texts_given)  # the question, its context (today), the replayed history
    for content in tool_results:
        try:
            texts.extend(_strings(json.loads(content)))
        except ValueError:
            texts.append(content)
    for text in texts:
        allowed |= numbers_in(text)
    return allowed


def ungrounded_numbers(narrative: str, allowed: set[Decimal]) -> set[Decimal]:
    return {n for n in numbers_in(narrative) if n not in allowed}


# A figure stated as money: "₹45,13,890.50", "Rs. 5 lakh", "INR 2 crore".
_MONEY = re.compile(
    r"(?:₹|\bRs\.?|\bINR)\s*(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|crores?|cr|l|k|million|mn)?(?![\w])",
    re.IGNORECASE,
)


def money_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for match in _MONEY.finditer(text):
        value = _value(match.group(1), match.group(2))
        if value is not None:
            found.add(value)
    return found


# The deal's CPT fields are the CRM's own statement of a per-test rate ("Rs 18 per test"):
# the model repeating one is stating a recorded fact, not inventing money (final audit RAG-2:
# the figure was "ungrounded" and the whole narrative withheld). Any other user-written text
# still grounds no money.
_CPT_KEYS = frozenset({"expected_cpt", "agreed_cpt", "latest_agreed_cpt"})


def _money_values(value: Any) -> Iterable[Decimal]:
    """Amounts of the money values a tool computed ({"amount", "currency", "display"}) and the
    figures of a deal's CPT fields, never numbers inside other user-written text."""
    if isinstance(value, dict):
        if "amount" in value and "currency" in value:
            amount = _value(str(value["amount"]), None)
            if amount is not None:
                yield amount
        for key, item in value.items():
            if key in _CPT_KEYS and isinstance(item, str):
                yield from numbers_in(item)
            elif key not in _SKIPPED_KEYS:
                yield from _money_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _money_values(item)


def allowed_money(tool_results: Iterable[str], *texts_given: str) -> set[Decimal]:
    """Money the model may state: amounts the tools computed, and money the question or
    the earlier turns already stated. A number in a note ("PO 7,77,77,777") is not money
    the CRM computed (Phase 9 review: it grounded an invented pipeline value)."""
    allowed: set[Decimal] = set()
    for content in tool_results:
        try:
            allowed.update(_money_values(json.loads(content)))
        except ValueError:
            continue
    for text in texts_given:
        allowed |= money_in(text)
    return allowed


def ungrounded_money(narrative: str, allowed: set[Decimal]) -> set[Decimal]:
    return {n for n in money_in(narrative) if n not in allowed}


# --- narrative -> blocks -----------------------------------------------------------------------
def _inline(text: str, records: dict[str, RecordRef], cited: list[str]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    position = 0
    for match in REF_PATTERN.finditer(text):
        parts.extend(_bold_runs(text[position : match.start()]))
        ref = f"{match.group(1).lower()}:{match.group(2).lower()}"
        if ref in records:
            parts.append({"ref": ref})
            if ref not in cited:
                cited.append(ref)
        position = match.end()
    parts.extend(_bold_runs(text[position:]))
    return [p for p in parts if p.get("ref") or p.get("text")]


def _bold_runs(text: str) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    position = 0
    for match in _BOLD.finditer(text):
        if match.start() > position:
            parts.append({"text": text[position : match.start()]})
        parts.append({"text": match.group(1), "bold": True})
        position = match.end()
    if position < len(text):
        parts.append({"text": text[position:]})
    return parts


def to_blocks(
    narrative: str, records: dict[str, RecordRef]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse the model's text into paragraphs and bullets of text and record references.
    Markdown links and images keep only their text; code marks and headings are dropped."""
    # Model output never carries what our input rules refuse (bidi overrides, tag
    # characters, controls): stripped, since nobody can be asked to fix it (Phase 9 review).
    narrative = without_refused(narrative[:MAX_NARRATIVE_CHARS])
    text = _MD_LINK.sub(r"\1", narrative).replace("`", "")
    text = _BOLD_REF.sub(r"\1", text)  # a bold reference is just a reference
    blocks: list[dict[str, Any]] = []
    cited: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            parts = _inline(" ".join(paragraph), records, cited)
            if parts:
                blocks.append({"type": "paragraph", "parts": parts})
            paragraph.clear()

    for raw_line in text.splitlines():
        line = _HEADING.sub("", raw_line).strip()
        if not line or set(line) <= set("-|=_* "):
            flush()
            continue
        if _BULLET.match(line):
            flush()
            parts = _inline(_BULLET.sub("", line, count=1), records, cited)
            if parts:
                blocks.append({"type": "bullet", "parts": parts})
        else:
            paragraph.append(line)
    flush()
    return blocks, cited


def plain_text(answer: dict[str, Any]) -> str:
    """A stored answer's narrative as plain text, records by their labels (the conversation
    history sent with the next question: the model's earlier words, never raw records).

    Masked like every user-written string that may reach the model (formatting.user_text):
    a cited label is the record's title as typed ("Call Priya +91 ...", a meeting link with
    its passcode), stored unmasked for the asker's own screen (privacy remediation P2-3)."""
    labels = {source["ref"]: source["label"] for source in answer.get("sources", [])}
    lines = []
    for block in answer.get("blocks", []):
        text = "".join(
            labels.get(part["ref"], "") if "ref" in part else str(part.get("text", ""))
            for part in block.get("parts", [])
        )
        lines.append(f"- {text}" if block.get("type") == "bullet" else text)
    return user_text("\n".join(lines))


# --- the answer ----------------------------------------------------------------------------------
def _fact(fact: Fact) -> dict[str, str]:
    return {"label": fact.label, "value": fact.value, "kind": fact.kind, "raw": fact.raw}


def _source(record: RecordRef) -> dict[str, str]:
    return {
        "ref": record.ref,
        "kind": record.kind,
        "id": str(record.id),
        "label": record.label,
        "detail": record.detail,
    }


def _citation(citation: Citation) -> dict[str, str]:
    return {
        "ref": citation.ref,
        "kind": citation.kind,
        "label": citation.label,
        "snippet": citation.snippet,
        "when": citation.when,
        # Internal (the API's citation serializer has no such field): which version of the
        # record was quoted, so a stored answer never shows or replays text since removed.
        "source_hash": citation.source_hash,
    }


def assemble(
    ctx: ToolContext,
    *,
    blocks: list[dict[str, Any]],
    cited: list[str],
    mode: str,
    notices: list[str] | None = None,
    grounded: bool = True,
    model: str = "",
) -> dict[str, Any]:
    """The answer as stored and returned. Sources are the records the narrative cites, then
    those quoted in citations; only records a tool returned in this question (so the caller
    may open them) ever appear."""
    refs = list(dict.fromkeys([*cited, *(c.ref for c in ctx.citations)]))
    sources = [_source(ctx.records[ref]) for ref in refs if ref in ctx.records]
    facts = []
    seen: set[str] = set()
    for fact in ctx.facts:
        if fact.label not in seen:
            seen.add(fact.label)
            facts.append(_fact(fact))
    citations = [_citation(c) for c in ctx.citations]
    return {
        # Every record a tool returned for this question (internal, never serialised): what
        # the narrative may be based on, re-checked against the reader's scope when the
        # answer is read again (service.visible_answer).
        "basis": sorted(ctx.records),
        "blocks": blocks,
        "facts": facts,
        "sources": sources,
        "citations": citations,
        "notices": notices or [],
        "provenance": {
            "mode": mode,
            "tools": list(dict.fromkeys(ctx.tools_used)),
            "grounded": grounded,
            "model": model,
        },
    }


def text_block(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "parts": [{"text": text}]}


def ref_bullet(ref: str, detail: str = "") -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"ref": ref}]
    if detail:
        parts.append({"text": f" — {detail}"})
    return {"type": "bullet", "parts": parts}
