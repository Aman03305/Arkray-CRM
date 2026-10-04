"""Phase 10 metrics endpoint: hidden unless configured, bearer-token protected, low-cardinality
gauges that reflect the outbox, the database and Ask Arkray, and no CRM data."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.ai.models import Question, QuestionStatus
from arkray.core.models import OutboxEvent, OutboxStatus
from tests.factories import LeadFactory

pytestmark = pytest.mark.django_db

URL = "/health/metrics"
TOKEN = "metrics-token-for-tests-0123456789"


def scrape(token: str | None = TOKEN) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client().get(URL, headers=headers)


@pytest.fixture
def configured(settings):
    settings.METRICS_TOKEN = TOKEN


def test_without_a_configured_token_the_endpoint_does_not_exist(settings):
    settings.METRICS_TOKEN = ""
    assert scrape().status_code == 404
    assert scrape(None).status_code == 404


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("token", [None, "wrong", TOKEN + "x", TOKEN[:-1]])
def test_a_wrong_or_missing_token_is_a_404(token):
    response = scrape(token)
    assert response.status_code == 404
    assert b"arkray_" not in response.content


@pytest.mark.usefixtures("configured")
def test_gauges_reflect_the_outbox_and_carry_no_crm_data(user_a):
    LeadFactory(owner=user_a, first_name="METRICS-SECRET-LEAD")
    now = timezone.now()
    OutboxEvent.objects.create(topic="t", queue="email", available_at=now - timedelta(minutes=5))
    OutboxEvent.objects.create(topic="t", queue="email", status=OutboxStatus.DEAD, finished_at=now)
    OutboxEvent.objects.create(topic="t", queue="ai_index", available_at=now + timedelta(hours=1))
    response = scrape()
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    assert response["Cache-Control"].startswith("max-age=0")
    body = response.content.decode()
    assert 'arkray_outbox_events{queue="email",status="pending"} 1' in body
    assert 'arkray_outbox_events{queue="email",status="dead"} 1' in body
    assert 'arkray_outbox_events{queue="ai_index",status="pending"} 1' in body
    # Due work only: a backed-off event isn't "late".
    prefix = 'arkray_outbox_oldest_due_seconds{queue="email"}'
    email_age = next(line for line in body.splitlines() if line.startswith(prefix))
    assert float(email_age.split()[-1]) >= 299
    assert 'arkray_outbox_oldest_due_seconds{queue="ai_index"} 0' in body
    for name in (
        "arkray_db_connections",
        "arkray_db_max_connections",
        "arkray_broker_up",
        "arkray_cache_up",
        "arkray_ai_questions_pending",
        "arkray_ai_breaker_open",
    ):
        assert f"# TYPE {name} gauge" in body, name
    assert "METRICS-SECRET-LEAD" not in body
    assert str(user_a.pk) not in body


@pytest.mark.usefixtures("configured")
def test_work_that_no_worker_has_taken_is_reported(settings):
    """Whole-software audit: with a queue's workers stopped, its events sat in flight under
    the dispatch lease, and every figure (pending, oldest due) stayed at 0."""
    import uuid

    now = timezone.now()
    dispatch_lease = settings.OUTBOX_DISPATCH_LEASE_SECONDS
    OutboxEvent.objects.create(  # handed to the broker 10 minutes ago, never started
        topic="t",
        queue="ai_index",
        status=OutboxStatus.IN_FLIGHT,
        claim_token=uuid.uuid4(),
        locked_until=now + timedelta(seconds=dispatch_lease - 600),
    )
    OutboxEvent.objects.create(  # started by a worker: the short running lease
        topic="t",
        queue="email",
        status=OutboxStatus.IN_FLIGHT,
        claim_token=uuid.uuid4(),
        locked_until=now + timedelta(seconds=120),
    )
    body = scrape().content.decode()
    prefix = 'arkray_outbox_oldest_undelivered_seconds{queue="ai_index"}'
    waiting = next(line for line in body.splitlines() if line.startswith(prefix))
    assert 595 <= float(waiting.split()[-1]) <= 610
    assert 'arkray_outbox_oldest_undelivered_seconds{queue="email"} 0' in body


@pytest.mark.usefixtures("configured")
def test_ask_arkray_outcomes_by_status_and_error(user_a):
    from arkray.ai.models import Conversation, WorkspaceKind

    conversation = Conversation.objects.create(
        actor=user_a, workspace_kind=WorkspaceKind.SELF, subject=user_a
    )
    now = timezone.now()
    for status, error in [(QuestionStatus.FAILED, "timeout"), (QuestionStatus.ANSWERED, "")]:
        Question.objects.create(
            conversation=conversation,
            actor=user_a,
            text="QUESTION-TEXT-NEVER-IN-METRICS",
            status=status,
            error_code=error,
            mode="retrieval" if status == QuestionStatus.ANSWERED else "",
            answer={} if status == QuestionStatus.FAILED else {"blocks": []},
            started_at=now,
            finished_at=now,
            expires_at=now + timedelta(minutes=1),
        )
    body = scrape().content.decode()
    assert 'arkray_ai_questions_last_hour{status="failed",mode="",error="timeout"} 1' in body
    assert 'arkray_ai_questions_last_hour{status="answered",mode="retrieval",error=""} 1' in body
    assert "QUESTION-TEXT-NEVER-IN-METRICS" not in body


@pytest.mark.usefixtures("configured")
def test_a_scrape_costs_a_constant_number_of_queries():
    scrape()
    with CaptureQueriesContext(connection) as few:
        scrape()
    for queue in ("default", "email", "ai", "outbox"):
        OutboxEvent.objects.bulk_create([OutboxEvent(topic="t", queue=queue) for _ in range(20)])
    with CaptureQueriesContext(connection) as many:
        scrape()
    assert len(many) == len(few) <= 8


@pytest.mark.usefixtures("configured")
def test_a_stalled_broker_probe_reports_it_down_instead_of_stalling_the_scrape(monkeypatch):
    """Phase 10 drill: with Redis's container stopped each probe stalled about 4 s in DNS
    (socket timeouts don't cover it) and 5 s scrapes timed out, during the very outage
    they should report."""
    import time

    import redis

    from arkray.core import metrics

    class Stalled:
        def pipeline(self):
            return self

        def llen(self, _name):
            return self

        def execute(self):
            time.sleep(1.5)
            return []

    monkeypatch.setattr(metrics, "PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *_a, **_k: Stalled()))
    started = time.monotonic()
    response = scrape()
    assert time.monotonic() - started < 1.0
    assert response.status_code == 200
    assert "arkray_broker_up 0" in response.content.decode()


# --- Phase 10 review ------------------------------------------------------------------------
@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("method", ["post", "put", "delete", "options", "head"])
def test_other_methods_are_a_plain_404_even_with_the_token(method):
    """Was a 405 with `Allow: GET` (from require_GET), which gave the endpoint away."""
    client = Client(enforce_csrf_checks=True)  # as in production: the CSRF check's 403 too
    response = getattr(client, method)(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404
    assert "Allow" not in response


@pytest.mark.usefixtures("configured")
def test_the_bearer_scheme_is_case_insensitive_and_a_wrong_token_is_logged(caplog):
    assert Client().get(URL, headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200
    with caplog.at_level("WARNING", logger="config.metrics"):
        assert scrape("guessed-token-value").status_code == 404
    messages = [r.getMessage() for r in caplog.records if r.name == "config.metrics"]
    assert messages == ["metrics_token_rejected"]
    assert "guessed-token-value" not in caplog.text


@pytest.mark.usefixtures("configured")
def test_a_failing_gauge_is_dropped_alone_and_the_database_stays_up(monkeypatch, caplog):
    """A statement timeout (or a table not migrated yet) is not an outage."""
    import psycopg
    from django.db import OperationalError

    from arkray.core import metrics

    def timed_out():
        try:
            raise OperationalError("canceling statement") from psycopg.errors.QueryCanceled()
        except OperationalError as exc:
            raise exc from exc.__cause__

    monkeypatch.setattr(metrics, "outbox", timed_out)
    with caplog.at_level("WARNING", logger="config.metrics"):
        body = scrape().content.decode()
    assert "arkray_db_up 1" in body
    assert "arkray_outbox_events" not in body
    assert "arkray_db_connections" in body
    assert "metrics_gauge_failed" in [r.getMessage() for r in caplog.records]


@pytest.mark.usefixtures("configured")
def test_the_ai_breaker_read_is_bounded_too(monkeypatch):
    """It reads the cache on the request thread: a stalled cache stalled the scrape."""
    import time

    from arkray.ai import breaker
    from arkray.core import metrics

    monkeypatch.setattr(metrics, "PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(breaker, "is_open", lambda: time.sleep(1.5) or False)
    started = time.monotonic()
    body = scrape().content.decode()
    assert time.monotonic() - started < 1.0
    samples = [x for x in body.splitlines() if x.startswith("arkray_ai_breaker_open")]
    assert samples == []  # unknown: no sample rather than "closed"


@pytest.mark.usefixtures("configured")
def test_the_server_wide_connection_count_is_reported():
    body = scrape().content.decode()
    line = next(x for x in body.splitlines() if x.startswith("arkray_db_server_connections "))
    assert float(line.split()[1]) >= 1


def test_each_outbox_count_reads_its_partial_index():
    """Was one `status IN (...)` over the whole table, done rows included (Phase 10 review):
    every scrape got slower as the outbox grew."""
    from django.db import transaction

    from arkray.core import metrics

    OutboxEvent.objects.bulk_create(
        [OutboxEvent(topic="t", queue="default", status=OutboxStatus.PENDING) for _ in range(3)]
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        with CaptureQueriesContext(connection) as captured:
            metrics.outbox()
        for query in captured.captured_queries:
            cursor.execute(f"EXPLAIN {query['sql']}")
            plan = "\n".join(row[0] for row in cursor.fetchall())
            assert "Seq Scan on core_outbox_event" not in plan, plan
