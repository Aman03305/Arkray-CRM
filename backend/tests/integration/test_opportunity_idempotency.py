"""Creating an opportunity is idempotent and requires an Idempotency-Key (R103,
docs/api-conventions.md#idempotency, docs/pipeline.md#the-lead-a-new-opportunity-creates).

One create writes a lead and an opportunity (ADR-0028), so a retried request without a key
would make a second pair. Attacked here for real (PostgreSQL, real threads, the HTTP API),
counting every table one create writes after each scenario:

- no key, or a malformed one: 400 and nothing written (sequences included);
- the same request 2, 20 or 100 times at once with one key: exactly the rows ONE create
  writes, every answer the same opportunity, exactly one not marked replayed; the duplicates
  wait for the first on the key (an advisory lock taken first in the transaction) and never
  start a create of their own (no lead insert, no rolled-back rows: sequences don't move);
- a retry after the answer was lost (or while the first is still running) replays it;
- a refused request doesn't use the key up; another request with the key is a 422 and
  writes nothing; another user's identical key is theirs; a new key is a new opportunity;
- a record past RETENTION is forgotten: the same key then creates anew (documented).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.db import connection
from django.utils import timezone

from arkray.activities.models import TimelineEntry
from arkray.audit.models import AuditEvent
from arkray.core.idempotency import RETENTION
from arkray.core.models import IdempotencyRecord, OutboxEvent
from arkray.leads import selectors as lead_selectors
from arkray.leads import services as lead_services
from arkray.leads.models import Lead, StatusCategory
from arkray.pipeline import services
from arkray.pipeline.models import NegotiationPrice, Opportunity, Stage, StageHistory
from tests.factories import AdminFactory, UserFactory
from tests.helpers import key_header, run_concurrently, signed_in

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]

API = "/api/v1/workspaces"
DEAL = {
    "account_name": "ABC Diagnostics",
    "customer_name": "ABC Diagnostics Mumbai",
    "contact_phone": "9876543210",
    "instrument_name": "Adams 8380 V-lite",
    "work_load": "300 tests/day",
    "value": "850000",
    "expected_cpt": "Rs 18 per test",
    "opportunity_date": "2026-10-05",
}
REQUIRED = (
    "Send an Idempotency-Key header: a new UUID for each new opportunity, "
    "the same one when retrying it."
)
WAVE = 25  # threads (connections) at once: the test database's max_connections is 100
TABLES: dict[str, Any] = {
    "leads": Lead,
    "opportunities": Opportunity,
    "idempotency": IdempotencyRecord,
    "audit": AuditEvent,
    "outbox": OutboxEvent,
    "timeline": TimelineEntry,
    "stage_history": StageHistory,
    "negotiation_prices": NegotiationPrice,
}
# Tables with a sequence: a rolled-back insert still consumes a value, so these show whether
# a duplicate ever STARTED a create, not only whether it committed one.
SEQUENCES = ("audit_event", "core_idempotency_record", "pipeline_stage_history")


def rows() -> dict[str, int]:
    return {name: model.objects.count() for name, model in TABLES.items()}


def sequences() -> dict[str, int]:
    """The next value of each sequence."""
    positions = {}
    with connection.cursor() as cursor:
        for table in SEQUENCES:
            cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
            (name,) = cursor.fetchone()
            cursor.execute(f"SELECT last_value, is_called FROM {name}")  # noqa: S608 - catalog name
            last, called = cursor.fetchone()
            positions[table] = last + int(called)
    return positions


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {name: after[name] - before[name] for name in before}


def url(workspace: str = "me") -> str:
    return f"{API}/{workspace}/opportunities"


def create(client: Any, body: dict[str, Any] | None = None, workspace: str = "me", **headers: str):
    return client.post(
        url(workspace), DEAL if body is None else body, format="json", headers=headers
    )


def keyed(client: Any, key: str, body: dict[str, Any] | None = None, workspace: str = "me"):
    return create(client, body, workspace, **key_header(key))


@contextmanager
def creates_started() -> Iterator[list[int]]:
    """Counts the creates that got past the key: each begins by inserting the lead."""
    started: list[int] = []
    guard = threading.Lock()
    original = lead_services.create_lead

    def counted(**kwargs: Any) -> Any:
        with guard:
            started.append(1)
        return original(**kwargs)

    with mock.patch.object(lead_services, "create_lead", counted):
        yield started


def one_create_writes() -> tuple[dict[str, int], dict[str, int]]:
    """The rows (and sequence values) one plain create writes, measured for a bystander."""
    bystander = signed_in(UserFactory())
    before, seq_before = rows(), sequences()
    assert keyed(bystander, str(uuid.uuid4())).status_code == 201
    return delta(before, rows()), delta(seq_before, sequences())


def dashboard(client: Any) -> dict[str, Any]:
    response = client.get(f"{API}/me/dashboard")
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def board_cards(client: Any) -> list[str]:
    response = client.get(f"{API}/me/pipeline-board")
    assert response.status_code == 200, response.content
    return [card["id"] for column in response.json()["columns"] for card in column["cards"]]


# --- the key is required ---------------------------------------------------------------------
def test_without_a_key_nothing_is_created_in_any_workspace():
    user = UserFactory()
    mine, theirs = signed_in(user), signed_in(AdminFactory())
    attempts = (
        (mine, "me", DEAL),
        (theirs, str(user.pk), DEAL),
        (theirs, "all", {**DEAL, "owner": str(user.pk)}),
    )
    for client, workspace, _ in attempts:
        # Opened first: an administrator's viewing of a workspace is audited on its own.
        assert client.get(url(workspace)).status_code == 200
    before, seq_before = rows(), sequences()
    for client, workspace, body in attempts:
        response = create(client, body, workspace)
        assert response.status_code == 400, response.content
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["message"] == REQUIRED
        assert error["details"] == {"idempotency_key": [REQUIRED]}
    assert rows() == before
    assert sequences() == seq_before  # not even a rolled-back insert


@pytest.mark.parametrize(
    "key",
    [
        "",
        " ",
        "not-a-uuid",
        "1",
        "3f2b8c1e9a4d4e2f8b7a1c2d3e4f5a6b",  # no hyphens: not the canonical form
        "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b0",  # too long
        "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b" * 20,
        "{3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b}",
        # Two headers reach the application joined with ", ".
        "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b, 7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
        "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b\x00",
    ],
    ids=[
        "empty",
        "blank",
        "word",
        "digit",
        "no-hyphens",
        "too-long",
        "very-long",
        "braces",
        "two-values",
        "nul",
    ],
)
def test_a_malformed_key_is_refused_and_nothing_is_created(key):
    client = signed_in(UserFactory())
    before, seq_before = rows(), sequences()
    response = keyed(client, key)
    assert response.status_code == 400, response.content
    assert response.json()["error"]["details"] == {
        "idempotency_key": ["Idempotency-Key must be a UUID."]
    }
    assert rows() == before
    assert sequences() == seq_before


# --- duplicates of one request ---------------------------------------------------------------
@pytest.mark.parametrize("copies", [2, 20, 100], ids=["double-click", "20", "100"])
def test_the_same_request_at_once_creates_exactly_once(copies):
    expected_rows, expected_sequences = one_create_writes()
    user = UserFactory()
    clients = [signed_in(user) for _ in range(copies)]
    reader = signed_in(user)
    dashboard_before = dashboard(reader)
    key = str(uuid.uuid4())
    before, seq_before = rows(), sequences()

    responses: list[Any] = []
    with creates_started() as started:
        for wave in range(0, copies, WAVE):
            responses += run_concurrently(
                *(lambda c=client: keyed(c, key) for client in clients[wave : wave + WAVE])
            )

    errors = [r for r in responses if isinstance(r, BaseException)]
    assert not errors, errors
    assert [r.status_code for r in responses] == [201] * copies
    assert len({r.json()["id"] for r in responses}) == 1
    assert [r.get("Idempotent-Replayed") for r in responses].count(None) == 1
    assert {r.get("Idempotent-Replayed") for r in responses} <= {None, "true"}
    # Exactly what one create writes, and only one create ever started: the duplicates never
    # inserted (and rolled back) a lead, audit event or history row of their own.
    assert len(started) == 1
    assert delta(before, rows()) == expected_rows
    assert delta(seq_before, sequences()) == expected_sequences

    opportunity = Opportunity.objects.get(pk=responses[0].json()["id"])
    lead = Lead.objects.get(pk=opportunity.lead_id)
    assert Lead.objects.filter(owner=user).count() == 1
    assert lead.owner_id == opportunity.owner_id == user.pk
    status = lead_selectors.status_by_key(lead.status_id)
    assert status is not None
    assert status.category == StatusCategory.CONVERTED
    assert board_cards(reader) == [str(opportunity.pk)]
    for action in ("lead.created", "opportunity.created"):
        assert AuditEvent.objects.filter(action=action, actor_id=user.pk).count() == 1
    figures = dashboard(reader)
    assert figures["leads"]["total"] == dashboard_before["leads"]["total"] + 1
    assert figures["leads"]["new_today"] == dashboard_before["leads"]["new_today"] + 1
    assert figures["leads"]["total"] == Lead.objects.filter(owner=user).count()
    assert figures["pipeline"]["open_count"] == 1
    assert Decimal(figures["pipeline"]["pipeline_value"]) == Decimal(DEAL["value"])
    assert [row["id"] for row in figures["new_leads"]] == [str(lead.pk)]


def test_a_retry_while_the_first_is_still_running_waits_for_it_and_replays_it():
    """The answer was slow (the client timed out) and the request is sent again while the
    first is still writing: the retry waits on the key, then replays the first."""
    user = UserFactory()
    first_client, retry_client = signed_in(user), signed_in(user)
    key = str(uuid.uuid4())
    inside, release = threading.Event(), threading.Event()
    original = services._mark_converted

    def slow(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        if not inside.is_set():
            inside.set()
            assert release.wait(timeout=20)

    results: dict[str, Any] = {}

    def run(name: str, client: Any) -> None:
        try:
            results[name] = keyed(client, key)
        finally:
            connection.close()

    waited = False
    with mock.patch.object(services, "_mark_converted", slow), creates_started() as started:
        first = threading.Thread(target=run, args=("first", first_client))
        retry = threading.Thread(target=run, args=("retry", retry_client))
        first.start()
        try:
            assert inside.wait(timeout=20)
            retry.start()
            # The retry is blocked on the key's advisory lock (not on a row or the index).
            deadline = time.monotonic() + 10
            while not waited and time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    )
                    waited = cursor.fetchone()[0] > 0
                time.sleep(0.05)
            assert retry.is_alive()
        finally:
            release.set()  # never leave a thread (and its transaction) behind
            first.join(timeout=30)
            if retry.ident is not None:  # started
                retry.join(timeout=30)

    assert waited, "the retry never waited on the key"
    assert results["first"].status_code == results["retry"].status_code == 201
    assert results["retry"].json()["id"] == results["first"].json()["id"]
    assert results["first"].get("Idempotent-Replayed") is None
    assert results["retry"]["Idempotent-Replayed"] == "true"
    assert len(started) == 1
    assert (Lead.objects.count(), Opportunity.objects.count()) == (1, 1)


def test_a_retry_after_the_answer_was_lost_replays_it_and_writes_nothing():
    client = signed_in(UserFactory())
    key = str(uuid.uuid4())
    first = keyed(client, key)  # the answer never reached the browser
    assert first.status_code == 201
    before, seq_before = rows(), sequences()
    for _ in range(3):
        again = keyed(client, key)
        assert again.status_code == 201
        assert again["Idempotent-Replayed"] == "true"
        assert again.json() == first.json()
        assert again["Location"] == first["Location"]
    assert rows() == before
    assert sequences() == seq_before


# --- a refused request doesn't use the key up --------------------------------------------------
def test_a_request_refused_before_its_transaction_leaves_the_key_unused():
    client = signed_in(UserFactory())
    key = str(uuid.uuid4())
    before = rows()
    refused = keyed(client, key, {**DEAL, "contact_email": "not an email"})
    assert refused.status_code == 400
    assert rows() == before
    corrected = keyed(client, key, {**DEAL, "contact_email": "lab@abc.example"})
    assert corrected.status_code == 201, corrected.content
    assert corrected.get("Idempotent-Replayed") is None
    assert (
        keyed(client, key, {**DEAL, "contact_email": "lab@abc.example"})["Idempotent-Replayed"]
        == "true"
    )
    assert (Lead.objects.count(), Opportunity.objects.count()) == (1, 1)


def test_a_request_refused_inside_its_transaction_rolls_back_and_leaves_the_key_unused():
    """Refused after its lead was inserted (a negotiation stage without the agreed terms is
    checked when the opportunity is): everything rolls back, the key included."""
    client = signed_in(UserFactory())
    negotiation = Stage.objects.get(pipeline__key="sales", key="negotiation")
    key = str(uuid.uuid4())
    before = rows()
    with creates_started() as started:
        refused = keyed(client, key, {**DEAL, "stage": str(negotiation.pk)})
    assert refused.status_code == 400, refused.content
    assert len(started) == 1  # it did get as far as the lead
    assert rows() == before
    body = {
        **DEAL,
        "stage": str(negotiation.pk),
        "negotiated_price": "800000",
        "agreed_cpt": "Rs 17",
    }
    created = keyed(client, key, body)
    assert created.status_code == 201, created.content
    assert created.get("Idempotent-Replayed") is None
    assert delta(before, rows())["opportunities"] == delta(before, rows())["leads"] == 1


# --- the key belongs to one request of one person ----------------------------------------------
@pytest.fixture
def negotiated(crm_configuration):
    """An administrator's organisation-wide create for Rahul, into negotiation."""
    rahul, priya, admin = UserFactory(), UserFactory(), AdminFactory()
    negotiation = Stage.objects.get(pipeline__key="sales", key="negotiation")
    body = {
        **DEAL,
        "owner": str(rahul.pk),
        "stage": str(negotiation.pk),
        "negotiated_price": "800000",
        "agreed_cpt": "Rs 17 per test",
    }
    return {"rahul": rahul, "priya": priya, "client": signed_in(admin), "body": body}


