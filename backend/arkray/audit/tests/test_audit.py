import uuid

import pytest
from django.db import DatabaseError, connection, transaction

from arkray.audit import services as audit
from arkray.audit.models import ActorType, AuditEvent
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.errors import AppendOnlyViolation

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    return audit.record("user.created", actor_id=uuid.uuid4(), target_type="user", target_id="t-1")


class TestRecord:
    def test_captures_request_context(self):
        token = bind_context(
            ExecutionContext(correlation_id="req-audit-123", client_ip="203.0.113.7")
        )
        try:
            event = audit.record("user.deactivated", actor_id=uuid.uuid4(), target_type="user")
        finally:
            reset_context(token)
        assert event.request_id == "req-audit-123"
        assert event.ip_address == "203.0.113.7"
        assert event.actor_type == ActorType.USER

    def test_system_actor(self):
        assert audit.record("outbox.dead_lettered", actor_id=None).actor_type == ActorType.SYSTEM

    def test_secrets_in_metadata_are_redacted_recursively(self):
        event = audit.record(
            "user.invited",
            actor_id=uuid.uuid4(),
            metadata={"role": "admin", "password": "p", "nested": {"api_key": "k", "ok": 1}},
        )
        assert event.metadata == {
            "role": "admin",
            "password": audit.REDACTED,
            "nested": {"api_key": audit.REDACTED, "ok": 1},
        }

    def test_oversized_metadata_is_truncated(self):
        event = audit.record("x", actor_id=uuid.uuid4(), metadata={"blob": "y" * 10_000})
        assert event.metadata == {"_truncated": True, "keys": ["blob"]}


class TestAppendOnly:
    def test_orm_save_of_existing_row_is_blocked(self, event):
        event.action = "tampered"
        with pytest.raises(AppendOnlyViolation):
            event.save()

    def test_orm_delete_is_blocked(self, event):
        with pytest.raises(AppendOnlyViolation):
            event.delete()

    def test_bulk_update_and_delete_are_blocked(self, event):
        with pytest.raises(AppendOnlyViolation):
            AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
        with pytest.raises(AppendOnlyViolation):
            AuditEvent.objects.filter(pk=event.pk).delete()

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE audit_event SET action = 'tampered' WHERE id = %s",
            "DELETE FROM audit_event WHERE id = %s",
        ],
    )
    def test_database_trigger_blocks_raw_sql(self, event, sql):
        with (
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, [event.pk])
        event.refresh_from_db()
        assert event.action == "user.created"
