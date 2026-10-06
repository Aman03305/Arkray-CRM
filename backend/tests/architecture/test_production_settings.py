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
    "CELERY_BROKER_URL": "rediss://:broker-secret@redis.internal:6380/0",
    "REDIS_CACHE_URL": "rediss://:cache-secret@redis.internal:6380/1",
    "DJANGO_ALLOWED_HOSTS": "crm.example.com",
    "DJANGO_SECRET_KEY": STRONG_KEY,
    "TRUSTED_PROXY_COUNT": "1",
    "APP_BASE_URL": "https://crm.example.com",
    "EMAIL_URL": "smtp+tls://mailer:secret@smtp.example.com:587",
}


DEFAULT_PRINT = (
    "settings.DEBUG, settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE,"
    " settings.SECURE_SSL_REDIRECT, settings.SECURE_HSTS_SECONDS"
)


def load_production_settings(
    _print: str = DEFAULT_PRINT, **overrides: str | None
) -> subprocess.CompletedProcess[str]:
    """Import production settings in a clean interpreter and print `_print` (settings
    attributes). An override of None unsets."""
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
        f"from django.conf import settings; print({_print})"
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
        {"REDIS_CACHE_URL": "redis://redis.internal:6379/1"},  # Phase 9: no AUTH
        {"CELERY_BROKER_URL": "redis://redis.internal:6379/0"},  # Phase 9: no AUTH
    ],
)
def test_weakening_https_requires_an_explicit_local_opt_in(weakening):
    refused = load_production_settings(**weakening)
    assert refused.returncode != 0
    assert "DJANGO_ALLOW_INSECURE_LOCAL_HTTP" in refused.stderr
    allowed = load_production_settings(**weakening, DJANGO_ALLOW_INSECURE_LOCAL_HTTP="true")
    assert allowed.returncode == 0, allowed.stderr


COOKIES = (
    "settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME, settings.LOGIN_DEVICE_COOKIE_NAME,"
    " settings.SESSION_COOKIE_DOMAIN, settings.CSRF_COOKIE_DOMAIN,"
    " settings.SESSION_COOKIE_PATH, settings.CSRF_COOKIE_PATH"
)


def test_https_cookies_carry_host_and_secure_prefixes():
    """Phase 9: `__Host-` (Secure, no Domain, Path=/) for the session and CSRF cookies; the
    path-scoped login-device cookie is `__Secure-`."""
    result = load_production_settings(COOKIES)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "__Host-arkray_session",
        "__Host-arkray_csrftoken",
        "__Secure-arkray_login_device",
        "None",
        "None",
        "/",
        "/",
    ]


