"""The evaluation's own database, and the guard that keeps it from ever touching another
(docs/ai-evaluation.md#the-database).

The suite seeds users and records, and sends what it seeds to a model provider. It must
never run against a database holding anything else, above all real customer data. Two
independent proofs are required before anything is written or sent:

1. **The name.** The database DATABASE_URL names must end in `_ai_eval`
   (`arkray_ai_eval`). Production, staging and development databases are never called that.
2. **The contents.** It holds no CRM data the evaluation didn't write: either it doesn't
   exist yet (it is created), or it has no users, leads, opportunities, activities or Ask
   Arkray questions at all (it is used), or it carries the evaluation's marker (a table the
   seeder writes in the same transaction as the data) *and* every user in it has a
   synthetic `@ai-eval.invalid` address *and* it still holds exactly the records the marker
   counted (it is dropped and created again: the dataset is relative to today, so every run
   starts from a fresh one). Anything else is refused, and nothing is changed.

A refused database is never modified: the decision is made from read-only queries first.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.core.management import call_command
from django.db import connection
from django.utils import timezone

DATABASE_SUFFIX = "_ai_eval"
MARKER_TABLE = "arkray_ai_eval_marker"
SYNTHETIC_EMAIL_DOMAIN = "ai-eval.invalid"
# Tables whose rows are CRM data (or derived from it): a database with any of them non-empty
# and no marker is somebody's, not the evaluation's.
DATA_TABLES = (
    "identity_user",
    "leads_lead",
    "pipeline_opportunity",
    "activities_activity",
    "ai_question",
)


class DatabaseRefused(Exception):
    """This database may not be used for the evaluation (the message says why)."""


@dataclass(frozen=True, slots=True)
class Inspection:
    exists: bool
    marker: dict[str, Any] | None = None  # the marker row's dataset and counts, if any
    data_rows: int = 0  # rows in DATA_TABLES
    foreign_users: int = 0  # users without a synthetic address
    counts: dict[str, int] | None = None  # the CRM records it holds now, as the marker counts


def database_name() -> str:
    return str(connection.settings_dict["NAME"])


def check_name(name: str) -> None:
    if not name.endswith(DATABASE_SUFFIX) or len(name) <= len(DATABASE_SUFFIX):
        raise DatabaseRefused(
            f"Refusing to use database {name!r}: the evaluation runs only in a database of "
            f"its own, whose name ends in {DATABASE_SUFFIX!r} (for example arkray_ai_eval). "
            "Point DATABASE_URL at one."
        )


def decide(name: str, found: Inspection) -> str:
    """'create', 'use' or 'recreate'; DatabaseRefused for anything else."""
    check_name(name)
    if not found.exists:
        return "create"
    if found.marker is None:
        if found.data_rows:
            raise DatabaseRefused(
                f"Refusing to use database {name!r}: it holds {found.data_rows} rows of CRM "
                "data the evaluation didn't write (no evaluation marker). It is left as it is."
            )
        return "use"
    if found.foreign_users:
        raise DatabaseRefused(
            f"Refusing to use database {name!r}: it carries the evaluation marker but "
            f"{found.foreign_users} of its users don't have a synthetic @{SYNTHETIC_EMAIL_DOMAIN}"
            " address. It is left as it is."
        )
    expected = found.marker.get("counts")
    if found.counts != expected:
        raise DatabaseRefused(
            f"Refusing to use database {name!r}: its records changed since the evaluation "
            f"seeded them ({found.counts} now, {expected} seeded). It is left as it is."
        )
    return "recreate"


# --- reading the database (read-only) -------------------------------------------------------------
def _exists(name: str) -> bool:
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
        return cursor.fetchone() is not None


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def inspect(name: str) -> Inspection:
    if not _exists(name):
        return Inspection(exists=False)
    tables = _tables()
    marker = read_marker() if MARKER_TABLE in tables else None
    rows = 0
    with connection.cursor() as cursor:
        for table in DATA_TABLES:
            if table in tables:
                cursor.execute(f"SELECT count(*) FROM {connection.ops.quote_name(table)}", [])  # noqa: S608 — a constant table name
                rows += int(cursor.fetchone()[0])
        foreign = 0
        if "identity_user" in tables:
            cursor.execute(
                "SELECT count(*) FROM identity_user WHERE email NOT LIKE %s",
                [f"%@{SYNTHETIC_EMAIL_DOMAIN}"],
            )
            foreign = int(cursor.fetchone()[0])
    return Inspection(
        exists=True,
        marker=marker,
        data_rows=rows,
        foreign_users=foreign,
        counts=record_counts() if marker is not None else None,
    )


def record_counts() -> dict[str, int]:
    """The CRM records the database holds, keyed as the dataset counts them."""
    found: dict[str, int] = {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM identity_user", [])
        found["users"] = int(cursor.fetchone()[0])
        cursor.execute("SELECT count(*) FROM leads_lead", [])
        found["lead"] = int(cursor.fetchone()[0])
        cursor.execute("SELECT count(*) FROM pipeline_opportunity", [])
        found["opportunity"] = int(cursor.fetchone()[0])
        cursor.execute("SELECT type, count(*) FROM activities_activity GROUP BY type", [])
        for kind, count in cursor.fetchall():
            found[str(kind)] = int(count)
    return {key: value for key, value in found.items() if value}


def read_marker() -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT dataset, counts, seeded_at FROM {MARKER_TABLE} WHERE id = 1", [])  # noqa: S608 — a constant table name
        row = cursor.fetchone()
    if row is None:
        return None
    counts = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    return {"dataset": row[0], "counts": counts, "seeded_at": row[2].isoformat()}


def write_marker(dataset: str, counts: dict[str, int]) -> None:
    """In the seeding transaction: the proof that this database's data is the evaluation's."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} ("
            "id integer PRIMARY KEY CHECK (id = 1), dataset text NOT NULL, "
            "counts jsonb NOT NULL, seeded_at timestamptz NOT NULL)",
            [],
        )
        cursor.execute(
            f"INSERT INTO {MARKER_TABLE} (id, dataset, counts, seeded_at) "  # noqa: S608
            "VALUES (1, %s, %s, %s)",
            [dataset, json.dumps(counts, sort_keys=True), timezone.now()],
        )


