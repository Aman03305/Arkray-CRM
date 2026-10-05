"""Regression tests for the Phase 3 adversarial review: one (or more) per confirmed finding,
so none can silently return. Three reviewers (API security, backend domain/concurrency,
frontend) reproduced them; the frontend ones are pinned in
frontend/src/features/pipeline/review-regressions.test.tsx.

  P1  opportunity writes also locked the shared stage row: crossing moves by different
      users deadlocked (500s) and every write in a stage queued behind the others
  P3  a conversion retried with the same key while the first was in flight got a 409
  P3  the board's counts, totals and cards came from different snapshots
  P3  a closed opportunity could be reopened (or an archived one restored) on an
      archived lead, whose pipeline is otherwise read-only
  P3  a move to the current stage silently dropped a lost reason
  P3  reassignment wrote one audit INSERT per open opportunity (N+1)
  P3  a lead left Converted without an opportunity (Phase 2 data) couldn't be converted
  P3  the "Converted needs an opportunity" veto revealed an opportunity the lead's owner
      can't see (a deal the previous owner closed before the reassignment)
  P3  value-sorted cursors carried the deal amount readable in URLs
  P3  a board column asked for without cards (cards_per_stage=0) had no `next` link
"""

from __future__ import annotations

import threading
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.core import keyset
from arkray.core.access import AccessScope
from arkray.core.errors import BusinessRuleViolation, InvalidInputError
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import selectors, services
from arkray.pipeline.models import Opportunity
from arkray.pipeline.selectors import OpportunityFilters
from tests.factories import (
    AdminFactory,
    LeadFactory,
    OpportunityFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import run_concurrently, signed_in

OWN = AccessScope.own
ORG = AccessScope.organization
FIELDS = {"title": "Review deal", "value": Decimal("100000")}
KEY = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"

transactional = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]


def create(owner, stage_key="new"):
    return services.create_opportunity(
        actor=owner,
        scope=OWN(owner.pk),
        lead_id=LeadFactory(owner=owner).pk,
        fields=FIELDS,
        stage_id=default_stage(stage_key).pk,
        # the seeded Negotiation is a negotiation stage (pipeline.0006)
        negotiated_price=Decimal("1000") if stage_key == "negotiation" else None,
    ).opportunity


def move(actor, opportunity, stage_key, version=1, **kwargs):
    if stage_key == "negotiation":
        kwargs.setdefault("negotiated_price", Decimal("1000"))
    return services.move_opportunity(
        actor=actor,
        scope=OWN(actor.pk),
        opportunity_id=opportunity.pk,
        version=version,
        stage_id=default_stage(stage_key).pk,
        **kwargs,
    )


# --- P1: stage rows are never locked by opportunity writes ------------------------------------
class TestStageRowsAreNotLocked:
    pytestmark = transactional

    def test_the_opportunity_lock_names_only_the_opportunity(self):
        owner = UserFactory()
        opportunity = create(owner)
        with CaptureQueriesContext(connection) as queries:
            move(owner, opportunity, "qualified")
        locking = [
            q["sql"]
            for q in queries.captured_queries
            if "pipeline_opportunity" in q["sql"] and " FOR " in q["sql"]
        ]
        assert locking, "the opportunity is locked"
        for sql in locking:
            assert 'FOR NO KEY UPDATE OF "pipeline_opportunity"' in sql, sql

    def test_crossing_moves_by_different_users_holding_their_locks_both_succeed(self, monkeypatch):
        """The reviewer's deterministic repro: both moves hold their locks before either
        updates (a barrier), then cross New <-> Qualified. It deadlocked before the fix."""
        a, b = UserFactory(), UserFactory()
        x, y = create(a, "new"), create(b, "qualified")
        barrier = threading.Barrier(2)
        original = services._require_version

        def paused(opportunity, version):
            barrier.wait(10)
            return original(opportunity, version)

        monkeypatch.setattr(services, "_require_version", paused)
        results = run_concurrently(
            lambda: move(a, x, "qualified"),
            lambda: move(b, y, "new"),
        )
        assert all(isinstance(r, Opportunity) for r in results), results

    def test_many_independent_salespeople_dragging_across_never_deadlock(self):
        """Uninstrumented: 3 rounds of 8 salespeople moving their own cards in opposite
        directions (reviewer: 59 of 80 failed with 'deadlock detected')."""
        failures = []
        for _ in range(3):
            calls = []
            for i in range(8):
                user = UserFactory()
                src, dst = ("new", "qualified") if i % 2 else ("qualified", "new")
                opportunity = create(user, src)
                calls.append(lambda u=user, o=opportunity, d=dst: move(u, o, d))
            failures += [r for r in run_concurrently(*calls) if isinstance(r, BaseException)]
        assert failures == []

    def test_closing_and_reopening_across_users_never_deadlock(self):
        a, b = UserFactory(), UserFactory()
        pairs = [(create(a, "negotiation"), create(b, "won")) for _ in range(4)]
        results = []
        for opening, closed in pairs:
            results += run_concurrently(
                lambda o=opening: move(a, o, "won"), lambda c=closed: move(b, c, "negotiation")
            )
        assert all(isinstance(r, Opportunity) for r in results), results


