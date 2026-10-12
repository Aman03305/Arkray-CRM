"""`manage.py preflight` against real protocols on 127.0.0.1: the test database (and, in a
fresh process, the command as a restricted role), Redis, SMTP, clamd and an HTTPS edge spoken
by small local servers over a throwaway CA (tests/tls_helpers.py), the S3 protocol fake
(fake_s3.py) and a file ledger. No external network; nothing but these servers is touched."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from django.db import OperationalError, connection, connections

from arkray.core import ledger_gate
from config import preflight
from config.preflight import FAIL, PASS, SKIP, WARN, Options
from config.settings.base import attachment_s3_backend
from tests.integration.fake_s3 import FakeS3, closed_port
from tests.tls_helpers import make_ca, serve, server_context, smtp

BACKEND = Path(__file__).resolve().parents[2]


def statuses(results) -> dict[str, str]:
    return {result.check: result.status for result in results}


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pki")
    ca = make_ca(directory)
    rogue = make_ca(directory / "rogue", "Rogue CA")
    return {
        "ca": ca.cert,
        "good": server_context(ca.server("good")),
        "wrong_name": server_context(ca.server("wrong", sans=("other.example.com",))),
        "untrusted": server_context(rogue.server("untrusted")),
    }


# --- PostgreSQL -------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_test_database():
    found = statuses(preflight.check_database(Options()))
    assert found["database.connect"] == PASS
    assert found["database.tls"] == SKIP  # 127.0.0.1: loopback, plain TCP allowed
    assert found["database.version"] == PASS
    assert found["database.extensions"] == PASS
    assert found["database.role"] == WARN  # the test cluster's superuser, the check off
    assert found["database.migrations"] == PASS
    assert found["database.timeouts"] == PASS
    assert found["database.logging"] in {PASS, WARN}  # the cluster's own settings
    assert found["database.connections"] in {PASS, WARN}  # other test runs share it


def test_an_unreachable_database_is_one_fail(monkeypatch):
    def refuse() -> None:
        raise OperationalError("connection refused")

    monkeypatch.setattr(connections["default"], "ensure_connection", refuse)
    found = list(preflight.check_database(Options()))
    assert [(r.check, r.status) for r in found] == [("database.connect", FAIL)]
    assert "OperationalError: connection refused" in found[0].detail


@pytest.fixture
def app_role(transactional_db) -> Iterator[tuple[str, str]]:
    name, password = f"arkray_preflight_{secrets.token_hex(4)}", secrets.token_hex(16)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
    try:
        yield name, password
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP OWNED BY {name}")
            cursor.execute(f"DROP ROLE {name}")


def test_the_command_as_the_restricted_application_role(app_role):
    """The whole command in a process of its own, as production runs it: the application
    role's credentials, a JSON report, the exit code."""
    from django.core.management import call_command

    call_command("grant_app_privileges", app_role[0])
    database = connection.settings_dict
    url = (
        f"postgres://{app_role[0]}:{app_role[1]}@{database['HOST'] or '127.0.0.1'}:"
        f"{database['PORT'] or 5432}/{database['NAME']}"
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "preflight", "--only", "database", "--json"],
        cwd=BACKEND,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test", "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    report = json.loads(result.stdout)
    found = {r["check"]: r["status"] for r in report["results"]}
    assert found["database.role"] == PASS, report
    assert found["database.connect"] == PASS
    assert app_role[1] not in result.stdout + result.stderr
    assert result.returncode == (1 if report["summary"]["FAIL"] else 0)
    assert report["summary"]["FAIL"] == 0, report


# --- Redis ------------------------------------------------------------------------------------
def resp3_map(values: dict[str, str | int]) -> bytes:
    body = b""
    for key, value in values.items():
        body += f"${len(key)}\r\n{key}\r\n".encode()
        if isinstance(value, int):
            body += f":{value}\r\n".encode()
        else:
            body += f"${len(value)}\r\n{value}\r\n".encode()
    return f"%{len(values)}\r\n".encode() + body


