# Adversarial review of 73ff1e3:
# - L1: a daily cap that locks password sign-in from unrecognised browsers (LoginLock) until
#   the owner's emailed unlock link (a new account-token purpose) or a reset, failures a
#   sign-in forgives kept for the day (`forgiven`);
# - L4: pseudonymisation of a former staff member is final (`pseudonymised_at`, a CHECK that
#   holds the account to its pseudonym, a trigger that keeps the mark), backfilled from the
#   audit trail and the pseudonym itself (review round 1: not the email domain alone).

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import django.db.models.functions.comparison
import django.db.models.functions.text
import django.utils.timezone
from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse

logger = logging.getLogger(__name__)

# Accounts the previous release pseudonymised (privacy.staff; it marked nothing but the
# email): marked, and put back under their pseudonym if an administrator had edited or revived
# them since (the finding): deactivated, no usable password, the pseudonym's name and email,
# every session ended, pending links revoked. Which accounts (review round 1, IDENTITY-4/5):
# - those a `user.pseudonymised` audit event names (append-only, written with every
#   pseudonymisation): including one fully revived, its email pointed elsewhere and its
#   invitation accepted, the dangerous case the email-domain rule missed;
# - those still under their own pseudonym with no usable password and not active (one an
#   audit purge left without its event; a live account can't be in that state).
# An address in the pseudonym domain alone is no proof (the API gave it to anyone until this
# release): such an account is never pseudonymised here. If it holds a marked account's own
# pseudonym it is moved to an address reserved for itself (the pseudonym is the former user's);
# either way it is reported (ids only) for an administrator to give it a real address.
# Parameterised SQL over the user table (small) and the audit trail's action index; nothing on
# an empty database.
@dataclass(frozen=True, slots=True)
class Backfill:
    marked: list[str]
    moved_aside: list[str]
    unmarked: list[str]


def backfill_pseudonyms(cursor: Any) -> Backfill:
    cursor.execute(
        "SELECT u.id::text FROM identity_user u"
        " WHERE u.pseudonymised_at IS NULL AND ("
        "   u.id::text IN (SELECT e.target_id FROM audit_event e"
        "                   WHERE e.action = 'user.pseudonymised' AND e.target_type = 'user')"
        "   OR (u.email = 'former-' || replace(u.id::text, '-', '') || '@pseudonymised.invalid'"
        "       AND u.status IN ('deactivated', 'invited') AND u.password LIKE '!%%'))"
        " ORDER BY u.id",
        [],
    )
    marked = [row[0] for row in cursor.fetchall()]
    moved: list[str] = []
    if marked:
        cursor.execute(
            "UPDATE identity_user s SET"
            "  email = 'reserved-' || replace(s.id::text, '-', '') || '@pseudonymised.invalid',"
            "  session_epoch = s.session_epoch + 1, version = s.version + 1, updated_at = now()"
            " WHERE s.id <> ALL(%s::uuid[]) AND s.email IN ("
            "   SELECT 'former-' || replace(m::text, '-', '') || '@pseudonymised.invalid'"
            "     FROM unnest(%s::uuid[]) AS m)"
            " RETURNING s.id::text",
            [marked, marked],
        )
        moved = sorted(row[0] for row in cursor.fetchall())
        cursor.execute(
            "UPDATE identity_account_token SET status = 'revoked', revoked_at = now()"
            " WHERE status = 'pending' AND user_id = ANY(%s::uuid[])",
            [marked],
        )
        cursor.execute(
            "UPDATE identity_user SET"
            "  pseudonymised_at = COALESCE(deactivated_at, updated_at, now()),"
            "  status = 'deactivated', is_active = false,"
            "  deactivated_at = COALESCE(deactivated_at, now()),"
            "  password = CASE WHEN password LIKE '!%%' THEN password"
            "                  ELSE '!' || md5(random()::text || id::text) END,"
            "  first_name = 'Former user',"
            "  last_name = left(replace(id::text, '-', ''), 8),"
            "  email = 'former-' || replace(id::text, '-', '') || '@pseudonymised.invalid',"
            "  session_epoch = session_epoch + 1, version = version + 1, updated_at = now()"
            " WHERE id = ANY(%s::uuid[])",
            [marked],
        )
    cursor.execute(
        "SELECT id::text FROM identity_user WHERE pseudonymised_at IS NULL"
        " AND (email LIKE '%%@pseudonymised.invalid' OR email LIKE '%%.pseudonymised.invalid')"
        " ORDER BY id",
        [],
    )
    unmarked = [row[0] for row in cursor.fetchall() if row[0] not in moved]
    return Backfill(marked=marked, moved_aside=moved, unmarked=unmarked)


