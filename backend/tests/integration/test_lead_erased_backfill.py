"""leads.0007 marks the leads erased before it (deep security audit M-1): from the audit trail's
`lead.erased` events (a lead restored and refilled afterwards included), else from the
placeholder an erasure leaves; nothing else is marked, and no lead's version or last change
moves (a backfill is not an edit). Its reverse is refused (docs/deployment.md#rollback)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError

from tests.integration.migration_states import build, latest, migrate, restore_latest, state_apps

pytestmark = pytest.mark.django_db(transaction=True)

AFTER = [("leads", "0007_lead_erased_at")]
ERASED = "[erased]"
SYSTEM = {"actor_type": "system"}
T_ARCHIVED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
T_AUDIT = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
T_LATER = datetime(2026, 3, 5, 11, 0, tzinfo=UTC)
T_UPDATED = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)


def rows(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def leads_before_0007():
    """Every app at its latest migration, leads just before 0007."""
    return [node for node in latest() if node[0] != "leads"] + [("leads", "0006_leads_autovacuum")]


def _seed(state):
    apps = state_apps(state)
    User = apps.get_model("identity", "User")
    Lead = apps.get_model("leads", "Lead")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    owner = User.objects.create(
        email="owner@x.test", first_name="Owner", status="invited", password="!"
    )

    def lead(first_name, archived_at=None, **fields):
        return Lead.objects.create(
            first_name=first_name,
            owner_id=owner.pk,
            created_by_id=owner.pk,
            status_id="new",
            archived_at=archived_at,
            **fields,
        ).pk

    ids = {
        # Erased by the command: the placeholder, archived, and its audit event.
        "erased": lead(ERASED, T_ARCHIVED),
        # Erased, then restored and refilled through the hole M-1 closes: only the audit
        # trail still knows; dated by its first erasure, as erase() keeps it (review P3-4).
        # trail still knows (the latest of its two erasures).
        "refilled": lead("Rahul", None, email="rahul@clinic.example"),
        # The placeholder alone (its audit event since purged): dated by its archive.
        "placeholder": lead(ERASED, T_ARCHIVED),
        # The placeholder, never archived: dated by its last change.
        "unarchived": lead(ERASED),
        # Not erased: a placeholder-like name with data, and an ordinary lead.
        "named": lead(ERASED, None, email="someone@x.test"),
        "ordinary": lead("Priya"),
    }
    for key, at in (("erased", T_AUDIT), ("refilled", T_AUDIT), ("refilled", T_LATER)):
        AuditEvent.objects.create(
            action="lead.erased",
            target_type="lead",
            target_id=str(ids[key]),
            occurred_at=at,
            **SYSTEM,
        )
    # Another target type with a lead's id, and a non-UUID target: neither marks anything.
    AuditEvent.objects.create(
        action="lead.erased", target_type="opportunity", target_id=str(ids["ordinary"]), **SYSTEM
    )
    AuditEvent.objects.create(
        action="lead.erased", target_type="lead", target_id="not-a-uuid", **SYSTEM
    )
    with connection.cursor() as cursor:
        cursor.execute("UPDATE leads_lead SET updated_at = %s", [T_UPDATED])
    return ids


def test_the_backfill_marks_exactly_the_erased_leads():
    state = leads_before_0007()
    build(state)
    try:
        ids = _seed(state)
        before = rows("SELECT id, version, updated_at FROM leads_lead ORDER BY id")
        migrate(AFTER)
        marked = dict(rows("SELECT id, erased_at FROM leads_lead"))
        assert marked == {
            ids["erased"]: T_AUDIT,
            ids["refilled"]: T_AUDIT,
            ids["placeholder"]: T_ARCHIVED,
            ids["unarchived"]: T_UPDATED,
            ids["named"]: None,
            ids["ordinary"]: None,
        }
        # Not an edit: versions and last changes as they were.
        assert rows("SELECT id, version, updated_at FROM leads_lead ORDER BY id") == before
        with pytest.raises(IrreversibleError, match=r"leads.0007_lead_erased_at"):
            migrate(state)
        assert rows("SELECT count(*) FROM leads_lead WHERE erased_at IS NOT NULL") == [(4,)]
    finally:
        restore_latest()
