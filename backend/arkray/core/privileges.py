"""The database role the application runs as (R74; docs/security.md#database-privileges).

Production runs migrations as the schema owner (`arkray_owner`) and everything else as an
application role (`arkray_app`) that owns nothing: the append-only trigger only binds a
role that neither owns the audit and history tables nor is a superuser, because an owner can
disable the trigger and TRUNCATE (the Phase 9 review did both as the development role).

- `grant()`: the application role's privileges, applied by the owner after every `migrate`
  (`manage.py grant_app_privileges arkray_app`). Ordinary tables: read and write. Append-only
  tables (found by their trigger, so a new one is covered without editing a list): SELECT
  and INSERT only. Never DDL, TRUNCATE, REFERENCES or TRIGGER; `django_migrations` read-only.
- `runtime_problems()`: what is wrong with the role this connection uses. The web server
  and the workers refuse to start on any (`DB_REQUIRE_RESTRICTED_ROLE`, on in production);
  `manage.py check --database default` reports them too.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper

from .db import FORBID_MUTATION_FUNCTION

logger = logging.getLogger(__name__)

SCHEMA = "public"


def append_only_tables(cursor: Any) -> list[str]:
    """Tables carrying the append-only trigger, whatever migration added them."""
    cursor.execute(
        "SELECT DISTINCT c.relname FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_proc p ON p.oid = t.tgfoid"
        " WHERE p.proname = %s AND n.nspname = %s AND NOT t.tgisinternal ORDER BY 1",
        [FORBID_MUTATION_FUNCTION, SCHEMA],
    )
    return [row[0] for row in cursor.fetchall()]


def declared_append_only_tables() -> set[str]:
    """What the code declares append-only (every `AppendOnlyModel`): a table among them
    without its trigger is a problem, not a table the checks may skip."""
    from django.apps import apps

    from .models import AppendOnlyModel

    return {
        model._meta.db_table for model in apps.get_models() if issubclass(model, AppendOnlyModel)
    }


def grant_statements(role: str, append_only: list[str], quote: Any) -> list[str]:
    """The statements `grant()` runs; `quote(*parts)` quotes a (schema-qualified) name."""
    who = quote(role)
    schema = quote(SCHEMA)
    statements = [
        f"GRANT USAGE ON SCHEMA {schema} TO {who}",
        f"REVOKE CREATE ON SCHEMA {schema} FROM {who}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {who}",
        f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA {schema} FROM {who}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {who}",
        f"REVOKE INSERT, UPDATE, DELETE ON {quote(SCHEMA, 'django_migrations')} FROM {who}",
    ]
    statements += [
        f"REVOKE UPDATE, DELETE ON {quote(SCHEMA, table)} FROM {who}" for table in append_only
    ]
    return statements


def owns_schema_objects(cursor: Any) -> bool:
    """Whether the connected role owns (or belongs to the owner of) every table and sequence
    in the schema: who may grant on them."""
    cursor.execute(
        "SELECT coalesce(bool_and(pg_has_role(current_user, c.relowner, 'MEMBER')), false)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'S')",
        [SCHEMA],
    )
    return bool(cursor.fetchone()[0])


def grant(connection: BaseDatabaseWrapper, role: str) -> list[str]:
    """Apply the application role's privileges, as the schema owner. Idempotent. Refused
    when run by anyone else (PostgreSQL would only warn and grant nothing) and when an
    append-only table has lost its trigger (it would be granted UPDATE and DELETE)."""
    from psycopg import sql

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        if cursor.fetchone() is None:
            raise ValueError(f"No database role {role!r}: create it first (roles.sql).")
        if not owns_schema_objects(cursor):
            raise ValueError("Run it as the schema owner (arkray_owner), not the application role.")
        tables = append_only_tables(cursor)
        missing = sorted(declared_append_only_tables() - set(tables))
        if missing:
            raise ValueError(f"Append-only tables without their trigger: {', '.join(missing)}.")
        raw = connection.connection

        def quote(*parts: str) -> str:
            return sql.Identifier(*parts).as_string(raw)

        statements = grant_statements(role, tables, quote)
        for statement in statements:
            cursor.execute(statement)
    return statements


SERVER_ROLES = ("pg_execute_server_program", "pg_read_server_files", "pg_write_server_files")


def runtime_problems(cursor: Any) -> list[str]:
    """Why the connected role must not run the application (empty: it may)."""
    problems: list[str] = []
    cursor.execute(
        "SELECT r.rolsuper, r.rolbypassrls, s.rolsuper FROM pg_roles r, pg_roles s"
        " WHERE r.rolname = current_user AND s.rolname = session_user"
    )
    superuser, bypass_rls, signed_in_as_superuser = cursor.fetchone()
    if superuser:
        problems.append("it is a superuser")
    elif signed_in_as_superuser:
        problems.append("it signed in as a superuser (session_user) and switched roles")
    if bypass_rls:
        problems.append("it bypasses row-level security")
    cursor.execute(
        "SELECT pg_has_role(current_user, datdba, 'MEMBER') FROM pg_database"
        " WHERE datname = current_database()"
    )
    if cursor.fetchone()[0]:
        problems.append("it owns the database (or is a member of its owner)")
    for role in SERVER_ROLES:
        cursor.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", [role])
        if cursor.fetchone()[0]:
            problems.append(f"it is a member of {role}")
    cursor.execute("SELECT has_schema_privilege(current_user, %s, 'CREATE')", [SCHEMA])
    if cursor.fetchone()[0]:
        problems.append(f"it can create objects in schema {SCHEMA} (DDL)")
    cursor.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND pg_has_role(current_user, c.relowner, 'MEMBER')",
        [SCHEMA],
    )
    owned = cursor.fetchone()[0]
    if owned:
        problems.append(f"it owns (or is a member of the owner of) {owned} objects in {SCHEMA}")
    tables = append_only_tables(cursor)
    for missing in sorted(declared_append_only_tables() - set(tables)):
        problems.append(f"the append-only table {missing} has lost its trigger")
    for table in tables:
        cursor.execute(
            "SELECT pg_has_role(current_user, c.relowner, 'MEMBER'),"
            " has_table_privilege(current_user, c.oid, 'UPDATE'),"
            " has_table_privilege(current_user, c.oid, 'DELETE'),"
            " has_table_privilege(current_user, c.oid, 'TRUNCATE')"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relname = %s AND n.nspname = %s",
            [table, SCHEMA],
        )
        owns, update, delete, truncate = cursor.fetchone()
        if owns:
            problems.append(f"it owns (or is a member of the owner of) {table}")
        granted = [
            name
            for name, held in (("UPDATE", update), ("DELETE", delete), ("TRUNCATE", truncate))
            if held
        ]
        if granted:
            problems.append(f"it may {', '.join(granted)} the append-only table {table}")
    return problems


def enforce_at_startup() -> bool:
    """Once per server or worker process, before it serves: refuse to start as a privileged
    role when DB_REQUIRE_RESTRICTED_ROLE (production), otherwise warn; warn about R63.
    Returns whether the check ran. An unreachable database is no reason not to start
    (readiness reports it); the web server's workers then check as they start. The
    connection (and a connection pool, `DB_POOL_ENABLED`) is closed again, so a forking
    server's workers inherit nothing."""
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured
    from django.db import DatabaseError, connection

    try:
        with connection.cursor() as cursor:
            problems = runtime_problems(cursor)
            statement_logging = logs_failed_statements(cursor)
    except DatabaseError as exc:
        logger.warning("database_role_unchecked", extra={"exc_type": type(exc).__name__})
        return False
    finally:
        connection.close()
        close_pool = getattr(connection, "close_pool", None)
        if callable(close_pool):  # psycopg's pool doesn't survive a fork (Phase 11 review)
            close_pool()
    if statement_logging:
        logger.warning("database_logs_failed_statements")
    if not problems:
        return True
    if settings.DB_REQUIRE_RESTRICTED_ROLE:
        raise ImproperlyConfigured(
            "The database role may not run the application: "
            + "; ".join(problems)
            + ". Connect as the application role (docs/security.md#database-privileges)."
        )
    logger.warning("database_role_privileged", extra={"problems": problems})
    return True


def logs_failed_statements(cursor: Any) -> bool:
    """R63: PostgreSQL writes the text of a failing statement, values included (Django
    binds values on the client), to its log unless log_min_error_statement is PANIC; and a
    failing row's values, in a constraint violation's DETAIL line ("Failing row contains
    (...)"), unless log_error_verbosity is TERSE (whole-software audit)."""
    cursor.execute(
        "SELECT current_setting('log_min_error_statement'), current_setting('log_error_verbosity')"
    )
    statement, verbosity = cursor.fetchone()
    return str(statement).lower() != "panic" or str(verbosity).lower() != "terse"
