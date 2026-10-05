# Product enhancement phase: audit events record the administrator's support session they were
# taken in (identity.SupportSession). A nullable column: no table rewrite, existing rows NULL.

from django.db import migrations, models


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
    ]
