"""`manage.py ai_reindex` never changes its database session (final audit P3): behind a
transaction-mode pooler a session `SET statement_timeout = 0` stayed on whichever server
connection ran it, for that connection's other clients. The `--rebuild` delete lifts the
timeout with `SET LOCAL`, inside its own transaction; nothing else touches it."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from arkray.ai.models import KnowledgeChunk
from tests.ai_fixtures import described_lead, index


def _timeout() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        return str(cursor.fetchone()[0])


def _timeout_statements(captured: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in captured.captured_queries if "statement_timeout" in q["sql"]]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rebuild", [False, True], ids=["reindex", "rebuild"])
def test_the_session_timeout_is_never_changed(lead_configuration, user_a, rebuild):
    described_lead(user_a, "Prefers the analyser demo on Tuesdays.")
    index()
    assert KnowledgeChunk.objects.exists()
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '7s'")  # the role's own setting, say
    try:
        with CaptureQueriesContext(connection) as captured:
            call_command("ai_reindex", *(["--rebuild"] if rebuild else []), stdout=StringIO())
        # Outside any transaction now: a session-level change would still show here.
        assert _timeout() == "7s"
        assert all(sql.startswith("SET LOCAL ") for sql in _timeout_statements(captured))
        assert len(_timeout_statements(captured)) == (1 if rebuild else 0)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET statement_timeout")
    assert KnowledgeChunk.objects.exists()  # rebuilt from the CRM


@pytest.mark.django_db
def test_the_rebuild_delete_runs_inside_its_own_transaction(user_a):
    """The SET LOCAL and the DELETE are one transaction (a SET LOCAL outside a transaction
    is a no-op with a warning, and behind a pooler would not cover the DELETE)."""
    described_lead(user_a, "Wants a second analyser next year.")
    index()
    with CaptureQueriesContext(connection) as captured:
        call_command("ai_reindex", "--rebuild", stdout=StringIO())
    sql = [q["sql"] for q in captured.captured_queries]
    lifted = next(i for i, s in enumerate(sql) if "statement_timeout" in s)
    deleted = next(i for i, s in enumerate(sql) if s.startswith('DELETE FROM "ai_knowledge_chunk"'))
    savepoints = [i for i, s in enumerate(sql) if s.startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))]
    # Inside the test's transaction the command's atomic block is a savepoint around both.
    assert len(savepoints) >= 2
    assert savepoints[0] < lifted < deleted < savepoints[1]
