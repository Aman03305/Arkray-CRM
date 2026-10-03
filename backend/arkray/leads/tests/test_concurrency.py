"""Races on one lead, run for real: each call in its own thread and database connection.

The rule under test (docs/leads.md#concurrency): every change locks the row and requires
the version its author saw, so of several changes made from the same version exactly one
succeeds and the others get ConflictError (409). Nothing is silently overwritten.
"""

from __future__ import annotations

import threading
import time

import pytest

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.errors import ConflictError, InvalidInputError
from arkray.identity import services as identity_services
from arkray.identity.models import UserStatus
from arkray.leads import services
from arkray.leads.events import LeadReassigned
from arkray.leads.models import Lead
from tests.factories import AdminFactory, LeadFactory, UserFactory
from tests.helpers import run_concurrently

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("lead_configuration"),
]

ORG = AccessScope.organization


def split(results):
    successes = [r for r in results if isinstance(r, Lead)]
    failures = [r for r in results if not isinstance(r, Lead)]
    return successes, failures


def test_two_admins_reassigning_the_same_lead_to_different_users():
    first, second = AdminFactory(), AdminFactory()
    owner, x, y = UserFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=owner)
    results = run_concurrently(
        lambda: services.reassign_lead(
            actor=first, scope=ORG(first.pk), lead_id=lead.pk, version=1, owner_id=x.pk
        ),
        lambda: services.reassign_lead(
            actor=second, scope=ORG(second.pk), lead_id=lead.pk, version=1, owner_id=y.pk
        ),
    )
    successes, failures = split(results)
    assert len(successes) == 1
    assert [type(f) for f in failures] == [ConflictError]
    lead.refresh_from_db()
    assert lead.owner_id == successes[0].owner_id
    assert lead.version == 2
    assert AuditEvent.objects.filter(action="lead.reassigned").count() == 1


def test_two_admins_reassigning_to_the_same_user_both_succeed_once():
    first, second = AdminFactory(), AdminFactory()
    owner, target = UserFactory(), UserFactory()
    lead = LeadFactory(owner=owner)
    results = run_concurrently(
        *(
            lambda actor=actor: services.reassign_lead(
                actor=actor, scope=ORG(actor.pk), lead_id=lead.pk, version=1, owner_id=target.pk
            )
            for actor in (first, second)
        )
    )
    assert all(isinstance(r, Lead) for r in results)  # the second is a no-op
    lead.refresh_from_db()
    assert (lead.owner_id, lead.version) == (target.pk, 2)
    assert AuditEvent.objects.filter(action="lead.reassigned").count() == 1


def test_an_admin_and_the_owner_editing_at_once_never_lose_an_update():
    admin, owner = AdminFactory(), UserFactory()
    lead = LeadFactory(owner=owner)
    results = run_concurrently(
        lambda: services.update_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, changes={"city": "Pune"}
        ),
        lambda: services.update_lead(
            actor=owner,
            scope=AccessScope.own(owner.pk),
            lead_id=lead.pk,
            version=1,
            changes={"job_title": "Director"},
        ),
    )
    successes, failures = split(results)
    assert len(successes) == 1
    assert [type(f) for f in failures] == [ConflictError]
    lead.refresh_from_db()
    winner = successes[0]
    assert (lead.city, lead.job_title) == (winner.city, winner.job_title)
    assert lead.version == 2


def test_archive_racing_an_edit():
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    scope = AccessScope.own(owner.pk)
    results = run_concurrently(
        lambda: services.archive_lead(actor=owner, scope=scope, lead_id=lead.pk, version=1),
        lambda: services.update_lead(
            actor=owner, scope=scope, lead_id=lead.pk, version=1, changes={"city": "Pune"}
        ),
    )
    successes, failures = split(results)
    assert len(successes) == 1
    assert [type(f) for f in failures] == [ConflictError]
    lead.refresh_from_db()
    # Either archived with its old data, or edited and still active: never both half-applied.
    assert (lead.archived_at is not None, lead.city) in {(True, ""), (False, "Pune")}


def test_a_status_change_racing_a_reassignment():
    admin, owner, target = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=owner)
    results = run_concurrently(
        lambda: services.change_status(
            actor=owner,
            scope=AccessScope.own(owner.pk),
            lead_id=lead.pk,
            version=1,
            status="contacted",
        ),
        lambda: services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=target.pk
        ),
    )
    successes, failures = split(results)
    assert len(successes) == 1
    assert len(failures) == 1
    # The loser either lost the version race (409) or, if the reassignment won, can no
    # longer even see the lead in the previous owner's workspace (404).
    lead.refresh_from_db()
    assert lead.version == 2
    assert (lead.status_id, lead.owner_id) in {("contacted", owner.pk), ("new", target.pk)}


def test_a_reassignment_and_the_new_owners_deactivation_serialise():
    """The new owner's row is share-locked until the reassignment commits, so a concurrent
    deactivation either waits for it or is seen by it: a lead is never handed to someone
    who had already been deactivated."""
    admin, other_admin = AdminFactory(), AdminFactory()
    owner, target = UserFactory(), UserFactory()
    lead = LeadFactory(owner=owner)
    locked = threading.Event()
    finished: dict[str, float] = {}

    def slow_subscriber(_event):
        locked.set()
        time.sleep(0.5)  # hold the transaction (and the share lock) open

    def reassign():
        with subscribed(LeadReassigned, slow_subscriber):
            result = services.reassign_lead(
                actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=target.pk
            )
        finished["reassign"] = time.monotonic()
        return result

    def deactivate():
        assert locked.wait(10)
        result = identity_services.deactivate_user(actor_id=other_admin.pk, user_id=target.pk)
        finished["deactivate"] = time.monotonic()
        return result

    results = run_concurrently(reassign, deactivate)
    assert isinstance(results[0], Lead), results[0]
    assert results[1].status == UserStatus.DEACTIVATED
    assert finished["deactivate"] >= finished["reassign"]  # it waited for the assignment


def test_a_deactivated_user_cannot_receive_a_lead_afterwards():
    admin, owner, target = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=owner)
    identity_services.deactivate_user(actor_id=admin.pk, user_id=target.pk)
    with pytest.raises(InvalidInputError):
        services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=target.pk
        )


def test_a_double_submitted_create_with_one_key_creates_one_lead():
    owner = UserFactory()
    key = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
    results = run_concurrently(
        *(
            lambda: services.create_lead(
                actor=owner,
                scope=AccessScope.own(owner.pk),
                fields={"first_name": "Rahul"},
                idempotency_key=key,
            )
            for _ in range(3)
        )
    )
    assert all(isinstance(r, services.CreateResult) for r in results), results
    assert len({r.lead.pk for r in results}) == 1
    assert sorted(r.replayed for r in results) == [False, True, True]
    assert Lead.objects.count() == 1
    assert AuditEvent.objects.filter(action="lead.created").count() == 1


def test_concurrent_creates_without_a_key_are_independent():
    owner = UserFactory()
    results = run_concurrently(
        *(
            lambda: services.create_lead(
                actor=owner, scope=AccessScope.own(owner.pk), fields={"first_name": "Rahul"}
            )
            for _ in range(3)
        )
    )
    assert all(isinstance(r, services.CreateResult) for r in results), results
    assert Lead.objects.count() == 3
