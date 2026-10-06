"""identity.0002 converts Phase 0 rows correctly, and can be rolled back with data present
within the released range; from the latest schema, the rollback is refused (identity.0004:
docs/deployment.md#rollback)."""

import pytest
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError

from tests.integration.migration_states import RELEASE_CANDIDATE, build, migrate, restore_latest

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("identity", "0001_initial")]
RELEASED = [node for node in RELEASE_CANDIDATE if node[0] == "identity"]  # identity.0003
PHASE_0_ROWS = [
    # id, email, full_name, role, is_active, password
    ("11111111-1111-4111-8111-111111111111", "admin@x.test", "Anita Admin", "admin", True, "pw"),
    ("22222222-2222-4222-8222-222222222222", "invitee@x.test", "Neha", "sales_user", True, None),
    (
        "33333333-3333-4333-8333-333333333333",
        "gone@x.test",
        "Rahul Kumar Sharma",
        "sales_user",
        False,
        "pw",
    ),
    # Phase 0 allowed 150-character names; each new column holds 100.
    (
        "44444444-4444-4444-8444-444444444444",
        "long@x.test",
        "A" * 120 + " " + "B" * 29,
        "sales_user",
        True,
        "pw",
    ),
]


def rows(sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def test_forward_and_backward_with_phase_0_data():
    # Built forwards from an empty schema: the test database is at the latest migrations,
    # and those after the release refuse to be reversed (migration_states.py).
    build(BEFORE)
    try:
        with connection.cursor() as cursor:
            for pk, email, name, role, active, password in PHASE_0_ROWS:
                cursor.execute(
                    "INSERT INTO identity_user (id, email, full_name, role, is_active, password,"
                    " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now(), now())",
                    [pk, email, name, role, active, make_password(password)],
                )
        migrate(RELEASED)
        assert rows(
            "SELECT email, first_name, last_name, status, is_active, activated_at IS NOT NULL,"
            " deactivated_at IS NOT NULL FROM identity_user ORDER BY email"
        ) == [
            ("admin@x.test", "Anita", "Admin", "active", True, True, False),
            ("gone@x.test", "Rahul", "Kumar Sharma", "deactivated", False, True, True),
            # Phase 0 stored password-less users as "active"; they could never sign in.
            ("invitee@x.test", "Neha", "", "invited", False, False, False),
            ("long@x.test", "A" * 100, "B" * 29, "active", True, True, False),
        ]
        with connection.cursor() as cursor:  # Phase 1 allows 100 + 100 characters
            cursor.execute(
                "UPDATE identity_user SET first_name = %s, last_name = %s WHERE email = %s",
                ["C" * 100, "D" * 100, "long@x.test"],
            )

        migrate(BEFORE)  # rollback with rows present, within the released range
        assert rows("SELECT email, full_name FROM identity_user ORDER BY email") == [
            ("admin@x.test", "Anita Admin"),
            ("gone@x.test", "Rahul Kumar Sharma"),
            ("invitee@x.test", "Neha"),
            ("long@x.test", ("C" * 100 + " " + "D" * 100)[:150]),
        ]

        # Past the release (identity.0004, support sessions): forwards keeps the rows, and
        # the way back is refused before anything is undone.
        migrate([("identity", "0004_support_sessions")])
        with pytest.raises(IrreversibleError, match=r"identity.0004_support_sessions"):
            migrate(BEFORE)
        assert rows("SELECT email, password_change_required FROM identity_user ORDER BY email") == [
            ("admin@x.test", False),
            ("gone@x.test", False),
            ("invitee@x.test", False),
            ("long@x.test", False),
        ]
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM identity_user")
        finally:
            restore_latest()


def test_the_schema_is_complete_again_afterwards():
    """Runs after the round trip above (same module, declared order)."""
    with connection.cursor() as cursor:
        for table in (
            "leads_lead",
            "leads_lead_status",
            "pipeline_opportunity",
            "pipeline_stage_history",
            "pipeline_negotiation_price",
            "activities_activity",
            "activities_timeline_entry",
            "identity_support_session",
        ):
            cursor.execute("SELECT to_regclass(%s)", [table])
            assert cursor.fetchone() == (table,), table
