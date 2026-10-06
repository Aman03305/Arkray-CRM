"""Attachment storage failures, end to end (final audit SRE-4, R105; docs/testing.md#attachment-
storage-faults): the real API, the real django-storages S3Storage and botocore client with the
production options (config.settings.base.attachment_s3_backend), against a local fake S3
endpoint that fails on request (tests/integration/fake_s3.py), and the filesystem backend
with failing disks. For each fault: the time it costs (bounded), what the API answers, the
row's state afterwards (never "stored" without a whole object, never downloadable after a
failed write), the reason in logs and metrics, nothing about the bucket, endpoint or key in
the response or the logs, the breaker, and recovery."""

from __future__ import annotations

import errno
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, storages
from django.db import connection
from django.test import Client
from django.utils import timezone

from arkray.activities import attachments, storage, telemetry
from arkray.activities.models import Attachment
from config.settings.base import attachment_s3_backend
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in
from tests.integration.fake_s3 import FakeS3, closed_port, fault

pytestmark = pytest.mark.django_db

DEADLINE = 2.5
SLACK = 1.5  # scheduling on a busy machine; the bound is what matters, not the exact figure
TOKEN = "metrics-token-for-tests-0123456789"


def s3_backend(endpoint: str, bucket: str = "arkray-fault-tests") -> dict[str, Any]:
    return attachment_s3_backend(
        bucket=bucket,
        endpoint_url=endpoint,
        region="us-east-1",
        access_key="fault-test-access-key",
        secret_key="fault-test-secret-key",
        connect_timeout_s=1,
        read_timeout_s=1,
        max_attempts=2,
    )


@pytest.fixture
def guarded(settings):
    settings.ATTACHMENT_STORAGE_DEADLINE_S = DEADLINE
    settings.ATTACHMENT_STORAGE_BREAKER_FAILURES = 3
    settings.ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S = 0.5
    settings.ATTACHMENT_STORAGE_MAX_IN_FLIGHT = 4
    settings.METRICS_TOKEN = TOKEN
    storage.reset_guard()
    storage.forget_probe()
    yield settings
    storage.reset_guard()
    storage.forget_probe()


@pytest.fixture
def fake_s3():
    server = FakeS3()
    yield server
    server.close()


@pytest.fixture
def on_s3(guarded, fake_s3):
    guarded.STORAGES = {**guarded.STORAGES, "attachments": s3_backend(fake_s3.endpoint)}
    return fake_s3


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


def upload(client, note, content=b"quarterly figures\n", name="figures.txt"):
    started = time.monotonic()
    response = client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )
    return response, time.monotonic() - started


def download(client, attachment_id):
    started = time.monotonic()
    response = client.get(f"/api/v1/workspaces/me/attachments/{attachment_id}/download")
    content = b"".join(response.streaming_content) if response.status_code == 200 else b""
    return response, content, time.monotonic() - started


