"""`manage.py preflight` (config.preflight): each decision with fakes, the framework (deadlines,
redaction, output, exit code) and the command. The checks against real protocols are in
test_preflight_live.py."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from arkray.core import metrics
from config import preflight
from config.preflight import FAIL, PASS, SKIP, WARN, Answer, Options, Result

REPOSITORY = Path(__file__).resolve().parents[3]


def statuses(results) -> dict[str, str]:
    return {result.check: result.status for result in results}


# --- the framework ----------------------------------------------------------------------------
def test_a_group_that_hangs_costs_one_fail_and_keeps_what_it_found():
    release = threading.Event()

    def hangs(options):
        yield Result("x.first", PASS, "done")
        release.wait(30)
        yield Result("x.never", PASS, "too late")

    started = time.monotonic()
    try:
        found = preflight.bounded("x", hangs, Options(timeout_s=0.5))
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert [(r.check, r.status) for r in found] == [("x.first", PASS), ("x", FAIL)]
    assert "0.5 s" in found[-1].detail


def test_a_group_that_crashes_is_a_fail_with_its_reason():
    def crashes(options):
        yield Result("x.first", PASS, "done")
        raise RuntimeError("boom\nsecond line")

    found = preflight.bounded("x", crashes, Options(timeout_s=5))
    assert found[-1] == Result("x", FAIL, "the check itself failed: RuntimeError: boom")


def synthetic(name: str, filler: str, length: int) -> str:
    """A secret-shaped test value, assembled here: a literal would look like a key to the
    history's secret scanning (gitleaks), which must stay free of exceptions."""
    return "-".join([name, "synthetic", filler * length])


def test_no_secret_is_ever_printed(settings, monkeypatch):
    settings.SECRET_KEY = synthetic("django", "x", 48)
    settings.ERASURE_LEDGER_KEY = synthetic("ledger", "y", 32)
    settings.METRICS_TOKEN = synthetic("metrics", "z", 32)
    settings.EMAIL_HOST_PASSWORD = "smtp pass/word"
    settings.CELERY_BROKER_URL = "rediss://:broker%2Fsecret@redis.example.com:6380/0"
    secrets = [
        settings.SECRET_KEY,
        settings.ERASURE_LEDGER_KEY,
        settings.METRICS_TOKEN,
        "smtp pass/word",
        "smtp%20pass%2Fword",
        "broker/secret",
        "broker%2Fsecret",
    ]

    def leaks(options):
        for secret in secrets:
            yield Result("x.leak", FAIL, f"an error message quoting {secret} somewhere")

    monkeypatch.setitem(preflight.GROUPS, "database", leaks)
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("preflight", "--only", "database", stdout=out)
    printed = out.getvalue()
    assert printed.count("[redacted]") == len(secrets)
    for secret in secrets:
        assert secret not in printed


def test_urls_are_shown_without_their_password():
    shown = preflight.display_url(
        "rediss://user:s3cret@redis.example.com:6380/0?ssl_cert_reqs=required"
    )
    assert shown == "rediss://user:***@redis.example.com:6380/0"
    assert preflight.display_url("redis://127.0.0.1:6379/1") == "redis://127.0.0.1:6379/1"


def test_the_table_and_the_json():
    results = [Result("a.one", PASS, "fine"), Result("b.two", FAIL, "broken")]
    table = preflight.as_table(results)
    assert table.splitlines()[0].split() == ["CHECK", "STATUS", "DETAIL"]
    assert "b.two  FAIL    broken" in table
    assert table.endswith("preflight FAILED: 1 PASS, 0 WARN, 1 FAIL, 0 SKIP")
    document = json.loads(preflight.as_json(results))
    assert document["ok"] is False
    assert document["summary"] == {"PASS": 1, "WARN": 0, "FAIL": 1, "SKIP": 0}
    assert document["results"][1] == {"check": "b.two", "status": "FAIL", "detail": "broken"}


