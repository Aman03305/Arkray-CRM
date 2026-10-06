"""Production settings: secure by default, every secret from the environment."""

import os
import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False

if len(SECRET_KEY) < 50 or SECRET_KEY.startswith(("dev-", "test-")):  # noqa: F405
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a strong, production-only secret.")
for _fallback in SECRET_KEY_FALLBACKS:  # noqa: F405
    if len(_fallback) < 50 or _fallback.startswith(("dev-", "test-")) or _fallback == SECRET_KEY:  # noqa: F405
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY_FALLBACKS must hold only earlier strong, production-only keys."
        )
if not ALLOWED_HOSTS:  # noqa: F405
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")
if "*" in ALLOWED_HOSTS:  # noqa: F405
    # A wildcard turns off Host-header validation, which password-reset and invitation
    # links, cache keys and redirects rely on (final audit SEC-3).
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must name the hosts, not '*'.")
if ATTACHMENT_STORAGE not in {"filesystem", "s3"}:  # noqa: F405
    # Anything else would silently fall back to local disk (a typo like "S3").
    raise ImproperlyConfigured("ATTACHMENT_STORAGE must be 'filesystem' or 's3'.")
if not 0 < ATTACHMENT_STORAGE_DEADLINE_S <= 25:  # noqa: F405
    # Within gunicorn's 30 s worker timeout, with room for the rest of the request.
    raise ImproperlyConfigured("ATTACHMENT_STORAGE_DEADLINE_S must be between 0 and 25.")
if (
    min(
        ATTACHMENT_STORAGE_MAX_IN_FLIGHT,  # noqa: F405
        ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED,  # noqa: F405
        ATTACHMENT_STORAGE_BREAKER_FAILURES,  # noqa: F405
    )
    < 1
):
    raise ImproperlyConfigured(
        "ATTACHMENT_STORAGE_MAX_IN_FLIGHT, ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED and"
        " ATTACHMENT_STORAGE_BREAKER_FAILURES must be"
        " at least 1."
    )
if ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S <= 0:  # noqa: F405
    raise ImproperlyConfigured("ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S must be positive.")
if "EMAIL_URL" not in os.environ:
    # Without it Django would print every email (invitation and reset links!) to stdout.
    raise ImproperlyConfigured("EMAIL_URL must be set explicitly in production.")
if "APP_BASE_URL" not in os.environ:
    # Emailed invitation and password-reset links point here; never guess it.
    raise ImproperlyConfigured("APP_BASE_URL must be set explicitly in production.")
if "TRUSTED_PROXY_COUNT" not in os.environ:
    # Client IPs drive rate limits and audit records; the proxy topology must be stated,
    # not defaulted (see docs/deployment.md).
    raise ImproperlyConfigured("TRUSTED_PROXY_COUNT must be set explicitly in production.")

# Ask Arkray: no unsafe or silently degraded AI configuration (docs/rag-architecture.md).
if AI_LLM_PROVIDER not in {"anthropic", "none"}:  # noqa: F405
    raise ImproperlyConfigured("AI_LLM_PROVIDER must be 'anthropic' or 'none'.")
if AI_ENABLED and AI_LLM_PROVIDER == "anthropic" and AI_LLM_KEY_HOLDER and not ANTHROPIC_API_KEY:  # noqa: F405
    raise ImproperlyConfigured(
        "AI_LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY (or set AI_LLM_PROVIDER=none)."
    )
if AI_CHAT_EFFORT not in {"low", "medium", "high", "xhigh", "max"}:  # noqa: F405
    raise ImproperlyConfigured("AI_CHAT_EFFORT must be low, medium, high, xhigh or max.")
if (AI_ENABLED or AI_INDEXING_ENABLED) and AI_EMBEDDING_PROVIDER != "local":  # noqa: F405
    # The hashing embedder is a lexical test stand-in: semantic search would silently be
    # word matching.
    raise ImproperlyConfigured("AI_EMBEDDING_PROVIDER must be 'local' in production.")

if os.environ.get("ANTHROPIC_LOG"):
    # The SDK's debug/info logging writes whole requests: questions, notes, tool results.
    raise ImproperlyConfigured("ANTHROPIC_LOG must not be set in production (it logs CRM data).")
for _sdk_variable in ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
    if os.environ.get(_sdk_variable):
        # Where model calls go is AI_LLM_BASE_URL alone (https only, below); headers aren't
        # configurable (Phase 10 review: either could route CRM data and the key elsewhere).
        raise ImproperlyConfigured(
            f"{_sdk_variable} must not be set in production; use AI_LLM_BASE_URL (https)."
        )
