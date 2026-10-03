"""Production settings must refuse to start with unsafe configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
STRONG_KEY = "k" * 64
BASE_ENV = {
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
    "CELERY_BROKER_URL": "memory://",
    "REDIS_CACHE_URL": "redis://localhost:6379/0",
    "DJANGO_ALLOWED_HOSTS": "crm.example.com",
    "DJANGO_SECRET_KEY": STRONG_KEY,
    "TRUSTED_PROXY_COUNT": "1",
    "APP_BASE_URL": "https://crm.example.com",
    "EMAIL_URL": "smtp+tls://mailer:secret@smtp.example.com:587",
}


def load_production_settings(**overrides: str | None) -> subprocess.CompletedProcess[str]:
    """Import production settings in a clean interpreter. An override of None unsets."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "DATABASE_", "TRUSTED_PROXY", "APP_BASE_URL", "EMAIL_"))
    }
    env.update(BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    code = (
        "import django, os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings.production';"
        "from django.conf import settings;"
        "print(settings.DEBUG, settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE,"
        " settings.SECURE_SSL_REDIRECT, settings.SECURE_HSTS_SECONDS)"
    )
    return subprocess.run(  # noqa: S603 — fixed interpreter and code
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_secure_defaults():
    result = load_production_settings()
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "True", "True", "True", "31536000"]


@pytest.mark.parametrize("key", ["short", "dev-only-insecure-key-" + "0" * 60])
def test_weak_or_dev_secret_key_is_rejected(key):
    result = load_production_settings(DJANGO_SECRET_KEY=key)
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_missing_allowed_hosts_is_rejected():
    result = load_production_settings(DJANGO_ALLOWED_HOSTS="")
    assert result.returncode != 0
    assert "DJANGO_ALLOWED_HOSTS" in result.stderr


def test_proxy_topology_must_be_stated_explicitly():
    result = load_production_settings(TRUSTED_PROXY_COUNT=None)
    assert result.returncode != 0
    assert "TRUSTED_PROXY_COUNT" in result.stderr


def test_email_delivery_must_be_configured_explicitly():
    """Without EMAIL_URL Django would print every email, links included, to stdout."""
    result = load_production_settings(EMAIL_URL=None)
    assert result.returncode != 0
    assert "EMAIL_URL" in result.stderr


def test_app_base_url_must_be_stated_explicitly():
    """Emailed invitation and reset links are built from it; never guess."""
    result = load_production_settings(APP_BASE_URL=None)
    assert result.returncode != 0
    assert "APP_BASE_URL" in result.stderr


@pytest.mark.parametrize(
    "weakening",
    [
        {"DJANGO_SECURE_COOKIES": "false"},
        {"DJANGO_SECURE_SSL_REDIRECT": "false"},
        {"DJANGO_HSTS_SECONDS": "0"},
        {"APP_BASE_URL": "http://crm.example.com"},  # account links over plain HTTP
        {"EMAIL_URL": "consolemail://"},  # account links printed to the logs
        {"EMAIL_URL": "filemail:///tmp/mail"},  # account links written to disk
    ],
)
def test_weakening_https_requires_an_explicit_local_opt_in(weakening):
    refused = load_production_settings(**weakening)
    assert refused.returncode != 0
    assert "DJANGO_ALLOW_INSECURE_LOCAL_HTTP" in refused.stderr
    allowed = load_production_settings(**weakening, DJANGO_ALLOW_INSECURE_LOCAL_HTTP="true")
    assert allowed.returncode == 0, allowed.stderr
