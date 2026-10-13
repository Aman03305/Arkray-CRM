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
- Every other character Unicode lists as Default_Ignorable_Code_Point (it renders as nothing)
  is refused too, whatever its category (adversarial review of 73ff1e3, L12, R164; its review
  round 1): the Hangul fillers (U+115F, U+1160, U+3164, U+FFA0), the combining grapheme joiner
  (U+034F), the Khmer inherent vowels (U+17B4, U+17B5), the unassigned U+2065, U+FFF0-U+FFF8 and
  the rest of the tag block (U+E0000-U+E0FFF). So are a few characters that render blank but are
  not on that list: the Braille blank (U+2800), U+1D159, U+FFFC, the Egyptian hieroglyph full and
  half blanks (U+13441, U+13442) and the Khitan filler (U+16FE4). A name can then neither render
  empty nor dodge an exact-match check by looking identical to another.
- Selectors are kept only where Unicode defines what they select (review rounds 2 and 3;
  Python has no emoji data, so it is kept here): U+FE0E/U+FE0F after one of the 359 emoji that
  have variation sequences (emoji-variation-sequences.txt), or after a keycap base (0-9, `#`, `*`)
  only when the keycap mark U+20E3 follows; U+FE00 after the math symbols and the digit zero
  that have a standardized variant; U+FE00-U+FE02 after the CJK ideograph that the CJK
  compatibility ideograph's variant is defined for (derived from the compatibility ideographs'
  decompositions: 1,002 sequences); an ideographic variation selector U+E0100-U+E013F after a CJK
  ideograph; a Mongolian free variation selector (U+180B-U+180D, U+180F) after a Mongolian
  letter. Anywhere else a selector renders as nothing ("Rahul" + U+FE0F looks exactly like
  "Rahul", "A+" + U+FE0F like "A+", an arrow or a star like itself) and is refused.
- Code points unassigned in this Python's Unicode data are refused (they may render as nothing),
  except in the emoji blocks (U+1F000-U+1FAFF), where newer emoji a phone sends arrive before
  Python knows them; so are U+303F (IDEOGRAPHIC HALF FILL SPACE) and the Tifinagh consonant
  joiner U+2D7F anywhere but between two Tifinagh letters (review round 3).
- Single-line values then have every run of whitespace (tabs and newlines pasted in
  included) collapsed to one space and are trimmed.
- A value with nothing visible left (only joiners or combining marks) counts as empty, so it
  can never pass for a name nobody can see.