# --- P3: conversion replays an in-flight duplicate -------------------------------------------
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_a_double_submitted_conversion_replays_instead_of_conflicting():
    for _ in range(3):
        owner = UserFactory()
        lead = LeadFactory(owner=owner)
        results = run_concurrently(
            *(
                lambda owner=owner, lead=lead: services.convert_lead(
                    actor=owner,
                    scope=OWN(owner.pk),
                    lead_id=lead.pk,
                    lead_version=1,
                    fields=FIELDS,
                    idempotency_key=KEY,
                )
                for _ in range(2)
            )
        )
        assert all(isinstance(r, services.ConversionResult) for r in results), results
        assert sorted(r.replayed for r in results) == [False, True]
        assert Opportunity.objects.filter(lead=lead).count() == 1


# --- P3: one snapshot per board ------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_the_boards_figures_describe_one_moment(monkeypatch):
    """A move to Won commits between the board's first query and its totals: every figure
    must still come from the same snapshot."""
    owner = UserFactory()
    opportunity = create(owner, "proposal")
    original = selectors.pipeline_totals
    fired = []

    def totals_after_a_concurrent_commit(scope, filters):
        if not fired:
            fired.append(True)

            def win():
                move(owner, opportunity, "won")
                connections.close_all()

            thread = threading.Thread(target=win)
            thread.start()
            thread.join()
        return original(scope, filters)

    monkeypatch.setattr(selectors, "pipeline_totals", totals_after_a_concurrent_commit)
    board = selectors.board(
        OWN(owner.pk),
        selectors.pipeline_for_board(OWN(owner.pk), None),
        OpportunityFilters(),
        binding_for=None,
    )
    open_in_columns = sum(c.count for c in board.columns if c.stage.category == "open")
    assert fired
    assert Opportunity.objects.get(pk=opportunity.pk).status == "won"  # it did commit
    assert open_in_columns == board.totals.open_count == 1  # the moment before it
    assert [o.status for c in board.columns for o in c.page.items] == ["open"]


# --- P3: an archived lead's pipeline stays read-only ------------------------------------------
@pytest.mark.django_db
class TestArchivedLeads:
    def test_no_reopening_on_an_archived_lead(self, user_a):
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage=default_stage("won"))
        Lead.objects.filter(pk=lead.pk).update(archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="record is archived"):
            move(user_a, won, "negotiation")
        won.refresh_from_db()
        assert won.status == "won"

    def test_no_restoring_an_opportunity_on_an_archived_lead(self, user_a):
        lead = LeadFactory(owner=user_a)
        archived = OpportunityFactory(lead=lead, archived_at=timezone.now())
        Lead.objects.filter(pk=lead.pk).update(archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="record is archived"):
            services.restore_opportunity(
                actor=user_a, scope=OWN(user_a.pk), opportunity_id=archived.pk, version=1
            )

    def test_closing_an_open_opportunity_of_an_archived_lead_is_still_possible(self, user_a):
        lead = LeadFactory(owner=user_a)
        opportunity = OpportunityFactory(lead=lead)
        Lead.objects.filter(pk=lead.pk).update(archived_at=timezone.now())
        assert move(user_a, opportunity, "lost").status == "lost"


