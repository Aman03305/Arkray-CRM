"""Phase 10: dead outbox events per queue, counted by the metrics endpoint on every scrape.

Built concurrently (no write lock on a busy outbox), like the other indexes added to live
tables (leads 0005, pipeline 0005, activities 0008); CONCURRENTLY can't run in a
transaction, so the migration isn't atomic and lifts the session's timeouts while it runs
(the build reads the whole table), restoring them at the end in either direction."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("core", "0004_idempotency_records"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="outboxevent",
            index=models.Index(
                condition=models.Q(("status", "dead")), fields=["queue"], name="outbox_dead_idx"
            ),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
    ]