# --- the command ------------------------------------------------------------------------------
@pytest.fixture
def fake_groups(monkeypatch):
    """Every group replaced: one that passes, one that warns."""
    for name in preflight.GROUPS:
        monkeypatch.setitem(
            preflight.GROUPS, name, lambda options, name=name: [Result(f"{name}.x", PASS, "ok")]
        )
    monkeypatch.setitem(preflight.GROUPS, "smtp", lambda options: [Result("smtp.x", WARN, "hm")])
    return preflight.GROUPS


def test_the_command_exits_0_without_a_fail(fake_groups):
    out = StringIO()
    call_command("preflight", stdout=out)
    assert (
        f"preflight passed: {len(fake_groups) - 1} PASS, 1 WARN, 0 FAIL, 0 SKIP" in out.getvalue()
    )


def test_the_command_exits_1_on_any_fail(fake_groups, monkeypatch):
    monkeypatch.setitem(preflight.GROUPS, "redis", lambda options: [Result("redis.x", FAIL, "no")])
    out = StringIO()
    with pytest.raises(CommandError, match="1 check") as raised:
        call_command("preflight", stdout=out)
    assert raised.value.returncode == 1
    assert "redis.x" in out.getvalue()


def test_the_command_prints_json(fake_groups):
    out = StringIO()
    call_command("preflight", "--json", stdout=out)
    document = json.loads(out.getvalue())
    assert document["ok"] is True
    assert [r["check"] for r in document["results"]] == [f"{n}.x" for n in preflight.GROUPS]


def test_only_and_skip_choose_the_groups(fake_groups, monkeypatch):
    seen = []
    original = preflight.run
    monkeypatch.setattr(
        preflight, "run", lambda options: seen.append(options.groups) or original(options)
    )
    call_command("preflight", "--only", "database,redis,edge", "--skip", "edge", stdout=StringIO())
    assert seen == [("database", "redis")]
    with pytest.raises(CommandError, match="Unknown group nope"):
        call_command("preflight", "--only", "nope", stdout=StringIO())


def test_the_options_reach_the_checks(fake_groups, monkeypatch):
    seen = []
    monkeypatch.setattr(preflight, "run", lambda options: seen.append(options) or [])
    call_command(
        "preflight",
        "--timeout",
        "7",
        "--connections",
        "40",
        "--send-test-email",
        "ops@example.com",
        "--base-url",
        "https://crm.example.com",
        "--http-url",
        "http://crm.example.com:8080",
        "--edge-ca-file",
        "/etc/arkray/edge-ca.crt",
        stdout=StringIO(),
    )
    assert seen == [
        Options(
            groups=tuple(preflight.GROUPS),
            timeout_s=7.0,
            send_test_email="ops@example.com",
            base_url="https://crm.example.com",
            http_url="http://crm.example.com:8080",
            edge_ca_file="/etc/arkray/edge-ca.crt",
            connections=40,
        )
    ]
    with pytest.raises(CommandError, match="positive"):
        call_command("preflight", "--timeout", "0", stdout=StringIO())


# --- PostgreSQL decisions ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("required", "in_use", "sslmode", "status"),
    [
        (True, True, "verify-full", PASS),
        (True, True, "require", FAIL),  # encrypted, certificate unchecked
        (True, False, "prefer", FAIL),
        (False, True, "prefer", PASS),
        (False, False, "prefer", SKIP),  # loopback, a socket, a listed private host
    ],
)
def test_database_tls(required, in_use, sslmode, status):
    found = preflight.judge_database_tls(
        required=required, in_use=in_use, sslmode=sslmode, protocol="TLSv1.3", pooler=False
    )
    assert found.status == status