# --- P3: a lost reason is never silently dropped -------------------------------------------------
@pytest.mark.django_db
def test_a_lost_reason_with_a_move_to_the_current_stage_is_refused(user_a):
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=default_stage("lost"))
    with pytest.raises(InvalidInputError) as error:
        move(user_a, opportunity, "lost", lost_reason="A new reason")
    assert "lost_reason" in error.value.details
    open_one = OpportunityFactory(lead=LeadFactory(owner=user_a))
    with pytest.raises(InvalidInputError):
        move(user_a, open_one, "new", lost_reason="Budget")


# --- P3: reassignment costs the same however many opportunities move --------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("n", [1, 25])
def test_reassignment_query_count_is_constant(n):
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    for _ in range(n):
        OpportunityFactory(lead=lead)
    with CaptureQueriesContext(connection) as queries:
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=b.pk
        )
    assert Opportunity.objects.filter(lead=lead, owner=b).count() == n
    # savepoint, lead lock, user share lock, lead update, lead audit, opportunities lock,
    # opportunities update, one audit insert for all of them, release, reload; since Phase 4
    # also the activities' lock query (no current work here) and the timeline entry
    # (arkray/activities/tests/test_reassignment.py pins the count with activities moving)
    assert len(queries) == 12


# --- P3: a Phase 2 "Converted" lead without an opportunity can be converted properly ---------
@pytest.mark.django_db
def test_a_legacy_converted_lead_without_an_opportunity_can_be_converted(user_a):
    lead = LeadFactory(owner=user_a, status_id="converted")
    result = services.convert_lead(
        actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, lead_version=1, fields=FIELDS
    )
    assert result.lead.status.key == "converted"
    assert Opportunity.objects.filter(lead=lead).count() == 1
    with pytest.raises(BusinessRuleViolation, match="already been converted"):
        services.convert_lead(
            actor=user_a,
            scope=OWN(user_a.pk),
            lead_id=lead.pk,
            lead_version=result.lead.version,
            fields=FIELDS,
        )


# --- P3: the conversion veto is not an oracle ---------------------------------------------------
@pytest.mark.django_db
def test_the_converted_veto_ignores_opportunities_the_owner_cant_see(admin, user_a, user_b):
    """A closed a deal on lead L; L was then reassigned to B. B can't see that deal, so
    marking L Converted must answer exactly as for a lead with no opportunity at all."""
    hidden = LeadFactory(owner=user_a)
    OpportunityFactory(lead=hidden, stage=default_stage("won"))
    lead_services.reassign_lead(
        actor=admin, scope=ORG(admin.pk), lead_id=hidden.pk, version=1, owner_id=user_b.pk
    )
    control = LeadFactory(owner=user_b)
    client = signed_in(user_b)
    answers = [
        client.post(
            f"/api/v1/workspaces/me/leads/{lead.pk}/status",
            {"status": "converted", "version": version},
            format="json",
        )
        for lead, version in ((hidden, 2), (control, 1))
    ]
    assert [a.status_code for a in answers] == [422, 422]
    bodies = [a.json()["error"] for a in answers]
    assert bodies[0]["message"] == bodies[1]["message"]


# --- P3: deal values never travel in URLs ----------------------------------------------------
@pytest.mark.django_db
def test_value_sorted_page_links_hold_no_amount(user_a):
    for value in ("987654321.99", "5", "6"):
        OpportunityFactory(lead=LeadFactory(owner=user_a), value=Decimal(value))
    body = (
        signed_in(user_a)
        .get("/api/v1/workspaces/me/opportunities", {"ordering": "-value", "page_size": 1})
        .json()
    )
    cursor = parse_qs(urlsplit(body["next"]).query)["cursor"][0]
    assert keyset._open(cursor)["v"][0] is None
    assert "987654321" not in body["next"]


# --- P3: summaries-only columns still link to their opportunities ---------------------------
@pytest.mark.django_db
def test_a_column_without_cards_links_to_its_list(user_a):
    OpportunityFactory(lead=LeadFactory(owner=user_a))
    client = signed_in(user_a)
    body = client.get("/api/v1/workspaces/me/pipeline-board", {"cards_per_stage": 0}).json()
    new = body["columns"][0]
    assert (new["count"], new["cards"]) == (1, [])
    assert new["next"] is not None
    assert "cursor=" not in new["next"]
    listed = client.get(new["next"]).json()
    assert len(listed["results"]) == 1
    assert body["columns"][1]["next"] is None  # an empty column has nothing to continue
