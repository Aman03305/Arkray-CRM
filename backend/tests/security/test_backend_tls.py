"""Backend connections use verified TLS (privacy remediation P2-1; docs/security.md#backend-tls).

Two layers:
- production settings refuse a configuration that would let a remote hop run in plain text
  or with an unverified certificate (checked in a fresh interpreter, as production starts);
- the clients the settings configure really refuse a downgrade, an untrusted certificate, a
  host-name mismatch and a missing CA, against real TLS handshakes on 127.0.0.1.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import psycopg
import pytest
import redis
from django.core.mail import EmailMessage
from django.test import override_settings

from arkray.core import transport
from arkray.core.mail import VerifiedSMTPBackend
from tests.architecture.test_production_settings import load_production_settings
from tests.tls_helpers import make_ca, postgres, serve, server_context, smtp, tls_then_close

REMOTE_DB = "postgres://u:p@db.example.com:5432/arkray"


# --- the rules --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("host", "options", "problem"),
    [
        ("db.example.com", {}, "sslmode=verify-full"),
        ("db.example.com", {"sslmode": "require"}, "sslmode=verify-full"),
        ("db.example.com", {"sslmode": "verify-ca"}, "sslmode=verify-full"),
        ("10.0.0.5", {"sslmode": "prefer"}, "sslmode=verify-full"),
        ("db.example.com", {"sslmode": "verify-full"}, "sslrootcert"),
        (
            "db.example.com",
            {"sslmode": "verify-full", "sslrootcert": "/no/such/ca.pem"},
            "does not exist",
        ),
        ("db.example.com", {"sslmode": "verify-full", "sslrootcert": "system"}, None),
        ("localhost", {}, None),
        ("127.0.0.1", {}, None),
        ("::1", {}, None),
        ("", {}, None),  # a Unix socket
        ("/var/run/postgresql", {}, None),
        ("postgres", {}, "sslmode=verify-full"),  # a container name is not exempt by itself
    ],
)
def test_database_rules(host, options, problem):
    found = transport.database_problem({"HOST": host, "OPTIONS": options})
    assert (found is None) if problem is None else (problem in found)


def test_database_ca_file_must_exist(tmp_path):
    ca = make_ca(tmp_path)
    options = {"sslmode": "verify-full", "sslrootcert": str(ca.cert)}
    assert transport.database_problem({"HOST": "db.example.com", "OPTIONS": options}) is None


def test_listed_private_container_names_are_the_only_exception():
    assert transport.database_problem({"HOST": "postgres", "OPTIONS": {}}, ["postgres"]) is None
    assert transport.database_problem({"HOST": "db.example.com", "OPTIONS": {}}, ["postgres"])
    # Only single-label names may be listed: a dotted name or an IP may leave the network.
    assert transport.private_host_problems(["postgres", "redis"]) == []
    assert len(transport.private_host_problems(["db.example.com", "10.0.0.5", "::1"])) == 3


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("redis://:p@redis.example.com:6379/0", "rediss://"),
        ("rediss://:p@redis.example.com:6380/0", "ssl_cert_reqs=required"),
        ("rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=none", "ssl_cert_reqs=required"),
        ("rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=optional", "ssl_cert_reqs=required"),
        (
            "rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=required&ssl_check_hostname=false",
            "ssl_check_hostname",
        ),
        ("rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=required", None),
        # As redis-py reads them (backend review P3): exact, lowercase.
        ("rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=CERT_REQUIRED", "ssl_cert_reqs"),
        ("REDISS://:p@redis.example.com:6380/0?ssl_cert_reqs=required", "rediss://"),
        (
            "rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=required&ssl_check_hostname=n",
            "ssl_check_hostname",
        ),
        (
            "rediss://:p@redis.example.com:6380/0?ssl_cert_reqs=required&ssl_cert_reqs=none",
            "ssl_cert_reqs",
        ),
        ("redis://:p@127.0.0.1:6379/0", None),
        ("unix:///run/redis.sock", None),
    ],
)
def test_redis_rules(url, problem):
    found = transport.redis_problem("REDIS", url)
    assert (found is None) if problem is None else (problem in found)


def test_smtp_s3_and_scanner_rules():
    assert transport.smtp_problem("smtp.example.com", use_tls=False, use_ssl=False)
    assert transport.smtp_problem("smtp.example.com", use_tls=True, use_ssl=False) is None
    assert transport.smtp_problem("smtp.example.com", use_tls=False, use_ssl=True) is None
    assert (
        transport.smtp_problem("mailpit", use_tls=False, use_ssl=False, private_hosts=["mailpit"])
        is None
    )
    assert transport.s3_problem("http://minio.example.com:9000")
    assert transport.s3_problem("https://minio.example.com:9000") is None
    assert transport.s3_problem("") is None
    assert transport.scanner_problem("clamd://clamav.example.com:3310")
    assert transport.scanner_problem("clamd://clamav:3310", ["clamav"]) is None
    assert transport.scanner_problem("clamd://127.0.0.1:3310") is None


# --- production refuses to start ------------------------------------------------------------
def test_remote_database_without_verified_tls_is_refused():
    result = load_production_settings(DATABASE_URL=REMOTE_DB)
    assert result.returncode != 0
    assert "sslmode=verify-full" in result.stderr
    result = load_production_settings(DATABASE_URL=REMOTE_DB + "?sslmode=require")
    assert result.returncode != 0


def test_remote_database_with_verify_full_and_a_ca_starts(tmp_path):
    ca = make_ca(tmp_path)
    url = f"{REMOTE_DB}?sslmode=verify-full&sslrootcert={ca.cert.as_posix()}"
    result = load_production_settings(
        "settings.DATABASES['default']['OPTIONS']['sslmode'], settings.CELERY_BROKER_USE_SSL",
        DATABASE_URL=url,
        CELERY_BROKER_URL="rediss://:b@redis.example.com:6380/0?ssl_cert_reqs=required",
        REDIS_CACHE_URL="rediss://:c@redis.example.com:6380/1?ssl_cert_reqs=required",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("verify-full")
    assert "CERT_REQUIRED" in result.stdout


def test_missing_ca_file_is_refused_at_start():
    url = f"{REMOTE_DB}?sslmode=verify-full&sslrootcert=/no/such/ca.pem"
    result = load_production_settings(DATABASE_URL=url)
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_rediss_without_certificate_checks_is_refused():
    # kombu would connect to this broker with CERT_NONE (accepting any certificate).
    result = load_production_settings(
        CELERY_BROKER_URL="rediss://:b@redis.example.com:6380/0",
        REDIS_CACHE_URL="rediss://:c@redis.example.com:6380/1?ssl_cert_reqs=required",
    )
    assert result.returncode != 0
    assert "CELERY_BROKER_URL" in result.stderr


def test_plain_smtp_to_a_remote_relay_is_refused():
    result = load_production_settings(EMAIL_URL="smtp://mailer:secret@smtp.example.com:25")
    assert result.returncode != 0
    assert "EMAIL_URL" in result.stderr
    assert "secret" not in result.stderr  # never the credentials


def test_http_object_storage_is_refused():
    result = load_production_settings(
        ATTACHMENT_STORAGE="s3",
        ATTACHMENT_S3_BUCKET="arkray",
        ATTACHMENT_S3_ENDPOINT_URL="http://minio.example.com:9000",
    )
    assert result.returncode != 0
    assert "ATTACHMENT_S3_ENDPOINT_URL" in result.stderr


def test_private_container_names_must_be_listed_and_single_label():
    plain = {
        "DATABASE_URL": "postgres://u:p@postgres:5432/arkray",
        "CELERY_BROKER_URL": "redis://:b@redis:6379/0",
        "REDIS_CACHE_URL": "redis://:c@redis:6379/1",
        "EMAIL_URL": "smtp://mailpit:1025",
    }
    refused = load_production_settings(**plain)
    assert refused.returncode != 0
    allowed = load_production_settings(BACKEND_TLS_PRIVATE_HOSTS="postgres,redis,mailpit", **plain)
    assert allowed.returncode == 0, allowed.stderr
    dotted = load_production_settings(BACKEND_TLS_PRIVATE_HOSTS="postgres.internal", **plain)
    assert dotted.returncode != 0
    assert "single-label" in dotted.stderr


def test_local_opt_in_never_covers_a_public_host_name():
    result = load_production_settings(
        DATABASE_URL=REMOTE_DB,
        DJANGO_ALLOW_INSECURE_LOCAL_HTTP="true",
        DJANGO_SECURE_COOKIES="false",
    )
    assert result.returncode != 0  # crm.example.com is public: the opt-in itself is refused


# --- the clients refuse what they must ---------------------------------------------------------
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


def _pg(port: int, ca: Path | str, **extra: Any) -> None:
    psycopg.connect(
        host="localhost",
        hostaddr="127.0.0.1",
        port=port,
        user="u",
        password="p",
        dbname="d",
        sslmode="verify-full",
        sslrootcert=str(ca),
        connect_timeout=5,
        **extra,
    ).close()


def test_postgres_refuses_a_server_that_declines_tls(pki):
    with serve(postgres(None)) as port, pytest.raises(psycopg.OperationalError) as caught:
        _pg(port, pki["ca"])
    assert "SSL" in str(caught.value)  # "server does not support SSL, but SSL was required"


@pytest.mark.parametrize("server", ["untrusted", "wrong_name"])
def test_postgres_refuses_an_unverifiable_certificate(pki, server):
    with serve(postgres(pki[server])) as port, pytest.raises(psycopg.OperationalError) as caught:
        _pg(port, pki["ca"])
    message = str(caught.value).lower()
    assert "certificate" in message or "does not match" in message


def test_postgres_refuses_a_missing_ca(pki, tmp_path):
    with serve(postgres(pki["good"])) as port, pytest.raises(psycopg.OperationalError) as caught:
        _pg(port, tmp_path / "missing.pem")
    assert "root certificate file" in str(caught.value)


def test_postgres_accepts_the_right_certificate(pki):
    # Control: the handshake succeeds, then the fake server hangs up (no TLS error).
    with serve(postgres(pki["good"])) as port, pytest.raises(psycopg.OperationalError) as caught:
        _pg(port, pki["ca"])
    message = str(caught.value).lower()
    assert "certificate" not in message
    assert "does not support ssl" not in message


def _redis_ping(url: str) -> None:
    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    try:
        client.ping()
    finally:
        client.close()


@pytest.mark.parametrize("server", ["untrusted", "wrong_name"])
def test_redis_cache_refuses_an_unverifiable_certificate(pki, server):
    with serve(tls_then_close(pki[server])) as port:
        url = f"rediss://127.0.0.1:{port}/0?ssl_cert_reqs=required&ssl_ca_certs={pki['ca'].as_posix()}"
        with pytest.raises(redis.exceptions.ConnectionError) as caught:
            _redis_ping(url)
    assert "certificate" in str(caught.value).lower() or "hostname" in str(caught.value).lower()


def test_redis_cache_refuses_a_plain_text_server(pki):
    with serve(lambda conn: conn.sendall(b"+OK\r\n")) as port:
        url = f"rediss://127.0.0.1:{port}/0?ssl_cert_reqs=required&ssl_ca_certs={pki['ca'].as_posix()}"
        with pytest.raises(redis.exceptions.ConnectionError):
            _redis_ping(url)


def test_broker_ssl_options_verify_even_without_url_parameters(pki):
    """kombu's own fallback for `rediss://` without ssl_* parameters is CERT_NONE; the
    settings' broker_use_ssl (transport.redis_ssl_options) restores verification."""
    from kombu import Connection
    from kombu.exceptions import OperationalError

    options = transport.redis_ssl_options("rediss://redis.example.com:6380/0")
    assert options == {"ssl_cert_reqs": ssl.CERT_REQUIRED, "ssl_check_hostname": True}
    with serve(tls_then_close(pki["untrusted"])) as port:
        connection = Connection(
            f"rediss://127.0.0.1:{port}/0", ssl=options, connect_timeout=5, transport_options={}
        )
        with pytest.raises(OperationalError, match=r"(?i)certificate") as caught:
            connection.ensure_connection(max_retries=0)
        connection.release()
    assert "certificate" in str(caught.value).lower()


