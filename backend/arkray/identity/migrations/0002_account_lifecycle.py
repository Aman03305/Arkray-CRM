"""Phase 1: account lifecycle (invited / active / deactivated), first and last names,
session epochs, optimistic versions, one-time account tokens, the durable login-throttle
log, and trigram indexes for the admin user search."""

import django.db.models.deletion
import django.db.models.functions.text
import django.utils.timezone
import uuid
from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import migrations, models
from django.db.models import TextField
from django.db.models.functions import Cast, Upper


def split_names_and_derive_status(apps, schema_editor):
    User = apps.get_model("identity", "User")
    for user in User.objects.all().iterator():
        first, _, last = user.full_name.strip().partition(" ")
        # Phase 0 names are up to 150 characters; the new columns hold 100 each.
        user.first_name = (first or user.email.split("@")[0])[:100]
        user.last_name = last.strip()[:100]
        usable_password = not user.password.startswith("!")
        if user.is_active and usable_password:
            user.status = "active"
            user.activated_at = user.created_at
        elif user.is_active:
            # Phase 0 created password-less users as "active"; they never could sign in.
            user.status = "invited"
            user.is_active = False
        else:
            user.status = "deactivated"
            user.deactivated_at = user.updated_at
            user.activated_at = user.created_at if usable_password else None
        user.save()


def join_names(apps, schema_editor):
    User = apps.get_model("identity", "User")
    for user in User.objects.all().iterator():
        user.full_name = f"{user.first_name} {user.last_name}".strip()[:150]
        user.is_active = user.status == "active"
        user.save()


