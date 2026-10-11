"""The database TLS requirement is judged on where libpq actually connects (final production
audit P3; core.transport.database_problem).

A loopback HOST exempted a URL whose `hostaddr` sent the connection elsewhere, and a URL
without a host was exempt although libpq then connects to PGHOST from the environment.
"""

from __future__ import annotations

import environ
import pytest

from arkray.core import transport


def problem(url: str, private: tuple[str, ...] = ()) -> str | None:
    return transport.database_problem(environ.Env.db_url_config(url), private)


@pytest.fixture(autouse=True)
def no_libpq_environment(monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGHOSTADDR", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u:p@127.0.0.1:5432/arkray?hostaddr=203.0.113.5",
        "postgres://u:p@/arkray?host=db.example.com",
        "postgres://u:p@/arkray?host=127.0.0.1,db.example.com",
    ],
    ids=["hostaddr", "host-option", "host-list"],
)
def test_a_remote_destination_needs_tls_however_it_is_named(url):
    assert problem(url) is not None


def test_the_urls_host_wins_over_a_host_option():
    """Django connects to HOST when it is set, whatever `?host=` says (review P3)."""
    assert problem("postgres://u:p@db.example.com/arkray?host=127.0.0.1") is not None


def test_no_host_follows_pghost_from_the_environment(monkeypatch):
    monkeypatch.setenv("PGHOST", "db.example.com")
    assert problem("postgres://u:p@/arkray") is not None
    monkeypatch.setenv("PGHOST", "/var/run/postgresql")
    assert problem("postgres://u:p@/arkray") is None


def test_pghostaddr_from_the_environment_counts_too(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.5")
    assert problem("postgres://u:p@127.0.0.1/arkray") is not None


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u:p@/arkray",  # no host anywhere: the local Unix socket
        "postgres://u:p@127.0.0.1:5432/arkray",
        "postgres://u:p@127.0.0.1/arkray?hostaddr=127.0.0.1",
        "postgres://u:p@db.internal/arkray?hostaddr=10.0.0.5",
    ],
    ids=["socket", "loopback", "loopback-hostaddr", "private-listed"],
)
def test_local_and_listed_private_destinations_stay_exempt(url):
    assert problem(url, ("db.internal", "10.0.0.5")) is None


def test_verified_tls_satisfies_a_remote_hostaddr():
    url = (
        "postgres://u:p@db.example.com/arkray?hostaddr=203.0.113.5"
        "&sslmode=verify-full&sslrootcert=system"
    )
    assert problem(url) is None