def fake_redis(
    *,
    password: str | None = None,
    config: dict[str, str] | None = None,
    run_id: str = "run-1",
    context: ssl.SSLContext | None = None,
):
    """Enough of Redis's protocol for preflight, as redis-py speaks it (RESP3: a HELLO that
    authenticates): PING, CONFIG GET, INFO; every other command +OK. Without the password
    everything but authenticating is NOAUTH."""

    def handle(conn: socket.socket) -> None:
        stream: Any = context.wrap_socket(conn, server_side=True) if context else conn
        reader = stream.makefile("rb")
        authed = password is None
        while True:
            line = reader.readline()
            if not line.startswith(b"*"):
                return
            args = []
            for _ in range(int(line[1:])):
                size = int(reader.readline()[1:])
                args.append(reader.read(size + 2)[:-2].decode())
            command = args[0].upper()
            if command == "HELLO":
                if "AUTH" in (arg.upper() for arg in args):
                    authed = authed or args[-1] == password
                if not authed:
                    stream.sendall(b"-NOAUTH HELLO must be called authenticated\r\n")
                    continue
                stream.sendall(resp3_map({"server": "redis", "version": "7.4.1", "proto": 3}))
            elif command == "AUTH":
                authed = authed or args[-1] == password
                stream.sendall(b"+OK\r\n" if authed else b"-WRONGPASS invalid password\r\n")
            elif not authed:
                stream.sendall(b"-NOAUTH Authentication required.\r\n")
            elif command == "PING":
                stream.sendall(b"+PONG\r\n")
            elif command == "CONFIG":
                if config is None:
                    stream.sendall(b"-ERR unknown command 'CONFIG'\r\n")
                    continue
                key = args[2]
                stream.sendall(resp3_map({key: config[key]} if key in config else {}))
            elif command == "INFO":
                info = f"# Server\r\nredis_version:7.4.1\r\nrun_id:{run_id}\r\n"
                stream.sendall(f"${len(info)}\r\n{info}\r\n".encode())
            else:
                stream.sendall(b"+OK\r\n")

    return handle


BROKER_CONFIG = {"maxmemory-policy": "noeviction", "appendonly": "yes", "save": "3600 1"}
CACHE_CONFIG = {"maxmemory-policy": "allkeys-lru", "appendonly": "no", "save": ""}


def test_two_redis_instances_with_passwords(settings):
    with (
        serve(fake_redis(password="b-secret", config=BROKER_CONFIG, run_id="broker")) as broker,
        serve(fake_redis(password="c-secret", config=CACHE_CONFIG, run_id="cache")) as cache,
    ):
        settings.CELERY_BROKER_URL = f"redis://:b-secret@127.0.0.1:{broker}/0"
        settings.REDIS_CACHE_URL = f"redis://:c-secret@127.0.0.1:{cache}/0"
        found = list(preflight.check_redis(Options(timeout_s=5)))
    assert statuses(found) == {
        "redis.broker.ping": PASS,
        "redis.broker.tls": SKIP,  # loopback
        "redis.broker.auth": PASS,
        "redis.broker.policy": PASS,
        "redis.broker.persistence": PASS,
        "redis.cache.ping": PASS,
        "redis.cache.tls": SKIP,
        "redis.cache.auth": PASS,
        "redis.cache.policy": PASS,
        "redis.cache.persistence": PASS,
        "redis.separate": PASS,
    }
    assert "redis://:***@127.0.0.1" in found[0].detail
    assert all("secret" not in result.detail for result in found)


def test_redis_without_a_password_and_without_config(settings):
    with serve(fake_redis(password=None, config=None, run_id="one")) as port:
        settings.CELERY_BROKER_URL = f"redis://127.0.0.1:{port}/0"
        settings.REDIS_CACHE_URL = f"redis://127.0.0.1:{port}/1"
        found = statuses(preflight.check_redis(Options(timeout_s=5)))
    assert found["redis.broker.auth"] == FAIL  # a PING without a password is answered
    assert found["redis.broker.policy"] == WARN  # CONFIG forbidden: unknown, not a failure
    assert found["redis.cache.persistence"] == WARN
    assert found["redis.separate"] == WARN  # one instance, two databases


