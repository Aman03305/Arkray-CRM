# ADR-0035: super administrators and team administrators.
#
# - The role "team_admin" ("Admin"): an administrator of the sales users they manage. The
#   existing role value "admin" keeps its meaning and its powers ("Super admin" now), so every
#   existing administrator becomes a super administrator with no data change, and the previous
#   release, reading the table during a rolling deploy, sees only roles it knows until a
#   super administrator creates the first team administrator; the previous release gives an
#   unknown role nothing (identity.policy), so that account is refused there, never widened.
# - `manager`: the team administrator managing a sales user, NULL for everyone else (CHECK).
#   Nullable with no default: a catalogue-only change (no rewrite, no backfill: every existing
#   user starts managed by the super administrators only), and the previous release, which
#   never names it, still inserts. The user table is small (staff accounts), so the index and
#   the CHECKs are built and validated in the migration's transaction.
#
# Not reversible: dropping the column would forget which administrator manages each user,
# so it refuses (RefuseReverse, last operation; docs/deployment.md#rollback).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0008_account_takeover_limits"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="user",
            name="identity_user_role_valid",
        ),
        migrations.AddField(
            model_name="user",
            name="manager",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="team_members",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("admin", "Super admin"), ("team_admin", "Admin"), ("sales_user", "User")],
                default="sales_user",
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(
                condition=models.Q(("manager__isnull", False)),
                fields=["manager"],
                name="identity_user_manager_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("manager__isnull", True), ("role", "sales_user"), _connector="OR"),
                name="identity_user_manager_only_for_sales_users",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("manager__isnull", True),
                    models.Q(("manager", models.F("id")), _negated=True),
                    _connector="OR",
                ),
                name="identity_user_manager_not_self",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("role__in", ["admin", "team_admin", "sales_user"])),
                name="identity_user_role_valid",
            ),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "identity.0009_team_administrators",
            "which team administrator manages each user (and the team administrators' role)",
        ),
    ]
