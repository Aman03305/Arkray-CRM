"""Settings shared by every environment.

All configuration comes from environment variables (12-factor). Environment modules
(`local`, `test`, `production`) only override what genuinely differs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import environ
from celery.schedules import crontab

from arkray.core.redis import fail_fast_pool_kwargs

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()

# --- Core ----------------------------------------------------------------------------------
DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY")
# Rotation without signing everyone out: the previous key(s), still accepted for sessions,
# sealed page links and trusted-browser cookies until removed (docs/runbooks.md#rotate-a-secret).
SECRET_KEY_FALLBACKS: list[str] = env.list("DJANGO_SECRET_KEY_FALLBACKS", default=[])
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# The API uses slash-less URLs (Next.js strips trailing slashes before proxying).
APPEND_SLASH = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    # Arkray modules — ordered from the lowest layer upwards.
    "arkray.core",
    "arkray.audit",
    "arkray.identity",
    "arkray.leads",
    "arkray.pipeline",
    "arkray.activities",
    "arkray.dashboard",
    "arkray.search",
    "arkray.ai",
    "arkray.privacy",
]

MIDDLEWARE = [
    "arkray.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Idle timeout + absolute lifetime for signed-in sessions (identity.sessions).
    "arkray.identity.sessions.SessionPolicyMiddleware",
    # An administrator's support session, if any (identity.support): after the session policy,
    # so an expired browser session never carries one.
    "arkray.identity.support.SupportSessionMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES: list[dict[str, Any]] = []  # API-only service: no server-rendered HTML.

# --- Database ------------------------------------------------------------------------------
# Every connection gets hard timeouts so one slow query or stuck transaction cannot pin a
# worker (and, transitively, exhaust the connection budget). See docs/reliability.md.
_db = env.db("DATABASE_URL")
_db_options: dict[str, Any] = {
    "connect_timeout": env.int("DB_CONNECT_TIMEOUT_S", default=5),
    # A database that stops answering without closing connections (a partition, a host
    # failing over) must become an error, not a request waiting for ever: keepalives notice
    # a silent peer within about a minute, and tcp_user_timeout fails a write the server
    # never acknowledges after 30 s (whole-software audit; libpq, where the OS supports it).
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 30_000,
    "options": " ".join(
        [
            f"-c statement_timeout={env.int('DB_STATEMENT_TIMEOUT_MS', default=10_000)}",
            f"-c lock_timeout={env.int('DB_LOCK_TIMEOUT_MS', default=5_000)}",
            "-c idle_in_transaction_session_timeout="
            f"{env.int('DB_IDLE_IN_TRANSACTION_TIMEOUT_MS', default=60_000)}",
            # Short OLTP statements: JIT compilation only adds latency here (Phase 5 review:
            # +10-30 ms per dashboard aggregate near the threshold, +290-430 ms above the
            # inlining threshold, for queries that run in milliseconds without it).
            "-c jit=off",
        ]
    ),
}
# The runtime role must own nothing and be no superuser (R74; arkray.core.privileges).
# Production refuses to start otherwise; development and tests run as the database's
# superuser.
DB_REQUIRE_RESTRICTED_ROLE = env.bool("DB_REQUIRE_RESTRICTED_ROLE", default=False)

if env.bool("DB_POOL_ENABLED", default=False):
    # Per-process pool (psycopg_pool). Total connections = processes x max_size; keep that
    # below the budget documented in docs/reliability.md ("Connection budget").
    _db_options["pool"] = {
        "min_size": env.int("DB_POOL_MIN_SIZE", default=1),
        "max_size": env.int("DB_POOL_MAX_SIZE", default=4),
        "timeout": env.int("DB_POOL_TIMEOUT_S", default=5),
    }
    _db["CONN_MAX_AGE"] = 0
else:
    _db["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE_S", default=60)
    _db["CONN_HEALTH_CHECKS"] = True
_db["OPTIONS"] = _db_options
DATABASES = {"default": _db}

# --- Cache (Redis) -------------------------------------------------------------------------
# The cache is an optimisation, never a dependency: Redis errors are swallowed (and logged)
# so the CRM keeps working during a Redis outage. See docs/reliability.md.
REDIS_CACHE_URL = env("REDIS_CACHE_URL")
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "KEY_PREFIX": "arkray",
        "OPTIONS": {
            # JSON, never pickle: whoever can write to Redis must not be able to run code in
            # every process that reads the cache (Phase 9 review). Cached values are numbers,
            # strings and lists of numbers.
            "SERIALIZER": "django_redis.serializers.json.JSONSerializer",
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
            "IGNORE_EXCEPTIONS": True,
            # No retries + a circuit breaker: an outage costs one failed attempt per
            # cool-down, not seconds on every request (arkray.core.redis).
            "CONNECTION_POOL_KWARGS": fail_fast_pool_kwargs(REDIS_CACHE_URL),
        },
    }
}
DJANGO_REDIS_IGNORE_EXCEPTIONS = True
# Not one traceback per cache operation during an outage: arkray.core.redis logs a single
# warning each time the circuit opens.
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = False

# --- Authentication ------------------------------------------------------------------------
AUTH_USER_MODEL = "identity.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
# Length and known-bad-password checks, not composition rules (NIST SP 800-63B); see
# docs/authorization.md#password-policy.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ("email", "first_name", "last_name")},
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "arkray.identity.passwords.MaximumLengthValidator", "OPTIONS": {"max_length": 128}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "arkray.identity.passwords.ContextSpecificWordsValidator"},
]

# Server-side sessions in PostgreSQL: a Redis outage never logs anyone out, and
# deactivating a user takes effect on their very next request.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "arkray_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Absolute lifetime: a session ends this long after sign-in, however active it is.
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE_S", default=12 * 60 * 60)
SESSION_COOKIE_SECURE = env.bool("DJANGO_SECURE_COOKIES", default=True)
# Idle timeout: a session unused for this long ends. Activity is recorded at most every
# SESSION_ACTIVITY_REFRESH_S, so an active user costs one session write per interval.
SESSION_IDLE_TIMEOUT_S = env.int("SESSION_IDLE_TIMEOUT_S", default=2 * 60 * 60)
SESSION_ACTIVITY_REFRESH_S = 5 * 60
# List page links (keyset cursors) expire after the longest a session can last: no signed-in
# user ever needs an older one (core/keyset.py).
KEYSET_CURSOR_MAX_AGE_S = SESSION_COOKIE_AGE

# The SPA reads the CSRF cookie and echoes it in the X-CSRFToken header (double submit).
CSRF_COOKIE_NAME = "arkray_csrftoken"
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = env.bool("DJANGO_SECURE_COOKIES", default=True)
CSRF_TRUSTED_ORIGINS: list[str] = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
CSRF_FAILURE_VIEW = "arkray.core.views.csrf_failure"

# --- Account flows (identity) ---------------------------------------------------------------
# Public base URL of the web app, used to build links in emails (no trailing slash).
APP_BASE_URL = env("APP_BASE_URL", default="http://localhost:3000").rstrip("/")
ACCOUNT_INVITATION_TTL_S = env.int("ACCOUNT_INVITATION_TTL_S", default=72 * 60 * 60)
PASSWORD_RESET_TTL_S = env.int("PASSWORD_RESET_TTL_S", default=60 * 60)
# An admin cannot re-send an invitation to the same user more often than this.
INVITATION_RESEND_COOLDOWN_S = 60

# Durable, PostgreSQL-backed login throttling (identity.throttling). Bounded by design:
# lockouts back off exponentially but never exceed LOGIN_LOCKOUT_MAX_S.
LOGIN_THROTTLE_WINDOW_S = 15 * 60
LOGIN_ACCOUNT_FAILURE_THRESHOLD = 5  # per submitted email and browser, within the window
LOGIN_LOCKOUT_BASE_S = 60
LOGIN_LOCKOUT_MAX_S = 15 * 60
LOGIN_SOURCE_FAILURE_THRESHOLD = 50  # per client IP, within the window
# Signed, HttpOnly cookie marking a browser that has signed in to an account before. That
# browser gets its own failure budget, so an attacker's failures cannot lock the real user
# out (see docs/authorization.md#login-throttling).
LOGIN_DEVICE_COOKIE_NAME = "arkray_login_device"
LOGIN_DEVICE_COOKIE_AGE_S = 90 * 24 * 60 * 60
# Password-reset requests: per client IP (durable), and emails per account (in the job).
PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR = 20
PASSWORD_RESET_ACCOUNT_LIMIT_PER_HOUR = 3
# Throttle evidence older than this is purged (hourly housekeeping).
AUTH_THROTTLE_RETENTION_S = 24 * 60 * 60

# --- Security headers (API responses) ------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- Internationalisation / time -----------------------------------------------------------
LANGUAGE_CODE = "en-us"
USE_I18N = True
TIME_ZONE = "UTC"  # storage and APIs are UTC; business "today" uses CRM_TIME_ZONE
USE_TZ = True

# --- Arkray business settings --------------------------------------------------------------
# Fixed, not deployment options: the web app formats every date in IST and every amount in
# rupees (frontend/src/lib/format.ts, money.ts). Configurable here alone, a different value
# made the API and the screens disagree on "today" and on the currency (whole-software
# audit, P2); tests/architecture/test_business_constants.py keeps the two in step.
CRM_TIME_ZONE = "Asia/Kolkata"
CRM_CURRENCY = "INR"
# Number of reverse proxies in front of Django whose X-Forwarded-For entries are trusted.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)
# Adopt an incoming X-Request-ID as our correlation id only when a trusted edge proxy sets
# (overwrites) it. Otherwise a client could spoof or replay ids that end up in audit records;
# the client's value is then only logged as `client_request_id`.
TRUST_INCOMING_REQUEST_ID = env.bool("TRUST_INCOMING_REQUEST_ID", default=False)
# Window during which repeated admin access to the same user's workspace is audited once.
WORKSPACE_ACCESS_AUDIT_WINDOW_S = env.int("WORKSPACE_ACCESS_AUDIT_WINDOW_S", default=15 * 60)
# Support sessions (identity.support): how long one lasts (no extension; start a new one).
SUPPORT_SESSION_TTL_S = env.int("SUPPORT_SESSION_TTL_S", default=30 * 60)
# How long an administrator-chosen password (a new user's initial one, or a reset) can be
# used to sign in before it must have been changed; after that an administrator sets a new one.
TEMPORARY_PASSWORD_TTL_S = env.int("TEMPORARY_PASSWORD_TTL_S", default=72 * 60 * 60)
# The metrics endpoint (/health/metrics, config/metrics.py) answers only a scraper sending
# this token as a bearer token; unset, it doesn't exist (404).
METRICS_TOKEN = env("METRICS_TOKEN", default="")

# --- Django REST Framework -----------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # DRF's session authentication, answering 401 (not 403) to anonymous requests.
        "arkray.core.authentication.SessionAuthentication",
    ],
    # Deny by default: every view must declare its own permission classes explicitly.
    "DEFAULT_PERMISSION_CLASSES": ["arkray.core.permissions.DenyAll"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # UTF-8 JSON only; the charset in Content-Type is never used as a codec (core.parsers).
    "DEFAULT_PARSER_CLASSES": ["arkray.core.parsers.Utf8JSONParser"],
    # No default pagination: every list pages through core.keyset's sealed, bound cursors
    # (ADR-0016). A DRF default was never used, and would have handed a future generic
    # list view unsigned, unbound cursors (whole-software audit).
    "DEFAULT_PAGINATION_CLASS": None,
    # No OPTIONS metadata: it would describe serializers to anyone who asks.
    "DEFAULT_METADATA_CLASS": None,
    "EXCEPTION_HANDLER": "arkray.core.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_THROTTLE_ANON", default="60/min"),
        "user": env("API_THROTTLE_USER", default="600/min"),
        # Public authentication endpoints (on top of the durable login throttle).
        "auth": env("API_THROTTLE_AUTH", default="20/min"),
        # Global search: the most expensive read per request, sent as people type (a
        # 250 ms debounce): about two searches a second sustained per user (docs/search.md).
        "search": env("API_THROTTLE_SEARCH", default="120/min"),
        # Ask Arkray: each question may call a language model (docs/rag-architecture.md).
        "ask": env("API_THROTTLE_ASK", default="20/min"),
    },
    "NUM_PROXIES": TRUSTED_PROXY_COUNT,
    "COERCE_DECIMAL_TO_STRING": True,  # money is serialised as strings, never floats
    # ASCII-escaped JSON output: any text echoed from a request (e.g. an unknown key holding
    # a lone surrogate) can always be rendered, never a 500.
    "UNICODE_JSON": False,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Arkray CRM API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # One enum per choice set, however many fields use it.
    "ENUM_NAME_OVERRIDES": {
        "RatingEnum": "arkray.leads.models.Rating",
        "StatusEnum": "arkray.identity.models.UserStatus",
        "CategoryEnum": "arkray.leads.models.StatusCategory",
        "StageCategoryEnum": "arkray.pipeline.models.StageCategory",
        "ActivityTypeEnum": "arkray.activities.models.ActivityType",
        "ActivityStatusEnum": "arkray.activities.models.ActivityStatus",
        "PriorityEnum": "arkray.activities.models.Priority",
        "TimelineKindEnum": "arkray.activities.models.TimelineKind",
        "QuestionStatusEnum": "arkray.ai.models.QuestionStatus",
        "AskRecordKindEnum": "arkray.ai.api.serializers.REF_KINDS",
        "AskFactKindEnum": "arkray.ai.api.serializers.FACT_KINDS",
        "AskBlockTypeEnum": "arkray.ai.api.serializers.BLOCK_TYPES",
        "AskAnswerModeEnum": "arkray.ai.api.serializers.ANSWER_MODES",
        "StageTypeEnum": "arkray.pipeline.models.StageType",
        "FieldTypeEnum": "arkray.pipeline.models.FieldType",
        "NegotiationSourceEnum": "arkray.pipeline.models.NegotiationSource",
        "ScanStatusEnum": "arkray.activities.models.ScanStatus",
    },
}

# --- Email ---------------------------------------------------------------------------------
# Email is always sent from background jobs (outbox), never inline in a web request.
_email = env.email_url("EMAIL_URL", default="consolemail://")
EMAIL_BACKEND = _email["EMAIL_BACKEND"]
EMAIL_HOST = _email.get("EMAIL_HOST") or "localhost"
EMAIL_PORT = _email.get("EMAIL_PORT") or 25
EMAIL_HOST_USER = _email.get("EMAIL_HOST_USER") or ""
EMAIL_HOST_PASSWORD = _email.get("EMAIL_HOST_PASSWORD") or ""
EMAIL_USE_TLS = bool(_email.get("EMAIL_USE_TLS", False))
EMAIL_USE_SSL = bool(_email.get("EMAIL_USE_SSL", False))
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT_S", default=10)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Arkray CRM <no-reply@arkray.local>")

# --- Celery (background jobs) --------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL")
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": 3600,
    "socket_timeout": 5,
    "socket_connect_timeout": 5,
}
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 100
CELERY_TASK_TIME_LIMIT = 120
CELERY_TASK_DEFAULT_QUEUE = "default"
# The relay gets its own queue so it is never stuck behind a backlog of real work (which
# would stall every queue, including email). Production runs a dedicated consumer for it.
CELERY_TASK_ROUTES = {
    "core.outbox.relay": {"queue": "outbox"},
    # Ask Arkray questions are dispatched straight to their own queue (an answer is
    # interactive; the outbox relay's 5 s tick would add latency for nothing) and answered
    # by their own workers, so a slow AI provider can never occupy the web workers or the
    # workers that send email and run CRM background work.
    "ai.answer_question": {"queue": "ai"},
    "ai.housekeeping": {"queue": "default"},
    "ai.reconcile_index": {"queue": "ai_index"},
}
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
# Wall-clock schedules for everything slower than the relay: beat keeps its schedule file
# in tmpfs (read-only containers), so every restart (every release) reset interval timers,
# and a daily job could be pushed back indefinitely (Phase 11 review). A crontab fires at
# its time whatever happened since.
CELERY_BEAT_SCHEDULE = {
    "outbox-relay": {
        "task": "core.outbox.relay",
        "schedule": 5.0,
        # A relay tick that cannot run within 10s is dropped rather than piling up.
        "options": {"expires": 10},
    },
    "core-housekeeping": {
        "task": "core.housekeeping",
        # purge expired idempotency records and old finished outbox events
        "schedule": crontab(minute=5),
        "options": {"expires": 30 * 60},
    },
    "identity-housekeeping": {
        "task": "identity.housekeeping",
        "schedule": crontab(minute=15),  # old throttle evidence, expired sessions
        "options": {"expires": 30 * 60},
    },
    "ai-housekeeping": {
        "task": "ai.housekeeping",
        "schedule": crontab(minute=25),  # expire stuck questions, purge old conversations
        "options": {"expires": 30 * 60},
    },
    "ai-reconcile-index": {
        "task": "ai.reconcile_index",
        # self-healing: re-enqueue sources whose chunks drifted; 21:30 UTC is 03:00 in India
        "schedule": crontab(hour=21, minute=30),
        "options": {"expires": 60 * 60},
    },
    "activities-housekeeping": {
        "task": "activities.housekeeping",
        # attachments: remove objects of deleted, failed or abandoned uploads (idempotent)
        "schedule": crontab(minute=35),
        "options": {"expires": 30 * 60},
    },
}

# --- Transactional outbox ------------------------------------------------------------------
# Maximum events per Celery queue that may be dispatched-but-unfinished at once. This is what
# keeps broker queues bounded: the durable backlog lives in PostgreSQL, not in Redis.
# `ai_index` is Ask Arkray's background indexing (embeddings), kept apart from `ai`, the
# interactive questions, so a re-indexing backlog never delays an answer.
# The relay refills a queue once per tick, so cap / 5 s is the queue's sustained ceiling
# however fast its workers are. Phase 10 measured `ai_index` at 20: a 4 events/s ceiling
# while one indexing process cleared each batch in under 0.5 s and idled the rest of the tick
# (a load test's 13 writes/s built a backlog of 616 events, draining at exactly 4/s). 100 is
# still a bounded broker (ids only) and a 20 events/s ceiling, below one process's measured
# rate for short notes (> 33/s); full-length notes embed at 1.5-7 chunks/s per process (R70),
# where the workers, not the relay, are the limit. The backlog stays in PostgreSQL.
OUTBOX_MAX_IN_FLIGHT = {"default": 200, "email": 50, "ai_index": 100}
OUTBOX_RELAY_BATCH_SIZE = 100
# While a claimed event's message waits in the broker. The message expires with it; after
# that the event is re-claimed under a new token (the old message is a no-op if it runs).
OUTBOX_DISPATCH_LEASE_SECONDS = 1800
# Once a worker starts the event. Must exceed CELERY_TASK_TIME_LIMIT.
OUTBOX_LEASE_SECONDS = 300
# Finished work is kept this long for investigation, then purged by the hourly housekeeping
# (Phase 10 review: nothing deleted it, so the table and every metrics scrape grew forever).
# Dead events stay until an operator re-queues or deletes them.
OUTBOX_DONE_RETENTION_DAYS = env.int("OUTBOX_DONE_RETENTION_DAYS", default=7)
OUTBOX_PURGE_BATCH = 5000
OUTBOX_PURGE_MAX_BATCHES = 100  # per run: at most 500,000 rows an hour

# --- Attachments (docs/activities.md#attachments) --------------------------------------------
# Files on notes. The bytes live in private object storage, never in PostgreSQL: a private
# directory locally (never served by a web server), private S3-compatible storage in
# production (ATTACHMENT_STORAGE=s3). Downloads always go through the API, which re-checks
# authorization on every request; nothing is ever public.
ATTACHMENT_MAX_BYTES = env.int("ATTACHMENT_MAX_BYTES", default=10 * 1024 * 1024)
ATTACHMENT_MAX_PER_NOTE = env.int("ATTACHMENT_MAX_PER_NOTE", default=10)
# Extensions accepted, from the catalog of types the server can recognise by their content
# (activities.storage.CATALOG: pdf png jpg jpeg webp gif docx xlsx pptx csv txt); anything
# else (executables, scripts, HTML, SVG, archives, ...) is refused. A system check refuses
# an extension outside the catalog.
ATTACHMENT_ALLOWED_EXTENSIONS = env.list(
    "ATTACHMENT_ALLOWED_EXTENSIONS",
    default=["pdf", "png", "jpg", "jpeg", "webp", "docx", "xlsx", "csv", "txt"],
)
# Malware scanning: "" (none: files are "not scanned" and downloadable) or
# "clamd://host:3310" (a ClamAV daemon: files are "pending" until clean; only clean files can
# be downloaded, infected ones are deleted). See docs/deployment.md#attachments.
ATTACHMENT_SCANNER = env.str("ATTACHMENT_SCANNER", default="")
ATTACHMENT_STORAGE = env.str("ATTACHMENT_STORAGE", default="filesystem")
ATTACHMENT_ROOT = env.str("ATTACHMENT_ROOT", default=str(BASE_DIR / "var" / "attachments"))
if ATTACHMENT_STORAGE == "s3":
    _ATTACHMENT_BACKEND: dict[str, Any] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env.str("ATTACHMENT_S3_BUCKET"),
            "endpoint_url": env.str("ATTACHMENT_S3_ENDPOINT_URL", default="") or None,
            "region_name": env.str("ATTACHMENT_S3_REGION", default="") or None,
            "access_key": env.str("ATTACHMENT_S3_ACCESS_KEY_ID", default="") or None,
            "secret_key": env.str("ATTACHMENT_S3_SECRET_ACCESS_KEY", default="") or None,
            "location": env.str("ATTACHMENT_S3_PREFIX", default="attachments"),
            # Private objects, never overwritten, no public URLs: the API streams them.
            "default_acl": "private",
            "file_overwrite": False,
            "querystring_auth": True,
            "querystring_expire": 60,
            "object_parameters": {"ServerSideEncryption": "AES256"},
        },
    }
else:
    _ATTACHMENT_BACKEND = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": ATTACHMENT_ROOT, "base_url": None},
    }
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "attachments": _ATTACHMENT_BACKEND,
}
# Uploads are read from the request stream in chunks and never buffered whole in memory
# above this (activities.storage.receive spools to a temporary file).
ATTACHMENT_SPOOL_MEMORY_BYTES = 1024 * 1024

# --- Ask Arkray (Phase 8, docs/rag-architecture.md) -----------------------------------------
# Every AI setting lives here: providers, models, timeouts and every bound. Nothing else in
# the code names a model or a limit.
AI_ENABLED = env.bool("AI_ENABLED", default=False)
# "anthropic": Claude through the Anthropic API (needs ANTHROPIC_API_KEY).
# "none": no language model. Ask Arkray still answers the questions its deterministic router
#   recognises (pipeline value, overdue tasks, today's meetings, ...) and otherwise shows the
#   most relevant records it can retrieve; no CRM text leaves the deployment.
AI_LLM_PROVIDER = env("AI_LLM_PROVIDER", default="none")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
# Where model calls go, passed to the SDK explicitly: otherwise it reads ANTHROPIC_BASE_URL
# from the environment, and a stray value (a drill's stand-in, a typo'd gateway) would send
# questions, CRM context and the key elsewhere, even over plain HTTP (Phase 10 review).
# Production requires https:// and refuses the SDK's own variables.
AI_LLM_BASE_URL = env("AI_LLM_BASE_URL", default="https://api.anthropic.com")
# Whether this process calls the provider (the ai worker). The web tier and the other workers
# never do: deployments give them no key and set this false (Phase 9 review: the key was in
# every container). They still know a model is configured from AI_LLM_PROVIDER.
AI_LLM_KEY_HOLDER = env.bool("AI_LLM_KEY_HOLDER", default=True)
AI_CHAT_MODEL = env("AI_CHAT_MODEL", default="claude-opus-5-5")
AI_CHAT_EFFORT = env("AI_CHAT_EFFORT", default="low")
AI_CHAT_MAX_TOKENS = env.int("AI_CHAT_MAX_TOKENS", default=8000)
AI_LLM_TIMEOUT_S = env.float("AI_LLM_TIMEOUT_S", default=20.0)
AI_LLM_MAX_RETRIES = env.int("AI_LLM_MAX_RETRIES", default=1)
AI_QUESTION_BUDGET_S = env.float("AI_QUESTION_BUDGET_S", default=30.0)
AI_MAX_TOOL_ROUNDS = env.int("AI_MAX_TOOL_ROUNDS", default=4)
AI_MAX_TOOL_CALLS_PER_ROUND = env.int("AI_MAX_TOOL_CALLS_PER_ROUND", default=6)
# Circuit breaker around the language model: this many failures in a row open it for the
# cool-down; while open, questions are answered without the model (router / retrieval).
AI_BREAKER_FAILURES = env.int("AI_BREAKER_FAILURES", default=3)
AI_BREAKER_COOLDOWN_S = env.int("AI_BREAKER_COOLDOWN_S", default=60)
# Embeddings are computed inside the deployment by a pinned open model (no external
# embeddings service): "local" (BAAI/bge-small-en-v1.5, ONNX, CPU) or "hashing" (a
# deterministic lexical stand-in for tests only; refused in production).
AI_EMBEDDING_PROVIDER = env("AI_EMBEDDING_PROVIDER", default="local")
AI_EMBEDDING_MODEL_DIR = env(
    "AI_EMBEDDING_MODEL_DIR", default=str(BASE_DIR / ".models" / "bge-small-en-v1.5")
)
AI_EMBEDDING_THREADS = env.int("AI_EMBEDDING_THREADS", default=2)
# Index CRM text as it changes (outbox, queue ai_index). Off while AI is off: turning AI on
# later is followed by `manage.py ai_reindex` (or the nightly reconciliation).
AI_INDEXING_ENABLED = env.bool("AI_INDEXING_ENABLED", default=AI_ENABLED)
AI_RETRIEVAL_TOP_K = env.int("AI_RETRIEVAL_TOP_K", default=8)
# Calibrated for bge-small-en-v1.5 on Arkray-like notes (ai/tests/test_local_model.py):
# relevant passages scored 0.505-0.78 (median 0.665), clearly off-topic questions 0.42-0.51,
# near-domain nonsense up to 0.57. The model's scores are compressed, so this filters the
# clearly unrelated; the language model (when there is one) judges the rest.
AI_RETRIEVAL_MIN_SIMILARITY = env.float("AI_RETRIEVAL_MIN_SIMILARITY", default=0.55)
# Per question: retrieved passage text, and all tool output together (what the model may
# be sent, beyond the question and its conversation).
AI_CONTEXT_MAX_CHARS = env.int("AI_CONTEXT_MAX_CHARS", default=12_000)
AI_TOOL_RESULTS_MAX_CHARS = env.int("AI_TOOL_RESULTS_MAX_CHARS", default=40_000)
AI_HISTORY_TURNS = env.int("AI_HISTORY_TURNS", default=4)
# A question not answered within this time is reported as failed (worker down, backlog).
AI_QUESTION_TIMEOUT_S = env.int("AI_QUESTION_TIMEOUT_S", default=90)
# Bulkheads: unanswered questions per person and in total (PostgreSQL-counted, so they hold
# when Redis is down).
AI_MAX_PENDING_PER_USER = env.int("AI_MAX_PENDING_PER_USER", default=2)
AI_MAX_PENDING_TOTAL = env.int("AI_MAX_PENDING_TOTAL", default=40)
AI_CONVERSATION_RETENTION_DAYS = env.int("AI_CONVERSATION_RETENTION_DAYS", default=30)

# --- Logging -------------------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_FORMAT = env("LOG_FORMAT", default="json")
LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"context": {"()": "arkray.core.logging.ContextFilter"}},
    "formatters": {
        "json": {"()": "arkray.core.logging.JsonFormatter"},
        "console": {"()": "arkray.core.logging.ConsoleFormatter"},
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT,
            "filters": ["context"],
        }
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        # Send Django's own loggers through our structured handler (this drops Django's
        # default console/mail_admins handlers). Our middleware writes the access log, so
        # runserver's per-request lines are suppressed.
        "django": {"level": "INFO", "propagate": True},
        "django.server": {"level": "ERROR", "propagate": True},
        # 4xx are already in the access log; 5xx (with tracebacks) are still logged here.
        "django.request": {"level": "ERROR", "propagate": True},
        "django.db.backends": {"level": "WARNING", "propagate": True},
        # The outbox relay runs every 5s; per-tick scheduler/task INFO lines are noise.
        # Task failures are still logged, and the outbox logs every meaningful outcome.
        "celery.beat": {"level": "WARNING", "propagate": True},
        "celery.worker.strategy": {"level": "WARNING", "propagate": True},
        "celery.app.trace": {"level": "WARNING", "propagate": True},
        # HTTP clients and the AI SDK log whole request bodies at DEBUG: a question, earlier
        # turns, notes and tool results. Never below WARNING, whatever LOG_LEVEL says
        # (Phase 9 review).
        **{
            name: {"level": "WARNING", "propagate": True}
            for name in ("anthropic", "httpx", "httpx2", "httpcore", "urllib3")
        },
    },
}
