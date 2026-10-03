from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from tests.helpers import signed_in

ME = "me"


def activities_url(workspace: str = ME) -> str:
    return f"/api/v1/workspaces/{workspace}/activities"


def activity_url(activity_id, workspace: str = ME, action: str = "") -> str:
    base = f"/api/v1/workspaces/{workspace}/activities/{activity_id}"
    return f"{base}/{action}" if action else base


def summary_url(workspace: str = ME) -> str:
    return f"/api/v1/workspaces/{workspace}/activity-summary"


def lead_timeline_url(lead_id, workspace: str = ME) -> str:
    return f"/api/v1/workspaces/{workspace}/leads/{lead_id}/timeline"


def opportunity_timeline_url(opportunity_id, workspace: str = ME) -> str:
    return f"/api/v1/workspaces/{workspace}/opportunities/{opportunity_id}/timeline"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def task_fields(**extra):
    return {"title": "Send the revised quotation", **extra}


def meeting_fields(*, start=None, minutes=60, **extra):
    start = start or timezone.now() + timedelta(days=1)
    return {
        "title": "Product demo",
        "starts_at": start,
        "ends_at": start + timedelta(minutes=minutes),
        **extra,
    }


def note_fields(text="Customer asked for a call back next week.", **extra):
    return {"description": text, **extra}


@pytest.fixture
def user_b_client(user_b):
    return signed_in(user_b)