# The web server and workers check their database role at start (R74): on by default
# here, off only with the explicit insecure opt-in below (the local Compose stack's single
# superuser). A value that isn't recognisably true or false is refused: env.bool reads
# "", "ture" or "yes please" as false, which silently switched the check off (Phase 11
# review).
_role_check = os.environ.get("DB_REQUIRE_RESTRICTED_ROLE")
if _role_check is not None and _role_check.strip().lower() not in {"true", "false", "1", "0"}:
    raise ImproperlyConfigured("DB_REQUIRE_RESTRICTED_ROLE must be true or false.")
DB_REQUIRE_RESTRICTED_ROLE = env.bool("DB_REQUIRE_RESTRICTED_ROLE", default=True)
# Rate limits (R77): DRF parses them on the first request, so a typo would surface as
# 500s there instead of here.
_RATE = re.compile(r"[1-9][0-9]*/(s|sec|second|m|min|minute|h|hour|d|day)")
for _scope, _rate in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].items():  # noqa: F405
    if not _RATE.fullmatch(str(_rate)):
        raise ImproperlyConfigured(
            f"API_THROTTLE_{_scope.upper()} must look like 600/min, 20/s or 1000/h (got {_rate!r})."
        )
if METRICS_TOKEN and len(METRICS_TOKEN) < 32:  # noqa: F405
    raise ImproperlyConfigured("METRICS_TOKEN must be at least 32 characters (or unset).")

# TLS terminates at the reverse proxy, which must *overwrite* X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_REDIRECT_EXEMPT = [r"^health/"]  # probes speak plain HTTP inside the cluster
SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=31_536_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = False

_NON_DELIVERING_EMAIL = {"console", "filebased", "locmem", "dummy"}

# Turning off HTTPS protections is only allowed with an explicit, greppable opt-in (used by
# the local docker-compose stack, which serves plain HTTP on localhost).
_insecure = [
    name
    for name, weakened in [
        ("DJANGO_SECURE_COOKIES", not SESSION_COOKIE_SECURE),  # noqa: F405
        ("DJANGO_SECURE_SSL_REDIRECT", not SECURE_SSL_REDIRECT),
        ("DJANGO_HSTS_SECONDS", SECURE_HSTS_SECONDS == 0),
        # Account links must not travel over plain HTTP.
        ("APP_BASE_URL", not APP_BASE_URL.startswith("https://")),  # noqa: F405
        # Console/file/in-memory backends would put one-time account links in logs or on
        # disk, or silently drop them.
        ("EMAIL_URL", EMAIL_BACKEND.rsplit(".", 2)[-2] in _NON_DELIVERING_EMAIL),  # noqa: F405
        # Redis holds the broker's tasks and the cache: never open to anyone who can reach
        # it (Phase 9 review). A password (AUTH/ACL) is required; use rediss:// across hosts.
        ("REDIS_CACHE_URL", not urlsplit(REDIS_CACHE_URL).password),  # noqa: F405
        ("CELERY_BROKER_URL", not urlsplit(CELERY_BROKER_URL).password),  # noqa: F405
        # Model calls carry questions, CRM context and the provider key.
        ("AI_LLM_BASE_URL", not AI_LLM_BASE_URL.startswith("https://")),  # noqa: F405
        # A runtime role that could rewrite the audit trail (R74).
        ("DB_REQUIRE_RESTRICTED_ROLE", not DB_REQUIRE_RESTRICTED_ROLE),
    ]
    if weakened
]
if _insecure and not env.bool("DJANGO_ALLOW_INSECURE_LOCAL_HTTP", default=False):
    raise ImproperlyConfigured(
        f"{', '.join(_insecure)} weaken HTTPS protections; set "
        "DJANGO_ALLOW_INSECURE_LOCAL_HTTP=true only for local, non-public deployments."
    )

# Cookie prefixes (Phase 9). Over HTTPS the session and CSRF cookies are `__Host-` cookies:
# Secure, host-only (no Domain) and Path=/, so a sibling subdomain can neither set nor
# shadow them (cookie tossing). The login-device cookie keeps its narrow path (the
# authentication endpoints only), so it is `__Secure-`: only an HTTPS response can set it,
# and its value is signed. Browsers reject prefixed cookies without Secure, so a plain-HTTP
# local stack keeps the plain names. Renaming signs everyone out once, at the first deploy
# with this setting.
if SESSION_COOKIE_SECURE != CSRF_COOKIE_SECURE:  # noqa: F405
    # Both come from DJANGO_SECURE_COOKIES; a mixed override would leave one cookie
    # unprefixed and the other unreadable to the frontend.
    raise ImproperlyConfigured("The session and CSRF cookies must both be Secure, or neither.")
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
SESSION_COOKIE_PATH = "/"
CSRF_COOKIE_PATH = "/"
if SESSION_COOKIE_SECURE and CSRF_COOKIE_SECURE:  # noqa: F405
    SESSION_COOKIE_NAME = "__Host-arkray_session"
    CSRF_COOKIE_NAME = "__Host-arkray_csrftoken"
    LOGIN_DEVICE_COOKIE_NAME = "__Secure-arkray_login_device"
