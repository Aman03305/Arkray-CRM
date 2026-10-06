"""Global search can't monopolise the database (docs/search.md#resource-protection).

- Every one of a search's five statements runs under the search's own statement timeout
  (search.selectors.STATEMENT_TIMEOUT_MS), never above the connection's, and a search the
  database gives up on is a 503 "search_busy" with Retry-After: never a 500, never retried by
  the web app on its own. Other database errors are not disguised as "busy".
- Twenty searches that would each run for ever finish within the timeout, in parallel, while
  another user's search stays fast.
- Results are bounded whatever the data: 5 per kind, in one's own workspace and the
  organisation's, for a word every record contains.
- Input is bounded before anything is searched: length, number of words, words made only of
  punctuation; metacharacters are literal.
- The search throttle is the user's, wherever they connect from, and a refused search
  searches nothing.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from rest_framework.throttling import ScopedRateThrottle

from arkray.activities import selectors as activity_selectors
from arkray.activities.models import ActivityType
from arkray.leads import selectors as lead_selectors
from arkray.pipeline import selectors as pipeline_selectors
from arkray.search import selectors
from arkray.search.api.views import BUSY_RETRY_AFTER_S
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
)
from tests.helpers import signed_in

from .conftest import GROUPS, search, search_url

SEARCH_TABLES = ("leads_lead", "pipeline_opportunity", "activities_activity", "audit_")


def searched(captured: CaptureQueriesContext) -> bool:
    """Whether any captured statement read a searched table or wrote an audit row."""
    return any(t in c["sql"] for c in captured.captured_queries for t in SEARCH_TABLES)


def statement_timeout() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        value: str = cursor.fetchone()[0]
    return value


def spy_on_every_group(monkeypatch) -> list[str]:
    """Each group's selector records the statement timeout it runs under."""
    seen: list[str] = []
    for module in (lead_selectors, pipeline_selectors, activity_selectors):
        original = module.search

        def spying(*args, _original=original, **kwargs):
            seen.append(statement_timeout())
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, "search", spying)
    return seen


def notes_never_finish(monkeypatch, *, when: str = "") -> None:
    """The last of the five statements (the notes group) would run for 30 s."""
    original = activity_selectors.search

    def slow(scope, query, activity_type, *, limit):
        if activity_type == ActivityType.NOTE and when in query.text.lower():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(30)")
        return original(scope, query, activity_type, limit=limit)

    monkeypatch.setattr(activity_selectors, "search", slow)


# --- the statement timeout -----------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
class TestStatementTimeout:
    @pytest.mark.parametrize(
        ("connection_timeout", "applied"),
        [("10s", "2s"), ("500ms", "500ms"), ("0", "2s")],
    )
    def test_every_statement_runs_under_the_search_timeout(
        self, monkeypatch, user_a, connection_timeout, applied
    ):
        """Never above the connection's own (DB_STATEMENT_TIMEOUT_MS may be lower); with none
        there (0), the search's still applies."""
        seen = spy_on_every_group(monkeypatch)
        with connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = '{connection_timeout}'")
        try:
            assert signed_in(user_a).get(search_url("anything")).status_code == 200
            assert seen == [applied] * 5
            # SET LOCAL: the connection's own timeout is back once the search is over.
            assert statement_timeout() == connection_timeout
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET statement_timeout")

    def test_a_search_the_database_gives_up_on_is_busy_not_a_500(self, monkeypatch, caplog, user_a):
        """Even when the slow statement is the last of the five, and fast: the search's
        timeout, not the connection's 10 s."""
        monkeypatch.setattr(selectors, "STATEMENT_TIMEOUT_MS", 200)
        notes_never_finish(monkeypatch)
        caplog.set_level(logging.WARNING)
        client = signed_in(user_a)
        client.raise_request_exception = False
        started = time.monotonic()
        response = client.get(search_url("QUERYMARKER4417"))
        assert time.monotonic() - started < 3
        assert response.status_code == 503
        assert response["Retry-After"] == str(BUSY_RETRY_AFTER_S)
        assert BUSY_RETRY_AFTER_S > 4  # the web app retries by itself only below 4 s
        error = response.json()["error"]
        assert error["code"] == "search_busy"
        assert error["message"] == (
            "Search is busy right now. Try again in a moment, or add another word."
        )
        for leaked in ("QUERYMARKER4417", "SELECT", "pg_sleep", "statement"):
            assert leaked not in response.content.decode()
        assert [r.getMessage() for r in caplog.records if r.name == selectors.__name__] == [
            "search_timed_out"
        ]
        assert "QUERYMARKER4417" not in caplog.text
        # The transaction was rolled back cleanly: the same connection searches again.
        monkeypatch.undo()
        assert client.get(search_url("anything")).status_code == 200

    def test_other_database_errors_are_not_disguised_as_busy(self, monkeypatch, user_a):
        def deadlock(*args, **kwargs):
            try:
                raise psycopg.errors.DeadlockDetected("deadlock detected")
            except psycopg.Error as cause:
                raise OperationalError("deadlock detected") from cause

        monkeypatch.setattr(lead_selectors, "search", deadlock)
        client = signed_in(user_a)
        client.raise_request_exception = False
        response = client.get(search_url("anything"))
        assert (response.status_code, response.json()["error"]["code"]) == (500, "server_error")

    def test_twenty_parallel_runaway_searches_stay_bounded_and_others_stay_fast(
        self, monkeypatch, user_a, user_b
    ):
        """Each runaway search would hold its connection for 10 s under the connection's own
        timeout (30 s without one); under the search's, all twenty are refused in about its
        length, and another user's search meanwhile answers normally."""
        monkeypatch.setattr(selectors, "STATEMENT_TIMEOUT_MS", 300)
        notes_never_finish(monkeypatch, when="runaway")
        LeadFactory(owner=user_b, first_name="Ordinary")
        flood = [signed_in(user_a) for _ in range(20)]  # sessions made before the threads
        colleague = signed_in(user_b)

        def run(client, q):
            try:
                started = time.monotonic()
                response = client.get(search_url(q))
                return response.status_code, time.monotonic() - started, response
            finally:
                connection.close()  # each thread's own connection

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=21) as pool:
            runaway = [pool.submit(run, client, "runaway query") for client in flood]
            time.sleep(0.1)  # the flood is in the database
            ordinary = pool.submit(run, colleague, "ordinary").result()
            results = [future.result() for future in runaway]
        elapsed = time.monotonic() - started
        assert [status for status, _, _ in results] == [503] * 20
        assert elapsed < 6, elapsed
        status, took, response = ordinary
        assert status == 200
        assert len(response.json()["leads"]["results"]) == 1
        assert took < 2, took