def _trigram_index(field, name):
    return GinIndex(OpClass(Upper(Cast(field, output_field=TextField())), name="gin_trgm_ops"), name=name)


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        # Never dropped on rollback: other modules' search indexes (Phase 7) will use it too.
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm", reverse_sql=migrations.RunSQL.noop
        ),
        # --- names: full_name -> first_name + last_name ------------------------------------
        migrations.AddField(
            model_name="user",
            name="first_name",
            field=models.CharField(default="", max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="user",
            name="last_name",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        # --- lifecycle ------------------------------------------------------------------
        migrations.AddField(
            model_name="user",
            name="status",
            field=models.CharField(
                choices=[("invited", "Invited"), ("active", "Active"), ("deactivated", "Deactivated")],
                default="invited",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="activated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="deactivated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="session_epoch",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="user",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="user",
            name="is_active",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("admin", "Admin"), ("sales_user", "User")],
                default="sales_user",
                max_length=32,
            ),
        ),
        # Constraint first, data second: on rollback the constraint returns only after
        # join_names has refilled full_name.
        migrations.RemoveConstraint(model_name="user", name="identity_user_name_present"),
        migrations.RunPython(split_names_and_derive_status, join_names),
        # Give the column a default before dropping it, so that rolling back (which re-adds
        # it to a table with rows, then refills it via join_names) can succeed.
        migrations.AlterField(
            model_name="user",
            name="full_name",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.RemoveField(model_name="user", name="full_name"),
        # --- constraints and indexes ------------------------------------------------------
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("first_name", ""), _negated=True),
                name="identity_user_first_name_present",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["invited", "active", "deactivated"])),
                name="identity_user_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("is_active", True), ("status", "active")),
                    models.Q(models.Q(("status", "active"), _negated=True), ("is_active", False)),
                    _connector="OR",
                ),
                name="identity_user_is_active_matches_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("activated_at__isnull", True), ("status", "invited")),
                    models.Q(("activated_at__isnull", False), ("status", "active")),
                    ("status", "deactivated"),
                    _connector="OR",
                ),
                name="identity_user_activated_at_matches_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("deactivated_at__isnull", False), ("status", "deactivated")),
                    models.Q(
                        models.Q(("status", "deactivated"), _negated=True),
                        ("deactivated_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="identity_user_deactivated_at_matches_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "active"), _negated=True),
                    models.Q(("password__startswith", "!"), _negated=True),
                    _connector="OR",
                ),
                name="identity_user_active_has_password",
            ),
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(fields=["-created_at", "-id"], name="identity_user_created_idx"),
        ),
        migrations.AddIndex(
            model_name="user",
            index=_trigram_index("first_name", "identity_user_first_trgm"),
        ),
        migrations.AddIndex(
            model_name="user",
            index=_trigram_index("last_name", "identity_user_last_trgm"),
        ),
        migrations.AddIndex(
            model_name="user",
            index=_trigram_index("email", "identity_user_email_trgm"),
        ),
        # --- one-time account tokens --------------------------------------------------
        migrations.CreateModel(
            name="AccountToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "purpose",
                    models.CharField(
                        choices=[("invitation", "Invitation"), ("password_reset", "Password reset")],
                        max_length=24,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("used", "Used"), ("revoked", "Revoked")],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("token_hash", models.CharField(blank=True, max_length=64, null=True, unique=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("issued_at", models.DateTimeField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="account_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "identity_account_token",
                "indexes": [
                    models.Index(fields=["user", "purpose", "-created_at"], name="identity_token_user_idx")
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("purpose__in", ["invitation", "password_reset"])),
                        name="identity_account_token_purpose_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("status__in", ["pending", "used", "revoked"])),
                        name="identity_account_token_status_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("expires_at__gt", models.F("created_at"))),
                        name="identity_account_token_expires_after_creation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("token_hash__isnull", True),
                            ("token_hash__regex", "^[0-9a-f]{64}$"),
                            _connector="OR",
                        ),
                        name="identity_account_token_hash_format",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("issued_at__isnull", True), ("token_hash__isnull", True)),
                            models.Q(("issued_at__isnull", False), ("token_hash__isnull", False)),
                            _connector="OR",
                        ),
                        name="identity_account_token_issued_iff_hashed",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("sent_at__isnull", True), ("issued_at__isnull", False), _connector="OR"
                        ),
                        name="identity_account_token_sent_after_issue",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("status", "used"),
                                ("token_hash__isnull", False),
                                ("used_at__isnull", False),
                            ),
                            models.Q(models.Q(("status", "used"), _negated=True), ("used_at__isnull", True)),
                            _connector="OR",
                        ),
                        name="identity_account_token_used_at_matches_status",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("revoked_at__isnull", False), ("status", "revoked")),
                            models.Q(
                                models.Q(("status", "revoked"), _negated=True), ("revoked_at__isnull", True)
                            ),
                            _connector="OR",
                        ),
                        name="identity_account_token_revoked_at_matches_status",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("status", "pending")),
                        fields=("user", "purpose"),
                        name="identity_account_token_one_pending",
                    ),
                ],
            },
        ),
        # --- durable authentication throttling -----------------------------------------
        migrations.CreateModel(
            name="AuthThrottleEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("login_failure", "Failed sign-in"),
                            ("password_reset_request", "Password reset request"),
                        ],
                        max_length=32,
                    ),
                ),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("identifier_hash", models.CharField(blank=True, default="", max_length=64)),
                ("device_id", models.CharField(blank=True, default="", max_length=32)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
            ],
            options={
                "db_table": "identity_auth_throttle_event",
                "indexes": [
                    models.Index(
                        condition=models.Q(("kind", "login_failure")),
                        fields=["identifier_hash", "device_id", "-occurred_at"],
                        name="auth_throttle_account_idx",
                    ),
                    models.Index(fields=["kind", "ip_address", "-occurred_at"], name="auth_throttle_source_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ["login_failure", "password_reset_request"])),
                        name="auth_throttle_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("identifier_hash", ""),
                            ("identifier_hash__regex", "^[0-9a-f]{64}$"),
                            _connector="OR",
                        ),
                        name="auth_throttle_identifier_format",
                    ),
                ],
            },
        ),
    ]
