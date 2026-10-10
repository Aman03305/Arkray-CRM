"""The audit trail's personal details expire; the trail itself stays (privacy remediation
P2-4; docs/privacy.md#audit-trail).

Client addresses (security events only), old and new sign-in emails, reset requesters'
addresses and support reasons live in AuditDetail, sealed by the event's digest, and are
deleted after AUDIT_DETAIL_RETENTION_DAYS by an idempotent job that respects legal holds.
Events written before this existed are moved there by the owner's one-off command.
"""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.utils import timezone

from arkray.audit import retention
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail, AuditEvent
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity import services as identity_services
from arkray.identity.models import SupportSession
from arkray.identity.tasks import housekeeping as identity_housekeeping
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

IP = "203.0.113.77"


@pytest.fixture
def request_context():
    token = bind_context(ExecutionContext(correlation_id="req-1", client_ip=IP))
    yield
    reset_context(token)


def _age(event: AuditEvent, days: int) -> None:
    AuditDetail.objects.filter(pk=event.pk).update(expires_at=timezone.now() - timedelta(days=days))


def test_security_events_keep_the_address_in_the_expiring_detail(request_context, user_a):
    event = audit.record("auth.login", actor_id=user_a.pk, target_type="user", target_id=user_a.pk)
    event.refresh_from_db()
    assert event.ip_address is None  # never in the append-only row
    detail = AuditDetail.objects.get(pk=event.pk)
    assert detail.ip_address == IP
    assert retention.verify(event) is True
    expected = timezone.now() + timedelta(days=90)
    assert abs((detail.expires_at - expected).total_seconds()) < 60


def test_crm_edits_keep_no_address_at_all(request_context, user_a):
    event = audit.record("opportunity.updated", actor_id=user_a.pk, metadata={"fields": ["value"]})
    assert not AuditDetail.objects.filter(pk=event.pk).exists()
    event.refresh_from_db()
    assert event.ip_address is None
    assert event.detail_digest == ""
    assert event.request_id == "req-1"  # still tied to the access log while that is kept


def test_email_change_keeps_the_addresses_out_of_the_row(admin, user_a):
    identity_services.change_user_email(
        actor_id=admin.pk,
        user_id=user_a.pk,
        version=user_a.version,
        email="new.address@example.com",
    )
    event = AuditEvent.objects.get(action="user.email_changed")
    assert "from" not in event.metadata
    assert "to" not in event.metadata
    assert "new.address@example.com" not in json.dumps(event.metadata)
    assert AuditDetail.objects.get(pk=event.pk).values["to"] == "new.address@example.com"


def test_reset_requester_address_expires_with_the_detail(user_a):
    identity_services.issue_password_reset(user_a.email, requested_from=IP)
    event = AuditEvent.objects.get(action="auth.password_reset_issued")
    assert "requested_from" not in event.metadata
    assert AuditDetail.objects.get(pk=event.pk).values == {"requested_from": IP}


def test_expiry_deletes_details_keeps_events_and_is_idempotent(request_context, user_a, user_b):
    old = audit.record("auth.login", actor_id=user_a.pk, target_type="user", target_id=user_a.pk)
    fresh = audit.record("auth.login", actor_id=user_b.pk, target_type="user", target_id=user_b.pk)
    _age(old, 1)
    first = retention.expire_details()
    assert first["details_expired"] == 1
    assert not AuditDetail.objects.filter(pk=old.pk).exists()
    assert AuditDetail.objects.filter(pk=fresh.pk).exists()
    assert AuditEvent.objects.filter(pk=old.pk).exists()  # who did what, when: kept
    assert retention.verify(AuditEvent.objects.get(pk=old.pk)) is None
    run = AuditEvent.objects.get(action="audit.details_expired")
    assert run.metadata["count"] == 1
    assert retention.expire_details() == {"details_expired": 0, "held": 0}
    assert AuditEvent.objects.filter(action="audit.details_expired").count() == 1


@pytest.mark.parametrize("held", ["actor", "subject", "target_user", "target_lead"])
def test_a_legal_hold_suspends_expiry(request_context, admin, user_a, held):
    lead_id = uuid4()
    if held == "target_lead":
        event = audit.record(
            "lead.erased", actor_id=admin.pk, target_type="lead", target_id=lead_id
        )
        subject = (HoldSubject.LEAD, lead_id)
    else:
        event = audit.record(
            "user.deactivated",
            actor_id=admin.pk,
            target_type="user",
            target_id=user_a.pk,
            subject_user_id=user_a.pk,
        )
        subject = (HoldSubject.USER, admin.pk if held == "actor" else user_a.pk)
    _age(event, 5)
    hold = LegalHold.objects.create(
        subject_type=subject[0], subject_id=subject[1], reference="CASE-1", placed_by=admin.pk
    )
    assert retention.expire_details() == {"details_expired": 0, "held": 1}
    assert AuditDetail.objects.filter(pk=event.pk).exists()
    hold.released_at, hold.released_by = timezone.now(), admin.pk
    hold.save()
    assert retention.expire_details()["details_expired"] == 1