def test_redis_over_verified_tls(settings, pki):
    query = f"ssl_cert_reqs=required&ssl_ca_certs={pki['ca'].as_posix()}"
    good = fake_redis(password="pw", config=BROKER_CONFIG, context=pki["good"], run_id="b")
    cache = fake_redis(password="pw", config=CACHE_CONFIG, context=pki["good"], run_id="c")
    with serve(good) as broker, serve(cache) as other:
        settings.CELERY_BROKER_URL = f"rediss://:pw@127.0.0.1:{broker}/0?{query}"
        settings.REDIS_CACHE_URL = f"rediss://:pw@127.0.0.1:{other}/0?{query}"
        found = statuses(preflight.check_redis(Options(timeout_s=5)))
    assert found["redis.broker.ping"] == PASS
    assert found["redis.broker.tls"] == PASS
    assert found["redis.broker.auth"] == PASS  # the unauthenticated probe over TLS too
    assert found["redis.cache.tls"] == PASS


@pytest.mark.parametrize("server", ["untrusted", "wrong_name"])
def test_redis_with_an_unverifiable_certificate(settings, pki, server):
    query = f"ssl_cert_reqs=required&ssl_ca_certs={pki['ca'].as_posix()}"
    with serve(fake_redis(password="pw", context=pki[server])) as port:
        settings.CELERY_BROKER_URL = f"rediss://:pw@127.0.0.1:{port}/0?{query}"
        settings.REDIS_CACHE_URL = "memory://"
        found = list(preflight.check_redis(Options(timeout_s=5)))
    assert [(r.check, r.status) for r in found] == [
        ("redis.broker.ping", FAIL),
        ("redis.cache", SKIP),
    ]
    assert "certificate" in found[0].detail.lower() or "hostname" in found[0].detail.lower()


def test_redis_down(settings):
    settings.CELERY_BROKER_URL = f"redis://:pw@127.0.0.1:{closed_port()}/0"
    settings.REDIS_CACHE_URL = "memory://"
    found = statuses(preflight.check_redis(Options(timeout_s=3)))
    assert found == {"redis.broker.ping": FAIL, "redis.cache": SKIP}


# --- SMTP -------------------------------------------------------------------------------------
@pytest.fixture
def smtp_settings(settings, pki):
    settings.EMAIL_BACKEND = "arkray.core.mail.VerifiedSMTPBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_USE_TLS = True
    settings.EMAIL_USE_SSL = False
    settings.EMAIL_TLS_CA_FILE = str(pki["ca"])
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""
    settings.EMAIL_TIMEOUT = 5
    return settings


def test_smtp_over_verified_starttls(smtp_settings, pki):
    with serve(smtp(pki["good"])) as port:
        smtp_settings.EMAIL_PORT = port
        found = list(preflight.check_smtp(Options()))
        sent = list(preflight.check_smtp(Options(send_test_email="ops@example.com")))
    assert statuses(found) == {
        "smtp.connect": PASS,
        "smtp.tls": PASS,
        "smtp.auth": WARN,  # this relay takes mail without credentials
        "smtp.send": SKIP,  # nothing sent unless asked
    }
    assert "STARTTLS (TLSv1." in found[1].detail
    assert statuses(sent)["smtp.send"] == PASS


@pytest.mark.parametrize("server", ["untrusted", "wrong_name"])
def test_smtp_with_an_unverifiable_certificate(smtp_settings, pki, server):
    with serve(smtp(pki[server])) as port:
        smtp_settings.EMAIL_PORT = port
        found = list(preflight.check_smtp(Options()))
    assert [(r.check, r.status) for r in found] == [("smtp.connect", FAIL)]
    assert "certificate" in found[0].detail


