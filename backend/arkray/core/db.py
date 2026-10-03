"""Database-level integrity helpers used by migrations.

Append-only tables get a BEFORE UPDATE OR DELETE trigger that raises, so audit and history
rows cannot be altered even by code that bypasses the ORM. (TRUNCATE is prevented in
production by DB role privileges — see docs/security.md.)
"""

from __future__ import annotations

from django.db import migrations
from django.db.backends.base.base import BaseDatabaseWrapper

FORBID_MUTATION_FUNCTION = "arkray_forbid_mutation"


def create_forbid_mutation_function() -> migrations.RunSQL:
    return migrations.RunSQL(
        sql=f"""
            CREATE OR REPLACE FUNCTION {FORBID_MUTATION_FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'table % is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                    USING ERRCODE = 'insufficient_privilege';
            END;
            $$;
        """,
        reverse_sql=f"DROP FUNCTION IF EXISTS {FORBID_MUTATION_FUNCTION}();",
    )


def _trigger_name(table: str) -> str:
    return f"{table}_append_only"


def append_only_trigger(table: str) -> migrations.RunSQL:
    """Migration operation that makes `table` append-only. `table` is a code constant."""
    trigger = _trigger_name(table)
    return migrations.RunSQL(
        sql=(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FORBID_MUTATION_FUNCTION}();"
        ),
        reverse_sql=f"DROP TRIGGER IF EXISTS {trigger} ON {table};",
    )


def has_append_only_trigger(connection: BaseDatabaseWrapper, table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname = %s AND tgrelid = %s::regclass",
            [_trigger_name(table), table],
        )
        return cursor.fetchone() is not None
