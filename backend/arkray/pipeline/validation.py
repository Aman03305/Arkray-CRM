"""Opportunity field rules, applied by the services to every write (API, conversion, future
imports and tools). Serializers check shapes; these decide validity and canonical form.

- Title: one line of text (core.text), required, up to 200 characters.
- Value: a Decimal (or int) amount in the organisation currency, 0 to 999,999,999,999.99,
  at most 2 decimal places. Floats are refused outright: money is never converted through
  binary floating point, and NaN/Infinity can't even be represented.
- Probability: a Decimal (or int) percentage, 0 to 100, at most 2 decimal places.
- Expected close date: a date (not a datetime: no time zone can shift it), 2000-2099.
- Description: multi-line text up to 5,000 characters. Lost reason: up to 500.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from arkray.core.errors import InvalidInputError
from arkray.core.text import TextRejected, clean_line, clean_multiline

from . import models as m

TITLE_REQUIRED = "Enter a title."
VALUE_INVALID = "Enter an amount such as 1250000 or 1250000.50."
PROBABILITY_INVALID = "Enter a percentage from 0 to 100, with at most 2 decimal places."


def _exact(value: Any, *, message: str) -> Decimal:
    # bool is an int subclass; float would already have lost exactness.
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise ValueError(message)
    number = Decimal(value)
    try:
        if not number.is_finite() or number != number.quantize(m.HUNDREDTH):
            raise ValueError(message)
    except InvalidOperation:  # too many digits to quantize: certainly out of range
        raise ValueError(message) from None
    return number.quantize(m.HUNDREDTH)


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter text.")
    title = clean_line(value)
    if not title:
        raise ValueError(TITLE_REQUIRED)
    if len(title) > m.TITLE_MAX_LENGTH:
        raise ValueError(f"Use at most {m.TITLE_MAX_LENGTH} characters.")
    return title


def clean_value(value: Any) -> Decimal:
    amount = _exact(value, message=VALUE_INVALID)
    if amount < 0:
        raise ValueError("The value can't be negative.")
    if amount > m.MAX_VALUE:
        raise ValueError("The value must be at most 999,999,999,999.99.")
    return amount


def clean_probability(value: Any) -> Decimal | None:
    """None means "use the stage's default"."""
    if value is None:
        return None
    probability = _exact(value, message=PROBABILITY_INVALID)
    if not m.IMPOSSIBLE <= probability <= m.CERTAIN:
        raise ValueError(PROBABILITY_INVALID)
    return probability


def clean_expected_close_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("Enter a date.")
    day: date = value
    if not m.EARLIEST_CLOSE_DATE <= day <= m.LATEST_CLOSE_DATE:
        raise ValueError("Enter a date between 2000 and 2099.")
    return day


def _multiline(max_length: int) -> Callable[[Any], str]:
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Enter text.")
        text = clean_multiline(value)
        if len(text) > max_length:
            raise ValueError(f"Use at most {max_length:,} characters.")
        return text

    return clean


CLEANERS: Mapping[str, Callable[[Any], Any]] = {
    "title": clean_title,
    "value": clean_value,
    "probability": clean_probability,
    "expected_close_date": clean_expected_close_date,
    "description": _multiline(m.DESCRIPTION_MAX_LENGTH),
    "lost_reason": _multiline(m.LOST_REASON_MAX_LENGTH),
}
# What PATCH may change. Lead, owner, pipeline, stage, status, closed_at, provenance,
# timestamps, archive state and version have their own rules and operations.
EDITABLE_FIELDS = frozenset(CLEANERS)


def clean_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every given field cleaned, or InvalidInputError listing every problem at once."""
    unknown = set(data) - EDITABLE_FIELDS
    if unknown:
        raise InvalidInputError(
            details={
                "non_field_errors": [f"These fields can't be set: {', '.join(sorted(unknown))}."]
            }
        )
    cleaned: dict[str, Any] = {}
    errors: dict[str, list[str]] = {}
    for field, value in data.items():
        try:
            cleaned[field] = CLEANERS[field](value)
        except (ValueError, TextRejected) as exc:
            errors[field] = [str(exc)]
    if errors:
        raise InvalidInputError(details=errors)
    return cleaned
