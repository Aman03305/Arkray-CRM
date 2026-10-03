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
"""

from __future__ import annotations

import unicodedata

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