def _send(port: int, ca: Path, *, use_tls: bool = True) -> None:
    with override_settings(EMAIL_TLS_CA_FILE=str(ca)):
        backend = VerifiedSMTPBackend(
            host="127.0.0.1", port=port, use_tls=use_tls, fail_silently=False, timeout=5
        )
        EmailMessage("s", "b", "from@example.com", ["to@example.com"], connection=backend).send()


def test_smtp_refuses_a_server_without_starttls(pki):
    import smtplib

    with (
        serve(smtp(pki["good"], offer_starttls=False)) as port,
        pytest.raises(smtplib.SMTPNotSupportedError),
    ):
        _send(port, pki["ca"])


@pytest.mark.parametrize("server", ["untrusted", "wrong_name"])
def test_smtp_refuses_an_unverifiable_certificate(pki, server):
    with serve(smtp(pki[server])) as port, pytest.raises(ssl.SSLCertVerificationError):
        _send(port, pki["ca"])


def test_smtp_refuses_a_missing_ca(pki, tmp_path):
    with serve(smtp(pki["good"])) as port, pytest.raises(FileNotFoundError):
        _send(port, tmp_path / "missing.pem")


def test_smtp_sends_over_verified_starttls(pki):
    with serve(smtp(pki["good"])) as port:
        _send(port, pki["ca"])  # no exception: delivered over TLS to the right server


def test_object_storage_client_refuses_an_unverifiable_certificate(pki):
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config
    from botocore.exceptions import SSLError

    with serve(tls_then_close(pki["untrusted"])) as port:
        client = boto3.client(
            "s3",
            endpoint_url=f"https://127.0.0.1:{port}",
            aws_access_key_id="k",
            aws_secret_access_key="s",
            region_name="us-east-1",
            verify=str(pki["ca"]),
            config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=5),
        )
        with pytest.raises(SSLError):
            client.list_objects_v2(Bucket="arkray")