@pytest.mark.parametrize(
    "change",
    [
        "value",
        "customer_name",
        "expected_cpt",
        "owner",
        "pipeline",
        "stage",
        "negotiated_price",
        "agreed_cpt",
        "workspace",
    ],
)
def test_the_key_with_any_other_request_is_refused_and_writes_nothing(negotiated, change):
    client, body = negotiated["client"], negotiated["body"]
    key = str(uuid.uuid4())
    first = keyed(client, key, body, "all")
    assert first.status_code == 201, first.content
    other, workspace = dict(body), "all"
    proposal = Stage.objects.get(pipeline__key="sales", key="proposal")
    other.update(
        {
            "value": {"value": "850001"},
            "customer_name": {"customer_name": "XYZ Laboratory"},
            "expected_cpt": {"expected_cpt": "Rs 19 per test"},
            "owner": {"owner": str(negotiated["priya"].pk)},
            "pipeline": {"pipeline": str(proposal.pipeline_id)},  # named instead of defaulted
            "stage": {"stage": str(proposal.pk)},
            "negotiated_price": {"negotiated_price": "800001"},
            "agreed_cpt": {"agreed_cpt": "Rs 17 per test (revised)"},
            "workspace": {},
        }[change]
    )
    if change == "workspace":  # the same administrator, in Rahul's own workspace
        workspace = str(negotiated["rahul"].pk)
        # Opened first: an administrator's viewing of a workspace is audited on its own.
        assert client.get(url(workspace)).status_code == 200
    before, seq_before = rows(), sequences()
    response = keyed(client, key, other, workspace)
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert rows() == before
    assert sequences() == seq_before
    # The first request itself still replays.
    assert keyed(client, key, body, "all")["Idempotent-Replayed"] == "true"


