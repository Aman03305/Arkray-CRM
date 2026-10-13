"""Test settings. Tests always run against real PostgreSQL (constraints, triggers,
SKIP LOCKED and pgvector behave differently on anything else)."""

import os

from django.core.exceptions import ImproperlyConfigured

# Never in the production image (review round 1 of the adversarial review of 73ff1e3,
# INFRA-6): `--settings=config.settings.test` is applied after the entry point's check.
if os.environ.get("ARKRAY_RUNTIME"):
    raise ImproperlyConfigured(
        "config.settings.test is the test suite's: ARKRAY_RUNTIME is set (the production image)."
    )

os.environ.setdefault(
    "DJANGO_SECRET_KEY", "test-only-secret-key-not-used-anywhere-else-000000000000"
)
os.environ.setdefault(
    "DATABASE_URL", "postgres://arkray:arkray_dev_only_password@127.0.0.1:55432/arkray"
)
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("REDIS_CACHE_URL", "redis://127.0.0.1:56379/15")

from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Fast, deterministic, isolated.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
# Two-step verification is opt-in per test (`settings.MFA_REQUIRED_FOR_ADMINS = True`), so
# the many tests that sign administrators in directly keep working (ADR-0033).
MFA_REQUIRED_FOR_ADMINS = False
# Likewise for every user (review of 73ff1e3, L1): `settings.MFA_REQUIRED_FOR_ALL_USERS = True`.
MFA_REQUIRED_FOR_ALL_USERS = False
LOG_LEVEL = "WARNING"
LOGGING["root"]["level"] = "WARNING"  # noqa: F405

# Attachments go to a throwaway directory per test process (never the developer's files).
import tempfile  # noqa: E402

ATTACHMENT_ROOT = tempfile.mkdtemp(prefix="arkray-attachments-")
STORAGES["attachments"] = {  # noqa: F405
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": ATTACHMENT_ROOT, "base_url": None},
}
EXPORT_ROOT = tempfile.mkdtemp(prefix="arkray-exports-")
STORAGES["exports"] = {  # noqa: F405
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": EXPORT_ROOT, "base_url": None},
}
