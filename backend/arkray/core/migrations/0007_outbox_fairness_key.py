# Security review M6: every outbox event carries a fairness key (the user whose request queued
# it, '' for system work), and the relay takes turns between keys (core.outbox).
#
# Adding a column with a constant default only changes the catalog (no table rewrite), so this
# runs under the application's own timeouts: if a long transaction holds the outbox table, the
# ALTER gives up after the lock timeout (5 s) instead of queueing every CRM write behind it.
# The index the relay reads is built concurrently, in core.0008.

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_privacy_remediation"),
    ]

    operations = [
        # db_default: the previous release (which never names the column) can still insert;
        # its events count as system work.
        migrations.AddField(
            model_name="outboxevent",
            name="fairness_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "core.0007_outbox_fairness_key",
            "whose work each queued background job is, which the relay's turns between users read",
        ),
    ]
