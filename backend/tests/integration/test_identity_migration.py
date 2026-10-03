"""identity.0002 converts Phase 0 rows correctly, and can be rolled back with data present."""

import pytest
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("identity", "0001_initial")]
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


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes("identity")


def everything():
    """Every app's latest migration. Migrating identity back to 0001 also unapplies the
    apps that depend on identity.0002 (leads, and pipeline through leads); restoring only
    identity left their tables missing for any later transactional test (found in
    Phase 3, when the first transactional tests after this one appeared)."""
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def rows(sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def test_forward_and_backward_with_phase_0_data():
    migrate(BEFORE)
    try:
        with connection.cursor() as cursor:
            for pk, email, name, role, active, password in PHASE_0_ROWS:
                cursor.execute(
                    "INSERT INTO identity_user (id, email, full_name, role, is_active, password,"
                    " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now(), now())",
                    [pk, email, name, role, active, make_password(password)],
                )
        migrate(latest())
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

        migrate(BEFORE)  # rollback with rows present
        assert rows("SELECT email, full_name FROM identity_user ORDER BY email") == [
            ("admin@x.test", "Anita Admin"),
            ("gone@x.test", "Rahul Kumar Sharma"),
            ("invitee@x.test", "Neha"),
            ("long@x.test", ("C" * 100 + " " + "D" * 100)[:150]),
        ]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM identity_user")
        migrate(everything())


def test_the_schema_is_complete_again_afterwards():
    """Runs after the round trip above (same module, declared order)."""
    with connection.cursor() as cursor:
        for table in (
            "leads_lead",
            "leads_lead_status",
            "pipeline_opportunity",
            "pipeline_stage_history",
            "activities_activity",
            "activities_timeline_entry",
        ):
            cursor.execute("SELECT to_regclass(%s)", [table])
            assert cursor.fetchone() == (table,), table
