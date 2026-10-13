"""Local development settings. Reads the repository-root `.env` if present."""

import os
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

# Never in the production image (review round 1 of the adversarial review of 73ff1e3,
# INFRA-6): `manage.py --settings=config.settings.local` is applied after the entry point's
# own check (config/settings_module.py), so these settings refuse that runtime themselves.
if os.environ.get("ARKRAY_RUNTIME"):
    raise ImproperlyConfigured(
        "config.settings.local is development's: ARKRAY_RUNTIME is set (the production image)."
    )

_env_file = Path(__file__).resolve().parents[3] / ".env"
if _env_file.exists():
    environ.Env.read_env(_env_file, overwrite=False)

from .base import *  # noqa: E402, F403

DEBUG = True
# A developer's own data on their own screen: full exception messages help (core.logging).
LOG_EXCEPTION_MESSAGES = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
