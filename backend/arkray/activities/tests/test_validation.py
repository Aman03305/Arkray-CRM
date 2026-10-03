"""Activity field rules for any caller (validation.py): shapes a serializer would normally
stop are refused here too, so a direct service call can't store odd values."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arkray.activities import validation
from arkray.activities.spec import SPECS
from arkray.core.errors import InvalidInputError


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", 42),
        ("description", ["not", "text"]),
        ("priority", "urgent"),
        ("due_at", "2026-10-03T10:00:00+05:30"),  # a string, not a datetime
        ("due_at", datetime(2026, 10, 3, 10, 0)),  # noqa: DTZ001 — naive, the case under test
        ("starts_at", None),  # required for a meeting when given
        ("ends_at", datetime(1999, 1, 1, tzinfo=UTC)),
        ("location", "x" * 201),
        ("location", 3.5),
        ("meeting_url", "https://"),
        ("title", "x" * 201),
        ("description", "x" * 10_001),
    ],
)
def test_bad_values_are_refused_per_field(field, value):
    with pytest.raises(InvalidInputError) as caught:
        validation.clean_fields({field: value})
    assert field in caught.value.details


def test_unknown_fields_are_refused():
    with pytest.raises(InvalidInputError) as caught:
        validation.clean_fields({"owner": "x", "status": "open"})
    assert "owner, status" in caught.value.details["non_field_errors"][0]


def test_every_problem_is_reported_at_once():
    with pytest.raises(InvalidInputError) as caught:
        validation.clean_fields({"title": 1, "priority": "x", "meeting_url": "ftp://x"})
    assert set(caught.value.details) == {"title", "priority", "meeting_url"}


def test_clean_values_are_canonical():
    cleaned = validation.clean_fields(
        {
            "title": "  Demo\tcall ",
            "description": "line one  \r\nline two\n\n",
            "meeting_url": " https://meet.example/abc ",
            "due_at": None,
        }
    )
    assert cleaned == {
        "title": "Demo call",
        "description": "line one\nline two",
        "meeting_url": "https://meet.example/abc",
        "due_at": None,
    }


def test_required_fields_and_fields_of_other_types():
    with pytest.raises(InvalidInputError) as caught:
        validation.require_fields_of(SPECS["meeting"], {"title": "x"}, creating=True)
    assert set(caught.value.details) == {"starts_at", "ends_at"}
    validation.require_fields_of(SPECS["task"], {}, creating=False)  # edits may omit anything
    with pytest.raises(InvalidInputError) as caught:
        validation.require_fields_of(
            SPECS["note"], {"description": "x", "location": "y"}, creating=True
        )
    assert caught.value.details == {"location": ["Notes don't have this field."]}
