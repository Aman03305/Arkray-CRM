"""Races on opportunities and their leads, run for real: each call in its own thread and
database connection (docs/pipeline.md#concurrency).

Rules under test:
- every change locks its rows and requires the version its author saw: of several changes
  made from the same version exactly one succeeds, the others get 409 (or 404 when a
  reassignment moved the record out of the loser's workspace meanwhile);
- lock order is always lead -> opportunities -> user rows, so no two operations can wait
  for each other in a cycle: the interleavings below would deadlock if any operation ever
  locked an opportunity before its lead;
- whatever the interleaving, an OPEN opportunity is never left owned by anyone but its
  lead's owner, conversion never creates two opportunities, and history is never corrupt.
"""

from __future__ import annotations

import itertools
import threading
import time
from decimal import Decimal

import pytest

from arkray.audit.models import AuditEvent
from arkray.core import domain_events
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.errors import BusinessRuleViolation, ConflictError, NotFoundError
from arkray.leads import services as lead_services
from arkray.leads.events import LeadReassigned
from arkray.leads.models import Lead
from arkray.pipeline import events, services
from arkray.pipeline.models import Opportunity, StageHistory
from tests.factories import (
    AdminFactory,
    LeadFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import run_concurrently

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]

OWN = AccessScope.own
ORG = AccessScope.organization
FIELDS = {"title": "Race", "value": Decimal("100000")}


def split(results):
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    return ok, failed


def assert_ownership_coherent():
    """No open opportunity is owned by anyone but its lead's owner (the database also
    enforces this at commit; this checks the committed state)."""
    for opportunity in Opportunity.objects.filter(status="open").select_related("lead"):
        assert opportunity.owner_id == opportunity.lead.owner_id, opportunity.pk


def assert_history_consistent():
    """Every opportunity's history is a chain ending in its current stage."""
    for opportunity in Opportunity.objects.all():
        rows = list(StageHistory.objects.filter(opportunity=opportunity).order_by("id"))
        if not rows:
            continue
        for before, after in itertools.pairwise(rows):
            assert after.from_stage_id == before.to_stage_id
        assert rows[-1].to_stage_id == opportunity.stage_id


def no_deadlocks(results):
    for result in results:
        assert "deadlock" not in str(result).lower(), result


def created(owner, lead):
    return services.create_opportunity(
        actor=owner, scope=OWN(owner.pk), lead_id=lead.pk, fields=FIELDS
    ).opportunity


def move(actor, scope, opportunity_id, stage_key, version=1):
    stage = default_stage(stage_key)
    return services.move_opportunity(
        actor=actor,
        scope=scope,
        opportunity_id=opportunity_id,
        version=version,
        stage_id=stage.pk,
        # Entering negotiation needs the price (product enhancement phase); without it the
        # move was refused or conflicted depending on which thread ran first.
        negotiated_price=Decimal("1000") if stage.is_negotiation else None,
    )


def reassign(admin, lead_id, to, version=1):
    return lead_services.reassign_lead(
        actor=admin, scope=ORG(admin.pk), lead_id=lead_id, version=version, owner_id=to.pk
    )


class first_subscriber:  # noqa: N801 — a context manager used like a function
    """Subscribe `callback` *before* the pipeline's own subscribers, e.g. to hold the lead's
    lock during a reassignment before its opportunities are touched."""

    def __init__(self, event_type, callback):
        self.event_type, self.callback = event_type, callback

    def __enter__(self):
        domain_events._SUBSCRIBERS.setdefault(self.event_type, []).insert(0, self.callback)

    def __exit__(self, *exc):
        domain_events._SUBSCRIBERS[self.event_type].remove(self.callback)


# --- the same opportunity, two writers ------------------------------------------------------
def test_move_racing_move():
    owner = UserFactory()
    opportunity = created(owner, LeadFactory(owner=owner))
    results = run_concurrently(
        lambda: move(owner, OWN(owner.pk), opportunity.pk, "proposal"),
        lambda: move(owner, OWN(owner.pk), opportunity.pk, "won"),
    )
    ok, failed = split(results)
    assert len(ok) == 1
    assert [type(f) for f in failed] == [ConflictError]
    opportunity.refresh_from_db()
    assert opportunity.stage_id == ok[0].stage_id
    assert opportunity.version == 2
    assert StageHistory.objects.filter(opportunity=opportunity).count() == 2  # created + 1 move
    assert_history_consistent()


