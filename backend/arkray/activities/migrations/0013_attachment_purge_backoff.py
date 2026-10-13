# Review of 73ff1e3, M3 (round 3, LEDGER3-3): a file whose object the store won't delete (an
# outage, or for good: Object Lock on a version, access denied) is tried again later, its wait
# doubling (attachments._purge_failed), so it never holds up the others. The count of failed
# attempts and the time of the next were kept in the cache, which is non-persistent and may
# be down: with it down, every purge stopped at the same oldest files and the count never
# reached the alert. They live on the file's row now.
#
# Adding columns with a constant default (or none) only changes the catalog (no table
# rewrite, and no CHECK to verify against every row: a plain integer), so this runs under
# the application's own timeouts, as core.0007. The paged purge and the housekeeping read
# them through the existing partial index (activities_attachment_open_idx): a file backing
# off is filtered out of the few rows it returns.

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0012_created_by_index"),
    ]

    operations = [
        # db_default: the previous release (which never names the columns) can still insert;
        # its files have failed no attempt and are due now.
        migrations.AddField(
            model_name="attachment",
            name="purge_attempts",
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="attachment",
            name="purge_next_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "activities.0013_attachment_purge_backoff",
            "how often each deleted file's removal from storage failed, which its alert reads",
        ),
    ]
