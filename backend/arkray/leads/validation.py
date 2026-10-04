"""Lead field rules, applied by the services to every write (API, future imports, jobs).

The serializers check shapes and types; these functions decide what is *valid* and store it
in canonical form. Each returns the cleaned value or raises ValueError with a message that
is safe to show next to the field. Rules (docs/leads.md#field-rules):

- Names, organisation, job title, address lines, city, state: one line of text (NFC,
  whitespace collapsed, control and bidi-override characters refused, core.text), any
  script. Both person-name fields are optional; see `require_identity`.
- Email: contact data, not a sign-in identity. Optional, syntax-checked, stored as typed
  (trimmed); international domain names are accepted. Compared case-insensitively.
- Phones: stored as typed, validated by `phones.clean_phone`.
- Postal code: letters, digits, spaces and hyphens (IN 400001, UK SW1A 1AA, US 12345-6789).
- Country: ISO 3166-1 alpha-2 code (case-insensitive on input, stored upper-case).
- Last contacted: an aware timestamp, not in the future (5 minutes' clock-skew allowance),
  not before 2000.
- Description: multi-line text up to 5,000 characters.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.utils import timezone

from arkray.core.errors import InvalidInputError
from arkray.core.text import TextRejected, clean_line, clean_multiline

from . import models as m
from .countries import COUNTRY_CODES
from .phones import PHONE_MAX_LENGTH, InvalidPhone, clean_phone

NAME_REQUIRED = "Enter the person's name or their organization."
_POSTAL_CODE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 \-]*[A-Za-z0-9])?$")
_EARLIEST_CONTACT = datetime(2000, 1, 1, tzinfo=UTC)
_CLOCK_SKEW = timedelta(minutes=5)
_email_syntax = EmailValidator(message="Enter a valid email address.")


def _text(max_length: int) -> Callable[[Any], str]:
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Enter text.")
        text = clean_line(value)
        if len(text) > max_length:
            raise ValueError(f"Use at most {max_length} characters.")
        return text

    return clean


# Characters the email standard allows before the "@" but that delimit a mailto link's
# headers: "x?bcc=spy@evil.example&body=..." would open a draft copying a third party
# (Phase 9 review). Real addresses don't use them.
_MAILTO_DELIMITERS = frozenset('?&=%#"<>\\')


def _email(value: Any) -> str:
    email = _text(m.EMAIL_MAX_LENGTH)(value)
    if email:
        try:
            _email_syntax(email)
        except ValidationError:
            raise ValueError("Enter a valid email address.") from None
        if any(char in _MAILTO_DELIMITERS for char in email):
            raise ValueError("Enter a valid email address.")
    return email


def _phone(value: Any) -> str:
    if isinstance(value, str):
        value = unicodedata.normalize("NFKC", value)  # full-width digits and symbols -> ASCII
    try:
        return clean_phone(_text(PHONE_MAX_LENGTH)(value))
    except InvalidPhone as exc:
        raise ValueError(str(exc)) from None


def _postal_code(value: Any) -> str:
    code = _text(m.POSTAL_CODE_MAX_LENGTH)(value)
    if code and not _POSTAL_CODE.fullmatch(code):
        raise ValueError("Use letters, digits, spaces and hyphens only.")
    return code


def _country(value: Any) -> str:
    code = _text(2)(value).upper()
    if code and code not in COUNTRY_CODES:
        raise ValueError("Choose a country from the list.")
    return code


def _description(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter text.")
    text = clean_multiline(value)
    if len(text) > m.DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"Use at most {m.DESCRIPTION_MAX_LENGTH} characters.")
    return text


def _rating(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if value not in m.Rating.values:
        raise ValueError("Choose hot, warm or cold.")
    return str(value)


def _last_contacted(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError("Include a time zone.")
    if value > timezone.now() + _CLOCK_SKEW:
        raise ValueError("The last contact can't be in the future.")
    if value < _EARLIEST_CONTACT:
        raise ValueError("Enter a date from 2000 onwards.")
    return value


def _source_key(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Choose a source from the list.")
    return value


CLEANERS: Mapping[str, Callable[[Any], Any]] = {
    "first_name": _text(m.NAME_MAX_LENGTH),
    "last_name": _text(m.NAME_MAX_LENGTH),
    "organization_name": _text(m.ORGANIZATION_MAX_LENGTH),
    "job_title": _text(m.JOB_TITLE_MAX_LENGTH),
    "email": _email,
    "phone": _phone,
    "mobile": _phone,
    "alternate_phone": _phone,
    "address_line_1": _text(m.ADDRESS_LINE_MAX_LENGTH),
    "address_line_2": _text(m.ADDRESS_LINE_MAX_LENGTH),
    "city": _text(m.LOCALITY_MAX_LENGTH),
    "state": _text(m.LOCALITY_MAX_LENGTH),
    "postal_code": _postal_code,
    "country": _country,
    "source": _source_key,
    "rating": _rating,
    "last_contacted_at": _last_contacted,
    "description": _description,
}
# The profile fields a lead's editors may change with PATCH. Owner, status and archive state
# have their own audited operations; ids, provenance, timestamps and version never change.
EDITABLE_FIELDS = frozenset(CLEANERS)


def clean_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every field cleaned, or InvalidInputError listing every problem at once."""
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


def require_identity(first_name: str, last_name: str, organization_name: str) -> None:
    """A lead needs someone to be about: a person's name, an organisation, or both."""
    if not (first_name or last_name or organization_name):
        raise InvalidInputError(details={"first_name": [NAME_REQUIRED]})
