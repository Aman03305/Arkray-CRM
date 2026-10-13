"""Gunicorn configuration (production container). Values are env-overridable."""

import multiprocessing
import os

# Serving HTTP runs the production settings unless the caller names a module. config/wsgi.py's
# own fallback comes too late: importing `config.wsgi` imports the `config` package first,
# whose Celery app falls back to the development settings (config/settings_module.py;
# adversarial review of 73ff1e3, L14). Gunicorn reads this file before it loads the
# application and before `on_starting` sets Django up, so a server started without the
# variable (outside the image, which also sets it) never runs DEBUG.
# An empty value counts as unset (review round 1, INFRA-6: `setdefault` kept "", and the
# Celery app's fallback then chose the development settings).
if not os.environ.get("DJANGO_SETTINGS_MODULE", "").strip():
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.production"

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 8)))
worker_class = "sync"
# A request that runs longer than this is killed; DB statement_timeout (10s) fires first.
timeout = int(os.environ.get("GUNICORN_TIMEOUT_S", "30"))
graceful_timeout = 30
keepalive = 5
# Recycle workers periodically to contain slow memory growth.
max_requests = 2000
max_requests_jitter = 200
# Access logging is done by RequestContextMiddleware (structured, with request IDs).
accesslog = None
errorlog = "-"
# gunicorn's own error log as the application's JSON lines (arkray.core.logging): no query
# strings (gunicorn writes the request URI into "Error handling request ..."), exceptions by
# type and frame, never their messages (privacy remediation P2-2).
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "arkray.core.logging.JsonFormatter"}},
    "filters": {"gunicorn": {"()": "arkray.core.logging.GunicornErrorFilter"}},
    "handlers": {
        "error_console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["gunicorn"],
            "stream": "ext://sys.stderr",
        }
    },
    "root": {"level": "INFO", "handlers": []},
    "loggers": {
        "gunicorn.error": {"level": "INFO", "handlers": ["error_console"], "propagate": False},
        "gunicorn.access": {"level": "INFO", "handlers": [], "propagate": False},
    },
}
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
# Gunicorn 26's control socket isn't used (no runtime administration), and the image's root
# filesystem is read-only: it logged an error at every start (Phase 11). Off.
control_socket_disable = True


_role_checked = False


def on_starting(server: object) -> None:
    """Refuse to serve as a privileged database role (R74), once, in the master, before any
    worker forks (the check closes its connection, so the workers inherit none)."""
    global _role_checked
    import django

    django.setup()
    from arkray.core.privileges import enforce_at_startup

    _role_checked = enforce_at_startup()

    # The erasure ledger's chain, verified once here: the workers fork with it, so a
    # recycled worker's first request reads only the entries added since (core.ledger_gate).
    from arkray.core.ledger_gate import prime

    prime()


def pre_fork(server: object, worker: object) -> None:
    """The master's verified chain brought up to date before each worker forks, for at most
    ledger_gate.REFRESH_BUDGET_S (the store only, no database): a worker recycled long after
    the start (max_requests) inherits a recent state instead of reading every entry added
    since inside its first checks (review round 2, LEDGER2-3)."""
    from arkray.core.ledger_gate import refresh

    refresh()


def post_worker_init(worker: object) -> None:
    """If the database was unreachable when the master started, each worker checks before
    it serves (Phase 11 review: otherwise a privileged role was never checked). A refusal
    here is a boot error: gunicorn stops instead of respawning."""
    if not _role_checked:
        from arkray.core.privileges import enforce_at_startup

        enforce_at_startup()


# No product or version in the Server header (Phase 9 review). gunicorn reads this module
# attribute for every response; the master sets it before forking the workers.
import gunicorn.http.wsgi  # type: ignore[import-untyped]  # noqa: E402

gunicorn.http.wsgi.SERVER = "arkray"
