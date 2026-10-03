"""Phone numbers: stored as the user typed them, matched through a canonical key.

Arkray sells across borders, so no country is assumed. A number is kept exactly as entered
(whitespace tidied) because that is how people recognise and dial it: "+91 98765 43210",
"(022) 2345 6789", "+1 555 010 9999 ext. 12". Nothing is reformatted, and the leading "+"
and any extension are never lost.

Accepted: an optional leading "+", then digits and the separators space ( ) . - /, then
optionally an extension introduced by "x", "ext", "ext.", "extension", "#" or ";ext=".
The number itself must have 5-17 digits (E.164 allows 15; national trunk and "00"
international prefixes add up to two), the extension 1-7. Full-width digits and symbols
(common in Japanese input) are converted to ASCII first (NFKC); other scripts' digits are
refused, so every stored number is dialable.

`phone_key()` is the canonical matching form used for duplicate assistance:
"+" + digits for international numbers (a leading "00" counts as "+", and a "(0)" trunk
marker after the country code is dropped), digits only for national numbers, then "x" +
extension digits. National and international spellings of
one number ("098765 43210" vs "+91 98765 43210") get different keys on purpose: telling
them apart needs the country's numbering plan, which a later phase can add (region-aware
parsing using the lead's country) without changing what is stored.
"""

from __future__ import annotations

import re

PHONE_MAX_LENGTH = 40
MIN_DIGITS, MAX_DIGITS = 5, 17
MAX_EXTENSION_DIGITS = 7

# ASCII digits only (a bare \d would accept Devanagari or Arabic-Indic digits).
_EXTENSION = re.compile(r"\s*(?:;ext=|extension|ext\.?|x|#)\s*([0-9]+)$", re.IGNORECASE)
_TRUNK_ZERO = re.compile(r"\(0\)")  # "+44 (0)20 ...": the (0) is dropped when dialling from abroad
_NUMBER = re.compile(r"^\+?[0-9 ().\-/]+$")
_NOT_DIGIT = re.compile(r"[^0-9]")

INVALID_PHONE = (
    "Enter a phone number using digits, spaces and ( ) . - / only, with an optional leading +"
    " and extension (for example +91 98765 43210 or 022 2345 6789 ext. 12)."
)


class InvalidPhone(ValueError):
    pass


def _split(value: str) -> tuple[str, str]:
    match = _EXTENSION.search(value)
    if match is None:
        return value, ""
    return value[: match.start()].strip(), match.group(1)


def clean_phone(value: str) -> str:
    """Validate a phone number (already a tidied single line) and return it unchanged."""
    if not value:
        return ""
    number, extension = _split(value)
    digits = _NOT_DIGIT.sub("", number)
    if (
        not _NUMBER.fullmatch(number)
        or not MIN_DIGITS <= len(digits) <= MAX_DIGITS
        or len(extension) > MAX_EXTENSION_DIGITS
    ):
        raise InvalidPhone(INVALID_PHONE)
    return value


def phone_key(value: str) -> str:
    """Canonical form for exact matching ("" for a blank or unparseable number)."""
    if not value:
        return ""
    number, extension = _split(value)
    if number.startswith(("+", "00")):
        number = _TRUNK_ZERO.sub("", number, count=1)
    digits = _NOT_DIGIT.sub("", number)
    if not digits:
        return ""
    if number.startswith("+"):
        key = f"+{digits}"
    elif digits.startswith("00"):
        key = f"+{digits[2:]}"
    else:
        key = digits
    return f"{key}x{extension}" if extension else key


def search_digits(term: str) -> str | None:
    """A search term that looks like (part of) a phone number, reduced to its digits, so
    "98765-43210" finds "+91 98765 43210". None for ordinary words."""
    if re.fullmatch(r"[+0-9().\-/]+", term) and len(_NOT_DIGIT.sub("", term)) >= 2:
        return _NOT_DIGIT.sub("", term)
    return None
