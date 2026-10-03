"""A committed change shows on the very next dashboard load: the figures are read from the
authoritative tables on every request (no cache, no stored metrics, no eventual
consistency). Driven through the real write APIs, with expectations that hold at any time
of day (undated tasks, meetings days ahead)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from tests.factories import LeadFactory, MeetingFactory, default_stage

from .conftest import dashboard_url

pytestmark = pytest.mark.django_db

ME = "/api/v1/workspaces/me"


def figures(client):
    response = client.get(dashboard_url())
    assert response.status_code == 200, response.content
    return response.json()


def post(client, path, body, expected=201):
    response = client.post(f"{ME}/{path}", body, format="json")
    assert response.status_code == expected, response.content
    return response.json()


def test_every_write_shows_on_the_next_load(user_a_client):
    client = user_a_client
    assert figures(client)["leads"] == {"total": 0, "new_today": 0}

    lead = post(client, "leads", {"first_name": "Fresh", "last_name": "Prospect"})
    body = figures(client)
    assert body["leads"] == {"total": 1, "new_today": 1}
    assert body["new_leads"][0]["display_name"] == "Fresh Prospect"

    opportunity = post(
        client,
        "opportunities",
        {
            "lead": lead["id"],
            "title": "Analyser",
            "value": "1000000",
            "stage": str(default_stage("proposal").pk),
        },
    )
    assert figures(client)["pipeline"] == {
        "pipeline_value": "1000000.00",
        "weighted_pipeline": "500000.00",
        "open_count": 1,
    }

    moved = post(
        client,
        f"opportunities/{opportunity['id']}/move",
        {"stage": str(default_stage("negotiation").pk), "version": opportunity["version"]},
        expected=200,
    )
    assert figures(client)["pipeline"]["weighted_pipeline"] == "750000.00"

    post(
        client,
        f"opportunities/{opportunity['id']}/move",
        {"stage": str(default_stage("won").pk), "version": moved["version"]},
        expected=200,
    )
    assert figures(client)["pipeline"] == {
        "pipeline_value": "0.00",
        "weighted_pipeline": "0.00",
        "open_count": 0,
    }

    task = post(client, "activities", {"type": "task", "lead": lead["id"], "title": "Call back"})
    body = figures(client)
    assert body["activities"]["open_tasks"] == 1
    assert [row["id"] for row in body["next_tasks"]] == [task["id"]]
    post(client, f"activities/{task['id']}/complete", {"version": task["version"]}, expected=200)
    body = figures(client)
    assert body["activities"]["open_tasks"] == 0
    assert body["next_tasks"] == []

    start = timezone.now() + timedelta(days=3)
    meeting = post(
        client,
        "activities",
        {
            "type": "meeting",
            "lead": lead["id"],
            "title": "Demo",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
        },
    )
    body = figures(client)
    assert body["activities"]["upcoming_meetings"] == 1
    assert [row["id"] for row in body["upcoming_meetings"]] == [meeting["id"]]
    post(
        client, f"activities/{meeting['id']}/cancel", {"version": meeting["version"]}, expected=200
    )
    body = figures(client)
    assert body["activities"]["upcoming_meetings"] == 0
    assert body["upcoming_meetings"] == []

    current = client.get(f"{ME}/leads/{lead['id']}").json()
    post(client, f"leads/{lead['id']}/archive", {"version": current["version"]}, expected=200)
    assert figures(client)["leads"] == {"total": 0, "new_today": 0}


def test_completing_a_meeting_keeps_it_among_the_days_meetings(user_a_client, user_a):
    """Phase 4's definition: today's meetings are the scheduled and completed ones, so
    completing a meeting changes its state but not that count (and it was never upcoming)."""
    lead = LeadFactory(owner=user_a)
    start = timezone.now() - timedelta(minutes=30)
    meeting = MeetingFactory(lead=lead, starts_at=start, ends_at=start + timedelta(hours=1))
    before = figures(user_a_client)["activities"]
    post(user_a_client, f"activities/{meeting.pk}/complete", {"version": 1}, expected=200)
    after = figures(user_a_client)["activities"]
    assert after == before
    assert after["upcoming_meetings"] == 0
