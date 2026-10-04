"""Gunicorn configuration (production container). Values are env-overridable."""

import multiprocessing
import os

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
