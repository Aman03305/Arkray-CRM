"""Test settings. Tests always run against real PostgreSQL (constraints, triggers,
SKIP LOCKED and pgvector behave differently on anything else)."""

import os

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
LOG_LEVEL = "WARNING"
LOGGING["root"]["level"] = "WARNING"  # noqa: F405

# Attachments go to a throwaway directory per test process (never the developer's files).
import tempfile  # noqa: E402

ATTACHMENT_ROOT = tempfile.mkdtemp(prefix="arkray-attachments-")
STORAGES["attachments"] = {  # noqa: F405
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": ATTACHMENT_ROOT, "base_url": None},
}
