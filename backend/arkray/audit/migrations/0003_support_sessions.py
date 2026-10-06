# Product enhancement phase: audit events record the administrator's support session they were
# taken in (identity.SupportSession). A nullable column: no table rewrite, existing rows NULL.
# Not reversible: dropping the column would erase that part of the audit trail, so it refuses
# (RefuseReverse, last operation; docs/deployment.md#rollback).

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_audit_event_append_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditevent",
            name="support_session_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        RefuseReverse(
            "audit.0003_support_sessions",
            "the support session recorded on every audit event taken in one",
        ),
    ]
