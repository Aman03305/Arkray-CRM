"""Regression tests for the Phase 4 adversarial review (docs/testing.md#what-exists-after-phase-4).
Each test reproduces a confirmed finding; the fix is referenced in the docstring."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.activities import services
from arkray.activities.models import Activity, ActivityStatus
from arkray.core.access import AccessScope
from arkray.core.errors import BusinessRuleViolation
from tests.factories import LeadFactory, MeetingFactory, TaskFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

ME = "/api/v1/workspaces/me"
OWN = AccessScope.own


# --- SEC-1 (P2): an upper-case "HTTPS://" meeting link was a 500 (validator vs CHECK) -------
@pytest.mark.parametrize("url", ["HTTPS://meet.example/X", "Https://meet.example/x"])
def test_any_spelling_of_https_is_stored_canonically_not_a_500(user_a, url):
    client = signed_in(user_a)
    lead = LeadFactory(owner=user_a)
    start = timezone.now() + timedelta(days=1)
    created = client.post(
        f"{ME}/activities",
        {
            "type": "meeting",
            "lead": str(lead.pk),
            "title": "Demo",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "meeting_url": url,
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    assert created.json()["meeting_url"] == "https://" + url[8:]
    edited = client.patch(
        f"{ME}/activities/{created.json()['id']}",
        {"version": 1, "meeting_url": url.upper()},
        format="json",
    )
    assert edited.status_code == 200, edited.content
    assert edited.json()["meeting_url"].startswith("https://")


# --- SEC-2 (P3): the previous owner learned whether the new owner archived the lead ---------
@pytest.fixture
def kept_by_previous_owner(admin, user_a, user_b):
    """A completed meeting user_a kept after the lead moved to user_b."""
    lead = LeadFactory(owner=user_b)
    meeting = MeetingFactory(
        lead=lead,
        owner=user_a,
        status=ActivityStatus.COMPLETED,
        starts_at=timezone.now() - timedelta(days=2),
    )
    return lead, meeting


@pytest.mark.parametrize("archived", [False, True])
def test_reopening_kept_work_answers_the_same_whatever_the_other_leads_state(
    user_a, kept_by_previous_owner, archived
):
    lead, meeting = kept_by_previous_owner
    if archived:
        lead.archived_at = timezone.now()
        lead.save()
    with pytest.raises(BusinessRuleViolation, match="belongs to someone else"):
        services.reopen_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=meeting.pk, version=1
        )


@pytest.mark.parametrize("archived", [False, True])
def test_restoring_kept_work_does_not_depend_on_the_other_leads_state(
    user_a, kept_by_previous_owner, archived
):
    lead, meeting = kept_by_previous_owner
    Activity.objects.filter(pk=meeting.pk).update(archived_at=timezone.now())
    if archived:
        lead.archived_at = timezone.now()
        lead.save()
    restored = services.restore_activity(
        actor=user_a, scope=OWN(user_a.pk), activity_id=meeting.pk, version=1
    )
    assert restored.archived_at is None


def test_in_the_leads_own_workspace_an_archived_lead_still_blocks_restoring(user_b):
    lead = LeadFactory(owner=user_b, archived_at=timezone.now())
    task = TaskFactory(lead=lead, archived_at=timezone.now())
    with pytest.raises(BusinessRuleViolation, match="record is archived"):
        services.restore_activity(
            actor=user_b, scope=OWN(user_b.pk), activity_id=task.pk, version=1
        )


# --- D-4 (P3): an opportunity-only create racing a reassignment answered a revealing 422 -----
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_an_opportunity_only_create_racing_a_reassignment_is_created_or_not_found():
    import threading

    from arkray.core import domain_events
    from arkray.core.errors import NotFoundError
    from arkray.leads import services as lead_services
    from arkray.leads.events import LeadReassigned
    from tests.factories import AdminFactory, OpportunityFactory, UserFactory
    from tests.helpers import run_concurrently

    admin, a, b = AdminFactory(), UserFactory(), UserFactory()
    opportunity = OpportunityFactory(lead=LeadFactory(owner=a))
    holding = threading.Event()

    def hold(event):
        holding.set()
        threading.Event().wait(0.5)

    domain_events._SUBSCRIBERS.setdefault(LeadReassigned, []).insert(0, hold)
    try:

        def create():
            holding.wait(10)
            return services.create_activity(
                actor=a,
                scope=OWN(a.pk),
                activity_type="task",
                fields={"title": "x"},
                opportunity_id=opportunity.pk,
            )

        reassigned, created = run_concurrently(
            lambda: lead_services.reassign_lead(
                actor=admin,
                scope=AccessScope.organization(admin.pk),
                lead_id=opportunity.lead_id,
                version=1,
                owner_id=b.pk,
            ),
            create,
        )
    finally:
        domain_events._SUBSCRIBERS[LeadReassigned].remove(hold)
    assert not isinstance(reassigned, BaseException), reassigned
    assert isinstance(created, NotFoundError), created  # never "belongs to someone else"
    assert not Activity.objects.exists()


# --- D-5 (P3, a policy made explicit): an archived lead takes no new, reopened or restored
# work, but its open tasks and meetings can still be progressed (as its open opportunities
# can be moved and closed, Phase 3).
def test_an_archived_leads_open_work_can_still_be_edited_and_closed(user_a):
    lead = LeadFactory(owner=user_a, archived_at=timezone.now())
    task = TaskFactory(lead=lead)
    edited = services.update_activity(
        actor=user_a,
        scope=OWN(user_a.pk),
        activity_id=task.pk,
        version=1,
        changes={"priority": "high"},
    )
    assert edited.priority == "high"
    done = services.complete_activity(
        actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=2
    )
    assert done.status == "completed"
    with pytest.raises(BusinessRuleViolation, match="customer's record is archived"):
        services.reopen_activity(actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=3)
