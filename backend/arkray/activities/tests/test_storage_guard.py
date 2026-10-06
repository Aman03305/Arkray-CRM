"""The guard around every attachment storage call (final audit SRE-4; docs/reliability.md#
attachment-storage): errors classified, a deadline, a cap on calls in flight, a breaker that
trips on outages only and recovers in the background; and the gauges built on it."""

from __future__ import annotations

import errno
import threading
import time
from datetime import timedelta

import pytest
from botocore import exceptions as boto
from django.core.cache import cache
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities import metrics, reconcile, storage, telemetry
from arkray.activities.models import Attachment
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory

TOKEN = "metrics-token-for-tests-0123456789"


@pytest.fixture(autouse=True)
def fresh_guard(settings):
    settings.ATTACHMENT_STORAGE_DEADLINE_S = 0.5
    settings.ATTACHMENT_STORAGE_BREAKER_FAILURES = 3
    settings.ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S = 0.3
    settings.ATTACHMENT_STORAGE_MAX_IN_FLIGHT = 2
    storage.reset_guard()
    storage.forget_probe()
    yield
    storage.reset_guard()
    storage.forget_probe()


def client_error(status: int, code: str) -> boto.ClientError:
    return boto.ClientError(
        {"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


def raising(error: BaseException):
    def work(_cancel):
        raise error

    return work


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (client_error(404, "NoSuchKey"), "missing"),
        (client_error(404, "404"), "missing"),
        (client_error(403, "AccessDenied"), "access_denied"),
        (client_error(500, "InternalError"), "server_error"),
        (client_error(503, "SlowDown"), "server_error"),
        (client_error(507, "XMinioStorageFull"), "no_space"),
        (client_error(400, "InvalidArgument"), "other"),
        (boto.ConnectTimeoutError(endpoint_url="http://x"), "timeout"),
        (boto.ReadTimeoutError(endpoint_url="http://x"), "timeout"),
        (boto.EndpointConnectionError(endpoint_url="http://x"), "connection"),
        (boto.ConnectionClosedError(endpoint_url="http://x"), "connection"),
        (boto.ResponseStreamingError(error="reset"), "connection"),
        (boto.NoCredentialsError(), "access_denied"),
        (FileNotFoundError(), "missing"),
        (PermissionError(), "access_denied"),
        (OSError(errno.ENOSPC, "full"), "no_space"),
        (TimeoutError(), "timeout"),
        (ConnectionResetError(), "connection"),
        (ValueError("?"), "other"),
    ],
)
def test_errors_are_classified(error, expected):
    assert storage.classify(error) == expected


def test_a_wrapped_error_is_classified_by_its_cause():
    outer = RuntimeError("s3transfer said no")
    outer.__cause__ = client_error(403, "AccessDenied")
    assert storage.classify(outer) == "access_denied"


class TestBreaker:
    def test_trips_after_outage_failures_in_a_row_only(self):
        outage = raising(boto.EndpointConnectionError(endpoint_url="http://x"))
        for _ in range(2):
            with pytest.raises(storage.StorageUnavailable):
                storage._call("save", outage)
        storage._call("save", lambda _c: None)  # a success resets the count
        for _ in range(2):
            with pytest.raises(storage.StorageUnavailable):
                storage._call("save", outage)
        assert not storage.breaker_open()
        with pytest.raises(storage.StorageUnavailable):
            storage._call("save", outage)
        assert storage.breaker_open()
        called = []
        started = time.monotonic()
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("save", lambda _c: called.append(1))
        assert failed.value.error_class == "circuit_open"
        assert called == []
        assert time.monotonic() - started < 0.05

    @pytest.mark.parametrize(
        "error", [client_error(404, "NoSuchKey"), client_error(403, "AccessDenied")]
    )
    def test_answers_never_trip_it(self, error):
        for _ in range(6):
            with pytest.raises(storage.StorageUnavailable):
                storage._call("open", raising(error))
        assert not storage.breaker_open()

    def test_a_missing_object_is_its_own_error(self):
        with pytest.raises(storage.ObjectMissing):
            storage._call("open", raising(client_error(404, "NoSuchKey")))

    def test_recovers_by_a_background_check(self, monkeypatch):
        probes = []
        monkeypatch.setattr(storage, "_probe", lambda: probes.append(threading.current_thread()))
        outage = raising(TimeoutError())
        for _ in range(3):
            with pytest.raises(storage.StorageUnavailable):
                storage._call("save", outage)
        assert storage.breaker_open()
        time.sleep(0.35)
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("save", lambda _c: None)  # starts the check, fails fast
        assert failed.value.error_class == "circuit_open"
        deadline = time.monotonic() + 3
        while storage.breaker_open() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not storage.breaker_open()
        assert probes
        assert probes[0] is not threading.current_thread()
        storage._call("save", lambda _c: None)