def test_database_tls_is_required_wherever_transport_requires_it(settings):
    settings.BACKEND_TLS_PRIVATE_HOSTS = ["postgres"]
    required = preflight.database_tls_required
    tls = {"sslmode": "verify-full", "sslrootcert": "system"}
    assert required({"HOST": "db.example.com", "OPTIONS": tls})
    assert required({"HOST": "db.example.com", "OPTIONS": {**tls, "hostaddr": "127.0.0.1"}})
    assert not required({"HOST": "127.0.0.1", "OPTIONS": {}})
    assert not required({"HOST": "postgres", "OPTIONS": {}})
    assert not required({"HOST": "/var/run/postgresql", "OPTIONS": {}})


def test_postgres_version_and_extensions():
    assert preflight.judge_version(160015).status == PASS
    assert preflight.judge_version(170002).status == WARN
    assert preflight.judge_version(150008).status == FAIL
    assert preflight.judge_extensions({"vector", "pg_trgm", "btree_gin", "plpgsql"}).status == PASS
    missing = preflight.judge_extensions({"pg_trgm"})
    assert missing.status == FAIL
    assert "btree_gin, vector" in missing.detail


def test_the_runtime_role():
    assert preflight.judge_role("arkray_app", [], required=True).status == PASS
    problems = [f"problem {n}" for n in range(5)]
    refused = preflight.judge_role("arkray_owner", problems, required=True)
    assert refused.status == FAIL
    assert "problem 2; and 2 more" in refused.detail
    assert preflight.judge_role("arkray", problems[:1], required=False).status == WARN


@pytest.mark.parametrize(
    ("shown", "ms"),
    [("10s", 10_000), ("1min", 60_000), ("0", 0), ("250ms", 250), ("2h", 7_200_000), ("x", None)],
)
def test_postgres_durations(shown, ms):
    assert preflight.duration_ms(shown) == ms


def test_timeouts():
    good = {
        "statement_timeout": "10s",
        "lock_timeout": "5s",
        "idle_in_transaction_session_timeout": "1min",
    }
    judge = preflight.judge_timeouts
    assert judge(good, 10_000, pooler=False).status == PASS
    assert judge({**good, "statement_timeout": "0"}, None, pooler=True).status == FAIL
    assert "on the role" in judge({**good, "statement_timeout": "0"}, None, pooler=True).detail
    assert judge(good, 60_000, pooler=False).status == WARN  # the settings ask for another
    assert judge({**good, "lock_timeout": "0"}, 10_000, pooler=False).status == WARN
    database = {"OPTIONS": {"options": "-c statement_timeout=60000 -c lock_timeout=5000"}}
    assert preflight.configured_statement_timeout_ms(database) == 60_000
    assert preflight.configured_statement_timeout_ms({"OPTIONS": {}}) is None


def test_migrations_and_connections():
    assert preflight.judge_migrations([], []).status == PASS
    assert preflight.judge_migrations(["pipeline.0010_x"], []).status == FAIL
    assert preflight.judge_migrations([], ["pipeline.0099_future"]).status == WARN
    judge = preflight.judge_connections
    assert judge(max_connections=100, in_use=10, planned=16, pooler=False).status == PASS
    assert judge(max_connections=30, in_use=3, planned=20, pooler=False).status == WARN  # 80% of 22
    assert judge(max_connections=100, in_use=80, planned=16, pooler=False).status == WARN
    assert judge(max_connections=30, in_use=3, planned=200, pooler=True).status == PASS
    assert preflight.PLANNED_CONNECTIONS == 16


def test_statement_logging():
    assert preflight.judge_statement_logging(False).status == PASS
    assert preflight.judge_statement_logging(True).status == WARN


# --- Redis decisions --------------------------------------------------------------------------
def test_redis_tls(settings):
    settings.BACKEND_TLS_PRIVATE_HOSTS = ["redis"]
    judge = preflight.judge_redis_tls
    assert judge("r", "rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=required").status == PASS
    assert judge("r", "redis://:p@127.0.0.1:6379/0").status == SKIP
    assert judge("r", "redis://:p@redis:6379/0").status == SKIP
    assert judge("r", "redis://:p@redis.example.com:6379/0").status == FAIL