def assert_seeded(dataset: str) -> None:
    """Before any question is asked: the marker is ours and the records are as seeded."""
    check_name(database_name())
    marker = read_marker()
    if marker is None or marker["dataset"] != dataset:
        raise DatabaseRefused("The evaluation database carries no marker for this dataset.")
    if record_counts() != marker["counts"]:
        raise DatabaseRefused("The evaluation database's records changed since seeding.")


# --- preparing it ---------------------------------------------------------------------------------
def prepare(say: Callable[[str], None]) -> None:
    """Make DATABASE_URL's database a fresh, migrated, empty evaluation database, or raise
    DatabaseRefused without changing anything."""
    name = database_name()
    check_name(name)  # before connecting to anything
    action = decide(name, inspect(name))
    quoted = connection.ops.quote_name(name)
    if action in {"create", "recreate"}:
        connection.close()
        with connection._nodb_cursor() as cursor:
            if action == "recreate":
                say(f"Dropping the previous evaluation database {name} (marker checked).")
                cursor.execute(f"DROP DATABASE {quoted} WITH (FORCE)", [])
            say(f"Creating the evaluation database {name}.")
            cursor.execute(f"CREATE DATABASE {quoted}", [])
        connection.close()
    else:
        say(f"Using the empty evaluation database {name}.")
    call_command("migrate", interactive=False, verbosity=0)
    # Migrated, still empty: nothing between the check and here wrote CRM data.
    if inspect(name).data_rows:
        raise DatabaseRefused(f"Database {name!r} gained CRM data while it was being prepared.")
