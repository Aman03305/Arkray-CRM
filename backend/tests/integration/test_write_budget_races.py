"""Activities racing for the last of a write budget (review of 73ff1e3, M6;
docs/reliability.md#write-budgets). The daily budget is counted under a per-person advisory lock
taken first in the creating transaction, so creations on different customers at once (which
the leads' locks don't serialise) never pass it together; the per-customer budget is counted
under the customer's own lock, which every creation on that customer takes first."""

from __future__ import annotations

import time

import pytest
from django.test import override_settings

from arkray.activities import services
from arkray.activities.models import Activity
from tests.factories import LeadFactory, NoteFactory, UserFactory
from tests.helpers import run_concurrently, signed_in

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]


def post_note(client, lead):
    return client.post(
        "/api/v1/workspaces/me/activities",
        {"type": "note", "lead": str(lead.pk), "description": "Called the lab manager."},
        format="json",
    )


def slowed(monkeypatch, name):
    """Widen the window between counting and writing: without the lock, every request would
    count the same room and all of them would pass."""
    measured = getattr(services, name)

    def slow(*args, **kwargs):
        found = measured(*args, **kwargs)
        time.sleep(0.2)
        return found

    monkeypatch.setattr(services, name, slow)


@override_settings(ACTIVITY_MAX_CREATED_PER_USER_PER_DAY=3)
def test_creations_on_different_customers_at_once_stay_within_the_daily_budget(monkeypatch):
    owner = UserFactory()
    leads = [LeadFactory(owner=owner) for _ in range(6)]
    clients = [signed_in(owner) for _ in leads]
    slowed(monkeypatch, "_created_today")
    responses = run_concurrently(
        *(lambda c=c, lead=lead: post_note(c, lead) for c, lead in zip(clients, leads, strict=True))
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 201, 201, 429, 429, 429], statuses
    assert Activity.objects.filter(created_by=owner).count() == 3


@override_settings(ACTIVITY_MAX_PER_CUSTOMER=3)
def test_creations_on_one_customer_at_once_stay_within_its_budget(monkeypatch):
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    NoteFactory(lead=lead)
    clients = [signed_in(owner) for _ in range(5)]
    slowed(monkeypatch, "_activities_of")
    responses = run_concurrently(*(lambda c=c: post_note(c, lead) for c in clients))
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 201, 422, 422, 422], statuses
    assert Activity.objects.filter(lead=lead).count() == 3
