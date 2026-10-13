"""Which settings module a process uses when DJANGO_SETTINGS_MODULE isn't set (adversarial
review of 73ff1e3, L14).

`manage.py` and `config/celery.py` fall back to `config.settings.local` (DEBUG, no
database-role check), deliberately: `uv run python manage.py runserver` and `celery -A config
worker` work on a developer's machine without the variable (docs/development.md). In
production that fallback was a trap: a cron job or a service unit started without the variable
ran with DEBUG and without the role check (R74), silently. And `config/wsgi.py`'s production
fallback never applied: importing `config.wsgi` imports the `config` package first, whose
`__init__` imports the Celery app, whose fallback was the development one.

So the backend image declares where it runs: `ARKRAY_RUNTIME=production` (its Dockerfile;
infrastructure/env/production.env.example; a host installation sets it in its service units).
With it, an unset or empty DJANGO_SETTINGS_MODULE means `config.settings.production`, and any
other module is refused, as is any other ARKRAY_RUNTIME value. Without it, development keeps
its fallback. Every entry point calls this before Django or Celery reads the variable
(tests/architecture/test_settings_module.py).
"""

from __future__ import annotations

import os

PRODUCTION = "config.settings.production"
LOCAL = "config.settings.local"
RUNTIME = "ARKRAY_RUNTIME"


class RuntimeMismatch(RuntimeError):
    """A production runtime asked for another settings module (or an unknown runtime)."""


def choose_settings_module(fallback: str = LOCAL) -> str:
    """Set DJANGO_SETTINGS_MODULE if it is unset or empty, by the rule above, and return it."""
    runtime = os.environ.get("ARKRAY_RUNTIME", "")
    chosen = os.environ.get("DJANGO_SETTINGS_MODULE", "").strip()
    if runtime == "production":
        if chosen and chosen != PRODUCTION:
            raise RuntimeMismatch(
                f"{RUNTIME}=production (the production image) runs {PRODUCTION} only, not"
                f" DJANGO_SETTINGS_MODULE={chosen}: unset it, or run another module outside the"
                " production image."
            )
        chosen = PRODUCTION
    elif runtime:
        raise RuntimeMismatch(
            f"{RUNTIME} must be 'production' or unset (got another value): the production"
            " image sets it; nothing else does."
        )
    elif not chosen:
        chosen = fallback
    os.environ["DJANGO_SETTINGS_MODULE"] = chosen
    return chosen
