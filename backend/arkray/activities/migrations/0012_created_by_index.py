# Review of 73ff1e3, M6 (round 1, QUEUES-3): every creation of a task, meeting or note counts
# its creator's creations in the last 24 hours against ACTIVITY_MAX_CREATED_PER_USER_PER_DAY
# (activities.services). Counted from the audit trail, that read every other event of theirs
# in the day too (400,000 audited edits: 66-85 ms per creation). created_by had no index
# (db_index=False); one on (created_by, created_at) answers it with at most the budget's worth
# of index entries, whatever else the creator wrote. Activities are never deleted, so the
# count is the same as the trail's.
#
# Built CONCURRENTLY, so reads and writes go on throughout; that can't run in a transaction,
# so the migration isn't atomic and lifts the timeouts for its session (not SET LOCAL),
# restoring them at the end in either direction (as activities.0011). If the build fails
# part-way, PostgreSQL leaves an INVALID index behind: drop it and run the migration again.
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
        ("activities", "0011_attachment_quota_index"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="activity",
            index=models.Index(
                models.F("created_by"), models.F("created_at"), name="activities_created_by_idx"
            ),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
        RefuseReverse("activities.0012_created_by_index"),
    ]