def test_an_altered_detail_no_longer_matches_its_seal(request_context, user_a):
    event = audit.record("auth.login", actor_id=user_a.pk, target_type="user", target_id=user_a.pk)
    AuditDetail.objects.filter(pk=event.pk).update(ip_address="198.51.100.1")
    assert retention.verify(event) is False


def test_the_application_cannot_rewrite_the_event_row(request_context, user_a):
    event = audit.record("auth.login", actor_id=user_a.pk, target_type="user", target_id=user_a.pk)
    with pytest.raises(DatabaseError, match="append-only"), connection.cursor() as cursor:
        cursor.execute("UPDATE audit_event SET detail_digest = '' WHERE id = %s", [event.pk])


def test_support_reason_shown_while_kept_then_gone(admin, user_a):
    client = signed_in(admin)
    started = client.post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "Ticket 4711"},
        format="json",
    )
    assert started.status_code in (200, 201)
    client.post("/api/v1/admin/support-sessions/current/exit")
    client.delete("/api/v1/admin/support-sessions/current")
    event = AuditEvent.objects.get(action="support_session.started")
    assert "reason" not in event.metadata

    def shown() -> list[dict[str, Any]]:
        page = signed_in(admin).get("/api/v1/admin/security-events").json()["results"]
        return [e["details"] for e in page if e["action"] == "support_session.started"]

    assert shown() == [{"reason": "Ticket 4711"}]
    _age(event, 1)
    retention.expire_details()
    assert shown() == [{}]
    session = SupportSession.objects.get()
    SupportSession.objects.filter(pk=session.pk).update(
        started_at=timezone.now() - timedelta(days=91),
        expires_at=timezone.now() - timedelta(days=90),
        ended_at=timezone.now() - timedelta(days=90),
        end_reason="exited",
    )
    assert identity_housekeeping()["redacted_reasons"] == 1
    assert SupportSession.objects.get().reason == ""
    assert identity_housekeeping().get("redacted_reasons", 0) == 0  # idempotent


def test_support_reason_on_the_session_is_kept_under_a_legal_hold(admin, user_a):
    signed_in(admin).post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "Case 9"},
        format="json",
    )
    SupportSession.objects.update(
        started_at=timezone.now() - timedelta(days=91),
        expires_at=timezone.now() - timedelta(days=90),
        ended_at=timezone.now() - timedelta(days=90),
        end_reason="exited",
    )
    LegalHold.objects.create(
        subject_type=HoldSubject.USER, subject_id=user_a.pk, reference="CASE-9", placed_by=admin.pk
    )
    identity_housekeeping()
    assert SupportSession.objects.get().reason == "Case 9"


def _legacy_event(**fields) -> AuditEvent:
    """An event as the previous release wrote it (address and values in the row)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO audit_event (occurred_at, actor_type, action, target_type, target_id,"
            " request_id, ip_address, metadata) VALUES (%s, 'system', %s, 'user', %s, '', %s,"
            " %s::jsonb) RETURNING id",
            [
                fields.get("occurred_at", timezone.now() - timedelta(days=10)),
                fields["action"],
                str(uuid4()),
                fields.get("ip"),
                json.dumps(fields.get("metadata", {})),
            ],
        )
        return AuditEvent.objects.get(pk=cursor.fetchone()[0])


def test_legacy_details_move_once_into_expiring_details(admin):
    changed = _legacy_event(
        action="user.email_changed",
        ip=IP,
        metadata={"from": "old@example.com", "to": "new@example.com", "revoked_links": 1},
    )
    edited = _legacy_event(action="opportunity.updated", ip=IP, metadata={"fields": ["value"]})
    ancient = _legacy_event(
        action="auth.login", ip=IP, occurred_at=timezone.now() - timedelta(days=400)
    )
    out = StringIO()
    call_command("audit_minimise_legacy", by=admin.email, stdout=out)  # dry run
    assert "3 legacy events" in out.getvalue()
    assert AuditEvent.objects.get(pk=changed.pk).ip_address == IP  # nothing changed yet

    call_command("audit_minimise_legacy", by=admin.email, yes=True, stdout=StringIO())
    changed.refresh_from_db()
    assert changed.ip_address is None
    assert changed.metadata == {"revoked_links": 1}
    assert AuditDetail.objects.get(pk=changed.pk).values == {
        "from": "old@example.com",
        "to": "new@example.com",
    }
    assert retention.verify(changed) is True
    edited.refresh_from_db()
    assert edited.ip_address is None
    assert AuditDetail.objects.get(pk=edited.pk).ip_address is None  # not a security event
    record = AuditEvent.objects.get(action="audit.legacy_minimised")
    assert record.metadata["events"] == 3
    assert len(record.metadata["before_digest"]) == 64
    assert "old@example.com" not in json.dumps(record.metadata)
    # The 400-day-old login's detail is already past its retention: the next run deletes it.
    retention.expire_details()
    assert not AuditDetail.objects.filter(pk=ancient.pk).exists()
    # Idempotent.
    out = StringIO()
    call_command("audit_minimise_legacy", by=admin.email, yes=True, stdout=out)
    assert "Moved 0 addresses and 0 values from 0 events" in out.getvalue()
