"""Request context middleware, health probes and the error envelope."""

import logging

import pytest
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from rest_framework import exceptions as drf_exceptions

from arkray.core import health
from arkray.core.errors import ConflictError, NotFoundError
from arkray.core.exceptions import api_exception_handler
from arkray.core.middleware import RequestContextMiddleware, client_ip

pytestmark = pytest.mark.django_db


class TestRequestId:
    def test_generated_when_absent(self, client):
        response = client.get("/health/live")
        assert len(response["X-Request-ID"]) == 32

    def test_client_supplied_id_is_not_trusted_by_default(self, client, caplog):
        """Clients must not be able to choose (or replay) ids stored in audit records."""
        with caplog.at_level(logging.INFO, logger="arkray.access"):
            response = client.get("/api/v1/x", HTTP_X_REQUEST_ID="client-chosen-1234")
        assert response["X-Request-ID"] != "client-chosen-1234"
        (record,) = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert record.client_request_id == "client-chosen-1234"
        assert record.correlation_id == response["X-Request-ID"]

    @override_settings(TRUST_INCOMING_REQUEST_ID=True)
    def test_id_from_a_trusted_edge_proxy_is_adopted(self, client):
        response = client.get("/health/live", HTTP_X_REQUEST_ID="edge-proxy-1234")
        assert response["X-Request-ID"] == "edge-proxy-1234"

    @override_settings(TRUST_INCOMING_REQUEST_ID=True)
    def test_malformed_incoming_id_is_replaced(self, client):
        response = client.get("/health/live", HTTP_X_REQUEST_ID="bad id\r\ninjected")
        assert response["X-Request-ID"] != "bad id\r\ninjected"
        assert len(response["X-Request-ID"]) == 32

    def test_access_log_has_route_status_and_latency_but_no_query_string(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="arkray.access"):
            client.get("/api/v1/nothing-here?q=secret-search-term")
        (record,) = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert record.status == 404
        assert record.path == "/api/v1/nothing-here"
        assert isinstance(record.duration_ms, float)
        assert "secret-search-term" not in str(record.__dict__)

    def test_access_log_survives_a_failing_user_lookup(self, caplog):
        """If resolving the session user fails (DB down), the response still goes out."""

        class ExplodingUser:
            @property
            def is_authenticated(self):
                raise DatabaseError("database unavailable")

        def view(request):
            request.user = ExplodingUser()
            return HttpResponse(status=503)

        with caplog.at_level(logging.INFO, logger="arkray.access"):
            response = RequestContextMiddleware(view)(RequestFactory().get("/api/v1/x"))
        assert response.status_code == 503
        (record,) = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert record.user_id is None

    def test_logging_never_looks_up_a_user_the_request_did_not(self, caplog):
        """Whole-software audit: resolving the lazy user for the log line read the session
        store again (with the database down, a second connection attempt per request)."""
        from django.utils.functional import SimpleLazyObject

        lookups = []

        def view(request):
            request.user = SimpleLazyObject(lambda: lookups.append(1))
            return HttpResponse(status=503)

        with caplog.at_level(logging.INFO, logger="arkray.access"):
            RequestContextMiddleware(view)(RequestFactory().get("/api/v1/x"))
        assert lookups == []
        (record,) = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert record.user_id is None


class TestClientIp:
    def test_ignores_forwarded_header_without_trusted_proxies(self):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="1.2.3.4")
        assert client_ip(request) == "10.0.0.5"

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_uses_entry_appended_by_trusted_proxy(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9"
        )
        assert client_ip(request) == "203.0.113.9"  # the spoofable first entry is ignored

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_invalid_address_yields_none(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="not-an-ip"
        )
        assert client_ip(request) is None


class TestHealth:
    def test_live(self, client):
        response = client.get("/health/live")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    def test_ready_when_dependencies_are_up(self, client):
        response = client.get("/health/ready")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    def test_not_ready_without_database(self, client, monkeypatch):
        monkeypatch.setattr(health, "_database_ok", lambda: False)
        response = client.get("/health/ready")
        assert (response.status_code, response.json()) == (503, {"status": "unavailable"})

    def test_a_stalled_database_is_reported_within_the_probe_deadline(self, client, monkeypatch):
        """Whole-software audit: a paused database made readiness wait as long as the stall
        (it never said "not ready"). The check is bounded now."""
        import threading
        import time

        from arkray.core import metrics

        release = threading.Event()
        monkeypatch.setattr(metrics, "PROBE_TIMEOUT_S", 0.2)
        monkeypatch.setattr(health, "_select_one", lambda: release.wait(5))
        started = time.monotonic()
        try:
            response = client.get("/health/ready")
        finally:
            release.set()
        assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
        assert time.monotonic() - started < 1

    def test_cache_outage_is_degraded_but_still_ready(self, client, monkeypatch):
        monkeypatch.setattr(health, "_cache_ok", lambda: False)
        response = client.get("/health/ready")
        assert (response.status_code, response.json()) == (200, {"status": "degraded"})

    def test_probes_reject_writes(self, client):
        assert client.post("/health/live").status_code == 405


class TestErrorEnvelope:
    def test_unknown_route_returns_json_404(self, client):
        response = client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        body = response.json()["error"]
        assert body["code"] == "not_found"
        assert body["request_id"] == response["X-Request-ID"]

    def test_domain_errors_map_to_their_status(self):
        response = api_exception_handler(NotFoundError(), {})
        assert response.status_code == 404
        assert response.data["error"]["code"] == "not_found"
        assert api_exception_handler(ConflictError(), {}).status_code == 409

    def test_validation_errors_carry_field_details(self):
        response = api_exception_handler(
            drf_exceptions.ValidationError({"email": ["Invalid."]}), {}
        )
        assert response.status_code == 400
        assert response.data["error"]["code"] == "validation_error"
        assert response.data["error"]["details"] == {"email": ["Invalid."]}

    def test_throttling_maps_to_rate_limited(self):
        response = api_exception_handler(drf_exceptions.Throttled(wait=30), {})
        assert response.status_code == 429
        assert response.data["error"]["code"] == "rate_limited"

    def test_unexpected_exceptions_are_left_to_django(self):
        assert api_exception_handler(RuntimeError("bug"), {}) is None
