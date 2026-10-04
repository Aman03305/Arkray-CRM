"""How Ask Arkray states figures and dates: formatted by the server from exact values, so
the model only ever repeats them (it is never asked to compute or convert).

- Money: Decimal in, Indian digit grouping out ("₹12,50,000", paise only when present),
  the same rules as the frontend's formatInr. Never float.
- Dates and times: in the organisation's time zone (CRM_TIME_ZONE), so the model never
  converts from UTC ("10:30" means 10:30 where the business is).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings

from arkray.core.business_time import business_zone

_PAISA = Decimal("0.01")
CURRENCY_SYMBOLS = {"INR": "₹"}


def group_indian(digits: str) -> str:
    """'1250000' -> '12,50,000'; '1234' -> '1,234'."""
    whole = digits.lstrip("0") or "0"
    if len(whole) <= 3:
        return whole
    head, last3 = whole[:-3], whole[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join([*pairs, last3])


def money(amount: Decimal) -> dict[str, str]:
    """An amount as the tools state it: the exact decimal string and its display form."""
    if not isinstance(amount, Decimal):
        raise TypeError("Money is Decimal, never float.")
    exact = amount.quantize(_PAISA, rounding=ROUND_HALF_UP)
    whole, _, paise = format(abs(exact), "f").partition(".")
    symbol = CURRENCY_SYMBOLS.get(settings.CRM_CURRENCY, f"{settings.CRM_CURRENCY} ")
    shown = f"{symbol}{group_indian(whole)}" + (f".{paise}" if paise.strip("0") else "")
    return {
        "amount": format(exact, "f"),
        "currency": settings.CRM_CURRENCY,
        "display": f"-{shown}" if exact < 0 else shown,
    }


def percent(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return f"{text}%"


def local(moment: datetime) -> datetime:
    return moment.astimezone(business_zone())


def when(moment: datetime | None) -> dict[str, str] | None:
    """A date and time in the business time zone: ISO (with offset) and a display form."""
    if moment is None:
        return None
    at = local(moment)
    hour = at.strftime("%I").lstrip("0")
    return {
        "iso": at.isoformat(timespec="minutes"),
        "display": f"{at.day} {at.strftime('%b %Y')}, {hour}:{at.strftime('%M %p')}",
    }


def day(value: date | datetime | None) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = local(value).date()
    return {"iso": value.isoformat(), "display": f"{value.day} {value.strftime('%b %Y')}"}


# What never leaves the deployment from user-written text (docs/rag-architecture.md#privacy):
# web links, with or without a scheme (they often carry meeting passcodes or document
# tokens), email addresses and phone numbers. The record itself is a click away.
_SENSITIVE = re.compile(
    r"(?P<link>(?:https?|ftp)://\S+|www\.\S+"
    # scheme-less host/path: zoom.us/j/123?pwd=..., meet.google.com/abc-defg-hij
    r"|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}/\S*)"
    r"|(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
    # +country numbers, and Indian mobiles (10 digits from 6-9, optionally 5+5): never a
    # grouped amount such as 7,77,77,777 (commas and dots around digits don't match).
    r"|(?P<phone>\+\d[\d ()-]{7,}\d|(?<![\d,.])[6-9]\d{4}[ -]?\d{5}(?![\d,]))",
    re.IGNORECASE,
)
_MARKERS = {"link": "[link]", "email": "[email]", "phone": "[phone]"}


def _marker(match: re.Match[str]) -> str:
    return _MARKERS[match.lastgroup or "link"]


def user_text(text: str) -> str:
    """User-written text as it may go to the model: links, email addresses and phone
    numbers replaced by markers (Phase 9 review: titles, lost reasons and scheme-less links
    went out verbatim). Every user-written string in a tool result passes through here."""
    return _SENSITIVE.sub(_marker, text)


def user_text_slice(text: str, start: int, end: int) -> str:
    """`user_text` of text[start:end], judged on the whole text: a link cut by the slice
    (a chunk boundary in the middle of a URL) is replaced as a whole, never left as a
    passcode-bearing tail."""
    parts: list[str] = []
    position = start
    for match in _SENSITIVE.finditer(text):
        if match.end() <= start:
            continue
        if match.start() >= end:
            break
        if match.start() > position:
            parts.append(text[position : match.start()])
        parts.append(_marker(match))
        position = max(position, match.end())
    if position < end:
        parts.append(text[position:end])
    return "".join(parts)


def clip(text: str, limit: int) -> dict[str, Any]:
    """Bounded user-written text for tool results: the first `limit` characters (links,
    email addresses and phone numbers removed) and whether it was cut."""
    text = user_text(text.strip())
    return {"text": text[:limit], "truncated": len(text) > limit}


def label(text: str | None) -> str | None:
    """A user-written title, name or label for a tool result (None stays None)."""
    return None if text is None else user_text(text)
