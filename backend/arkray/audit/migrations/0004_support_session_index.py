# The actions of one support session (a partial index: only rows taken in one). Built
# CONCURRENTLY: every write in the system appends audit rows, and a plain CREATE INDEX would
# block them for the whole build. Not atomic, so it lifts the timeouts for its session and
# restores them at the end, in either direction (tests/architecture/test_migrations.py).

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("audit", "0003_support_sessions"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="auditevent",
            index=models.Index(
                condition=models.Q(("support_session_id__isnull", False)),
                fields=["support_session_id", "occurred_at"],
                name="audit_support_idx",
            ),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
    ]
