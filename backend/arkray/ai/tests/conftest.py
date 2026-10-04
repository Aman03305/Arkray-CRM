"""Ask Arkray tests run with AI on, the hashing embedder and no language model unless a
test installs one (tests.ai_fixtures)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ai_on(ai_on: None) -> None:
    """Every test in this package (see tests.ai_fixtures.ai_on)."""
