"""Building a database at an earlier schema, for the tests of data migrations.

The migrations after the previous release refuse to be reversed (docs/deployment.md#rollback),
so an older schema is never reached by migrating the test database backwards: it is built
forwards, from an empty schema, to that state's leaf nodes (`build`). Whatever a test does,
it must leave every app at its latest migration again (`restore_latest`), or later
transactional tests find tables missing (Phase 3: the identity round trip once restored only
identity).
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import StateApps

# The leaf nodes of the supported previous release, v1.0 RC (64bb641:
# `git ls-tree -r 64bb641 --name-only | grep migrations/0`), for every app with migrations.
RELEASE_CANDIDATE = (
    ("activities", "0009_activities_autovacuum"),
    ("ai", "0003_question_finished_index"),
    ("audit", "0002_audit_event_append_only"),
    ("auth", "0012_alter_user_first_name_max_length"),
    ("contenttypes", "0002_remove_content_type_name"),
    ("core", "0005_outbox_dead_index"),
    ("identity", "0003_workspace_access_window"),
    ("leads", "0006_leads_autovacuum"),
    ("pipeline", "0005_search_indexes"),
    ("sessions", "0001_initial"),
)


def _replace(state: tuple[tuple[str, str], ...], **nodes: str) -> tuple[tuple[str, str], ...]:
    return tuple((app, nodes.get(app, name)) for app, name in state)


# The later commits an installation may also be on.
ENHANCEMENTS = _replace(  # 0c31aee: product enhancements
    RELEASE_CANDIDATE,
    activities="0010_note_edits_and_attachments",
    audit="0004_support_session_index",
    identity="0004_support_sessions",
    pipeline="0006_ownership_negotiation_fields",
)
ADR_0027 = _replace(ENHANCEMENTS, pipeline="0007_search_customer_names")  # 5b05177
ADR_0028 = _replace(ENHANCEMENTS, pipeline="0008_opportunity_expected_cpt")  # 989830b


def executor() -> MigrationExecutor:
    return MigrationExecutor(connection)


def migrate(targets) -> None:
    executor().migrate(list(targets))


def latest() -> list[tuple[str, str]]:
    """Every app's latest migration."""
    return list(executor().loader.graph.leaf_nodes())


def everything_but(app: str) -> list[tuple[str, str]]:
    return [node for node in latest() if node[0] != app]


def state_apps(targets) -> StateApps:
    """The historical models at `targets`: what that release's code reads and writes."""
    return executor().loader.project_state(list(targets), at_end=True).apps


def fresh_schema() -> None:
    """An empty `public` schema, with the owner and privileges a new database's has. The
    extensions in it go too; the migrations that need them create them again."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT nspowner::regrole::text, nspacl::text FROM pg_namespace"
            " WHERE nspname = 'public'"
        )
        owner, acl = cursor.fetchone()
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute(f'CREATE SCHEMA public AUTHORIZATION "{owner}"')
        cursor.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        cursor.execute("SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public'")
        assert cursor.fetchone() == (acl,)
    connection.close()  # nothing cached about the objects just dropped


def build(targets) -> None:
    """A database at exactly `targets`, built forwards from nothing."""
    fresh_schema()
    migrate(targets)


def restore_latest() -> None:
    """Every app at its latest migration again. Forwards from any earlier state; a schema
    left half-built by a failed test is rebuilt."""
    try:
        migrate(latest())
    except Exception:
        build(latest())
