from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from arkray.dashboard.api import views

# 00:15 IST on 3 October 2026 (18:45 UTC on 2 October): the business day has just started
# while UTC is still on the previous date.
NOW = datetime(2026, 10, 2, 18, 45, tzinfo=UTC)
IST_DAY_START = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)  # 00:00 IST, 3 October
IST_DAY_END = datetime(2026, 10, 3, 18, 30, tzinfo=UTC)  # 00:00 IST, 4 October
MICROSECOND = timedelta(microseconds=1)


def dashboard_url(workspace: str = "me") -> str:
    return f"/api/v1/workspaces/{workspace}/dashboard"


@pytest.fixture
def frozen_now(monkeypatch):
    """The dashboard view reads the clock once per request; fix it at NOW (only there:
    records still get real creation times unless a test sets them)."""
    monkeypatch.setattr(views, "timezone", SimpleNamespace(now=lambda: NOW))
    return NOW