def scrape() -> str:
    response = Client().get("/health/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    return response.content.decode()


def sample(body: str, line_start: str) -> float:
    return float(next(x for x in body.splitlines() if x.startswith(line_start)).split()[-1])


def assert_no_leak(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text, secret


def assert_storage_down(response, elapsed: float) -> None:
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert response["Retry-After"] == str(storage.STORAGE_RETRY_AFTER_S)
    assert elapsed < DEADLINE + SLACK


def in_thread(work, *args):
    """Run `work` on a pool thread and close that thread's database connection after."""
    try:
        return work(*args)
    finally:
        connection.close()


def only_row() -> Attachment:
    return Attachment.objects.get()


class TestS3Healthy:
    def test_upload_and_download_through_the_real_client(self, on_s3, user_a_client, note):
        response, _ = upload(user_a_client, note, b"x" * 70_000)
        assert response.status_code == 201
        row = only_row()
        assert row.state == "stored"
        assert on_s3.objects[f"attachments/{row.storage_key}"] == b"x" * 70_000
        got, content, _ = download(user_a_client, row.pk)
        assert (got.status_code, content) == (200, b"x" * 70_000)
        body = scrape()
        assert 'arkray_attachment_storage_up{backend="s3"} 1' in body
        assert 'arkray_attachment_uploads_last_hour{outcome="stored",reason=""} 1' in body

    def test_a_file_near_the_size_limit_goes_up_in_parts(self, on_s3, user_a_client, note):
        """Above 8 MB s3transfer uploads in parts: the deadline, the cancellable source
        and the size read back work on that path too."""
        content = b"%PDF-1.7 " + b"p" * (9 * 1024 * 1024)
        response, _ = upload(user_a_client, note, content, name="contract.pdf")
        assert response.status_code == 201
        row = only_row()
        assert on_s3.count("POST") == 2  # initiate, complete
        assert on_s3.objects[f"attachments/{row.storage_key}"] == content
        got, body, _ = download(user_a_client, row.pk)
        assert (got.status_code, len(body)) == (200, len(content))

    def test_a_slow_answer_within_the_timeouts_still_works(self, on_s3, user_a_client, note):
        on_s3.once(fault("slow", 0.4, "PUT"))
        response, elapsed = upload(user_a_client, note)
        assert response.status_code == 201
        assert 0.4 <= elapsed < DEADLINE


class TestS3Faults:
    @pytest.mark.parametrize(
        ("standing", "error_class"),
        [
            (fault("403", 0, "PUT"), "access_denied"),
            (fault("500", 0, "PUT"), "server_error"),
            (fault("503", 0, "PUT"), "server_error"),
            (fault("hang", 0, "PUT"), "timeout"),
            (fault("drop", 0, "PUT"), "connection"),
        ],
    )
    def test_a_failed_write_is_a_503_and_never_a_stored_row(
        self, on_s3, user_a_client, note, caplog, standing, error_class
    ):
        on_s3.always(standing)
        with caplog.at_level("INFO"):
            response, elapsed = upload(user_a_client, note)
        assert_storage_down(response, elapsed)
        row = only_row()
        assert row.state == "failed"
        assert download(user_a_client, row.pk)[0].status_code == 404
        failures = [r for r in caplog.records if r.getMessage() == "attachment_storage_failed"]
        assert failures
        assert failures[-1].error_class == error_class
        assert failures[-1].backend == "s3"
        secrets = (on_s3.bucket, on_s3.endpoint.split("//")[1], row.storage_key)
        assert_no_leak(response.content.decode(), *secrets)
        assert_no_leak(caplog.text, *secrets)
        reason = "timeout" if error_class == "timeout" else "storage_error"
        assert f'outcome="failed",reason="{reason}"}} 1' in scrape()

    def test_a_write_the_store_kept_only_part_of_is_never_stored(self, on_s3, user_a_client, note):
        """The PUT answered 200 but the object is short: the size read back catches it."""
        on_s3.once(fault("truncate", 0, "PUT"))
        response, elapsed = upload(user_a_client, note, b"y" * 10_000)
        assert_storage_down(response, elapsed)
        row = only_row()
        assert row.state == "failed"
        assert len(on_s3.objects[f"attachments/{row.storage_key}"]) == 5_000

    def test_connection_refused(self, guarded, user_a_client, note):
        guarded.STORAGES = {
            **guarded.STORAGES,
            "attachments": s3_backend(f"http://127.0.0.1:{closed_port()}"),
        }
        response, elapsed = upload(user_a_client, note)
        assert_storage_down(response, elapsed)
        assert only_row().state == "failed"

    def test_a_black_holed_endpoint_costs_at_most_the_deadline(self, guarded, user_a_client, note):
        import socket

        probe = socket.socket()
        probe.settimeout(0.5)
        started = time.monotonic()
        try:
            probe.connect(("10.255.255.1", 9))
        except TimeoutError:
            pass
        except OSError:
            if time.monotonic() - started < 0.4:
                pytest.skip("this host answers 10.255.255.1 at once: no black hole to test")
        finally:
            probe.close()
        guarded.STORAGES = {
            **guarded.STORAGES,
            "attachments": s3_backend("http://10.255.255.1:9"),
        }
        response, elapsed = upload(user_a_client, note)
        assert_storage_down(response, elapsed)
        assert only_row().state == "failed"

    def test_a_lost_object_is_a_410_not_an_outage(self, on_s3, user_a_client, note, caplog):
        upload(user_a_client, note)
        row = only_row()
        del on_s3.objects[f"attachments/{row.storage_key}"]
        for _ in range(4):  # more than the breaker's threshold: a 404 isn't an outage
            with caplog.at_level("INFO"):
                got, _, _ = download(user_a_client, row.pk)
            assert got.status_code == 410
            assert got.json()["error"]["code"] == "attachment_unavailable"
            assert got.json()["error"]["message"] == (
                "This file is no longer available. Ask an administrator."
            )
            assert "Retry-After" not in got
        assert not storage.breaker_open()
        row.refresh_from_db()
        assert row.state == "stored"  # the download path writes nothing
        missing = [r for r in caplog.records if r.getMessage() == "attachment_object_missing"]
        assert missing
        assert missing[0].attachment_id == str(row.pk)
        assert_no_leak(caplog.text, row.storage_key, "figures.txt", on_s3.bucket)
        body = scrape()
        assert "arkray_attachment_objects_missing_last_hour 4" in body
        assert 'arkray_attachment_download_failures_last_hour{reason="missing"} 4' in body

    @pytest.mark.parametrize("kind", ["slow_body", "drop"])
    def test_a_download_that_stalls_or_breaks_mid_body_is_a_503_not_a_cut_off_200(
        self, on_s3, user_a_client, note, kind
    ):
        upload(user_a_client, note, b"z" * 200_000)
        row = only_row()
        on_s3.always(fault(kind, 30, "GET"))
        got, _, elapsed = download(user_a_client, row.pk)
        assert_storage_down(got, elapsed)
        on_s3.heal()
        storage.reset_guard()
        got, content, _ = download(user_a_client, row.pk)
        assert (got.status_code, content) == (200, b"z" * 200_000)

    def test_a_deleted_file_whose_purge_fails_stays_hidden_and_is_purged_later(
        self, on_s3, user_a_client, note
    ):
        upload(user_a_client, note)
        row = only_row()
        assert (
            user_a_client.delete(f"/api/v1/workspaces/me/attachments/{row.pk}").status_code == 204
        )
        on_s3.always(fault("500", 0, "DELETE"))
        drain_outbox(rounds=1)  # the purge job fails and is retried later
        row.refresh_from_db()
        assert (row.deleted_at is not None, row.purged_at) == (True, None)
        assert download(user_a_client, row.pk)[0].status_code == 404
        on_s3.heal()
        storage.reset_guard()
        assert attachments.housekeeping(timezone.now())["purged"] == 1
        assert f"attachments/{row.storage_key}" not in on_s3.objects


class TestBreaker:
    def test_opens_after_repeated_outages_fails_fast_and_recovers(self, on_s3, user_a_client, note):
        on_s3.always(fault("503"))
        for _ in range(3):
            response, elapsed = upload(user_a_client, note)
            assert_storage_down(response, elapsed)
        assert storage.breaker_open()
        calls = on_s3.count("PUT") + on_s3.count("GET")
        response, elapsed = upload(user_a_client, note)
        assert_storage_down(response, elapsed)
        assert elapsed < 0.5  # no network call at all
        assert on_s3.count("PUT") + on_s3.count("GET") == calls
        on_s3.heal()
        # After the cool-down, the next call starts the background check and still fails
        # fast; the check closes the breaker, and the next upload works.
        time.sleep(0.6)
        response, elapsed = upload(user_a_client, note)
        assert response.status_code == 503
        assert elapsed < 0.5
        deadline = time.monotonic() + 5
        while storage.breaker_open() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not storage.breaker_open()
        response, _ = upload(user_a_client, note)
        assert response.status_code == 201
        assert Attachment.objects.filter(state="stored").count() == 1
        storage.forget_probe()
        assert 'arkray_attachment_storage_up{backend="s3"} 1' in scrape()

    def test_a_missing_object_or_a_refusal_never_opens_it(self, on_s3, user_a_client, note):
        on_s3.always(fault("403", 0, "PUT"))
        for _ in range(5):
            assert upload(user_a_client, note)[0].status_code == 503
        assert not storage.breaker_open()
        assert on_s3.count("PUT") == 5  # each one asked the store


class TestCrmUnaffected:
    @pytest.mark.django_db(transaction=True)
    def test_other_endpoints_answer_quickly_while_storage_hangs(
        self, crm_configuration, on_s3, user_a, user_a_client, note
    ):
        on_s3.always(fault("hang", 30, "PUT"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            hanging = pool.submit(in_thread, upload, user_a_client, note)
            time.sleep(0.3)  # the upload is now waiting on storage
            other = signed_in(user_a)
            for path in ("/api/v1/auth/me", "/api/v1/workspaces/me/dashboard"):
                started = time.monotonic()
                assert other.get(path).status_code == 200
                assert time.monotonic() - started < 1.5, path
            response, elapsed = hanging.result()
        assert_storage_down(response, elapsed)

    def test_storage_down_is_a_degraded_signal_never_unreadiness(self, on_s3):
        on_s3.always(fault("hang", 30))
        ready = Client().get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() in ({"status": "ok"}, {"status": "degraded"})
        started = time.monotonic()
        body = scrape()
        assert time.monotonic() - started < 2 + SLACK  # the probe's deadline
        assert 'arkray_attachment_storage_up{backend="s3"} 0' in body
        assert "arkray_db_up 1" in body


@pytest.mark.django_db(transaction=True)
def test_parallel_uploads_to_a_dead_store_all_return_within_the_deadline(
    crm_configuration, on_s3, user_a
):
    """Twelve uploads at once (as from twelve threads of one process): none waits past the
    deadline; beyond the in-flight cap they fail at once, and once the breaker is open every
    later one fails in well under 100 ms, without a network call."""
    on_s3.always(fault("hang", 30))
    lead = LeadFactory(owner=user_a)
    note = NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)

    def one(_index: int) -> tuple[int, float]:
        response, elapsed = in_thread(lambda: upload(signed_in(user_a), note))
        return response.status_code, elapsed

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, range(12)))
    assert {status for status, _ in results} == {503}
    assert max(elapsed for _, elapsed in results) < DEADLINE + SLACK
    # The in-flight cap (4) turned the rest away at once (the session set-up is included).
    assert sum(1 for _, elapsed in results if elapsed < DEADLINE / 2) >= 12 - 4
    assert storage.breaker_open()
    calls = on_s3.count("PUT")
    for _ in range(5):
        assert one(0)[0] == 503
    started = time.monotonic()
    storage_calls = []
    for _ in range(20):
        t0 = time.monotonic()
        try:
            storage.size("2026/10/anything")
        except storage.StorageUnavailable as exc:
            storage_calls.append((exc.error_class, time.monotonic() - t0))
    assert {cls for cls, _ in storage_calls} == {"circuit_open"}
    assert max(seconds for _, seconds in storage_calls) < 0.1
    assert time.monotonic() - started < 1.0
    assert on_s3.count("PUT") == calls  # nothing reached the store
    assert set(Attachment.objects.values_list("state", flat=True)) == {"failed"}


# --- the filesystem backend ---------------------------------------------------------------------
class RefusingStorage(FileSystemStorage):
    """A root the server may not write to."""

    def _save(self, name, content):
        raise PermissionError(errno.EACCES, "Permission denied")


class FullStorage(FileSystemStorage):
    """A full disk."""

    def _save(self, name, content):
        raise OSError(errno.ENOSPC, "No space left on device")


class ShortWriteStorage(FileSystemStorage):
    """A write that silently keeps half the bytes."""

    def _save(self, name, content):
        data = content.read()
        return super()._save(name, ContentFile(data[: len(data) // 2]))


class SlowStorage(FileSystemStorage):
    def _save(self, name, content):
        time.sleep(DEADLINE + 1)
        return super()._save(name, content)


def on_disk(settings, cls) -> None:
    location = settings.STORAGES["attachments"]["OPTIONS"]["location"]
    settings.STORAGES = {
        **settings.STORAGES,
        "attachments": {
            "BACKEND": f"{__name__}.{cls.__name__}",
            "OPTIONS": {"location": location, "base_url": None},
        },
    }


class TestFilesystemFaults:
    @pytest.mark.parametrize(
        ("cls", "error_class", "trips"),
        [
            (RefusingStorage, "access_denied", False),
            (FullStorage, "no_space", True),
            (ShortWriteStorage, "integrity", True),
            (SlowStorage, "timeout", True),
        ],
    )
    def test_a_failed_write_is_a_503_and_never_a_stored_row(
        self, guarded, user_a_client, note, caplog, cls, error_class, trips
    ):
        on_disk(guarded, cls)
        with caplog.at_level("WARNING"):
            response, elapsed = upload(user_a_client, note)
        assert_storage_down(response, elapsed)
        row = only_row()
        assert row.state == "failed"
        failure = next(r for r in caplog.records if r.getMessage() == "attachment_storage_failed")
        assert (failure.error_class, failure.backend) == (error_class, "filesystem")
        assert_no_leak(caplog.text + response.content.decode(), row.storage_key)
        for _ in range(2):
            upload(user_a_client, note)
        assert storage.breaker_open() is trips

    def test_a_lost_file_is_a_410(self, guarded, user_a_client, note):
        upload(user_a_client, note)
        row = only_row()
        storages["attachments"].delete(row.storage_key)
        got, _, _ = download(user_a_client, row.pk)
        assert got.status_code == 410
        assert got.json()["error"]["code"] == "attachment_unavailable"

    def test_the_probe_reports_a_store_that_cant_write(self, guarded):
        assert 'arkray_attachment_storage_up{backend="filesystem"} 1' in scrape()
        on_disk(guarded, FullStorage)
        storage.forget_probe()
        assert 'arkray_attachment_storage_up{backend="filesystem"} 0' in scrape()


def test_the_counters_are_bounded():
    """Unknown label values fold into "other": the key space stays fixed."""
    telemetry.upload_failed("something-new")
    telemetry.storage_call("save", 0.2, "weird")
    totals = telemetry.last_hour()
    assert totals[("upload", "failed", "other")] == 1
    assert totals[("error", "save", "other")] == 1
    assert totals[("latency", "save", "1")] == 1  # 0.2 s: the <= 0.5 s bucket
    assert set(totals) == set(telemetry.series())
