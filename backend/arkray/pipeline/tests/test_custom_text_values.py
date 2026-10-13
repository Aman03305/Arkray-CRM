"""A custom text value far past its limit is refused before it is cleaned (adversarial review of
73ff1e3, upload review round 4, PARSERS4-2): a custom value is a JSON field with no length
limit of its own, up to the edge's 1 MiB body, and cleaning, linear now, still costs time."""

from __future__ import annotations

import pytest

from arkray.pipeline import validation


@pytest.mark.parametrize("multiline", [False, True], ids=["line", "multiline"])
def test_a_value_far_past_the_limit_is_refused_before_it_is_cleaned(monkeypatch, multiline):
    cleaned: list[str] = []

    def spy(value: str) -> str:
        cleaned.append(value)
        return value

    monkeypatch.setattr(validation, "clean_line", spy)
    monkeypatch.setattr(validation, "clean_multiline", spy)
    limit = validation.LONG_TEXT_VALUE_MAX_LENGTH if multiline else validation.TEXT_VALUE_MAX_LENGTH
    with pytest.raises(ValueError, match="Use at most"):
        validation._text_value("x" * (4 * limit + 1), multiline=multiline)
    assert cleaned == []


def test_a_value_within_the_margin_is_still_cleaned_first():
    """Blanks around a value are trimmed before its length is judged, as before."""
    limit = validation.TEXT_VALUE_MAX_LENGTH
    assert validation._text_value("x" * limit + " " * 10, multiline=False) == "x" * limit
    with pytest.raises(ValueError, match="Use at most"):
        validation._text_value("x" * (limit + 1), multiline=False)
