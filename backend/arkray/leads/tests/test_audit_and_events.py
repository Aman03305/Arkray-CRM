"""Every lead operation leaves exactly one audit event with safe metadata, publishes exactly
one domain event inside its transaction, queues no background work, and puts no contact
data into logs or audit records."""

from __future__ import annotations

import json
import logging

import pytest
from django.db import transaction

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.models import OutboxEvent
from arkray.leads import events, services
from arkray.leads.models import Lead
from tests.factories import LeadFactory, UserFactory

from .conftest import lead_url, leads_url

pytestmark = pytest.mark.django_db

CONTACT = {
    "first_name": "Priya",
    "last_name": "Patel",
    "organization_name": "Apollo Hospitals",
    "email": "priya.patel@apollo.example",
    "phone": "+91 98765 43210",
    "mobile": "+91 91234 56789",
    "address_line_1": "7 Hospital Road",
    "city": "Navi Mumbai",
    "description": "Confidential budget notes",
}
PII_FRAGMENTS = [
    "Priya",
    "Patel",
    "Apollo",
    "apollo.example",
    "98765",
    "91234",
    "Hospital Road",
    "Confidential",
]


def assert_no_pii(text: str) -> None:
    for fragment in PII_FRAGMENTS:
        assert fragment.lower() not in text.lower(), fragment


@pytest.fixture
def lifecycle(user_a_client, admin_client, user_a, user_b):
    """Create, edit, change status, reassign, archive and restore one lead through the API."""
    lead_id = user_a_client.post(leads_url(), CONTACT, format="json").json()["id"]
    user_a_client.patch(
        lead_url(lead_id), {"version": 1, "city": "Pune", "email": "p@x.example"}, format="json"
    )
    user_a_client.post(
        lead_url(lead_id, action="status"), {"version": 2, "status": "qualified"}, format="json"
    )
    admin_client.post(
        lead_url(lead_id, "all", "assign"), {"version": 3, "owner": str(user_b.pk)}, format="json"
    )
    admin_client.post(lead_url(lead_id, "all", "archive"), {"version": 4}, format="json")
    admin_client.post(lead_url(lead_id, "all", "restore"), {"version": 5}, format="json")
    return lead_id


def test_each_operation_is_audited_once_with_safe_metadata(lifecycle, admin, user_a, user_b):
    events_ = list(
        AuditEvent.objects.filter(target_type="lead", target_id=lifecycle).order_by("id")
    )
    assert [(e.action, e.actor_id, e.subject_user_id, e.metadata) for e in events_] == [
        (
            "lead.created",
            user_a.pk,
            None,
            {"workspace": "self", "owner_id": str(user_a.pk), "status": "new"},
        ),
        ("lead.updated", user_a.pk, None, {"workspace": "self", "fields": ["city", "email"]}),
        (
            "lead.status_changed",
            user_a.pk,
            None,
            {"workspace": "self", "from": "new", "to": "qualified"},
        ),
        (
            "lead.reassigned",
            admin.pk,
            user_a.pk,
            {
                "workspace": "organization",
                "from_owner_id": str(user_a.pk),
                "to_owner_id": str(user_b.pk),
            },
        ),
        ("lead.archived", admin.pk, user_b.pk, {"workspace": "organization"}),
        ("lead.restored", admin.pk, user_b.pk, {"workspace": "organization"}),
    ]
    assert all(e.request_id for e in events_)


def test_audit_records_never_hold_contact_data(lifecycle):
    for event in AuditEvent.objects.all():
        assert_no_pii(json.dumps(event.metadata) + event.target_id)


def test_no_changes_means_no_audit_event(user_a_client, user_a):
    lead = LeadFactory(owner=user_a, city="Pune")
    user_a_client.patch(lead_url(lead.pk), {"version": 1, "city": "Pune"}, format="json")
    user_a_client.post(
        lead_url(lead.pk, action="status"), {"version": 1, "status": "new"}, format="json"
    )
    user_a_client.post(lead_url(lead.pk, action="restore"), {"version": 1}, format="json")
    assert not AuditEvent.objects.filter(target_type="lead").exists()


def test_refused_changes_leave_no_audit_event(user_a_client, user_a):
    lead = LeadFactory(owner=user_a, version=2)
    user_a_client.patch(lead_url(lead.pk), {"version": 1, "city": "Pune"}, format="json")
    user_a_client.patch(lead_url(lead.pk), {"version": 2, "email": "bad"}, format="json")
    assert not AuditEvent.objects.filter(target_type="lead").exists()


def test_lead_operations_queue_no_background_work(lifecycle):
    """Phase 2 has no asynchronous consumers of lead changes: nothing meaningless is queued."""
    assert not OutboxEvent.objects.exists()