def test_redis_auth_policy_and_persistence():
    assert preflight.judge_redis_auth("r", "refused").status == PASS
    assert preflight.judge_redis_auth("r", "answered").status == FAIL
    assert preflight.judge_redis_auth("r", "TimeoutError").status == WARN
    policy = preflight.judge_redis_policy
    broker, cache = preflight.BROKER_POLICIES, preflight.CACHE_POLICIES
    assert policy("r", "broker", {"maxmemory-policy": "noeviction"}, broker).status == PASS
    assert policy("r", "broker", {"maxmemory-policy": "allkeys-lru"}, broker).status == FAIL
    assert policy("r", "cache", {"maxmemory-policy": "allkeys-lru"}, cache).status == PASS
    assert policy("r", "cache", {"maxmemory-policy": "noeviction"}, cache).status == WARN
    unknown = policy("r", "cache", None, cache)
    assert unknown.status == WARN
    assert "unknown" in unknown.detail
    persistence = preflight.judge_redis_persistence
    assert persistence("r", "broker", {"appendonly": "yes"}).status == PASS
    assert persistence("r", "broker", {"appendonly": "no"}).status == WARN
    assert persistence("r", "cache", {"appendonly": "no", "save": ""}).status == PASS
    assert persistence("r", "cache", {"appendonly": "no", "save": "3600 1"}).status == WARN
    assert persistence("r", "cache", None).status == WARN


def test_redis_broker_and_cache_apart():
    identity = preflight.RedisIdentity
    separate = preflight.judge_redis_separate
    assert separate(identity("a:1", 0, "r1"), identity("b:1", 0, "r2")).status == PASS
    # Two names for one server: the run id tells.
    assert separate(identity("a:1", 0, "r1"), identity("b:1", 1, "r1")).status == WARN
    same = separate(identity("a:1", 0, None), identity("a:1", 0, None))
    assert same.status == WARN
    assert "SAME database" in same.detail


# --- SMTP decisions ---------------------------------------------------------------------------
def test_smtp_tls_and_auth():
    judge = preflight.judge_smtp_tls
    assert judge(encrypted=True, protocol="TLSv1.3", implicit=False, required=True).status == PASS
    assert judge(encrypted=False, protocol=None, implicit=False, required=True).status == FAIL
    assert judge(encrypted=False, protocol=None, implicit=False, required=False).status == SKIP
    assert preflight.judge_smtp_auth(True).status == PASS
    assert preflight.judge_smtp_auth(False).status == WARN


# --- object storage decisions ------------------------------------------------------------------
def ok(**response) -> Answer:
    return Answer("ok", response)


ABSENT = Answer("absent", code="NoSuchConfiguration")
DENIED = Answer("unknown", code="AccessDenied")
ALL_BLOCKED = ok(PublicAccessBlockConfiguration=dict.fromkeys(preflight._BLOCKS, True))


def test_public_access():
    judge = preflight.judge_public
    assert judge("p", ALL_BLOCKED, ok(PolicyStatus={"IsPublic": False})).status == PASS
    assert judge("p", ALL_BLOCKED, ABSENT).status == PASS
    assert judge("p", ABSENT, ok(PolicyStatus={"IsPublic": True})).status == FAIL
    partly = ok(
        PublicAccessBlockConfiguration={
            **ALL_BLOCKED.response["PublicAccessBlockConfiguration"],
            "BlockPublicPolicy": False,
        }
    )
    assert judge("p", partly, ABSENT).status == WARN
    assert judge("p", ABSENT, ABSENT).status == WARN
    unknown = judge("p", DENIED, DENIED)
    assert unknown.status == WARN
    assert "AccessDenied" in unknown.detail