def test_edit_racing_edit():
    admin, owner = AdminFactory(), UserFactory()
    opportunity = created(owner, LeadFactory(owner=owner))
    results = run_concurrently(
        lambda: services.update_opportunity(
            actor=admin,
            scope=ORG(admin.pk),
            opportunity_id=opportunity.pk,
            version=1,
            changes={"value": Decimal("1")},
        ),
        lambda: services.update_opportunity(
            actor=owner,
            scope=OWN(owner.pk),
            opportunity_id=opportunity.pk,
            version=1,
            changes={"title": "Mine"},
        ),
    )
    ok, failed = split(results)
    assert len(ok) == 1
    assert [type(f) for f in failed] == [ConflictError]
    opportunity.refresh_from_db()
    assert (opportunity.value, opportunity.title) == (ok[0].value, ok[0].title)  # no lost update
    assert AuditEvent.objects.filter(action="opportunity.updated").count() == 1


def test_move_racing_edit():
    owner = UserFactory()
    opportunity = created(owner, LeadFactory(owner=owner))
    results = run_concurrently(
        lambda: move(owner, OWN(owner.pk), opportunity.pk, "negotiation"),
        lambda: services.update_opportunity(
            actor=owner,
            scope=OWN(owner.pk),
            opportunity_id=opportunity.pk,
            version=1,
            changes={"probability": Decimal("33")},
        ),
    )
    ok, failed = split(results)
    assert (len(ok), [type(f) for f in failed]) == (1, [ConflictError])
    opportunity.refresh_from_db()
    # Either moved (with the stage's default) or overridden in place: never a mix.
    assert (opportunity.stage_id, opportunity.probability, opportunity.probability_overridden) in {
        (default_stage("negotiation").pk, Decimal("75.00"), False),
        (default_stage("new").pk, Decimal("33.00"), True),
    }


def test_archive_racing_move():
    owner = UserFactory()
    opportunity = created(owner, LeadFactory(owner=owner))
    results = run_concurrently(
        lambda: services.archive_opportunity(
            actor=owner, scope=OWN(owner.pk), opportunity_id=opportunity.pk, version=1
        ),
        lambda: move(owner, OWN(owner.pk), opportunity.pk, "won"),
    )
    ok, failed = split(results)
    assert (len(ok), [type(f) for f in failed]) == (1, [ConflictError])
    opportunity.refresh_from_db()
    assert (opportunity.archived_at is not None, opportunity.status) in {
        (True, "open"),
        (False, "won"),
    }


# --- the lead and its opportunities ----------------------------------------------------------
@pytest.mark.parametrize("target", ["proposal", "won", "lost"])
def test_reassignment_racing_a_stage_move_or_close(target):
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    opportunity = created(a, lead)
    results = run_concurrently(
        lambda: move(a, OWN(a.pk), opportunity.pk, target),
        lambda: reassign(admin, lead.pk, b),
    )
    no_deadlocks(results)
    moved, reassigned = results
    assert isinstance(reassigned, Lead), reassigned
    opportunity.refresh_from_db()
    if isinstance(moved, Opportunity):  # the move committed first
        closed = target != "proposal"
        assert opportunity.owner_id == (a.pk if closed else b.pk)
    else:  # the reassignment committed first: A can't see it any more
        assert isinstance(moved, NotFoundError | ConflictError), moved
        assert (opportunity.owner_id, opportunity.stage_id) == (b.pk, default_stage("new").pk)
    assert_ownership_coherent()
    assert_history_consistent()


