"""Activity field rules, applied by the services to every write (API, future imports, Ask
Arkray tools). Serializers check shapes; these decide validity and canonical form, and
which fields each type may carry at all (a task with a meeting link is a 400, not a
silently ignored key).

- Subject (`title`): one line of text (core.text), up to 200 characters; tasks and meetings.
- Description / agenda / note body: multi-line text up to 10,000 characters; required for
  notes (it *is* the note).
- Priority: low, normal (default) or high; tasks.
- Due time: an aware timestamp, 2000-2099, optional; tasks. Overdue is computed when read.
- Start and end: aware timestamps, 2000-2099, the end after the start, at most 24 hours
  apart; meetings.
- Location: one line, up to 200 characters. Meeting link: an https:// URL up to 500
  characters without a user name or password in it (a link with credentials would leak them
  to everyone who can see the meeting); meetings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone

from arkray.core.errors import InvalidInputError
from arkray.core.text import TextRejected, clean_line, clean_multiline

from . import models as m
from .spec import ALL_FIELDS, TypeSpec

TITLE_REQUIRED = "Enter a subject."
NOTE_REQUIRED = "Write the note."
END_BEFORE_START = "The end must be after the start."
TOO_LONG_MEETING = "A meeting can last at most 24 hours."
OUT_OF_RANGE = "Enter a date between 2000 and 2099."
NOT_FOR_TYPE = "{label}s don't have this field."
REQUIRED = "This field is required."
_https_url = URLValidator(schemes=["https"])


def _line(max_length: int) -> Callable[[Any], str]:
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Enter text.")
        text = clean_line(value)
        if len(text) > max_length:
            raise ValueError(f"Use at most {max_length} characters.")
        return text

    return clean


def _description(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter text.")
    text = clean_multiline(value)
    if len(text) > m.DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"Use at most {m.DESCRIPTION_MAX_LENGTH:,} characters.")
    return text


def _priority(value: Any) -> str:
    if value not in m.Priority.values:
        raise ValueError("Choose low, normal or high.")
    return str(value)


def _moment(*, optional: bool) -> Callable[[Any], datetime | None]:
    def clean(value: Any) -> datetime | None:
        if value is None and optional:
            return None
        if not isinstance(value, datetime) or timezone.is_naive(value):
            raise ValueError("Enter a date and time with a time zone.")
        if not m.EARLIEST_TIME <= value < m.LATEST_TIME:
            raise ValueError(OUT_OF_RANGE)
        return value

    return clean


def _meeting_url(value: Any) -> str:
    url = _line(m.MEETING_URL_MAX_LENGTH)(value)
    if not url:
        return ""
    try:
        _https_url(url)
    except ValidationError:
        raise ValueError("Enter a link starting with https://.") from None
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ValueError("Remove the user name and password from the link.")
    # The validator accepts any spelling of the scheme ("HTTPS://"); store the canonical
    # one, which is also what the database's CHECK requires (review: a 500 otherwise).
    return "https://" + url[len("https://") :]


CLEANERS: Mapping[str, Callable[[Any], Any]] = {
    "title": _line(m.TITLE_MAX_LENGTH),
    "description": _description,
    "priority": _priority,
    "due_at": _moment(optional=True),
    "starts_at": _moment(optional=False),
    "ends_at": _moment(optional=False),
    "location": _line(m.LOCATION_MAX_LENGTH),
    "meeting_url": _meeting_url,
}


def clean_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every given field cleaned, or InvalidInputError listing every problem at once."""
    unknown = set(data) - ALL_FIELDS
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


def require_fields_of(spec: TypeSpec, cleaned: Mapping[str, Any], *, creating: bool) -> None:
    """Only the type's own fields; on creation, every required one (and non-empty)."""
    errors: dict[str, list[str]] = {
        field: [NOT_FOR_TYPE.format(label=spec.label)]
        for field in cleaned
        if field not in spec.fields
    }
    for field in spec.required:
        present = field in cleaned
        if (creating and not present) or (present and cleaned[field] in ("", None)):
            errors[field] = [_required_message(field)]
    if errors:
        raise InvalidInputError(details=errors)


def _required_message(field: str) -> str:
    if field == "title":
        return TITLE_REQUIRED
    if field == "description":
        return NOTE_REQUIRED
    return REQUIRED


def check_meeting_times(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise InvalidInputError(details={"ends_at": [END_BEFORE_START]})
    if ends_at - starts_at > m.MAX_MEETING_DURATION:
        raise InvalidInputError(details={"ends_at": [TOO_LONG_MEETING]})
