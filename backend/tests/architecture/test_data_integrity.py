"""Schema-wide invariants that every future model must respect."""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import connection, models

from arkray.core.db import has_append_only_trigger
from arkray.core.models import AppendOnlyModel


def arkray_models() -> list[type[models.Model]]:
    return [m for m in apps.get_models() if m.__module__.startswith("arkray.")]


def test_no_floating_point_columns():
    """Money and probabilities are Decimal. Floats are banned outright."""
    offenders = [
        f"{model.__name__}.{field.name}"
        for model in arkray_models()
        for field in model._meta.get_fields()
        if isinstance(field, models.FloatField)
    ]
    assert not offenders, f"Use DecimalField instead of FloatField: {offenders}"


def test_business_entities_use_uuid_primary_keys():
    """Anything addressable from the API by ID must not be enumerable. Internal append-only
    logs (audit, outbox, history) use bigint keys for index locality."""
    internal = {
        "AuditEvent",
        "OutboxEvent",
        "AuthThrottleEvent",
        "IdempotencyRecord",
        "StageHistory",
        "TimelineEntry",
        "KnowledgeChunk",  # derived index rows, never addressed by id (Phase 8)
        "WorkspaceAccessWindow",  # audit bookkeeping, never addressed by id (Phase 9)
        # append-only price history; the API shows it through an opaque id
        "NegotiationPrice",
    }
    offenders = [
        m.__name__
        for m in arkray_models()
        if m.__name__ not in internal and not isinstance(m._meta.pk, models.UUIDField)
    ]
    assert not offenders, f"Use UUIDPrimaryKeyModel (or list as internal): {offenders}"


@pytest.mark.django_db
def test_every_append_only_model_is_protected_by_a_database_trigger():
    missing = [
        m._meta.db_table
        for m in arkray_models()
        if issubclass(m, AppendOnlyModel)
        and not has_append_only_trigger(connection, m._meta.db_table)
    ]
    assert not missing, f"Add core.db.append_only_trigger() migrations for: {missing}"


@pytest.mark.django_db
def test_pgvector_is_available_for_ask_arkray():
    """Phase 8 depends on the pgvector extension being installable in this PostgreSQL."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        assert cursor.fetchone() == (1,)