def test_the_same_key_from_another_user_is_their_own_request():
    rahul, priya = signed_in(UserFactory()), signed_in(UserFactory())
    key = str(uuid.uuid4())
    mine, theirs = keyed(rahul, key), keyed(priya, key)
    assert mine.status_code == theirs.status_code == 201
    assert mine.get("Idempotent-Replayed") is None
    assert theirs.get("Idempotent-Replayed") is None
    assert mine.json()["id"] != theirs.json()["id"]
    assert (Lead.objects.count(), Opportunity.objects.count()) == (2, 2)


def test_a_new_key_is_a_new_opportunity_even_for_identical_details():
    """Legitimate duplicates exist (two deals for one lab): nothing is merged or refused."""
    client = signed_in(UserFactory())
    first, second = keyed(client, str(uuid.uuid4())), keyed(client, str(uuid.uuid4()))
    other = keyed(client, str(uuid.uuid4()), {**DEAL, "instrument_name": "Adams 8180 T"})
    assert [r.status_code for r in (first, second, other)] == [201, 201, 201]
    assert len({r.json()["id"] for r in (first, second, other)}) == 3
    assert len({r.json()["lead"]["id"] for r in (first, second, other)}) == 3
    assert (Lead.objects.count(), Opportunity.objects.count()) == (3, 3)


def test_a_key_past_its_retention_is_forgotten_and_creates_anew():
    """Documented: records last RETENTION (24 h). The same key sent later is a new request."""
    client = signed_in(UserFactory())
    key = str(uuid.uuid4())
    first = keyed(client, key)
    assert first.status_code == 201
    IdempotencyRecord.objects.filter(key=key).update(
        created_at=timezone.now() - RETENTION - timedelta(minutes=1)
    )
    later = keyed(client, key)
    assert later.status_code == 201
    assert later.get("Idempotent-Replayed") is None
    assert later.json()["id"] != first.json()["id"]
    assert (Lead.objects.count(), Opportunity.objects.count()) == (2, 2)
    # The expired record was replaced, not kept beside the new one.
    assert IdempotencyRecord.objects.filter(key=key).count() == 1
