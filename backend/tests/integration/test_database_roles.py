"""The application's database role (R74; docs/security.md#database-privileges): granted
exactly what it needs, and refused at startup when it could rewrite the audit trail.

Real roles in the test cluster: one is created, granted by `grant_app_privileges`, used over
its own connection, and dropped again.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.core.management.base import SystemCheckError
from django.db import OperationalError, connection

from arkray.core import privileges

pytestmark = pytest.mark.django_db(transaction=True)


def admin_sql(*statements: str) -> None:
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


@pytest.fixture
def app_role() -> Iterator[tuple[str, str]]:
    name, password = f"arkray_test_app_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    admin_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
    try:
        yield name, password
    finally:
        admin_sql(f"DROP OWNED BY {name}", f"DROP ROLE {name}")


def connect_as(role: tuple[str, str]) -> psycopg.Connection[Any]:
    settings = connection.settings_dict
    return psycopg.connect(
        host=settings["HOST"] or "127.0.0.1",
        port=settings["PORT"] or 5432,
        dbname=settings["NAME"],
        user=role[0],
        password=role[1],
        autocommit=True,
    )


def refused(conn: psycopg.Connection[Any], statement: str) -> bool:
    try:
        conn.execute(statement)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


def test_the_granted_role_can_work_but_not_rewrite_history(app_role):
    call_command("grant_app_privileges", app_role[0])
    with connect_as(app_role) as conn:
        assert privileges.runtime_problems(conn.cursor()) == []
        # Ordinary tables: reads and writes.
        conn.execute("SELECT count(*) FROM leads_lead")
        conn.execute("UPDATE leads_lead SET city = city WHERE false")
        # The append-only trail, as the Phase 9 review attacked it with the owner role.
        for table in ("audit_event", "pipeline_stage_history", "activities_timeline_entry"):
            conn.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — fixed names
            assert refused(conn, f"UPDATE {table} SET id = id WHERE false"), table  # noqa: S608
            assert refused(conn, f"DELETE FROM {table} WHERE false"), table  # noqa: S608
            assert refused(conn, f"TRUNCATE {table}"), table
            assert refused(conn, f"ALTER TABLE {table} DISABLE TRIGGER ALL"), table
        assert refused(conn, "SET session_replication_role = replica")
        assert refused(conn, "CREATE TABLE arkray_ddl_probe (id int)")
        assert refused(conn, "UPDATE django_migrations SET app = app WHERE false")


def test_the_grants_follow_the_append_only_trigger_not_a_list():
    with connection.cursor() as cursor:
        found = privileges.append_only_tables(cursor)
    assert found == [
        "activities_timeline_entry",
        "audit_event",
        "pipeline_negotiation_price",
        "pipeline_stage_history",
    ]


def test_privileged_roles_are_named_for_what_they_could_do(app_role):
    with connection.cursor() as cursor:  # the test database's superuser
        problems = privileges.runtime_problems(cursor)
    assert "it is a superuser" in problems
    assert any(
        p.startswith("it owns (or is a member of the owner of) audit_event") for p in problems
    )
    # A role that merely belongs to the owner's role inherits the power to disable triggers.
    call_command("grant_app_privileges", app_role[0])
    owner = connection.settings_dict["USER"]
    admin_sql(f"GRANT {connection.ops.quote_name(owner)} TO {app_role[0]}")
    with connect_as(app_role) as conn:
        problems = privileges.runtime_problems(conn.cursor())
    assert "it is a superuser" not in problems
    assert any("member of the owner of) audit_event" in p for p in problems)


def test_startup_is_refused_as_a_privileged_role_when_required(settings, caplog):
    settings.DB_REQUIRE_RESTRICTED_ROLE = True
    with pytest.raises(ImproperlyConfigured, match="superuser"):
        privileges.enforce_at_startup()
    settings.DB_REQUIRE_RESTRICTED_ROLE = False
    with caplog.at_level(logging.WARNING, logger="arkray.core.privileges"):
        privileges.enforce_at_startup()
    assert "database_role_privileged" in [r.getMessage() for r in caplog.records]


def test_an_unreachable_database_does_not_stop_a_start(settings, monkeypatch, caplog):
    """Readiness reports an outage; the role is checked at the next start."""
    settings.DB_REQUIRE_RESTRICTED_ROLE = True

    def down(*_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError("connection refused")

    monkeypatch.setattr(connection, "cursor", down)
    with caplog.at_level(logging.WARNING, logger="arkray.core.privileges"):
        privileges.enforce_at_startup()
    assert "database_role_unchecked" in [r.getMessage() for r in caplog.records]


def test_the_release_check_reports_the_role_and_statement_logging(settings):
    settings.DB_REQUIRE_RESTRICTED_ROLE = True
    with pytest.raises(SystemCheckError, match=r"arkray.E001"):
        call_command("check", databases=["default"], deploy=True)


def test_granting_an_unknown_role_is_a_clear_error():
    with pytest.raises(CommandError, match="No database role"):
        call_command("grant_app_privileges", "arkray_no_such_role")


def test_web_and_workers_both_check_at_start():
    from celery.signals import worker_init

    from config import celery  # noqa: F401 — connects the receiver

    names = {getattr(receiver(), "__name__", "") for _, receiver in worker_init.receivers}
    assert "_check_database_role" in names
    gunicorn_conf = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"
    assert "enforce_at_startup()" in gunicorn_conf.read_text(encoding="utf-8")


def test_migrate_as_the_owner_is_not_refused():
    """The migrate job connects as the owner on purpose: the role check is a deploy check
    (Phase 11: as a plain database check it stopped `migrate`)."""
    call_command("check", databases=["default"])  # what migrate runs: no arkray.E001


def test_a_worker_on_a_privileged_role_exits_instead_of_running(settings):
    """Phase 11 live check: Celery logs and ignores an Exception raised by a signal handler,
    so the worker started anyway; a SystemExit gets through."""
    from config.celery import _check_database_role

    settings.DB_REQUIRE_RESTRICTED_ROLE = True
    with pytest.raises(SystemExit):
        _check_database_role()


# --- Phase 11 review -------------------------------------------------------------------------
def test_membership_in_a_server_file_role_is_a_problem(app_role):
    """COPY ... TO PROGRAM or a written server file leads to the superuser."""
    call_command("grant_app_privileges", app_role[0])
    admin_sql(f"GRANT pg_write_server_files TO {app_role[0]}")
    with connect_as(app_role) as conn:
        problems = privileges.runtime_problems(conn.cursor())
    assert "it is a member of pg_write_server_files" in problems


def test_a_lost_trigger_is_reported_and_stops_the_grant(app_role):
    """Otherwise the grant would give UPDATE and DELETE on that table and every per-table
    check would pass because the table was no longer found."""
    from django.db import transaction

    with transaction.atomic():
        admin_sql("DROP TRIGGER audit_event_append_only ON audit_event")
        with connection.cursor() as cursor:
            problems = privileges.runtime_problems(cursor)
        assert "the append-only table audit_event has lost its trigger" in problems
        with pytest.raises(CommandError, match="without their trigger: audit_event"):
            call_command("grant_app_privileges", app_role[0])
        transaction.set_rollback(True)


def test_only_the_owner_may_grant(app_role, monkeypatch):
    """A non-owner's GRANT is a warning in PostgreSQL, not an error: it granted nothing and
    the command reported success."""
    call_command("grant_app_privileges", app_role[0])
    with connect_as(app_role) as conn:
        assert privileges.owns_schema_objects(conn.cursor()) is False
    with connection.cursor() as cursor:
        assert privileges.owns_schema_objects(cursor) is True  # the test database's owner
    monkeypatch.setattr(privileges, "owns_schema_objects", lambda cursor: False)
    with pytest.raises(CommandError, match="schema owner"):
        call_command("grant_app_privileges", app_role[0])


def test_the_startup_check_closes_a_connection_pool(monkeypatch):
    """psycopg's pool doesn't survive gunicorn's fork: workers would share connections."""
    closed: list[bool] = []
    monkeypatch.setattr(connection, "close_pool", lambda: closed.append(True), raising=False)
    assert privileges.enforce_at_startup() is True
    assert closed == [True]


def test_workers_check_when_the_master_could_not():
    gunicorn_conf = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"
    source = gunicorn_conf.read_text(encoding="utf-8")
    assert "def post_worker_init" in source
    assert "if not _role_checked" in source
