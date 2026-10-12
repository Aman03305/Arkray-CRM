# Deep security audit L-7: every upload sums the uploader's files still in storage
# (attachments.stored_bytes: uploaded_by = X AND purged_at IS NULL) to check their quota.
# uploaded_by had no index (db_index=False), so that was a sequential scan of the whole
# attachments table per upload. A partial index on uploaded_by over the files not yet purged,
# carrying size, answers it from the index alone (docs/database.md).
#
# Built CONCURRENTLY, so reads and writes go on throughout; that can't run in a transaction,
# so the migration isn't atomic and lifts the timeouts for its session (not SET LOCAL),
# restoring them at the end in either direction (as leads.0005 and pipeline.0010).
#
# Holds no data of its own, but refuses to be reversed like every migration after the
# previous release (RefuseReverse, last operation; docs/deployment.md#rollback).

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("activities", "0010_note_edits_and_attachments"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="attachment",
            index=models.Index(
                models.F("uploaded_by"),
                condition=models.Q(("purged_at__isnull", True)),
                include=("size",),
                name="activities_attach_quota_idx",
            ),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
        RefuseReverse("activities.0011_attachment_quota_index"),
    ]
