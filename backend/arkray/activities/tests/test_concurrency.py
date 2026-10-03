"""Races on activities and their leads, run for real: each call in its own thread and
database connection (docs/activities.md#concurrency).

Rules under test:
- every change locks its rows and requires the version its author saw: of several changes
  made from the same version exactly one succeeds, the others get 409 (or 404 when a
  reassignment moved the record out of the loser's workspace meanwhile); repeating a
  change that already happened (completing a completed task) is a no-op success;
- lock order is always lead -> opportunities -> activities -> user rows, so no two
  operations can wait for each other in a cycle;
- whatever the interleaving, current work is never left with anyone but its lead's owner,
  history is never duplicated, and the lead's last contact never moves backwards.
"""

from __future__ import annotations

import itertools
import random
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from arkray.activities import services
from arkray.activities.models import (
    CURRENT_STATUSES,
    Activity,
    ActivityStatus,
    ActivityType,
    TimelineEntry,
)
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import BusinessRuleViolation, ConflictError, NotFoundError
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import services as pipeline_services
from tests.factories import (
    AdminFactory,
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import run_concurrently

from .conftest import meeting_fields, note_fields, task_fields

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]

OWN = AccessScope.own
ORG = AccessScope.organization


def split(results):
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    return ok, failed


def no_deadlocks(results):
    for result in results:
        assert "deadlock" not in str(result).lower(), result


def assert_current_work_coherent():
    """No current work (open task, scheduled meeting, note) owned by anyone but its lead's
    owner (the database also refuses this at commit; this checks what was committed)."""
    for activity in Activity.objects.select_related("lead"):
        if activity.type == ActivityType.NOTE or activity.status in CURRENT_STATUSES:
            assert activity.owner_id == activity.lead.owner_id, activity.pk


def act(operation, actor, activity, version=1, scope=None):
    return getattr(services, f"{operation}_activity")(
        actor=actor, scope=scope or OWN(actor.pk), activity_id=activity.pk, version=version
    )


def edit(actor, activity, version=1, scope=None, **changes):
    return services.update_activity(
        actor=actor,
        scope=scope or OWN(actor.pk),
        activity_id=activity.pk,
        version=version,
        changes=changes,
    )


def reassign(admin, lead, to, version=1):
    return lead_services.reassign_lead(
        actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=version, owner_id=to.pk
    )


def started_meeting(lead, hours_ago=2.0):
    start = timezone.now() - timedelta(hours=hours_ago)
    return MeetingFactory(lead=lead, starts_at=start, ends_at=start + timedelta(minutes=30))


# --- one activity, two writers ------------------------------------------------------------------
def test_complete_racing_complete_completes_once():
    owner = UserFactory()
    task = TaskFactory(lead=LeadFactory(owner=owner))
    results = run_concurrently(*(lambda: act("complete", owner, task) for _ in range(3)))
    ok, failed = split(results)
    assert len(ok) == 3, failed  # the repeats are no-op successes
    task.refresh_from_db()
    assert (task.status, task.version) == ("completed", 2)
    assert TimelineEntry.objects.filter(kind="task.completed").count() == 1
    assert AuditEvent.objects.filter(action="task.completed").count() == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [("edit", "complete"), ("cancel", "complete"), ("edit", "cancel")],
)
@pytest.mark.parametrize("kind", ["task", "meeting"])
def test_two_changes_from_one_version_one_wins_the_other_is_a_conflict(first, second, kind):
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    activity = TaskFactory(lead=lead) if kind == "task" else started_meeting(lead)

    def run(operation):
        if operation == "edit":
            return edit(owner, activity, title="Changed by a racer")
        return act(operation, owner, activity)

    results = run_concurrently(lambda: run(first), lambda: run(second))
    no_deadlocks(results)
    ok, failed = split(results)
    assert len(ok) == 1
    assert [type(f) for f in failed] == [ConflictError]
    activity.refresh_from_db()
    assert activity.version == 2  # exactly one change applied, none lost


