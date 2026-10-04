"""Search is read-only and forgets what was asked (docs/search.md#privacy).

- PostgreSQL refuses any write inside a search (READ ONLY transaction), and a search writes
  nothing: no history, no "recent searches", no analytics, no domain events, no outbox work.
- The query never reaches a log line, an audit row or an error body: not for a 200, a 400, a
  404, nor a statement timeout. Delegated searches are audited only as workspace access.
- Notes: the body never leaves the database (test_ranking.py), and never reaches logs.
- No AI, embeddings or network calls anywhere on the search path.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from django.apps import apps
from django.db import InternalError, connection

from arkray.audit.models import AuditEvent
from arkray.core.logging import JsonFormatter
from arkray.core.models import OutboxEvent
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from tests.factories import LeadFactory, NoteFactory
from tests.helpers import signed_in

from .conftest import search_url

MARKER = "QUERYMARKER7731"
NOTE_MARKER = "NOTEBODYMARKER5519"


def table_sizes() -> dict[str, int]:
    return {
        model._meta.db_table: model._default_manager.count()
        for model in apps.get_models()
        if not model._meta.proxy
    }


@pytest.mark.django_db
def test_searching_writes_nothing(user_a, admin, django_capture_on_commit_callbacks):
    lead = LeadFactory(owner=user_a, first_name=MARKER)
    NoteFactory(lead=lead, description=f"{NOTE_MARKER} text")
    own, delegated = signed_in(user_a), signed_in(admin)
    with django_capture_on_commit_callbacks(execute=True):  # opens the audit windows
        delegated.get(search_url("warm-up", str(user_a.pk)))
        delegated.get(search_url("warm-up", "all"))
    before = table_sizes()
    for q in (MARKER, NOTE_MARKER, "nothing", "x"):
        own.get(search_url(q))
        delegated.get(search_url(q, str(user_a.pk)))
        delegated.get(search_url(q, "all"))
    after = table_sizes()
    assert {t: (before[t], after[t]) for t in before if before[t] != after[t]} == {}
    assert not OutboxEvent.objects.exists()


@pytest.mark.django_db
def test_delegated_searches_are_audited_as_access_never_with_the_query(user_a, admin):
    LeadFactory(owner=user_a, first_name=MARKER)
    client = signed_in(admin)
    for workspace in (str(user_a.pk), "all"):
        for _ in range(3):
            assert client.get(search_url(MARKER, workspace)).status_code == 200
    events = list(AuditEvent.objects.all())
    assert {e.action for e in events} == {"workspace.accessed"}
    assert MARKER not in str([(e.metadata, e.target_id, e.target_type) for e in events])


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_postgresql_refuses_any_write_inside_a_search(user_a, monkeypatch):
    """The guarantee doesn't rely on review: a write slipped into a search fails."""
    LeadFactory(owner=user_a, first_name="Readonly")
    original = lead_selectors.search

    def search_that_writes(scope, query, *, limit):
        Lead.objects.filter(owner=user_a).update(rating="hot")
        return original(scope, query, limit=limit)

    monkeypatch.setattr(lead_selectors, "search", search_that_writes)
    with pytest.raises(InternalError, match="read-only transaction"):
        signed_in(user_a).get(search_url("readonly"))
    assert not Lead.objects.filter(rating="hot").exists()


class TestNothingIsLogged:
    @pytest.fixture
    def records(self, caplog):
        caplog.set_level(logging.DEBUG)
        return caplog

    @staticmethod
    def rendered(caplog) -> str:
        formatter = JsonFormatter()
        return "\n".join(formatter.format(record) for record in caplog.records)

    @pytest.mark.django_db
    def test_not_for_successes_errors_or_refusals(self, records, user_a, user_b):
        lead = LeadFactory(owner=user_a, first_name=MARKER)
        NoteFactory(lead=lead, description=f"{NOTE_MARKER} body")
        client = signed_in(user_a)
        assert client.get(search_url(MARKER)).status_code == 200
        assert client.get(search_url(NOTE_MARKER)).status_code == 200
        assert client.get(search_url(MARKER + chr(0x202E))).status_code == 400
        assert client.get(search_url(MARKER, str(user_b.pk))).status_code == 404
        text = self.rendered(records)
        assert "http_request" in text  # the access log ran (path and duration only)
        assert MARKER not in text
        assert NOTE_MARKER not in text

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.usefixtures("crm_configuration")
    def test_not_when_the_database_gives_up(self, records, user_a, monkeypatch):
        """A real statement timeout inside a search: a generic 500, no SQL, no query."""

        def slow(scope, query, *, limit):
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = 50")
                cursor.execute("SELECT pg_sleep(2)")
            raise AssertionError("unreachable")

        monkeypatch.setattr(lead_selectors, "search", slow)
        client = signed_in(user_a)
        client.raise_request_exception = False
        response = client.get(search_url(MARKER))
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "server_error"
        body = response.content.decode()
        for leaked in (MARKER, "SELECT", "statement"):
            assert leaked not in body
        assert MARKER not in self.rendered(records)


SEARCH_PATH = [
    "arkray/search",
    "arkray/core/ranking.py",
    "arkray/core/text.py",
]
FORBIDDEN_IMPORTS = {
    "anthropic",
    "openai",
    "voyageai",
    "requests",
    "httpx",
    "urllib.request",
    "http.client",
    "socket",
    "celery",
    "pgvector",
}


def test_the_search_path_imports_no_ai_network_or_task_code():
    root = Path(__file__).resolve().parents[3]
    files = [
        p
        for entry in SEARCH_PATH
        for p in ((root / entry).rglob("*.py") if (root / entry).is_dir() else [root / entry])
        if "tests" not in p.parts
    ]
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(
                    name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS
                ), (path, name)