def test_logs_carry_no_contact_data(user_a_client, user_a, caplog):
    caplog.set_level(logging.DEBUG)
    lead_id = user_a_client.post(leads_url(), CONTACT, format="json").json()["id"]
    user_a_client.get(leads_url(), {"q": "Priya Patel"})
    user_a_client.get(leads_url(suffix="/duplicates"), {"email": CONTACT["email"]})
    user_a_client.patch(lead_url(lead_id), {"version": 1, "email": "bad"}, format="json")
    text = "\n".join(
        f"{r.getMessage()} {json.dumps({k: str(v) for k, v in vars(r).items()})}"
        for r in caplog.records
        if r.name != "django.db.backends"  # SQL echo only exists at DEBUG in tests
    )
    assert "http_request" in text  # the access log did run
    assert_no_pii(text)


class TestDomainEvents:
    def capture(self, event_type):
        seen = []
        return seen, subscribed(event_type, seen.append)

    def test_every_operation_publishes_its_event(self, admin, user_a, user_b):
        scope = AccessScope.organization(admin.pk)
        received = []
        types = [
            events.LeadCreated,
            events.LeadUpdated,
            events.LeadStatusChanged,
            events.LeadReassigned,
            events.LeadArchived,
            events.LeadRestored,
        ]
        managers = [subscribed(t, received.append) for t in types]
        for manager in managers:
            manager.__enter__()
        try:
            lead = services.create_lead(
                actor=admin, scope=scope, fields={"first_name": "R"}, owner_id=user_a.pk
            ).lead
            services.update_lead(
                actor=admin, scope=scope, lead_id=lead.pk, version=1, changes={"city": "Pune"}
            )
            services.change_status(
                actor=admin, scope=scope, lead_id=lead.pk, version=2, status="contacted"
            )
            services.reassign_lead(
                actor=admin, scope=scope, lead_id=lead.pk, version=3, owner_id=user_b.pk
            )
            services.archive_lead(actor=admin, scope=scope, lead_id=lead.pk, version=4)
            services.restore_lead(actor=admin, scope=scope, lead_id=lead.pk, version=5)
        finally:
            for manager in reversed(managers):
                manager.__exit__(None, None, None)
        assert [type(e) for e in received] == types
        created, updated, status, reassigned, _, _ = received
        assert (created.owner_id, created.actor_id, created.status) == (user_a.pk, admin.pk, "new")
        assert updated.fields == ("city",)
        assert (status.from_status, status.to_status, status.to_category) == (
            "new",
            "contacted",
            "open",
        )
        assert (reassigned.from_owner_id, reassigned.owner_id) == (user_a.pk, user_b.pk)

    def test_a_failing_subscriber_undoes_the_reassignment(self, admin, user_a, user_b):
        """What Phase 3 relies on: moving a lead's opportunities is atomic with the lead."""
        lead = LeadFactory(owner=user_a)

        def refuse(_event):
            raise RuntimeError("could not move the open opportunities")

        with subscribed(events.LeadReassigned, refuse), pytest.raises(RuntimeError):
            services.reassign_lead(
                actor=admin,
                scope=AccessScope.organization(admin.pk),
                lead_id=lead.pk,
                version=1,
                owner_id=user_b.pk,
            )
        lead.refresh_from_db()
        assert (lead.owner_id, lead.version) == (user_a.pk, 1)
        assert not AuditEvent.objects.filter(action="lead.reassigned").exists()

    def test_subscribers_see_the_change_inside_the_same_transaction(self, admin, user_a, user_b):
        lead = LeadFactory(owner=user_a)
        observed = []

        def check(event):
            assert transaction.get_connection().in_atomic_block
            observed.append(Lead.objects.get(pk=event.lead_id).owner_id)

        with subscribed(events.LeadReassigned, check):
            services.reassign_lead(
                actor=admin,
                scope=AccessScope.organization(admin.pk),
                lead_id=lead.pk,
                version=1,
                owner_id=user_b.pk,
            )
        assert observed == [user_b.pk]


def test_services_enforce_authorization_for_every_caller(user_a, user_b):
    """Not just the HTTP layer: a direct service call is scoped and authorised the same way."""
    from arkray.core.errors import NotFoundError, PermissionDeniedError

    lead = LeadFactory(owner=user_b)
    own = AccessScope.own(user_a.pk)
    with pytest.raises(NotFoundError):
        services.update_lead(
            actor=user_a, scope=own, lead_id=lead.pk, version=1, changes={"city": "X"}
        )
    with pytest.raises(PermissionDeniedError):
        services.reassign_lead(
            actor=user_a, scope=own, lead_id=lead.pk, version=1, owner_id=user_a.pk
        )
    with pytest.raises(PermissionDeniedError):  # a scope that isn't the actor's own
        services.update_lead(
            actor=user_a, scope=AccessScope.own(user_b.pk), lead_id=lead.pk, version=1, changes={}
        )
    with pytest.raises(PermissionDeniedError):  # a sales user can't act organisation-wide
        services.archive_lead(
            actor=user_a, scope=AccessScope.organization(user_a.pk), lead_id=lead.pk, version=1
        )
    other = UserFactory()
    with pytest.raises(PermissionDeniedError):
        services.create_lead(
            actor=user_a,
            scope=AccessScope.for_user(user_a.pk, other.pk),
            fields={"first_name": "R"},
        )
