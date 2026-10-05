"""An opportunity's name (docs/pipeline.md#the-opportunitys-name).

Nobody types an opportunity's name: it is derived from the deal's own details, the same way
every time, and stored as its `title` (what the board, search, Ask Arkray and the history
show):

    "<customer> — <instrument>"     e.g. "ABC Diagnostics Mumbai — Adams 8380 V-lite"
    "<customer>"                    no instrument chosen

where <customer> is the customer name, or the account name when there is no customer name.
The services set it when the opportunity is created and again whenever its customer name,
account name or instrument changes; nothing else changes it. An opportunity created before
this rule keeps the name someone typed until one of those details changes.

A name longer than the title's 200 characters is shortened with an ellipsis, the customer
part first (an instrument is short), so it always fits and still reads the same.
"""

from __future__ import annotations

from .models import TITLE_MAX_LENGTH

SEPARATOR = " — "  # an em dash between spaces
ELLIPSIS = "…"
# The fields the name is made of: a change to any of them renames the opportunity.
SOURCES = frozenset({"customer_name", "account_name", "instrument_name"})


def _shorten(text: str, length: int) -> str:
    return text if len(text) <= length else text[: length - 1].rstrip() + ELLIPSIS


def opportunity_title(*, customer_name: str, account_name: str, instrument_name: str) -> str:
    who = customer_name.strip() or account_name.strip()
    if not who:
        raise ValueError("An opportunity's name needs its customer or account name.")
    instrument = instrument_name.strip()
    if not instrument:
        return _shorten(who, TITLE_MAX_LENGTH)
    if len(who) + len(SEPARATOR) + len(instrument) <= TITLE_MAX_LENGTH:
        return f"{who}{SEPARATOR}{instrument}"
    # Only an over-long legacy instrument text can need more than half of the room.
    instrument = _shorten(instrument, TITLE_MAX_LENGTH // 2)
    who = _shorten(who, TITLE_MAX_LENGTH - len(SEPARATOR) - len(instrument))
    return f"{who}{SEPARATOR}{instrument}"