def test_smtp_without_starttls(smtp_settings, pki):
    with serve(smtp(pki["good"], offer_starttls=False)) as port:
        smtp_settings.EMAIL_PORT = port
        found = list(preflight.check_smtp(Options()))
    assert [(r.check, r.status) for r in found] == [("smtp.connect", FAIL)]
    assert "STARTTLS" in found[0].detail


def test_plain_smtp_on_loopback(smtp_settings):
    smtp_settings.EMAIL_USE_TLS = False
    with serve(smtp(None, offer_starttls=False)) as port:
        smtp_settings.EMAIL_PORT = port
        found = statuses(preflight.check_smtp(Options()))
    assert found["smtp.connect"] == PASS
    assert found["smtp.tls"] == SKIP  # loopback: allowed, and said so


def test_a_backend_that_isnt_smtp(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    assert statuses(preflight.check_smtp(Options())) == {"smtp": SKIP}


# --- object storage ---------------------------------------------------------------------------
def test_private_directories(settings, tmp_path):
    attachments, exports, ledger = (
        tmp_path / name for name in ("attachments", "exports", "ledger")
    )
    for directory in (attachments, exports, ledger):
        directory.mkdir(mode=0o700)
    settings.STORAGES = {
        **settings.STORAGES,
        "attachments": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(attachments)},
        },
        "exports": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(exports)},
        },
    }
    settings.ERASURE_LEDGER_URL = ledger.as_uri()
    found = statuses(preflight.check_storage(Options()))
    permissions = SKIP if os.name == "nt" else PASS
    assert found == {
        "storage.attachments.directory": PASS,
        "storage.attachments.permissions": permissions,
        "storage.attachments.probe": PASS,
        "storage.exports.directory": PASS,
        "storage.exports.permissions": permissions,
        "storage.exports.probe": PASS,
        "storage.ledger.directory": PASS,
        "storage.ledger.permissions": permissions,
    }
    # The probes are gone, and nothing was ever written in the ledger's directory.
    assert [*attachments.iterdir(), *exports.iterdir(), *ledger.iterdir()] == []


def test_a_missing_volume(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "attachments": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "not-mounted")},
        },
    }
    found = statuses(preflight.check_storage(Options()))
    assert found["storage.attachments.directory"] == FAIL
    assert "storage.attachments.probe" not in found  # nothing written where it would land
    assert not (tmp_path / "not-mounted").exists()


@pytest.fixture
def s3():
    server = FakeS3()
    yield server
    server.close()


def _s3(endpoint: str, prefix: str) -> dict[str, Any]:
    backend = attachment_s3_backend(
        bucket="arkray-fault-tests",
        endpoint_url=endpoint,
        region="us-east-1",
        access_key="preflight-access-key",
        secret_key="preflight-secret-key",
        prefix=prefix,
        connect_timeout_s=2,
        read_timeout_s=2,
        max_attempts=1,
    )
    return backend


def test_an_s3_compatible_store(settings, s3):
    """The S3 protocol fake: the bucket answers, its configuration calls don't (as on many
    S3-compatible stores: unknown, never a pass), and a probe is written, read back and
    removed under each prefix."""
    settings.STORAGES = {
        **settings.STORAGES,
        "attachments": _s3(s3.endpoint, "attachments"),
        "exports": _s3(s3.endpoint, "exports"),
    }
    settings.ERASURE_LEDGER_URL = ""
    found = list(preflight.check_storage(Options()))
    assert statuses(found) == {
        "storage.attachments.bucket": PASS,
        "storage.attachments.public": WARN,
        "storage.attachments.versioning": WARN,
        "storage.attachments.encryption": WARN,
        "storage.attachments.probe": PASS,
        "storage.exports.bucket": PASS,  # the same bucket: checked once
        "storage.exports.probe": PASS,
        "storage.ledger": SKIP,
    }
    written = {key for method, key in s3.requests if method == "PUT"}
    assert sorted(key.split("/")[0] for key in written) == ["attachments", "exports"]
    assert all(key.split("/")[1].startswith(".arkray-probe-preflight-") for key in written)
    assert s3.objects == {}  # removed again
    assert "preflight-secret-key" not in preflight.as_table(found)