def test_reassignment_racing_opportunity_creation():
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    results = run_concurrently(lambda: created(a, lead), lambda: reassign(admin, lead.pk, b))
    no_deadlocks(results)
    new, reassigned = results
    assert isinstance(reassigned, Lead), reassigned
    if isinstance(new, Opportunity):  # created first, then moved with the lead
        assert Opportunity.objects.get(pk=new.pk).owner_id == b.pk
    else:  # reassigned first: the lead left A's workspace
        assert isinstance(new, NotFoundError), new
        assert not Opportunity.objects.exists()
    assert_ownership_coherent()


def test_conversion_racing_conversion_creates_one_opportunity():
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    results = run_concurrently(
        *(
            lambda: services.convert_lead(
                actor=owner, scope=OWN(owner.pk), lead_id=lead.pk, lead_version=1, fields=FIELDS
            )
            for _ in range(3)
        )
    )
    ok, failed = split(results)
    assert len(ok) == 1
    assert all(isinstance(f, ConflictError | BusinessRuleViolation) for f in failed), failed
    assert Opportunity.objects.filter(lead=lead).count() == 1
    assert AuditEvent.objects.filter(action="lead.converted").count() == 1
    lead.refresh_from_db()
    assert lead.status_id == "converted"


def test_a_double_submitted_conversion_with_one_key_converts_once():
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    key = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
    results = run_concurrently(
        *(
            lambda: services.convert_lead(
                actor=owner,
                scope=OWN(owner.pk),
                lead_id=lead.pk,
                lead_version=1,
                fields=FIELDS,
                idempotency_key=key,
            )
            for _ in range(3)
        )
    )
    ok, failed = split(results)
    # The key serialises them: one converts, the rest replay it (or, having read the lead
    # before the winner committed, lose on the version: 409, and a retry replays).
    assert len(ok) >= 1
    assert all(isinstance(f, ConflictError) for f in failed), failed
    assert len({r.opportunity.pk for r in ok}) == 1
    assert [r.replayed for r in ok].count(False) == 1
    assert Opportunity.objects.count() == 1


def test_a_double_submitted_create_with_one_key_creates_one_opportunity():
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    key = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
    results = run_concurrently(
        *(
            lambda: services.create_opportunity(
                actor=owner,
                scope=OWN(owner.pk),
                lead_id=lead.pk,
                fields=FIELDS,
                idempotency_key=key,
            )
            for _ in range(3)
        )
    )
    assert all(isinstance(r, services.CreateResult) for r in results), results
    assert len({r.opportunity.pk for r in results}) == 1
    assert sorted(r.replayed for r in results) == [False, True, True]
    assert Opportunity.objects.count() == 1