class TestBounds:
    def test_a_stalled_call_costs_the_deadline_and_is_told_to_stop(self):
        told: list[bool] = []

        def stalled(cancel):
            told.append(cancel.wait(5))

        started = time.monotonic()
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("open", stalled)
        assert failed.value.error_class == "timeout"
        assert time.monotonic() - started < 0.5 + 0.3
        deadline = time.monotonic() + 2
        while not told and time.monotonic() < deadline:
            time.sleep(0.01)
        assert told == [True]

    def test_calls_beyond_the_cap_fail_at_once(self):
        release = threading.Event()
        started = threading.Barrier(3)

        def busy(_cancel):
            started.wait(2)
            release.wait(2)

        threads = [threading.Thread(target=lambda: storage._call("save", busy)) for _ in range(2)]
        for thread in threads:
            thread.start()
        started.wait(2)
        t0 = time.monotonic()
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("save", lambda _c: None)
        assert failed.value.error_class == "busy"
        assert time.monotonic() - t0 < 0.05
        release.set()
        for thread in threads:
            thread.join(3)
        storage._call("save", lambda _c: None)  # slots are given back


class TestShared:
    """The breaker and the cap shared by every process through the cache (capacity test: a
    per-process breaker let a black-holed store hold all 8 web workers). Another process is
    simulated by its traces in the cache."""

    def test_another_process_tripping_the_breaker_stops_this_one_at_once(self):
        outage = raising(boto.EndpointConnectionError(endpoint_url="http://x"))
        # one failure here; two "elsewhere" (the shared count is the deployment's)
        cache.set(storage._SHARED_FAILURES, 2, timeout=5)
        with pytest.raises(storage.StorageUnavailable):
            storage._call("save", outage)
        assert storage.breaker_open()
        called = []
        started = time.monotonic()
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("save", lambda _c: called.append(1))
        assert failed.value.error_class == "circuit_open"
        assert called == []
        assert time.monotonic() - started < 0.05

    def test_an_open_shared_breaker_expires_with_its_cool_down(self):
        cache.set(storage._SHARED_OPEN, 1, timeout=1)
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("open", lambda _c: None)
        assert failed.value.error_class == "circuit_open"
        cache.delete(storage._SHARED_OPEN)  # the cool-down ran out
        assert storage._call("open", lambda _c: "fine") == "fine"

    def test_a_success_anywhere_resets_the_outage_count(self):
        cache.set(storage._SHARED_FAILURES, 2, timeout=5)
        storage._call("save", lambda _c: None)
        with pytest.raises(storage.StorageUnavailable):
            storage._call("save", raising(TimeoutError()))
        assert not storage.breaker_open()

    def test_calls_beyond_the_deployment_wide_cap_fail_at_once(self, settings):
        settings.ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED = 3
        cache.set(storage._SHARED_IN_FLIGHT, 3, timeout=30)  # three in flight elsewhere
        called = []
        started = time.monotonic()
        with pytest.raises(storage.StorageUnavailable) as failed:
            storage._call("save", lambda _c: called.append(1))
        assert failed.value.error_class == "busy"
        assert called == []
        assert time.monotonic() - started < 0.05
        assert cache.get(storage._SHARED_IN_FLIGHT) == 3  # the refusal took no slot
        cache.set(storage._SHARED_IN_FLIGHT, 2, timeout=30)  # one finished
        storage._call("save", lambda _c: called.append(1))
        assert called == [1]
        assert cache.get(storage._SHARED_IN_FLIGHT) == 2  # given back

    def test_without_the_cache_each_process_keeps_its_own(self, monkeypatch):
        def down(*_args, **_kwargs):
            raise ConnectionError("redis down")

        for name in ("get", "add", "incr", "decr", "set", "delete"):
            monkeypatch.setattr(storage.cache, name, down)
        assert storage._call("save", lambda _c: "fine") == "fine"
        outage = raising(TimeoutError())
        for _ in range(3):
            with pytest.raises(storage.StorageUnavailable):
                storage._call("save", outage)
        assert storage.breaker_open()  # this process's own breaker