def test_note_edit_racing_reassignment():
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    note = NoteFactory(lead=lead, created_by=a)
    results = run_concurrently(
        lambda: edit(a, note, description="Edited by the author"),
        lambda: reassign(admin, lead, b),
    )
    no_deadlocks(results)
    edited, reassigned = results
    assert not isinstance(reassigned, BaseException), reassigned
    note.refresh_from_db()
    assert note.owner_id == b.pk  # the note followed the lead either way
    assert note.created_by_id == a.pk
    if isinstance(edited, BaseException):
        assert isinstance(edited, NotFoundError)  # the reassignment went first
        assert note.description != "Edited by the author"
    else:
        assert note.description == "Edited by the author"  # never lost by the move
    assert_current_work_coherent()


@pytest.mark.parametrize("kind", ["task", "meeting", "note"])
def test_creation_racing_reassignment(kind):
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    fields = {"task": task_fields, "meeting": meeting_fields, "note": note_fields}[kind]()
    results = run_concurrently(
        lambda: services.create_activity(
            actor=a, scope=OWN(a.pk), activity_type=kind, fields=fields, lead_id=lead.pk
        ),
        lambda: reassign(admin, lead, b),
    )
    no_deadlocks(results)
    created, reassigned = results
    assert not isinstance(reassigned, BaseException), reassigned
    if isinstance(created, BaseException):
        assert isinstance(created, NotFoundError)
        assert not Activity.objects.exists()
    else:
        assert Activity.objects.get().owner_id == b.pk  # created, then moved with the lead
    assert_current_work_coherent()


def test_creation_racing_the_opportunity_closing():
    owner = UserFactory()
    opportunity = OpportunityFactory(lead=LeadFactory(owner=owner))
    results = run_concurrently(
        lambda: services.create_activity(
            actor=owner,
            scope=OWN(owner.pk),
            activity_type="task",
            fields=task_fields(),
            opportunity_id=opportunity.pk,
        ),
        lambda: pipeline_services.move_opportunity(
            actor=owner,
            scope=OWN(owner.pk),
            opportunity_id=opportunity.pk,
            version=1,
            stage_id=default_stage("won").pk,
        ),
    )
    no_deadlocks(results)
    ok, failed = split(results)
    assert len(ok) == 2, failed  # activities may follow up a closed opportunity
    assert Activity.objects.get().opportunity_id == opportunity.pk


def test_completion_racing_reassignment():
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    task = TaskFactory(lead=lead)
    results = run_concurrently(lambda: act("complete", a, task), lambda: reassign(admin, lead, b))
    no_deadlocks(results)
    completed, reassigned = results
    assert not isinstance(reassigned, BaseException), reassigned
    task.refresh_from_db()
    if isinstance(completed, BaseException):
        assert isinstance(completed, NotFoundError)  # moved away first: still open, now B's
        assert (task.status, task.owner_id) == ("open", b.pk)
    else:
        # completed first: history, so it stays with A, who did it
        assert (task.status, task.owner_id, task.completed_by_id) == ("completed", a.pk, a.pk)
    assert_current_work_coherent()


def test_meeting_scheduling_racing_reassignment_with_the_lock_held():
    """The reassignment holds the lead's lock (before any opportunity or activity is touched)
    while the scheduling request arrives: it must wait, then find the lead gone from its
    workspace. A wrong lock order (activities before the lead) would deadlock here."""
    from arkray.core import domain_events
    from arkray.leads.events import LeadReassigned

    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    holding = threading.Event()

    def hold(event):
        holding.set()
        threading.Event().wait(0.5)

    domain_events._SUBSCRIBERS.setdefault(LeadReassigned, []).insert(0, hold)
    try:

        def schedule():
            holding.wait(10)
            return services.create_activity(
                actor=a,
                scope=OWN(a.pk),
                activity_type="meeting",
                fields=meeting_fields(),
                lead_id=lead.pk,
            )

        results = run_concurrently(lambda: reassign(admin, lead, b), schedule)
    finally:
        domain_events._SUBSCRIBERS[LeadReassigned].remove(hold)
    no_deadlocks(results)
    reassigned, scheduled = results
    assert not isinstance(reassigned, BaseException), reassigned
    assert isinstance(scheduled, NotFoundError)
    assert not Activity.objects.exists()