def test_an_unreachable_bucket(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "attachments": _s3(f"http://127.0.0.1:{closed_port()}", "attachments"),
    }
    settings.ERASURE_LEDGER_URL = ""
    found = statuses(preflight.check_storage(Options()))
    assert found["storage.attachments.bucket"] == FAIL
    assert "storage.attachments.probe" not in found


# --- ClamAV -----------------------------------------------------------------------------------
def fake_clamd(*, finds: bool = True, version: str | None = None):
    """clamd's PING, VERSION and zINSTREAM; finds EICAR when `finds`."""
    built = datetime.now(UTC).strftime("%a %b %d %H:%M:%S %Y")
    answer_version = version or f"ClamAV 1.5.4/28150/{built}"

    def handle(conn: socket.socket) -> None:
        command = b""
        while not command.endswith(b"\0"):
            command += conn.recv(1)
        if command == b"zPING\0":
            conn.sendall(b"PONG\0")
        elif command == b"zVERSION\0":
            conn.sendall(answer_version.encode() + b"\0")
        elif command == b"zINSTREAM\0":
            data = b""
            while True:
                header = b""
                while len(header) < 4:
                    header += conn.recv(4 - len(header))
                (size,) = struct.unpack("!L", header)
                if size == 0:
                    break
                while size:
                    part = conn.recv(size)
                    data += part
                    size -= len(part)
            infected = finds and b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data
            conn.sendall(b"stream: Win.Test.EICAR_HDB-1 FOUND\0" if infected else b"stream: OK\0")

    return handle


def test_a_clamd_that_scans(settings):
    with serve(fake_clamd()) as port:
        settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{port}"
        found = list(preflight.check_scanner(Options()))
    assert statuses(found) == {
        "scanner.ping": PASS,
        "scanner.signatures": PASS,
        "scanner.eicar": PASS,
        "scanner.clean": PASS,
    }
    assert "Win.Test.EICAR_HDB-1" in found[2].detail


def test_a_clamd_that_finds_nothing_or_is_out_of_date(settings):
    with serve(
        fake_clamd(finds=False, version="ClamAV 1.5.4/27000/Mon Jan  5 06:00:00 2026")
    ) as port:
        settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{port}"
        found = statuses(preflight.check_scanner(Options()))
    assert found["scanner.eicar"] == FAIL
    assert found["scanner.signatures"] == WARN


def test_an_unreachable_clamd(settings):
    settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{closed_port()}"
    found = list(preflight.check_scanner(Options(timeout_s=3)))
    assert [(r.check, r.status) for r in found] == [("scanner.ping", FAIL)]
    assert "--skip scanner" in found[0].detail


