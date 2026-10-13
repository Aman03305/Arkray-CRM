# ADR-0035: a team administrator asks Ask Arkray in their team's workspace ("all" for them):
# a conversation's workspace kind may be "team", with no subject (several owners), like the
# organisation's. The constraint only gains a branch, so every existing row satisfies it; it
# is still checked against the whole table, under the lock ALTER TABLE takes, so the
# statement timeout is lifted for this transaction first (tests/architecture/
# test_migrations.py). Conversations are deleted after 30 days: the scan is short.
#
# The previous release, during a rolling deploy, never makes a team conversation (it has no
# team scope) and lists conversations by its own kinds only, so it never shows one.
#
# Not reversible: putting the old constraint back fails on any team conversation, and
# deleting them first would delete what people asked, so it refuses (RefuseReverse, last
# operation; docs/deployment.md#rollback).

from django.conf import settings
from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0004_conversation_forgotten_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL statement_timeout = 0;", migrations.RunSQL.noop),
        migrations.RemoveConstraint(
            model_name="conversation",
            name="ai_conversation_workspace_valid",
        ),
        migrations.AlterField(
            model_name="conversation",
            name="workspace_kind",
            field=models.CharField(
                choices=[
                    ("self", "Self"),
                    ("user", "User"),
                    ("team", "Team"),
                    ("organization", "Organization"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("subject__isnull", True), ("workspace_kind", "organization")),
                    models.Q(("subject__isnull", True), ("workspace_kind", "team")),
                    models.Q(
                        ("subject", models.F("actor")),
                        ("subject__isnull", False),
                        ("workspace_kind", "self"),
                    ),
                    models.Q(
                        ("subject__isnull", False),
                        ("workspace_kind", "user"),
                        models.Q(("subject", models.F("actor")), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="ai_conversation_workspace_valid",
            ),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "ai.0005_team_administrators",
            "team administrators' Ask Arkray conversations (the previous constraint refuses them)",
        ),
    ]