def test_versioning_encryption_lifecycle():
    assert preflight.judge_versioning("v", ok(Status="Enabled"), ledger_bucket=False).status == PASS
    assert (
        preflight.judge_versioning("v", ok(Status="Suspended"), ledger_bucket=True).status == WARN
    )
    assert preflight.judge_versioning("v", ok(), ledger_bucket=False).status == WARN
    assert preflight.judge_versioning("v", DENIED, ledger_bucket=False).status == WARN
    rule = {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}
    encrypted = ok(ServerSideEncryptionConfiguration={"Rules": [rule]})
    assert preflight.judge_encryption("e", encrypted) == Result(
        "e", PASS, "default encryption aws:kms"
    )
    assert preflight.judge_encryption("e", ABSENT).status == WARN
    expiring = ok(
        Rules=[{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]
    )
    assert preflight.judge_lifecycle("l", expiring).status == PASS
    assert preflight.judge_lifecycle("l", ok(Rules=[{"Status": "Enabled"}])).status == WARN
    assert preflight.judge_lifecycle("l", ABSENT).status == WARN


def _lock(mode: str | None, days: int = 3650) -> Answer:
    rule = {"Rule": {"DefaultRetention": {"Mode": mode, "Days": days}}} if mode else {}
    return ok(ObjectLockConfiguration={"ObjectLockEnabled": "Enabled", **rule})


def test_object_lock_for_the_ledger():
    judge = preflight.judge_object_lock
    assert judge("o", _lock("COMPLIANCE")) == Result(
        "o", PASS, "Object Lock: compliance mode, 3650 days by default"
    )
    assert judge("o", _lock("GOVERNANCE")).status == WARN
    assert judge("o", _lock(None)).status == WARN  # enabled, nothing locked by default
    assert judge("o", ABSENT).status == WARN
    assert judge("o", DENIED).status == WARN


class FakeS3Client:
    """A boto3 S3 client's bucket-configuration calls, answering from `config`; records
    every call by name."""

    def __init__(self, **config):
        self.config = config
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append(name)
            answer = self.config.get(name, {})
            if isinstance(answer, str):
                raise ClientError({"Error": {"Code": answer}}, name)
            return answer

        return call


GOOD_BUCKET = {
    "get_public_access_block": ALL_BLOCKED.response,
    "get_bucket_policy_status": {"PolicyStatus": {"IsPublic": False}},
    "get_bucket_versioning": {"Status": "Enabled"},
    "get_bucket_encryption": {
        "ServerSideEncryptionConfiguration": {
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        }
    },
    "get_bucket_lifecycle_configuration": {
        "Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]
    },
    "get_object_lock_configuration": _lock("COMPLIANCE").response,
}


def test_a_well_configured_bucket(settings):
    settings.ATTACHMENT_S3_PURGE_VERSIONS = False
    client = FakeS3Client(**GOOD_BUCKET)
    found = list(
        preflight.judge_bucket("s", "b", preflight.bucket_facts(client, "b"), ledger_bucket=False)
    )
    assert statuses(found) == {
        "s.bucket": PASS,
        "s.public": PASS,
        "s.versioning": PASS,
        "s.encryption": PASS,
        "s.lifecycle": PASS,  # versioned, versions not purged by the application: a rule must
    }
    ledger = preflight.bucket_facts(client, "l", object_lock=True)
    assert (
        statuses(preflight.judge_bucket("l", "l", ledger, ledger_bucket=True))["l.object_lock"]
        == PASS
    )


def test_an_unreachable_bucket_stops_there():
    client = FakeS3Client(head_bucket="403")
    found = list(
        preflight.judge_bucket("s", "b", preflight.bucket_facts(client, "b"), ledger_bucket=False)
    )
    assert found == [Result("s.bucket", FAIL, "b: not reachable (403)")]


def test_the_ledger_bucket_is_only_ever_read(settings, monkeypatch):
    """Object Lock could make anything written there undeletable: the storage checks read its
    configuration and nothing else."""
    from arkray.core import ledger

    client = FakeS3Client(**GOOD_BUCKET)
    real = ledger.S3Store
    monkeypatch.setattr(ledger, "S3Store", lambda url: real(url, client=client))
    settings.ERASURE_LEDGER_URL = "s3://arkray-erasure-ledger/production"
    found = [r for r in preflight.check_storage(Options()) if r.check.startswith("storage.ledger")]
    assert statuses(found) == {
        "storage.ledger.bucket": PASS,
        "storage.ledger.public": PASS,
        "storage.ledger.versioning": PASS,
        "storage.ledger.encryption": PASS,
        "storage.ledger.object_lock": PASS,
    }
    assert client.calls
    assert all(call.startswith(("get_", "head_")) for call in client.calls), client.calls


def test_directory_permissions():
    judge = preflight.judge_permissions
    assert judge("d", 0o700, 10001, 10001) == Result("d", PASS, "mode 0700, this user's")
    assert judge("d", 0o750, 10001, 10001).status == WARN
    assert judge("d", 0o700, 0, 10001).status == WARN  # someone else's
    assert judge("d", 0o755, 10001, 10001).status == FAIL
    assert judge("d", 0o777, 10001, 10001).status == FAIL


# --- ClamAV decisions -------------------------------------------------------------------------
def test_signature_age():
    now = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    fresh = preflight.judge_signatures("ClamAV 1.5.4/28150/Sun Oct 11 06:26:01 2026", now)
    assert fresh == Result(
        "scanner.signatures", PASS, "ClamAV 1.5.4, signatures 28150 from 2026-10-11 06:26 UTC"
    )
    stale = preflight.judge_signatures("ClamAV 1.5.4/28140/Wed Oct  7 06:26:01 2026", now)
    assert stale.status == WARN
    assert "4 days old" in stale.detail
    assert preflight.judge_signatures("ClamAV 1.5.4", now).status == WARN


def test_verdicts():
    judge = preflight.judge_verdict
    assert judge("e", clean=False, signature="Win.Test.EICAR_HDB-1", infected=True).status == PASS
    assert judge("e", clean=True, signature="", infected=True).status == FAIL  # scans nothing
    assert judge("c", clean=True, signature="", infected=False).status == PASS
    assert judge("c", clean=False, signature="X", infected=False).status == FAIL  # flags everything


def test_the_eicar_file_is_assembled_at_run_time():
    assert len(preflight.eicar()) == 68
    assert preflight.eicar().startswith(b"X5O!P%@AP[4")
    assert b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" not in Path(preflight.__file__).read_bytes()


# --- erasure ledger decisions -----------------------------------------------------------------
def test_ledger_decisions():
    assert preflight.judge_ledger_chain("ok", 12, "abcd").status == PASS
    assert "12 entries" in preflight.judge_ledger_chain("ok", 12, "abcd").detail
    assert preflight.judge_ledger_chain("tampered", None, "abcd").status == FAIL
    assert preflight.judge_ledger_chain("unavailable", None, "abcd").status == FAIL
    for status in ("ok", "settling", "disabled"):
        assert preflight.judge_ledger_gate(status).status == PASS
    behind = preflight.judge_ledger_gate("behind")
    assert behind.status == FAIL
    assert "replay_erasures" in behind.detail
    assert preflight.judge_ledger_gate("ahead").status == FAIL
    assert preflight.judge_ledger_keys(0).status == PASS
    assert preflight.judge_ledger_keys(1).status == WARN


# --- Celery decisions -------------------------------------------------------------------------
TOPOLOGY = {
    "outbox@w1": {"outbox", "email"},
    "celery@w2": {"default"},
    "index@w3": {"ai_index"},
    "ai@w4": {"ai"},
}


def test_the_documented_workers():
    found = statuses(preflight.judge_workers(set(TOPOLOGY), TOPOLOGY, ai=True, indexing=True))
    assert found == {
        "celery.outbox": PASS,
        "celery.celery": PASS,
        "celery.index": PASS,
        "celery.ai": PASS,
    }


def test_a_worker_consuming_the_wrong_queues():
    # The development stack before the outbox worker of its own (R128): one worker for all.
    queues = {**TOPOLOGY, "celery@w2": {"default", "outbox", "email"}}
    del queues["outbox@w1"]
    found = list(preflight.judge_workers(set(queues), queues, ai=True, indexing=True))
    assert statuses(found) == {
        "celery.outbox": FAIL,
        "celery.celery": FAIL,
        "celery.index": PASS,
        "celery.ai": PASS,
    }
    assert "expected exactly default" in found[1].detail


def test_ask_arkray_workers_are_needed_only_when_it_is_on():
    queues = {k: v for k, v in TOPOLOGY.items() if not k.startswith(("ai@", "index@"))}
    off = statuses(preflight.judge_workers(set(queues), queues, ai=False, indexing=False))
    assert (off["celery.ai"], off["celery.index"]) == (SKIP, SKIP)
    on = statuses(preflight.judge_workers(set(queues), queues, ai=True, indexing=True))
    assert (on["celery.ai"], on["celery.index"]) == (FAIL, FAIL)


def test_an_unknown_or_half_answering_worker():
    queues = {**TOPOLOGY, "reports@w9": {"reports"}}
    found = statuses(
        preflight.judge_workers({*TOPOLOGY, "reports@w9"}, queues, ai=True, indexing=True)
    )
    assert found["celery.reports"] == WARN
    answered = set(TOPOLOGY) - {"ai@w4"}  # answered the inspection, not the ping
    assert (
        statuses(preflight.judge_workers(answered, TOPOLOGY, ai=True, indexing=True))["celery.ai"]
        == WARN
    )


def test_check_celery_asks_the_broker(monkeypatch):
    from config.celery import app

    asked = {}

    class Inspect:
        def active_queues(self):
            return {node: [{"name": q} for q in sorted(qs)] for node, qs in TOPOLOGY.items()}

    def ping(timeout):
        asked["ping"] = timeout
        return [{node: {"ok": "pong"}} for node in TOPOLOGY]

    def inspect(timeout):
        asked["inspect"] = timeout
        return Inspect()

    monkeypatch.setattr(app.control, "ping", ping)
    monkeypatch.setattr(app.control, "inspect", inspect)
    overdue = metrics.Gauge("arkray_outbox_oldest_due_seconds", "")
    overdue.add(90.0, queue="outbox")
    monkeypatch.setattr(metrics, "outbox", lambda: [overdue])
    found = statuses(preflight.check_celery(Options(timeout_s=3)))
    assert asked == {"ping": 3, "inspect": 3}
    assert found["celery.outbox"] == PASS
    assert found["celery.beat"] == WARN  # an event 90 s overdue: the relay isn't ticking


def test_beat():
    assert preflight.judge_beat(0.0).status == SKIP
    assert preflight.judge_beat(61.0).status == WARN


def test_every_queue_has_its_worker():
    """The workers preflight expects consume every queue the application sends to."""
    consumed = set().union(*preflight.WORKERS.values())
    routed = {route["queue"] for route in settings.CELERY_TASK_ROUTES.values()}
    assert consumed == set(metrics.QUEUES)
    assert (
        routed | {settings.CELERY_TASK_DEFAULT_QUEUE} | set(settings.OUTBOX_MAX_IN_FLIGHT)
        <= consumed
    )


def test_the_expected_workers_are_the_documented_and_composed_ones():
    """docs/deployment.md's topology table and docker-compose.yml's commands name the same
    workers and queues as preflight (Celery's default `celery@` where no --hostname is given)."""
    guide = (REPOSITORY / "docs" / "deployment.md").read_text(encoding="utf-8")
    documented = {
        name: frozenset(queues.split(","))
        for queues, name in re.findall(r"-Q ([a-z_,]+)[^|]*?--hostname (\w+)@%h", guide)
    }
    compose = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
    composed = {}
    for command in re.findall(r'command: \["celery", "-A", "config", "worker", (.*)\]', compose):
        arguments = re.findall(r'"([^"]*)"', command)
        queues = frozenset(arguments[arguments.index("--queues") + 1].split(","))
        hostname = (
            arguments[arguments.index("--hostname") + 1]
            if "--hostname" in arguments
            else "celery@%h"
        )
        composed[hostname.split("@")[0]] = queues
    assert documented == preflight.WORKERS
    assert composed == preflight.WORKERS


# --- edge decisions ---------------------------------------------------------------------------
def test_hsts():
    judge = preflight.judge_hsts
    assert judge("h", {"strict-transport-security": "max-age=31536000"}).status == PASS
    assert (
        judge("h", {"strict-transport-security": "max-age=63072000; includeSubDomains"}).status
        == PASS
    )
    assert judge("h", {"strict-transport-security": "max-age=86400"}).status == FAIL
    assert judge("h", {}).status == FAIL


PAGE = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-request-id": "abc",
}