# --- the gauges -------------------------------------------------------------------------------
@pytest.mark.django_db
class TestGauges:
    @pytest.fixture
    def rows(self, user_a):
        lead = LeadFactory(owner=user_a)
        note = NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)
        now = timezone.now()

        def row(key, **fields):
            return Attachment.objects.create(
                note=note,
                original_name="SECRET-CUSTOMER-FILE.pdf",
                extension="pdf",
                content_type="application/pdf",
                size=1,
                sha256="0" * 64,
                storage_key=key,
                uploaded_by=user_a,
                **fields,
            )

        row("k/1", state="uploading", created_at=now - timedelta(minutes=90))
        row("k/2", state="uploading", created_at=now - timedelta(minutes=1))
        row("k/3", state="failed", created_at=now - timedelta(hours=3))
        row("k/4", state="stored", scan_status="pending", created_at=now)
        row("k/5", state="stored", created_at=now, deleted_at=now)
        row("k/6", state="stored", created_at=now)
        return note

    def test_rows_by_state_and_oldest_ages(self, settings, rows):
        settings.METRICS_TOKEN = TOKEN
        body = Client().get("/health/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        text = body.content.decode()
        assert 'arkray_attachments{state="uploading"} 2' in text
        assert 'arkray_attachments{state="failed"} 1' in text
        assert 'arkray_attachments{state="pending_scan"} 1' in text
        uploading = next(x for x in text.splitlines() if 'oldest_seconds{state="uploading"}' in x)
        assert 5390 <= float(uploading.split()[-1]) <= 5420
        unpurged = next(x for x in text.splitlines() if 'oldest_seconds{state="unpurged"}' in x)
        assert float(unpurged.split()[-1]) >= 3 * 3600 - 10
        assert 'arkray_attachment_storage_up{backend="filesystem"} 1' in text
        assert "SECRET-CUSTOMER-FILE" not in text
        assert "k/1" not in text
        assert str(rows.owner_id) not in text

    def test_the_gauges_read_the_partial_index(self, rows):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            with CaptureQueriesContext(connection) as captured:
                metrics.gauges()
            (query,) = captured.captured_queries
            cursor.execute(f"EXPLAIN {query['sql']}")
            plan = "\n".join(line[0] for line in cursor.fetchall())
        assert "Seq Scan on activities_attachment" not in plan, plan

    def test_the_last_reconciliation_is_reported(self, settings, rows, tmp_path):
        settings.METRICS_TOKEN = TOKEN
        settings.STORAGES = {
            **settings.STORAGES,
            "attachments": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path), "base_url": None},
            },
        }
        reconcile.run(reconcile.Options(retry_pause_s=0))
        text = (
            Client()
            .get("/health/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
            .content.decode()
        )
        assert 'arkray_attachment_reconcile_outcome{mode="check"} 1' in text
        assert 'arkray_attachment_reconcile_mismatches{kind="missing"} 2' in text  # k/4, k/6
        assert 'arkray_attachment_reconcile_mismatches{kind="stale_pending"} 1' in text
        age = next(x for x in text.splitlines() if x.startswith("arkray_attachment_reconcile_age"))
        assert float(age.split()[-1]) < 60

    def test_a_cache_that_cant_answer_leaves_the_counters_out(self, settings, monkeypatch):
        settings.METRICS_TOKEN = TOKEN

        def down():
            raise ConnectionError

        monkeypatch.setattr(telemetry, "last_hour", down)
        text = (
            Client()
            .get("/health/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
            .content.decode()
        )
        assert "arkray_attachment_uploads_last_hour" not in text
        assert "arkray_attachment_storage_up" in text
        assert "arkray_db_up 1" in text

    def test_a_storage_probe_that_hangs_reports_down_within_the_deadline(
        self, settings, monkeypatch
    ):
        from arkray.core import metrics as core_metrics

        settings.METRICS_TOKEN = TOKEN
        monkeypatch.setattr(core_metrics, "PROBE_TIMEOUT_S", 0.2)
        monkeypatch.setattr(storage, "_probe", lambda: time.sleep(1.5))
        started = time.monotonic()
        text = (
            Client()
            .get("/health/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
            .content.decode()
        )
        assert time.monotonic() - started < 1.0
        assert 'arkray_attachment_storage_up{backend="filesystem"} 0' in text
