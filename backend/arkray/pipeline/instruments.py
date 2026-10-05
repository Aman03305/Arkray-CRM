"""The instruments an opportunity can be for: the one authoritative list
(docs/pipeline.md#instruments).

The API serves it (GET /api/v1/config/opportunity-options), the UI offers exactly these, and
the services refuse any other instrument for a new opportunity or a changed instrument
(validation is never left to the browser). An opportunity stores the instrument's name, as
it is spelled here.

Adding an instrument is one line here (nothing in the database lists them, so no migration).
Names are stored on opportunities, so renaming one would also need a data migration of
`pipeline_opportunity.instrument_name`: keep names stable. Opportunities saved before the list
existed may hold any text; they keep it until someone changes the instrument.
"""

from __future__ import annotations

INSTRUMENTS: tuple[str, ...] = (
    "Adams 8380 V-lite",
    "Adams 8180 V",
    "Adams 8180 T",
    "PCBA with Printer",
)

UNKNOWN_INSTRUMENT = "Choose an instrument from the list."

_BY_KEY = {" ".join(name.split()).casefold(): name for name in INSTRUMENTS}


def canonical(name: str) -> str | None:
    """The list's spelling of `name` (letter case and spacing aside), or None when it isn't
    one of the instruments."""
    return _BY_KEY.get(" ".join(name.split()).casefold())