def test_page_headers():
    judge = preflight.judge_page_headers
    assert judge(PAGE).status == PASS
    assert judge({k: v for k, v in PAGE.items() if k != "x-frame-options"}).status == PASS  # CSP
    assert judge({k: v for k, v in PAGE.items() if k != "content-security-policy"}).status == FAIL
    assert judge({**PAGE, "x-content-type-options": "sniff"}).status == FAIL
    assert judge({k: v for k, v in PAGE.items() if k != "x-request-id"}).status == WARN


def test_server_header_and_routes():
    assert preflight.judge_server_header({"server": "nginx"}).status == PASS
    assert preflight.judge_server_header({}).status == PASS
    assert preflight.judge_server_header({"server": "nginx/1.30.0"}).status == FAIL
    response = preflight.Response
    hsts = {"strict-transport-security": "max-age=31536000"}
    json_401 = response(401, {"content-type": "application/json", **hsts})
    assert preflight.judge_api(json_401).status == PASS
    assert preflight.judge_api(response(401, {"content-type": "application/json"})).status == FAIL
    assert preflight.judge_api(response(404, {"content-type": "text/html"})).status == FAIL
    assert preflight.judge_api(response(200, {"content-type": "application/json"})).status == FAIL
    assert preflight.judge_health(response(404, {})).status == PASS
    assert preflight.judge_health(response(200, {})).status == WARN
    redirect = preflight.judge_http_redirect
    assert redirect(response(308, {"location": "https://crm.example.com/login"})).status == PASS
    assert redirect(response(200, {})).status == FAIL
    assert redirect(response(308, {"location": "http://crm.example.com/login"})).status == FAIL
    assert preflight.judge_login(response(200, {}, "TLSv1.3")).status == PASS
    assert preflight.judge_login(response(503, {}, "TLSv1.3")).status == FAIL


def test_the_edge_is_checked_only_when_asked():
    assert statuses(preflight.check_edge(Options())) == {"edge": SKIP}
    assert statuses(preflight.check_edge(Options(base_url="http://crm.example.com"))) == {
        "edge": FAIL
    }


def test_the_ledger_is_skipped_without_one(settings):
    settings.ERASURE_LEDGER_URL = ""
    assert statuses(preflight.check_ledger(Options())) == {"ledger": SKIP}


def test_no_scanner_is_a_warning(settings):
    settings.ATTACHMENT_SCANNER = ""
    assert statuses(preflight.check_scanner(Options())) == {"scanner": WARN}


def test_a_signature_date_now_is_fresh():
    built = datetime.now(UTC) - timedelta(hours=2)
    version = f"ClamAV 1.5.4/1/{built:%a %b %d %H:%M:%S %Y}"
    assert preflight.judge_signatures(version, datetime.now(UTC)).status == PASS