# --- the erasure ledger -----------------------------------------------------------------------
@pytest.fixture
def file_ledger(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = "preflight-ledger-key-" + "k" * 24
    settings.ERASURE_LEDGER_VERIFY_KEYS = []
    ledger_gate.GATE.reset()
    yield directory
    ledger_gate.GATE.reset()


@pytest.mark.django_db
def test_a_verified_ledger_with_the_gate_open(file_ledger):
    found = list(preflight.check_ledger(Options()))
    assert statuses(found) == {"ledger.chain": PASS, "ledger.gate": PASS, "ledger.keys": PASS}
    assert found[0].detail.startswith("0 entries, every one verified (signing key id ")


@pytest.mark.django_db
def test_a_tampered_ledger(file_ledger):
    entry = {
        "seq": 1,
        "kind": "lead_erased",
        "subject": "x",
        "at": "t",
        "prev": "",
        "mac": "0" * 64,
    }
    (file_ledger / "000000000001.json").write_text(json.dumps(entry), encoding="utf-8")
    found = statuses(preflight.check_ledger(Options()))
    assert found == {"ledger.chain": FAIL, "ledger.gate": FAIL, "ledger.keys": PASS}


# --- the edge ---------------------------------------------------------------------------------
@contextlib.contextmanager
def edge(routes: dict[str, tuple[int, dict[str, str]]], *, server: str, context=None):
    """A local HTTP(S) server answering each path with a status and headers."""

    class Handler(BaseHTTPRequestHandler):
        def version_string(self) -> str:
            return server

        def do_GET(self) -> None:
            status, headers = routes.get(self.path, (404, {"content-type": "text/html"}))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if context is not None:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


HSTS = {"strict-transport-security": "max-age=31536000"}
GOOD_ROUTES = {
    "/login": (
        200,
        {
            **HSTS,
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
            "referrer-policy": "same-origin",
            "cache-control": "no-store",
            "x-request-id": "0123abcd",
        },
    ),
    "/api/v1/auth/me": (401, {**HSTS, "content-type": "application/json"}),
    "/health/ready": (404, {**HSTS, "content-type": "text/html"}),
}


def test_an_edge_like_the_reference_one(pki):
    with (
        edge(GOOD_ROUTES, server="nginx", context=pki["good"]) as port,
        edge({"/login": (308, {"location": "https://127.0.0.1/login"})}, server="nginx") as plain,
    ):
        options = Options(
            base_url=f"https://127.0.0.1:{port}",
            http_url=f"http://127.0.0.1:{plain}",
            edge_ca_file=str(pki["ca"]),
        )
        found = list(preflight.check_edge(options))
    assert statuses(found) == {
        "edge.login": PASS,
        "edge.hsts": PASS,
        "edge.headers": PASS,
        "edge.server": PASS,
        "edge.api": PASS,
        "edge.health": PASS,
        "edge.http": PASS,
    }
    assert "over TLSv1." in found[0].detail


def test_an_edge_that_leaks(pki):
    routes = {
        "/login": (200, {"x-content-type-options": "nosniff"}),
        "/api/v1/auth/me": (404, {"content-type": "text/html"}),
        "/health/ready": (200, {"content-type": "application/json"}),
    }
    with (
        edge(routes, server="nginx/1.27.0", context=pki["good"]) as port,
        edge({"/login": (200, {})}, server="nginx/1.27.0") as plain,
    ):
        options = Options(
            base_url=f"https://127.0.0.1:{port}",
            http_url=f"http://127.0.0.1:{plain}",
            edge_ca_file=str(pki["ca"]),
        )
        found = statuses(preflight.check_edge(options))
    assert found == {
        "edge.login": PASS,
        "edge.hsts": FAIL,
        "edge.headers": FAIL,
        "edge.server": FAIL,
        "edge.api": FAIL,
        "edge.health": WARN,
        "edge.http": FAIL,
    }


def test_an_edge_with_an_untrusted_certificate(pki):
    with edge(GOOD_ROUTES, server="nginx", context=pki["untrusted"]) as port:
        options = Options(base_url=f"https://127.0.0.1:{port}", edge_ca_file=str(pki["ca"]))
        found = list(preflight.check_edge(options))
    assert [(r.check, r.status) for r in found] == [("edge.login", FAIL)]
    assert "certificate" in found[0].detail


def test_an_edge_on_a_non_standard_port_needs_its_http_url(pki):
    with edge(GOOD_ROUTES, server="nginx", context=pki["good"]) as port:
        options = Options(base_url=f"https://127.0.0.1:{port}", edge_ca_file=str(pki["ca"]))
        found = statuses(preflight.check_edge(options))
    assert found["edge.http"] == SKIP


# --- the whole run ----------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_a_run_on_threads_closes_its_connections(settings):
    """Each group runs on a thread of its own (its deadline) and closes that thread's
    database connection: nothing is left open behind a preflight."""
    settings.ERASURE_LEDGER_URL = ""

    def backends() -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid()"
            )
            return int(cursor.fetchone()[0])

    before = backends()
    found = preflight.run(Options(groups=("database", "ledger")))
    assert statuses(found)["database.connect"] == PASS
    for _ in range(100):  # a closed connection's server process ends a moment later
        if backends() <= before:
            break
        time.sleep(0.05)
    assert backends() <= before
