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