def _backfill(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        done = backfill_pseudonyms(cursor)
    if done.moved_aside or done.unmarked:
        # Ids only: give each a real address on the Users page (docs/privacy.md#staff).
        logger.warning(
            "pseudonym_domain_accounts_not_pseudonymised",
            extra={"moved_aside": done.moved_aside, "unmarked": done.unmarked},
        )


# The mark is never taken back (a CHECK can't see the previous row): not by the ORM, raw SQL
# or the application's database role, which can't disable a trigger it doesn't own.
KEEP_PSEUDONYM = [
    """
CREATE FUNCTION identity_user_keep_pseudonym() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.pseudonymised_at IS NOT NULL
       AND NEW.pseudonymised_at IS DISTINCT FROM OLD.pseudonymised_at THEN
        RAISE EXCEPTION 'a pseudonymised account stays pseudonymised'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
""",
    """
CREATE TRIGGER identity_user_pseudonym_kept
    BEFORE UPDATE OF pseudonymised_at ON identity_user
    FOR EACH ROW EXECUTE FUNCTION identity_user_keep_pseudonym();
""",
]


def _id_hex():
    return django.db.models.functions.text.Replace(
        django.db.models.functions.comparison.Cast("id", output_field=models.TextField()),
        models.Value("-"),
        models.Value(""),
        output_field=models.TextField(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0007_mfa_cumulative_failures"),
        # The backfill reads the audit trail (`user.pseudonymised` events).
        ("audit", "0002_audit_event_append_only"),
    ]

    operations = [
        # The backfill reads the whole user table: lifted for this transaction only.
        migrations.RunSQL("SET LOCAL statement_timeout = 0;", migrations.RunSQL.noop),
        # --- L4 ---------------------------------------------------------------------------
        migrations.AddField(
            model_name="user",
            name="pseudonymised_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_backfill, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("pseudonymised_at__isnull", True),
                    models.Q(
                        (
                            "email",
                            django.db.models.functions.text.Concat(
                                models.Value("former-"),
                                _id_hex(),
                                models.Value("@pseudonymised.invalid"),
                                output_field=models.TextField(),
                            ),
                        ),
                        ("first_name", "Former user"),
                        ("last_name", django.db.models.functions.text.Left(_id_hex(), 8)),
                        ("password__startswith", "!"),
                        ("status", "deactivated"),
                    ),
                    _connector="OR",
                ),
                name="identity_user_pseudonym_final",
            ),
        ),
        migrations.RunSQL(KEEP_PSEUDONYM, migrations.RunSQL.noop),
        # --- L1 ---------------------------------------------------------------------------
        migrations.CreateModel(
            name="LoginLock",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("identifier_hash", models.CharField(max_length=64, unique=True)),
                ("locked_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "identity_login_lock",
                "indexes": [models.Index(fields=["locked_at"], name="identity_login_lock_at_idx")],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("identifier_hash__regex", "^[0-9a-f]{64}$")),
                        name="identity_login_lock_identifier_format",
                    )
                ],
            },
        ),
        # db_default: the previous release (which never names the column) can still insert.
        migrations.AddField(
            model_name="auththrottleevent",
            name="forgiven",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RemoveConstraint(
            model_name="accounttoken",
            name="identity_account_token_purpose_valid",
        ),
        migrations.AlterField(
            model_name="accounttoken",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("invitation", "Invitation"),
                    ("password_reset", "Password reset"),
                    ("sign_in_unlock", "Sign-in unlock"),
                ],
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="accounttoken",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("purpose__in", ["invitation", "password_reset", "sign_in_unlock"])
                ),
                name="identity_account_token_purpose_valid",
            ),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "identity.0008_account_takeover_limits",
            "sign-in locks, the pseudonymisation marks and the CHECK that keeps them final",
        ),
    ]
