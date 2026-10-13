"""Security review M6: the index fair claiming reads (core.outbox._claim): the keys with pending
work (a loose index scan) and each key's oldest due events, in index order, however long one
key's backlog is. The metrics' per-key counts read it too.

Built concurrently (no write lock on a busy outbox), like core.0005; CONCURRENTLY can't run in
a transaction, so the migration isn't atomic and lifts the session's timeouts while it runs
(the build reads the whole table), restoring them at the end in either direction. If the
build fails part-way, PostgreSQL leaves an INVALID index behind: drop it and run the migration
again."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("core", "0007_outbox_fairness_key"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="outboxevent",
            index=models.Index(
                condition=models.Q(("status", "pending")),
                fields=["queue", "fairness_key", "available_at", "id"],
                name="outbox_pending_fair_idx",
            ),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse("core.0008_outbox_fair_claim_index"),
    ]
