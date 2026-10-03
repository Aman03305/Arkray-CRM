"""Lead reassignment moves current work, never history (docs/activities.md#lead-reassignment).

Lead A -> B in one transaction: open opportunities (pipeline), open tasks, scheduled
meetings and notes follow the lead to B; completed and cancelled tasks and meetings stay
with whoever had them; who created, completed or cancelled anything, and who did what on
the timeline, never changes. All or nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities import events
from arkray.activities.models import Activity, ActivityStatus, TimelineEntry
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.leads import services as lead_services
from arkray.leads.events import LeadReassigned
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
)
from tests.helpers import collected

pytestmark = pytest.mark.django_db

ORG = AccessScope.organization


def reassign(admin, lead, to):
    lead.refresh_from_db()
    return lead_services.reassign_lead(
        actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=lead.version, owner_id=to.pk
    )


@pytest.fixture
def world(admin, user_a, user_b):
    lead = LeadFactory(owner=user_a)
    open_opportunity = OpportunityFactory(lead=lead)
    won = OpportunityFactory(lead=lead, stage_key="won")
    past = timezone.now() - timedelta(days=2)
    rows = {
        "open_task": TaskFactory(lead=lead),
        "task_on_open_opportunity": TaskFactory(opportunity=open_opportunity),
        "task_on_won_opportunity": TaskFactory(opportunity=won),
        "scheduled_meeting": MeetingFactory(lead=lead),
        "note": NoteFactory(lead=lead, created_by=admin),
        "archived_open_task": TaskFactory(lead=lead, archived_at=timezone.now()),
        "completed_task": TaskFactory(lead=lead, status=ActivityStatus.COMPLETED),
        "cancelled_task": TaskFactory(lead=lead, status=ActivityStatus.CANCELLED),
        "completed_meeting": MeetingFactory(
            lead=lead, status=ActivityStatus.COMPLETED, starts_at=past
        ),
        "cancelled_meeting": MeetingFactory(lead=lead, status=ActivityStatus.CANCELLED),
    }
    return lead, open_opportunity, won, rows


FOLLOW = {
    "open_task",
    "task_on_open_opportunity",
    "task_on_won_opportunity",
    "scheduled_meeting",
    "note",
    "archived_open_task",
}
STAY = {"completed_task", "cancelled_task", "completed_meeting", "cancelled_meeting"}


def test_current_work_follows_the_lead_and_history_stays(admin, user_a, user_b, world):
    lead, open_opportunity, won, rows = world
    before = {
        name: (a.created_by_id, a.completed_by_id, a.cancelled_by_id) for name, a in rows.items()
    }
    reassign(admin, lead, user_b)

    owners = {name: Activity.objects.get(pk=a.pk).owner_id for name, a in rows.items()}
    assert {name for name, owner in owners.items() if owner == user_b.pk} == FOLLOW
    assert {name for name, owner in owners.items() if owner == user_a.pk} == STAY
    # Coherent with the pipeline: the open opportunity moved, the won one stayed.
    assert Opportunity.objects.get(pk=open_opportunity.pk).owner_id == user_b.pk
    assert Opportunity.objects.get(pk=won.pk).owner_id == user_a.pk
    # No "Lead owner B, open opportunity owner B, open task owner A".
    assert Activity.objects.get(pk=rows["task_on_open_opportunity"].pk).owner_id == user_b.pk
    # Historical attribution never changes.
    after = {
        name: (a.created_by_id, a.completed_by_id, a.cancelled_by_id)
        for name, a in ((n, Activity.objects.get(pk=r.pk)) for n, r in rows.items())
    }
    assert after == before
    assert Activity.objects.get(pk=rows["note"].pk).created_by_id == admin.pk


def test_moved_work_gets_new_versions_audit_and_events(admin, user_a, user_b, world):
    lead, _, _, rows = world
    with collected(events.ActivityOwnerChanged) as changed:
        reassign(admin, lead, user_b)
    assert {e.activity_id for e in changed} == {rows[n].pk for n in FOLLOW}
    assert all(e.reason == "lead_reassigned" and e.from_owner_id == user_a.pk for e in changed)
    for name in FOLLOW:
        assert Activity.objects.get(pk=rows[name].pk).version == 2
    for name in STAY:
        assert Activity.objects.get(pk=rows[name].pk).version == 1
    audits = AuditEvent.objects.filter(action__endswith=".owner_changed", target_type="activity")
    assert audits.count() == len(FOLLOW)
    for audit in audits:
        assert audit.actor_id == admin.pk
        assert audit.subject_user_id == user_a.pk
        assert audit.metadata["reason"] == "lead_reassigned"
        assert audit.metadata["to_owner_id"] == str(user_b.pk)


def test_the_reassignment_is_on_the_timeline_once(admin, user_a, user_b, world):
    lead, _, _, _ = world
    reassign(admin, lead, user_b)
    entries = TimelineEntry.objects.filter(lead=lead, kind="lead.reassigned")
    assert entries.count() == 1
    entry = entries.get()
    assert entry.actor_id == admin.pk
    assert entry.data == {"from_owner_id": str(user_a.pk), "to_owner_id": str(user_b.pk)}


def test_all_or_nothing_when_anything_after_the_move_fails(admin, user_a, user_b, world):
    lead, open_opportunity, _, _ = world

    def boom(event):
        raise RuntimeError("a later subscriber fails")

    with subscribed(LeadReassigned, boom), pytest.raises(RuntimeError):
        reassign(admin, lead, user_b)
    assert Lead.objects.get(pk=lead.pk).owner_id == user_a.pk
    assert Opportunity.objects.get(pk=open_opportunity.pk).owner_id == user_a.pk
    assert not Activity.objects.filter(owner=user_b).exists()
    assert not TimelineEntry.objects.filter(kind="lead.reassigned").exists()
    assert not AuditEvent.objects.filter(action__endswith=".owner_changed").exists()


def test_reassigning_to_the_current_owner_moves_nothing(admin, user_a, world):
    lead, _, _, rows = world
    reassign(admin, lead, user_a)
    assert all(Activity.objects.get(pk=a.pk).version == 1 for a in rows.values())
    assert not TimelineEntry.objects.filter(kind="lead.reassigned").exists()


def test_activity_subscribers_run_after_the_pipelines(world):
    """The lock order inside a reassignment (lead -> opportunities -> activities) relies on
    the registration order of the subscribers."""
    from arkray.core.domain_events import _SUBSCRIBERS

    modules = [s.__module__ for s in _SUBSCRIBERS[LeadReassigned]]
    assert modules.index("arkray.pipeline.subscribers") < modules.index(
        "arkray.activities.subscribers"
    )


@pytest.mark.parametrize("n", [1, 25])
def test_query_count_is_constant_however_much_work_moves(admin, user_a, user_b, n):
    lead = LeadFactory(owner=user_a)
    for _ in range(n):
        TaskFactory(lead=lead)
        NoteFactory(lead=lead)
    with CaptureQueriesContext(connection) as queries:
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
    assert Activity.objects.filter(lead=lead, owner=user_b).count() == 2 * n
    # savepoint, lead lock, user share lock, lead update, lead audit, opportunities lock,
    # activities lock, activities update, one audit insert for all of them, timeline entry,
    # release, reload
    assert len(queries) == 12
