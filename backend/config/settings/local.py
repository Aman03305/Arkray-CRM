"""Local development settings. Reads the repository-root `.env` if present."""

from pathlib import Path

import environ

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