Search input follows the same rules, so what is typed and what is stored compare alike
(the search-input section below).
"""

from __future__ import annotations

import itertools
import unicodedata
from collections.abc import Callable, Iterator

_ALLOWED_FORMAT = frozenset({chr(0x200C), chr(0x200D)})  # ZWNJ, ZWJ
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Zl", "Zp"})
_LINE_CONTROLS = frozenset("\t\n\r")  # collapsed to a space in single-line values
_MULTILINE_CONTROLS = frozenset("\t\n")
# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties.txt, 15.1): characters that
# render as nothing. Most are Cf and refused by category already; the others (Lo fillers, Mn
# marks, unassigned Cn) are refused by code point here (adversarial review of 73ff1e3, L12,
# R164). ZWJ/ZWNJ (allowed above) and the selectors (allowed only after a base, see _selects)
# are taken out of the plain refusal.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
)
_TAG_BLOCK = range(0xE0000, 0xE1000)  # E0000..E0FFF, ignorable whole (the tags are Cf)
# Variation selectors (VS1-16, VS17-256) and Mongolian free variation selectors.
_VARIATION_SELECTORS = frozenset((*range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)))
_IDEOGRAPHIC_SELECTORS = range(0xE0100, 0xE01F0)
_PRESENTATION_SELECTORS = frozenset({0xFE0E, 0xFE0F})  # text and emoji presentation
_MONGOLIAN_SELECTORS = frozenset({0x180B, 0x180C, 0x180D, 0x180F})
# The emoji with variation sequences (UTS #51, emoji-variation-sequences.txt), keycaps aside:
# review round 2 (PARSERS2-8) kept a selector after any symbol of the arrows-to-dingbats and emoji
# blocks, and review round 3 (PARSERS3-4) found it after an arrow, a sum, an infinity, a star, a
# check mark, an emoji-default face or a playing card, none of which has a sequence. Two parts:
# every emoji (Emoji=Yes, not ASCII) that is shown as text by default or is in the BMP, derived
# from Unicode 17.0 (ICU 78, Node 24: 267 code points); and the emoji shown as emoji by default,
# outside the BMP, that the file lists too (92 code points, typed from it).
_EMOJI_TEXT_OR_BMP = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122),
    (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328),
    (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2604), (0x260E, 0x260E),
    (0x2611, 0x2611), (0x2614, 0x2615), (0x2618, 0x2618), (0x261D, 0x261D), (0x2620, 0x2620),
    (0x2622, 0x2623), (0x2626, 0x2626), (0x262A, 0x262A), (0x262E, 0x262F), (0x2638, 0x263A),
    (0x2640, 0x2640), (0x2642, 0x2642), (0x2648, 0x2653), (0x265F, 0x2660), (0x2663, 0x2663),
    (0x2665, 0x2666), (0x2668, 0x2668), (0x267B, 0x267B), (0x267E, 0x267F), (0x2692, 0x2697),
    (0x2699, 0x2699), (0x269B, 0x269C), (0x26A0, 0x26A1), (0x26A7, 0x26A7), (0x26AA, 0x26AB),
    (0x26B0, 0x26B1), (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26C8, 0x26C8), (0x26CE, 0x26CF),
    (0x26D1, 0x26D1), (0x26D3, 0x26D4), (0x26E9, 0x26EA), (0x26F0, 0x26F5), (0x26F7, 0x26FA),
    (0x26FD, 0x26FD), (0x2702, 0x2702), (0x2705, 0x2705), (0x2708, 0x270D), (0x270F, 0x270F),
    (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721),
    (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2763, 0x2764), (0x2795, 0x2797),
    (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F170, 0x1F171), (0x1F17E, 0x1F17F),
    (0x1F202, 0x1F202), (0x1F237, 0x1F237), (0x1F321, 0x1F321), (0x1F324, 0x1F32C),
    (0x1F336, 0x1F336), (0x1F37D, 0x1F37D), (0x1F396, 0x1F397), (0x1F399, 0x1F39B),
    (0x1F39E, 0x1F39F), (0x1F3CB, 0x1F3CE), (0x1F3D4, 0x1F3DF), (0x1F3F3, 0x1F3F3),
    (0x1F3F5, 0x1F3F5), (0x1F3F7, 0x1F3F7), (0x1F43F, 0x1F43F), (0x1F441, 0x1F441),
    (0x1F4FD, 0x1F4FD), (0x1F549, 0x1F54A), (0x1F56F, 0x1F570), (0x1F573, 0x1F579),
    (0x1F587, 0x1F587), (0x1F58A, 0x1F58D), (0x1F590, 0x1F590), (0x1F5A5, 0x1F5A5),
    (0x1F5A8, 0x1F5A8), (0x1F5B1, 0x1F5B2), (0x1F5BC, 0x1F5BC), (0x1F5C2, 0x1F5C4),
    (0x1F5D1, 0x1F5D3), (0x1F5DC, 0x1F5DE), (0x1F5E1, 0x1F5E1), (0x1F5E3, 0x1F5E3),
    (0x1F5E8, 0x1F5E8), (0x1F5EF, 0x1F5EF), (0x1F5F3, 0x1F5F3), (0x1F5FA, 0x1F5FA),
    (0x1F6CB, 0x1F6CB), (0x1F6CD, 0x1F6CF), (0x1F6E0, 0x1F6E5), (0x1F6E9, 0x1F6E9),
    (0x1F6F0, 0x1F6F0), (0x1F6F3, 0x1F6F3),
)  # fmt: skip
_EMOJI_DEFAULT_WITH_VARIANTS = (
    (0x1F004, 0x1F004), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F30D, 0x1F30F),
    (0x1F315, 0x1F315), (0x1F31C, 0x1F31C), (0x1F378, 0x1F378), (0x1F393, 0x1F393),
    (0x1F3A7, 0x1F3A7), (0x1F3AC, 0x1F3AE), (0x1F3C2, 0x1F3C2), (0x1F3C4, 0x1F3C4),
    (0x1F3C6, 0x1F3C6), (0x1F3CA, 0x1F3CA), (0x1F3E0, 0x1F3E0), (0x1F3ED, 0x1F3ED),
    (0x1F408, 0x1F408), (0x1F415, 0x1F415), (0x1F41F, 0x1F41F), (0x1F426, 0x1F426),
    (0x1F442, 0x1F442), (0x1F446, 0x1F449), (0x1F44D, 0x1F44E), (0x1F453, 0x1F453),
    (0x1F46A, 0x1F46A), (0x1F47D, 0x1F47D), (0x1F4A3, 0x1F4A3), (0x1F4B0, 0x1F4B0),
    (0x1F4B3, 0x1F4B3), (0x1F4BB, 0x1F4BB), (0x1F4BF, 0x1F4BF), (0x1F4CB, 0x1F4CB),
    (0x1F4DA, 0x1F4DA), (0x1F4DF, 0x1F4DF), (0x1F4E4, 0x1F4E6), (0x1F4EA, 0x1F4ED),
    (0x1F4F7, 0x1F4F7), (0x1F4F9, 0x1F4FB), (0x1F508, 0x1F508), (0x1F50D, 0x1F50D),
    (0x1F512, 0x1F513), (0x1F550, 0x1F567), (0x1F610, 0x1F610), (0x1F687, 0x1F687),
    (0x1F68D, 0x1F68D), (0x1F691, 0x1F691), (0x1F694, 0x1F694), (0x1F698, 0x1F698),
    (0x1F6AD, 0x1F6AD), (0x1F6B2, 0x1F6B2), (0x1F6B9, 0x1F6BA), (0x1F6BC, 0x1F6BC),
)  # fmt: skip
_EMOJI_WITH_VARIANTS = frozenset(
    chr(code)
    for low, high in (*_EMOJI_TEXT_OR_BMP, *_EMOJI_DEFAULT_WITH_VARIANTS)
    for code in range(low, high + 1)
)
_KEYCAP_BASES = frozenset("0123456789#*")
_KEYCAP = chr(0x20E3)
# StandardizedVariants.txt, outside CJK: VS1 (U+FE00) on these math symbols and the digit zero.
# Myanmar, Phags-pa, Manichaean and Egyptian variants are rare in names and not kept.
_STANDARDIZED_BASES = frozenset("0") | frozenset(
    chr(code)
    for code in (
        0x2229, 0x222A, 0x2268, 0x2269, 0x2272, 0x2273, 0x228A, 0x228B, 0x2293, 0x2294,
        0x2295, 0x2297, 0x229C, 0x22DA, 0x22DB, 0x2A3C, 0x2A3D, 0x2A9D, 0x2A9E, 0x2AAC,
        0x2AAD, 0x2ACB, 0x2ACC,
    )
)  # fmt: skip
# The CJK compatibility ideographs' standardized variants (StandardizedVariants.txt): each
# compatibility ideograph is its unified ideograph followed by VS1, VS2 or VS3, numbered in the
# compatibility ideographs' code point order (U+8C48 U+FE00 is U+F900). Derived from their
# canonical decompositions: 1,002 sequences on 902 ideographs.
_CJK_VARIANTS: frozenset[tuple[str, int]] = frozenset(
    (base, 0xFE00 + index)
    for base, variants in itertools.groupby(
        sorted(
            (
                (unicodedata.normalize("NFC", chr(code)), code)
                for code in (*range(0xF900, 0xFB00), *range(0x2F800, 0x2FA20))
                if unicodedata.decomposition(chr(code))[:1] not in ("", "<")
            ),
            key=lambda pair: (pair[0], pair[1]),
        ),
        key=lambda pair: pair[0],
    )
    for index, _ in enumerate(variants)
)
_IVS_IN_USE = range(0xE0100, 0xE0140)  # VS17-VS80: what the Ideographic Variation Database uses
_REFUSED_CODE_POINTS = frozenset(
    code
    for low, high in _DEFAULT_IGNORABLE_RANGES
    for code in range(low, high + 1)
    if code not in (0x200C, 0x200D)
    and code not in _VARIATION_SELECTORS
    and code not in _MONGOLIAN_SELECTORS
) | {
    # Render blank but aren't Default_Ignorable (So).
    0x2800,  # BRAILLE PATTERN BLANK
    0x1D159,  # MUSICAL SYMBOL NULL NOTEHEAD
    0xFFFC,  # OBJECT REPLACEMENT CHARACTER
    0x13441,  # EGYPTIAN HIEROGLYPH FULL BLANK (Lo; review round 2)
    0x13442,  # EGYPTIAN HIEROGLYPH HALF BLANK (Lo)
    0x16FE4,  # KHITAN SMALL SCRIPT FILLER (Mn)
    0x303F,  # IDEOGRAPHIC HALF FILL SPACE (So; review round 3)
}
_TIFINAGH_JOINER = 0x2D7F  # TIFINAGH CONSONANT JOINER (Mn): only between two Tifinagh letters
_TIFINAGH_LETTERS = range(0x2D30, 0x2D68)
_EMOJI_BLOCKS = range(0x1F000, 0x1FB00)  # where newer emoji land (unassigned here, still kept)


class TextRejected(ValueError):
    """The value contains characters that are never accepted."""


def _refused(char: str, allowed_controls: frozenset[str]) -> bool:
    if char in allowed_controls:
        return False
    code = ord(char)
    if code in _REFUSED_CODE_POINTS:
        return True
    if code in _TAG_BLOCK and code not in _IDEOGRAPHIC_SELECTORS:
        return True
    category = unicodedata.category(char)
    if category in _REFUSED_CATEGORIES:
        return True
    if category == "Cf":
        return char not in _ALLOWED_FORMAT
    if category == "Cn":  # unassigned (noncharacters too), outside the emoji blocks
        return code not in _EMOJI_BLOCKS or 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFE) == 0xFFFE
    return False


def _ideograph(char: str) -> bool:
    return unicodedata.name(char, "").startswith(
        ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")
    )


def _joins_tifinagh(previous: str, following: str) -> bool:
    """Whether a Tifinagh consonant joiner stands between two Tifinagh letters (`previous` and
    `following`: a character each, or "")."""
    return bool(previous and following) and (
        ord(previous) in _TIFINAGH_LETTERS and ord(following) in _TIFINAGH_LETTERS
    )


def _selects(selector: int, base: str, following: str) -> bool:
    """Whether a selector (`selector`, a code point) right after `base`, with `following` after
    it, selects a variant, from the sets above: a presentation selector after an emoji with
    variation sequences, or after a keycap base when the keycap mark follows; VS1 after a symbol
    with a standardized variant; VS1-VS3 after the CJK ideograph its compatibility variant is
    defined for; an ideographic selector in use after a CJK ideograph; a Mongolian free
    variation selector after a Mongolian letter. "" (nothing before) carries none."""
    if not base:
        return False
    if selector in _MONGOLIAN_SELECTORS:
        return unicodedata.name(base, "").startswith("MONGOLIAN LETTER")
    if selector in _IDEOGRAPHIC_SELECTORS:
        return selector in _IVS_IN_USE and _ideograph(base)
    if selector in _PRESENTATION_SELECTORS:
        if base in _KEYCAP_BASES:
            return following == _KEYCAP
        return base in _EMOJI_WITH_VARIANTS
    if (base, selector) in _CJK_VARIANTS:
        return True
    return selector == 0xFE00 and base in _STANDARDIZED_BASES


def _refusals(value: str, allowed_controls: frozenset[str]) -> Iterator[int]:
    """The position of each character of `value` that is refused: one the rules above never
    accept, or a selector that selects nothing after the character before it. Linear: what
    follows a character is looked at one character at a time (review round 4, PARSERS4-2: the
    Tifinagh joiner's check copied the rest of the value, 1.6 s for 340,000 characters)."""
    previous = ""
    for index, char in enumerate(value):
        code = ord(char)
        selector = code in _VARIATION_SELECTORS or code in _MONGOLIAN_SELECTORS
        if (
            _refused(char, allowed_controls)
            or (selector and not _selects(code, previous, value[index + 1 : index + 2]))
            or (
                code == _TIFINAGH_JOINER
                and not _joins_tifinagh(previous, value[index + 1 : index + 2])
            )
        ):
            yield index
        else:
            previous = "" if selector else char  # a selector is no base for another


def _check(value: str, allowed_controls: frozenset[str]) -> None:
    if next(_refusals(value, allowed_controls), None) is not None:
        raise TextRejected("Remove the invisible or control characters from this text.")


def without_refused(value: str) -> str:
    """Text we didn't receive from a person (a language model's answer) with every
    character the input rules refuse removed: bidi overrides, tag characters, blank
    look-alikes, standalone selectors, controls other than newline and tab. Input is refused
    instead (the person can fix it)."""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    dropped = set(_refusals(value, _MULTILINE_CONTROLS))
    return "".join(char for index, char in enumerate(value) if index not in dropped)


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
