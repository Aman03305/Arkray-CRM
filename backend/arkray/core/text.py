"""Normalising human-entered text (names, addresses, free-text notes).

Rules, applied identically to every write path (API, imports, background jobs):
- Unicode is normalised to NFC, so "é" typed as one code point or as "e" + combining accent
  is stored (and therefore searched, sorted and compared) the same way.
- Characters that are invisible or change how text displays are **refused** (checked on
  the raw input, before anything is collapsed), never silently removed:
  control characters other than tab/newline/carriage return; format characters (bidi
  embeddings/overrides/isolates used for "Trojan Source" spoofing, zero-width spaces, soft
  hyphens, the Unicode "tag" block used to smuggle hidden text into AI prompts, the BOM);
  line/paragraph separators; private-use characters, lone surrogates and noncharacters.
  Zero-width joiner/non-joiner (U+200D/U+200C) are the exception: Indic scripts such as
  Devanagari and Malayalam need them to spell names correctly.
- Single-line values then have every run of whitespace (tabs and newlines pasted in
  included) collapsed to one space and are trimmed.
- A value with nothing visible left (only joiners or combining marks) counts as empty, so it
  can never pass for a name nobody can see.

Search input follows the same rules, so what is typed and what is stored compare alike
(the search-input section below).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

_ALLOWED_FORMAT = frozenset({chr(0x200C), chr(0x200D)})  # ZWNJ, ZWJ
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Zl", "Zp"})
_LINE_CONTROLS = frozenset("\t\n\r")  # collapsed to a space in single-line values
_MULTILINE_CONTROLS = frozenset("\t\n")


class TextRejected(ValueError):
    """The value contains characters that are never accepted."""


def _refused(char: str, allowed_controls: frozenset[str]) -> bool:
    if char in allowed_controls:
        return False
    category = unicodedata.category(char)
    if category in _REFUSED_CATEGORIES:
        return True
    if category == "Cf":
        return char not in _ALLOWED_FORMAT
    code = ord(char)
    return 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFE) == 0xFFFE  # noncharacters


def _check(value: str, allowed_controls: frozenset[str]) -> None:
    if any(_refused(char, allowed_controls) for char in value):
        raise TextRejected("Remove the invisible or control characters from this text.")


def _visible(value: str) -> bool:
    """At least one letter, number, punctuation mark or symbol (not only joiners/marks)."""
    return any(unicodedata.category(char)[0] in "LNPS" for char in value)


def clean_line(value: str) -> str:
    """One line of text: NFC, checked, whitespace collapsed, trimmed ("" if invisible)."""
    value = unicodedata.normalize("NFC", value)
    _check(value, _LINE_CONTROLS)
    value = " ".join(value.split())
    return value if _visible(value) else ""


def clean_multiline(value: str) -> str:
    """Free text that may span lines: NFC, CRLF/CR -> LF, checked, trailing spaces per line
    and surrounding blank lines removed ("" if invisible)."""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    _check(value, _MULTILINE_CONTROLS)
    value = "\n".join(line.rstrip() for line in value.split("\n")).strip()
    return value if _visible(value) else ""


# --- search input ----------------------------------------------------------------------------
# One set of rules for every search box (the Leads list since Phase 2, global search since
# Phase 7), so the same words find the same leads in both: a query of 2-100 characters, cleaned
# like any single line of text (so a stored value and a typed one compare alike), whose words
# of at least 2 characters are searched, at most 5 of them.
SEARCH_MIN_LENGTH = 2
SEARCH_MAX_LENGTH = 100
SEARCH_MAX_TERMS = 5
SEARCH_TERM_MIN_LENGTH = 2
SEARCH_TERM_TOO_SHORT = f"Use at least {SEARCH_TERM_MIN_LENGTH} characters in a search word."


def search_terms(q: str) -> list[str]:
    """`q` split into at most SEARCH_MAX_TERMS whitespace-separated terms."""
    return q.split()[:SEARCH_MAX_TERMS]


SEARCH_TOO_LONG = f"Use at most {SEARCH_MAX_LENGTH} characters."


def clean_search_line(value: str) -> str:
    """A search box's value cleaned like any line of text (clean_line), and still within
    SEARCH_MAX_LENGTH afterwards: NFC can lengthen text (U+FB2C is three code points), and
    the limit is on what is searched. TextRejected or ValueError otherwise."""
    value = clean_line(value)
    if len(value) > SEARCH_MAX_LENGTH:
        raise ValueError(SEARCH_TOO_LONG)
    return value


def clean_search_query(value: str) -> str:
    """The Leads list's search box: clean_search_line, and ValueError when no word is long
    enough to search ("" stays "")."""
    value = clean_search_line(value)
    if value and all(len(term) < SEARCH_TERM_MIN_LENGTH for term in value.split()):
        raise ValueError(SEARCH_TERM_TOO_SHORT)
    return value


def search_needles(q: str, *, searchable: Callable[[str], bool] | None = None) -> list[str]:
    """The words of `q` that are searched: cleaned, searchable (by default at least
    SEARCH_TERM_MIN_LENGTH characters: the Leads list; global search passes its own rule,
    core.ranking.indexable), each once (ignoring case), the first SEARCH_MAX_TERMS of those.
    Other words are skipped *before* counting, so "a b c d e Rahul" searches "Rahul" (it
    used to search nothing) and "rahul rahul rahul rahul rahul sharma" searches both
    names. TextRejected for characters no stored text can contain."""
    accept = searchable or (lambda needle: len(needle) >= SEARCH_TERM_MIN_LENGTH)
    needles: list[str] = []
    seen: set[str] = set()
    for term in q.split():
        needle = clean_line(term)
        if accept(needle) and needle.casefold() not in seen:
            seen.add(needle.casefold())
            needles.append(needle)
    return needles[:SEARCH_MAX_TERMS]
