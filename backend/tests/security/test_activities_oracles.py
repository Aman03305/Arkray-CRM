"""Oracles about records outside the caller's scope after a lead is reassigned: the previous
owner keeps completed work and won opportunities, the new owner gets the lead and its open
work, and neither may learn anything about what the other holds from the answers they get.

Reopening or restoring kept work whatever the other user's lead state is covered once, at
the service level, in test_phase4_review_regressions.py (SEC-2).
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.db import transaction

from arkray.activities import services
from arkray.core import holds
from arkray.core.access import AccessScope
from arkray.core.models import HoldSubject
from arkray.leads import services as lead_services
from tests.factories import LeadFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

ME = "/api/v1/workspaces/me"
OWN = AccessScope.own
ORG = AccessScope.organization


def reassign(admin, lead, to):
    lead.refresh_from_db()
    lead_services.reassign_lead(
        actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=lead.version, owner_id=to.pk
    )


class TestRelationshipOracles:
    def test_the_closer_creating_work_on_their_kept_opportunity_is_refused_with_a_reason(
        self, admin, user_a, user_b
    ):
        """A's won opportunity stayed with A when the lead went to B. A knows it (it is A's),
        so the refusal is the documented 422, not a 404."""
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage_key="won")
        reassign(admin, lead, user_b)
        response = signed_in(user_a).post(
            f"{ME}/activities",
            {"type": "task", "title": "x", "opportunity": str(won.pk)},
            format="json",
        )
        assert response.status_code == 422

    def test_new_owner_linking_to_previous_owners_kept_opportunity_is_a_plain_404(
        self, admin, user_a, user_b
    ):
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage_key="won")
        reassign(admin, lead, user_b)
        client = signed_in(user_b)
        real = client.post(
            f"{ME}/activities",
            {"type": "task", "title": "x", "lead": str(lead.pk), "opportunity": str(won.pk)},
            format="json",
        )
        ghost = client.post(
            f"{ME}/activities",
            {
                "type": "task",
                "title": "x",
                "lead": str(lead.pk),
                "opportunity": str(uuid.uuid4()),
            },
            format="json",
        )
        alone = client.post(
            f"{ME}/activities",
            {"type": "task", "title": "x", "opportunity": str(won.pk)},
            format="json",
        )
        assert real.status_code == ghost.status_code == alone.status_code == 404
        assert without_request_id(real) == without_request_id(ghost)

    def test_a_visible_task_on_a_hidden_opportunity_never_shows_its_id(self, admin, user_a, user_b):
        """A's open task on A's won opportunity follows the lead to B; the opportunity stays
        with A, so B sees it as restricted, in the list and in the detail."""
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage_key="won")
        TaskFactory(opportunity=won, title="follow-up")
        reassign(admin, lead, user_b)
        client = signed_in(user_b)
        body = client.get(f"{ME}/activities").json()
        assert str(won.pk) not in str(body)
        row = body["results"][0]
        assert row["opportunity"] == {"id": None, "restricted": True}
        detail = client.get(f"{ME}/activities/{row['id']}").json()
        assert str(won.pk) not in str(detail)


class TestRepeatableOracle:
    def test_reopening_kept_work_answers_the_same_whatever_the_new_owner_does_with_the_lead(
        self, admin, user_a, user_b
    ):
        """A refused reopen changes nothing (no audit, no version bump), so if its answer
        depended on the lead's state A could poll B's lead at will through any completed task
        A kept. The answer is the same before B archives the lead, while it is archived and
        after B restores it."""
        lead = LeadFactory(owner=user_a)
        task = TaskFactory(lead=lead, title="A's finished work")
        services.complete_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
        )
        reassign(admin, lead, user_b)
        client = signed_in(user_a)

        def poll():
            response = client.post(
                f"{ME}/activities/{task.pk}/reopen", {"version": 2}, format="json"
            )
            return response.status_code, json.dumps(without_request_id(response), sort_keys=True)

        answers = [poll()]
        lead.refresh_from_db()
        lead_services.archive_lead(
            actor=user_b, scope=OWN(user_b.pk), lead_id=lead.pk, version=lead.version
        )
        answers.append(poll())
        lead.refresh_from_db()
        lead_services.restore_lead(
            actor=user_b, scope=OWN(user_b.pk), lead_id=lead.pk, version=lead.version
        )
        answers.append(poll())
        task.refresh_from_db()
        assert task.version == 2  # nothing recorded on A's side
        assert len(set(answers)) == 1, answers

    def test_kept_work_answers_the_same_whether_or_not_its_customer_is_held(
        self, admin, user_a, user_b
    ):
        """Follow-up to the adversarial review of 73ff1e3, M1 (a legal-hold oracle): an edit
        or a file deletion that touches a held customer's text answers 409 `legal_hold`, so
        the previous owner of a reassigned lead must never reach that check. Notes and open
        work move with the lead; the completed work A keeps is refused before any hold is
        read (an edit: "reopen it first"; a reopen: "belongs to someone else"), with the same
        answer whether or not B's customer is under a hold."""

        def kept_task():
            lead = LeadFactory(owner=user_a)
            task = TaskFactory(lead=lead, title="A's finished work")
            services.complete_activity(
                actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
            )
            reassign(admin, lead, user_b)
            return lead, task

        held_lead, held_task = kept_task()
        _, free_task = kept_task()
        with transaction.atomic():
            holds.place(HoldSubject.LEAD, held_lead.pk, reference="CASE-9", placed_by=admin.pk)
        client = signed_in(user_a)

        def answers(task):
            edited = client.patch(
                f"{ME}/activities/{task.pk}", {"version": 2, "title": "Renamed"}, format="json"
            )
            reopened = client.post(
                f"{ME}/activities/{task.pk}/reopen", {"version": 2}, format="json"
            )
            return [
                (r.status_code, json.dumps(without_request_id(r), sort_keys=True))
                for r in (edited, reopened)
            ]

        held, free = answers(held_task), answers(free_task)
        assert held == free, (held, free)
        assert [status for status, _ in held] == [422, 422]
        assert "legal_hold" not in str(held)
        held_task.refresh_from_db()
        assert (held_task.version, held_task.title) == (2, "A's finished work")
