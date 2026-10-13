"""One account can't starve the shared queues or break someone's data rights (review of 73ff1e3,
M6; docs/reliability.md#fairness-between-users, docs/reliability.md#write-budgets).

The reviewer's chain: a sales user posts long notes as fast as the API allows, the single
indexing worker serves first-come, first-served, and everyone else's new notes wait behind the
backlog for hours; the same account can pile enough activities onto one customer that the
customer's data-rights export runs out of memory or their erasure times out. Each link is cut:

- creating is budgeted per person per day and per customer (activities.services);
- the outbox takes turns between users, so another user's work is claimed at once whatever
  one account has queued (core.outbox);
- a large lead's re-sync goes in bounded batches that re-enter the queue (ai.indexing);
- the export streams in bounded memory and the erasure runs in bounded statements, and a
  customer at the budget is exported and erased in full (privacy).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from arkray.activities.models import Activity
from arkray.core import outbox
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.models import OutboxEvent, OutboxStatus
from arkray.privacy import exports, services
from arkray.privacy.models import DataExport
from tests.ai_fixtures import index, note
from tests.factories import LeadFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

EXPORTS = "/api/v1/admin/privacy/exports"
LONG_NOTE = "Visited the lab; the manager wants the HbA1c analyser demo next week. " * 140


@contextmanager
def acting_as(user: Any) -> Iterator[None]:
    """As the signed-in user's request runs (the middleware and authentication bind it)."""
    token = bind_context(ExecutionContext(correlation_id="req-1", user_id=str(user.pk)))
    try:
        yield
    finally:
        reset_context(token)


def relayed() -> list[OutboxEvent]:
    """One relay tick: the events handed out, in order (nothing runs)."""
    sent: list[OutboxEvent] = []
    outbox.relay(dispatch=sent.append)
    return sent


def test_another_users_note_is_indexed_first_whatever_one_account_has_queued(ai_on, user_a, user_b):
    """Fifty long notes from one account, then one from someone else: the other person's is
    in the first claims (it was the 51st), and indexed with them."""
    flooded = LeadFactory(owner=user_a)
    with acting_as(user_a):
        for _ in range(50):
            note(user_a, flooded, LONG_NOTE)
    with acting_as(user_b):
        mine = note(user_b, LeadFactory(owner=user_b), "Dr Mehta wants a demo.")
    sent = relayed()
    ids = [e.payload.get("id") for e in sent]
    assert ids.index(str(mine.pk)) <= 1
    OutboxEvent.objects.filter(status=OutboxStatus.IN_FLIGHT).update(
        status=OutboxStatus.PENDING, claim_token=None, locked_until=None
    )
    index()


def test_a_flood_of_one_accounts_jobs_does_not_delay_another_customers_export(
    monkeypatch, admin, user_a
):
    """The `default` queue also builds the data-rights exports: an administrator's export of
    another customer is claimed at once, not behind the account's backlog."""
    monkeypatch.setattr(outbox, "_HANDLERS", dict(outbox._HANDLERS))

    outbox.handler("test.flood", queue="default")(lambda payload: None)
    with acting_as(user_a):
        for n in range(50):
            outbox.enqueue("test.flood", {"n": n})
    customer = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
    client = signed_in(admin)
    response = client.post(
        EXPORTS,
        {
            "subject_type": "lead",
            "subject_id": str(customer.pk),
            "reference": "DSAR-2026-17",
            "identity_verified": True,
        },
        format="json",
    )
    assert response.status_code == 202
    sent = relayed()
    topics = [e.topic for e in sent]
    assert topics.index(exports.TOPIC_BUILD) <= 1  # was the 51st


def test_a_customer_filled_to_the_budget_is_exported_and_erased_in_full(settings, admin, user_a):
    """The account fills one customer up to ACTIVITY_MAX_PER_CUSTOMER (the next one is
    refused); the customer's export and erasure still complete. Representative (review round
    1): notes of the maximum length in Hindi, posted through the API (their timeline entries,
    audit events and re-indexing with them), at 1/100 of the defaults with the export's cap
    scaled alike (the full scale is measured in docs/reliability.md#export-sizing)."""
    import random

    from tests.security.test_export_streaming import hindi_note

    budget = settings.ACTIVITY_MAX_PER_CUSTOMER // 100
    settings.EXPORT_MAX_BYTES //= 100
    settings.ACTIVITY_MAX_PER_CUSTOMER = budget
    customer = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
    sales = signed_in(user_a)
    rng = random.Random(11)
    for _ in range(budget):
        created = sales.post(
            "/api/v1/workspaces/me/activities",
            {"type": "note", "lead": str(customer.pk), "description": hindi_note(rng)},
            format="json",
        )
        assert created.status_code == 201, created.content
    refused = sales.post(
        "/api/v1/workspaces/me/activities",
        {"type": "note", "lead": str(customer.pk), "description": "One more."},
        format="json",
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "activity_limit_reached"

    client = signed_in(admin)
    response = client.post(
        EXPORTS,
        {
            "subject_type": "lead",
            "subject_id": str(customer.pk),
            "reference": "DSAR-2026-17",
            "identity_verified": True,
        },
        format="json",
    )
    assert response.status_code == 202
    drain_outbox(rounds=20)
    export = DataExport.objects.get(pk=response.json()["id"])
    assert (export.status, export.error_code) == ("ready", "")

    erased = services.erase(customer.pk, operator_id=admin.pk)
    assert erased.activities == budget
    assert not Activity.objects.filter(lead=customer).exclude(description="[erased]").exists()
