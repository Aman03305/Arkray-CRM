"""Production settings: secure by default, every secret from the environment."""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False

if len(SECRET_KEY) < 50 or SECRET_KEY.startswith(("dev-", "test-")):  # noqa: F405
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a strong, production-only secret.")
if not ALLOWED_HOSTS:  # noqa: F405
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")
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
    ]
    if weakened
]
if _insecure and not env.bool("DJANGO_ALLOW_INSECURE_LOCAL_HTTP", default=False):
    raise ImproperlyConfigured(
        f"{', '.join(_insecure)} weaken HTTPS protections; set "
        "DJANGO_ALLOW_INSECURE_LOCAL_HTTP=true only for local, non-public deployments."
    )