def test_a_plain_http_local_stack_keeps_unprefixed_cookie_names():
    """Browsers reject prefixed cookies without Secure: the opted-in local stack would
    otherwise lose its session and CSRF cookies."""
    result = load_production_settings(
        COOKIES, DJANGO_SECURE_COOKIES="false", DJANGO_ALLOW_INSECURE_LOCAL_HTTP="true"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[:3] == [
        "arkray_session",
        "arkray_csrftoken",
        "arkray_login_device",
    ]


def test_the_ai_sdks_debug_logging_is_refused():
    """Phase 9 review: ANTHROPIC_LOG makes the SDK log whole requests (questions, notes)."""
    result = load_production_settings(ANTHROPIC_LOG="debug")
    assert result.returncode != 0
    assert "ANTHROPIC_LOG" in result.stderr


@pytest.mark.parametrize("variable", ["ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"])
def test_the_sdks_own_routing_variables_are_refused(variable):
    """Phase 10 review, P2: the SDK read ANTHROPIC_BASE_URL (even plain http) and extra
    headers from the environment, so a stray value could send questions, CRM context and
    the key elsewhere. Where calls go is AI_LLM_BASE_URL alone."""
    result = load_production_settings(**{variable: "http://elsewhere.invalid:8080"})
    assert result.returncode != 0
    assert variable in result.stderr


def test_model_calls_must_use_https():
    plain = load_production_settings(AI_LLM_BASE_URL="http://gateway.internal")
    assert plain.returncode != 0
    assert "AI_LLM_BASE_URL" in plain.stderr
    gateway = load_production_settings(
        "settings.AI_LLM_BASE_URL", AI_LLM_BASE_URL="https://gateway.example.com"
    )
    assert gateway.returncode == 0, gateway.stderr
    assert gateway.stdout.strip() == "https://gateway.example.com"
    assert load_production_settings("settings.AI_LLM_BASE_URL").stdout.strip() == (
        "https://api.anthropic.com"
    )


def test_the_provider_client_is_given_the_configured_host(settings, monkeypatch):
    """Even outside production, the SDK never picks ANTHROPIC_BASE_URL up by itself."""
    from arkray.ai.llm import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://elsewhere.invalid:8080")
    settings.ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"
    settings.AI_LLM_BASE_URL = "https://api.anthropic.com"
    client = AnthropicProvider()._client
    assert str(client.base_url).startswith("https://api.anthropic.com")


@pytest.mark.parametrize(
    ("rate", "accepted"),
    [
        ("60/min", True),
        ("20/s", True),
        ("1000/hour", True),
        ("5/day", True),
        ("10/week", False),
        ("0/min", False),
        ("abc", False),
        ("60", False),
        ("60/", False),
    ],
)
def test_rate_limits_are_validated_at_startup(rate, accepted):
    """Phase 11 (R77: operators resize the sign-in limit): DRF parses rates on the first
    request, so a typo was a 500 there, not a failed start."""
    result = load_production_settings(API_THROTTLE_AUTH=rate)
    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "API_THROTTLE_AUTH" in result.stderr


def test_a_previous_secret_key_can_be_kept_for_a_rotation():
    """Phase 11: the code honoured SECRET_KEY_FALLBACKS since Phase 9, but no deployment
    could set it, so rotating the key signed everyone out at once."""
    previous = "p" * 64
    kept = load_production_settings(
        "settings.SECRET_KEY_FALLBACKS", DJANGO_SECRET_KEY_FALLBACKS=previous
    )
    assert kept.returncode == 0, kept.stderr
    assert kept.stdout.strip() == f"['{previous}']"
    for weak in ("short", "dev-" + "x" * 60, STRONG_KEY):
        refused = load_production_settings(DJANGO_SECRET_KEY_FALLBACKS=weak)
        assert refused.returncode != 0
        assert "DJANGO_SECRET_KEY_FALLBACKS" in refused.stderr


def test_the_database_role_is_checked_by_default_in_production():
    printed = load_production_settings("settings.DB_REQUIRE_RESTRICTED_ROLE").stdout.strip()
    assert printed == "True"


@pytest.mark.parametrize(("token", "accepted"), [("", True), ("x" * 31, False), ("x" * 32, True)])
def test_a_metrics_token_is_long_or_absent(token, accepted):
    """Phase 10 review: the endpoint's only protection is the token."""
    result = load_production_settings(METRICS_TOKEN=token)
    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "METRICS_TOKEN" in result.stderr


def test_only_the_process_calling_the_provider_needs_its_key():
    """Phase 9 review: the key was in every container; only the ai worker holds it now."""
    anthropic = {"AI_ENABLED": "true", "AI_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": ""}
    refused = load_production_settings(**anthropic)  # AI_LLM_KEY_HOLDER defaults to true
    assert refused.returncode != 0
    assert "ANTHROPIC_API_KEY" in refused.stderr
    web = load_production_settings(**anthropic, AI_LLM_KEY_HOLDER="false")
    assert web.returncode == 0, web.stderr


def test_the_cache_never_unpickles():
    result = load_production_settings("settings.CACHES['default']['OPTIONS']['SERIALIZER']")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "django_redis.serializers.json.JSONSerializer"


@pytest.mark.parametrize("value", ["", " ", "ture", "yes please"])
def test_an_unrecognisable_role_check_value_is_refused(value):
    """Phase 11 review: env.bool read these as false and silently switched the check off."""
    result = load_production_settings(DB_REQUIRE_RESTRICTED_ROLE=value)
    assert result.returncode != 0
    assert "DB_REQUIRE_RESTRICTED_ROLE" in result.stderr


def test_switching_the_role_check_off_needs_the_insecure_opt_in():
    off = load_production_settings(DB_REQUIRE_RESTRICTED_ROLE="false")
    assert off.returncode != 0
    assert "DB_REQUIRE_RESTRICTED_ROLE" in off.stderr
    local = load_production_settings(
        "settings.DB_REQUIRE_RESTRICTED_ROLE",
        DB_REQUIRE_RESTRICTED_ROLE="false",
        DJANGO_ALLOW_INSECURE_LOCAL_HTTP="true",
    )
    assert local.returncode == 0, local.stderr
    assert local.stdout.strip() == "False"


def test_wildcard_allowed_hosts_is_rejected():
    """'*' disables Host-header validation, which emailed links and redirects rely on."""
    result = load_production_settings(DJANGO_ALLOWED_HOSTS="*")
    assert result.returncode != 0
    assert "DJANGO_ALLOWED_HOSTS" in result.stderr
    # A leading-dot subdomain wildcard is Django's own, bounded syntax: still allowed.
    assert load_production_settings(DJANGO_ALLOWED_HOSTS=".example.com").returncode == 0


@pytest.mark.parametrize("value", ["S3", "s3 ", "minio", "disk"])
def test_attachment_storage_must_be_exactly_filesystem_or_s3(value):
    """A typo would silently put attachments on the container's local disk."""
    result = load_production_settings(ATTACHMENT_STORAGE=value)
    assert result.returncode != 0
    assert "ATTACHMENT_STORAGE" in result.stderr


def test_s3_attachment_storage_has_bounded_client_timeouts():
    """A dead S3 endpoint must cost a request seconds, not a web worker (final audit SRE-1:
    botocore's defaults took 113 s against a black hole, beyond gunicorn's 30 s timeout)."""
    options = "settings.STORAGES['attachments']['OPTIONS']['client_config']"
    result = load_production_settings(
        f"{options}.connect_timeout, {options}.read_timeout, {options}.retries",
        ATTACHMENT_STORAGE="s3",
        ATTACHMENT_S3_BUCKET="arkray-attachments",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3 10 {'total_max_attempts': 2, 'mode': 'standard'}"
    # (connect + read) x total attempts, for the one call that can stall: inside gunicorn's 30 s
    assert (3 + 10) * 2 < 30