# --- last contact ---------------------------------------------------------------------------
def test_older_and_newer_meeting_completions_never_move_last_contact_backwards():
    owner = UserFactory()
    for _ in range(4):
        lead = LeadFactory(owner=owner)
        meetings = [started_meeting(lead, hours) for hours in (30, 5, 50, 12)]
        random.shuffle(meetings)
        results = run_concurrently(*(lambda m=m: act("complete", owner, m) for m in meetings))
        no_deadlocks(results)
        assert split(results)[1] == []
        newest = min(meetings, key=lambda m: timezone.now() - m.starts_at)
        assert Lead.objects.get(pk=lead.pk).last_contacted_at == newest.starts_at


# --- many writers ---------------------------------------------------------------------------------
def test_a_storm_of_mixed_writers_on_one_lead_never_deadlocks():
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    opportunity = OpportunityFactory(lead=lead)
    owners = itertools.cycle([b, a])
    for _ in range(3):
        current = Lead.objects.get(pk=lead.pk)
        actor = current.owner
        tasks = list(Activity.objects.filter(lead=lead, type="task", status="open")[:3])
        meetings = [started_meeting(current) for _ in range(2)]
        calls = [
            lambda actor=actor: services.create_activity(
                actor=actor,
                scope=OWN(actor.pk),
                activity_type="note",
                fields=note_fields(),
                lead_id=lead.pk,
            ),
            lambda: services.create_activity(
                actor=admin,
                scope=ORG(admin.pk),
                activity_type="task",
                fields=task_fields(),
                opportunity_id=opportunity.pk,
            ),
            *(lambda m=m: act("complete", admin, m, scope=ORG(admin.pk)) for m in meetings),
            *(lambda t=t, actor=actor: act("cancel", actor, t, version=t.version) for t in tasks),
            lambda current=current: reassign(admin, lead, next(owners), version=current.version),
            lambda: pipeline_services.update_opportunity(
                actor=admin,
                scope=ORG(admin.pk),
                opportunity_id=opportunity.pk,
                version=opportunity.version,
                changes={"value": Decimal(random.randint(1, 9))},
            ),
        ]
        results = run_concurrently(*calls)
        no_deadlocks(results)
        for failure in split(results)[1]:
            assert isinstance(failure, ConflictError | NotFoundError | BusinessRuleViolation), (
                failure
            )
        opportunity.refresh_from_db()
        assert_current_work_coherent()


def test_different_users_on_different_leads_never_wait_for_each_other_in_a_cycle(monkeypatch):
    """Two users each hold their own lead and activity locks at the same moment (a barrier
    inside both operations), then finish: the only shared rows are configuration and user
    rows, which activity writes never lock exclusively."""
    a, b = UserFactory(), UserFactory()
    task_a = TaskFactory(lead=LeadFactory(owner=a))
    task_b = TaskFactory(lead=LeadFactory(owner=b))
    barrier = threading.Barrier(2)
    original = services._require_version

    def paused(activity, version):
        barrier.wait(10)
        return original(activity, version)

    monkeypatch.setattr(services, "_require_version", paused)
    results = run_concurrently(
        lambda: act("complete", a, task_a), lambda: act("complete", b, task_b)
    )
    no_deadlocks(results)
    assert split(results)[1] == []


def test_the_same_create_twice_with_one_key_creates_once():
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    key = uuid.uuid4()
    results = run_concurrently(
        *(
            lambda: services.create_activity(
                actor=owner,
                scope=OWN(owner.pk),
                activity_type="task",
                fields=task_fields(),
                lead_id=lead.pk,
                idempotency_key=key,
            )
            for _ in range(3)
        )
    )
    ok, failed = split(results)
    assert failed == []
    assert len({r.activity.pk for r in ok}) == 1
    assert Activity.objects.count() == 1
    assert TimelineEntry.objects.filter(kind="task.created").count() == 1


def test_reopen_racing_reassignment_never_leaves_open_work_behind():
    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    lead = LeadFactory(owner=a)
    done = TaskFactory(lead=lead, status=ActivityStatus.COMPLETED)
    results = run_concurrently(
        lambda: act("reopen", admin, done, scope=ORG(admin.pk)),
        lambda: reassign(admin, lead, b),
    )
    no_deadlocks(results)
    assert split(results)[1] == []
    done.refresh_from_db()
    assert (done.status, done.owner_id) == ("open", b.pk)
    assert_current_work_coherent()
