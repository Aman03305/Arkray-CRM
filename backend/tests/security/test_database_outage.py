"""A database outage answers 503 service_unavailable with Retry-After, not a generic 500
(Phase 10 drill: PostgreSQL stopped under a live stack). Only connection-level failures:
a statement timeout or a deadlock is a fault to fix, and stays a 500."""

from __future__ import annotations

from unittest import mock

import psycopg
import pytest
from django.db import IntegrityError, InterfaceError, OperationalError
from django.test import Client
from psycopg_pool import PoolTimeout

from arkray.core.views import DATABASE_RETRY_AFTER_S, database_unavailable

pytestmark = pytest.mark.django_db


def wrapped(cause: BaseException) -> OperationalError:
    """As Django's DatabaseErrorWrapper raises it: the driver's error as the cause."""
    try:
        raise OperationalError(str(cause)) from cause
    except OperationalError as exc:
        return exc


def failing_with(exc: BaseException):
    def fail(*_args, **_kwargs):
        raise exc

    return fail


@pytest.mark.parametrize(
    ("exc", "unavailable"),
    [
        (wrapped(psycopg.OperationalError("connection refused")), True),
        (wrapped(psycopg.errors.AdminShutdown("terminating connection")), True),
        (wrapped(psycopg.errors.CannotConnectNow("the database system is starting up")), True),
        (wrapped(psycopg.errors.TooManyConnections("too many clients")), True),
        (wrapped(psycopg.errors.ConnectionFailure("connection failure")), True),
        # Phase 10 review, P2: a host that never answers, and an exhausted pool.
        (wrapped(psycopg.errors.ConnectionTimeout("connection timeout expired")), True),
        (wrapped(PoolTimeout("couldn't get a connection after 30.00 sec")), True),
        (InterfaceError("connection already closed"), True),
        (wrapped(psycopg.errors.QueryCanceled("statement timeout")), False),
        (wrapped(psycopg.errors.DeadlockDetected("deadlock detected")), False),
        (wrapped(psycopg.errors.LockNotAvailable("lock timeout")), False),
        (OperationalError("no cause"), False),
        (IntegrityError("duplicate key"), False),
        (ValueError("unrelated"), False),
        (None, False),
    ],
)
def test_only_connection_level_failures_count_as_an_outage(exc, unavailable):
    assert database_unavailable(exc) is unavailable


def test_an_outage_in_the_middleware_is_a_503_with_retry_after():
    """The drill's failure came from the session middleware, before any view ran."""
    outage = wrapped(psycopg.OperationalError("could not translate host name"))
    with mock.patch(
        "arkray.identity.sessions.SessionPolicyMiddleware.__call__", failing_with(outage)
    ):
        response = Client(raise_request_exception=False).get("/api/v1/auth/me")
    assert response.status_code == 503
    assert response["Retry-After"] == str(DATABASE_RETRY_AFTER_S)
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    assert error["request_id"]
    assert "host name" not in response.content.decode()  # no driver text reaches clients


def test_an_outage_inside_an_api_view_is_a_503(user_a):
    from tests.helpers import signed_in

    client = signed_in(user_a)
    client.raise_request_exception = False
    outage = wrapped(psycopg.errors.AdminShutdown("terminating connection"))
    with mock.patch("arkray.leads.api.views.LeadListView.get", failing_with(outage)):
        response = client.get("/api/v1/workspaces/me/leads")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_a_statement_timeout_stays_a_500(user_a):
    from tests.helpers import signed_in

    client = signed_in(user_a)
    client.raise_request_exception = False
    timeout = wrapped(psycopg.errors.QueryCanceled("canceling statement due to timeout"))
    with mock.patch("arkray.leads.api.views.LeadListView.get", failing_with(timeout)):
        response = client.get("/api/v1/workspaces/me/leads")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "server_error"
    assert "Retry-After" not in response


def test_the_metrics_scrape_reports_the_database_down_and_keeps_the_rest(settings):
    """The drill's scrape was a 500 while PostgreSQL was down, losing the broker and cache
    gauges with it."""
    settings.METRICS_TOKEN = "metrics-token-for-tests-0123456789"
    outage = wrapped(psycopg.OperationalError("connection refused"))
    with mock.patch("arkray.core.metrics.outbox", failing_with(outage)):
        response = Client().get(
            "/health/metrics", headers={"Authorization": f"Bearer {settings.METRICS_TOKEN}"}
        )
    body = response.content.decode()
    assert response.status_code == 200
    assert "arkray_db_up 0" in body
    assert "arkray_broker_up" in body
    assert "arkray_cache_up" in body
    assert "arkray_outbox_events" not in body
