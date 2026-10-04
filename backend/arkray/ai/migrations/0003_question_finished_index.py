"""Phase 10 review: the metrics endpoint counts the questions finished in the last hour on
every scrape; without an index on `finished_at` that read every question.

Built concurrently with the session's timeouts lifted, restored at the end in either
direction (docs/database.md#migrations)."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("ai", "0002_conversation_self_subject_required"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="question",
            index=models.Index(fields=["finished_at"], name="ai_question_finished_idx"),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
    ]