# --- bounded results --------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("who", ["owner", "organisation"])
def test_a_word_in_every_record_returns_five_of_each_kind(user_a, admin, who):
    """'india' in 8 records of every kind: 5 shown per kind (has_more), in one's own workspace
    and organisation-wide alike; the response stays small whatever the data."""
    for i in range(8):
        lead = LeadFactory(owner=user_a, first_name=f"Lead{i}", organization_name="India Labs")
        OpportunityFactory(lead=lead, title=f"India deal {i}", account_name="India Labs")
        TaskFactory(lead=lead, title=f"Call India lab {i}")
        MeetingFactory(lead=lead, title=f"India review {i}")
        NoteFactory(lead=lead, description=f"Price for India {i}: " + "words " * 400)
    client, workspace = (signed_in(user_a), "me") if who == "owner" else (signed_in(admin), "all")
    response = client.get(search_url("india", workspace))
    body = response.json()
    assert {g: len(body[g]["results"]) for g in GROUPS} == dict.fromkeys(GROUPS, selectors.LIMIT)
    assert all(body[g]["has_more"] for g in GROUPS)
    assert len(response.content) < 20_000  # notes are bounded previews, never whole notes


# --- bounded input -----------------------------------------------------------------------------
@pytest.mark.django_db
class TestInputIsBoundedBeforeAnythingIsSearched:
    def test_a_huge_query_is_refused_without_searching(self, user_a):
        client = signed_in(user_a)
        with CaptureQueriesContext(connection) as captured:
            response = client.get(search_url("rahul " * 2_000))
        assert response.status_code == 400
        assert len(captured) == 2  # the session and the user
        assert not searched(captured)

    def test_only_the_first_five_different_words_are_searched(self, user_a):
        words = [f"word{n:02d}" for n in range(40)]
        body = search(signed_in(user_a), " ".join(words)[:100])
        assert body["terms"] == words[:5]

    def test_case_and_whitespace_are_normalised(self, user_a):
        lead = LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
        body = search(signed_in(user_a), "  rAHUL \t\n  SHARMA  ")
        assert body["query"] == "rAHUL SHARMA"
        assert [row["id"] for row in body["leads"]["results"]] == [str(lead.pk)]

    @pytest.mark.parametrize("q", ["%% __ ** :: && ||", "!! (( )) '' \"\" \\\\", "a an of to"])
    def test_words_of_punctuation_or_one_two_letters_alone_are_refused(self, user_a, q):
        """Nothing a trigram index can narrow by: refused, never a scan of every record."""
        with CaptureQueriesContext(connection) as captured:
            response = signed_in(user_a).get(search_url(q))
        assert response.status_code == 400
        assert not searched(captured)

    def test_like_and_tsquery_metacharacters_are_literal(self, user_a):
        lead = LeadFactory(owner=user_a, first_name="Rahul")
        client = signed_in(user_a)
        # Attached to the word, they must occur as typed: nothing does.
        glued = search(client, "rahul%_\\'\":&|!()*")
        assert all(not glued[g]["results"] for g in GROUPS)
        # On their own they only rank: the name is found.
        apart = search(client, "rahul %_\\'\":&|!()*")
        assert [row["id"] for row in apart["leads"]["results"]] == [str(lead.pk)]


# --- the throttle ------------------------------------------------------------------------------
def limit_searches(monkeypatch, rate: str) -> None:
    monkeypatch.setattr(
        ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "search": rate}
    )


@pytest.mark.django_db
class TestThrottle:
    def test_a_refused_search_searches_nothing(self, monkeypatch, admin, user_a):
        """Refused before the workspace is resolved (no audit) or any record is read."""
        limit_searches(monkeypatch, "1/min")
        client = signed_in(admin)
        assert client.get(search_url("rahul", str(user_a.pk))).status_code == 200
        with CaptureQueriesContext(connection) as captured:
            refused = client.get(search_url("rahul", str(user_a.pk)))
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limited"
        assert int(refused["Retry-After"]) >= 1
        assert len(captured) <= 2  # the session and the user
        assert not searched(captured)

    def test_the_budget_is_the_users_wherever_they_connect_from(self, monkeypatch, user_a, user_b):
        """Keyed by the user, not the address: changing address doesn't reset it, and a
        colleague behind the same office address keeps their own."""
        limit_searches(monkeypatch, "2/min")
        rahul, priya = signed_in(user_a), signed_in(user_b)

        def status(client, address):
            return client.get(search_url("abc"), REMOTE_ADDR=address).status_code

        assert [status(rahul, ip) for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [
            200,
            200,
            429,
        ]
        assert status(priya, "10.0.0.1") == 200