# --- lock order: interleavings that deadlock if anything locks opportunity -> lead ----------
OPERATIONS = {
    "move": lambda a, opp: move(a, OWN(a.pk), opp.pk, "proposal"),
    "close": lambda a, opp: move(a, OWN(a.pk), opp.pk, "won"),
    "edit": lambda a, opp: services.update_opportunity(
        actor=a, scope=OWN(a.pk), opportunity_id=opp.pk, version=1, changes={"title": "Edited"}
    ),
    "archive": lambda a, opp: services.archive_opportunity(
        actor=a, scope=OWN(a.pk), opportunity_id=opp.pk, version=1
    ),
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_an_operation_arriving_while_a_reassignment_holds_the_lead_waits_for_it(operation):
    """The reassignment locks the lead, then pauses *before* moving the opportunities; the
    operation starts meanwhile. Locking lead first, it simply waits (and then finds the
    opportunity gone from A's workspace). Had it locked the opportunity first, the
    reassignment would need that lock while the operation needs the lead: a deadlock."""
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    opportunity = created(a, lead)
    lead_locked = threading.Event()

    def hold(_event):
        lead_locked.set()
        time.sleep(0.6)

    def run_operation():
        assert lead_locked.wait(10)
        time.sleep(0.1)  # well inside the reassignment's pause
        return OPERATIONS[operation](a, opportunity)

    with first_subscriber(LeadReassigned, hold):
        results = run_concurrently(lambda: reassign(admin, lead.pk, b), run_operation)
    no_deadlocks(results)
    reassigned, outcome = results
    assert isinstance(reassigned, Lead), reassigned
    assert isinstance(outcome, NotFoundError), outcome  # waited, then: no longer A's
    assert Opportunity.objects.get(pk=opportunity.pk).owner_id == b.pk
    assert_ownership_coherent()


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_a_reassignment_arriving_while_an_operation_holds_the_opportunity_waits_for_it(
    operation,
):
    """The reverse: the operation holds lead and opportunity (paused in its own domain
    event), the reassignment waits for the lead, then moves the result if still open."""
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    opportunity = created(a, lead)
    holding = threading.Event()
    event_type = {
        "move": events.OpportunityStageChanged,
        "close": events.OpportunityStageChanged,
        "edit": events.OpportunityUpdated,
        "archive": events.OpportunityArchived,
    }[operation]

    def hold(_event):
        holding.set()
        time.sleep(0.6)

    def run_reassign():
        assert holding.wait(10)
        time.sleep(0.1)
        return reassign(admin, lead.pk, b)

    with subscribed(event_type, hold):
        results = run_concurrently(lambda: OPERATIONS[operation](a, opportunity), run_reassign)
    no_deadlocks(results)
    done, reassigned = results
    assert not isinstance(done, BaseException), done
    assert isinstance(reassigned, Lead), reassigned
    opportunity.refresh_from_db()
    assert opportunity.owner_id == (a.pk if operation == "close" else b.pk)
    assert_ownership_coherent()


def test_conversion_and_reassignment_serialise():
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    holding = threading.Event()

    def hold(_event):
        holding.set()
        time.sleep(0.6)

    def run_reassign():
        assert holding.wait(10)
        time.sleep(0.1)
        return reassign(admin, lead.pk, b, version=2)  # the version after the conversion

    with subscribed(events.OpportunityCreated, hold):
        results = run_concurrently(
            lambda: services.convert_lead(
                actor=a, scope=OWN(a.pk), lead_id=lead.pk, lead_version=1, fields=FIELDS
            ),
            run_reassign,
        )
    no_deadlocks(results)
    converted, reassigned = results
    assert isinstance(converted, services.ConversionResult), converted
    assert isinstance(reassigned, Lead), reassigned
    assert Opportunity.objects.get(lead=lead).owner_id == b.pk
    assert_ownership_coherent()


def test_a_storm_of_mixed_operations_on_one_lead_never_deadlocks_or_breaks_ownership():
    """Twelve concurrent writers per round (reassignments both ways, creates, moves, closes,
    reopens, edits, archives), several rounds. Losers get 404/409/422; nobody deadlocks
    and every invariant holds afterwards."""
    admin, other_admin = AdminFactory(), AdminFactory()
    a, b = UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    for _ in range(4):
        created(a, lead)
    for round_ in range(4):
        lead.refresh_from_db()
        owner = lead.owner
        other = b if owner.pk == a.pk else a
        opportunities = list(Opportunity.objects.filter(lead=lead).order_by("id"))
        version = lead.version
        calls = [
            lambda other=other, version=version: reassign(admin, lead.pk, other, version=version),
            lambda other=other, version=version: reassign(
                other_admin, lead.pk, other, version=version
            ),
            lambda owner=owner: created(owner, lead),
        ]
        for i, opp in enumerate(opportunities[:9]):
            stage = ["proposal", "won", "negotiation", "lost", "new"][(i + round_) % 5]
            if i % 3 == 2:
                calls.append(
                    lambda opp=opp, value=Decimal(round_ + 1): services.update_opportunity(
                        actor=admin,
                        scope=ORG(admin.pk),
                        opportunity_id=opp.pk,
                        version=opp.version,
                        changes={"value": value},
                    )
                )
            else:
                calls.append(
                    lambda opp=opp, stage=stage: move(
                        admin, ORG(admin.pk), opp.pk, stage, version=opp.version
                    )
                )
        results = run_concurrently(*calls)
        no_deadlocks(results)
        for result in results:
            if isinstance(result, BaseException):
                assert isinstance(result, NotFoundError | ConflictError | BusinessRuleViolation), (
                    repr(result)
                )
        assert_ownership_coherent()
        assert_history_consistent()
