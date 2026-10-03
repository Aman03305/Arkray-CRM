"""Settings shared by every environment.

All configuration comes from environment variables (12-factor). Environment modules
(`local`, `test`, `production`) only override what genuinely differs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import environ

from arkray.core.redis import fail_fast_pool_kwargs

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()

# --- Core ----------------------------------------------------------------------------------
DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY")
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
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES: list[dict[str, Any]] = []  # API-only service: no server-rendered HTML.

# --- Database ------------------------------------------------------------------------------
# Every connection gets hard timeouts so one slow query or stuck transaction cannot pin a
# worker (and, transitively, exhaust the connection budget). See docs/reliability.md.
_db = env.db("DATABASE_URL")
_db_options: dict[str, Any] = {
    "connect_timeout": env.int("DB_CONNECT_TIMEOUT_S", default=5),
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
CRM_TIME_ZONE = env("CRM_TIME_ZONE", default="Asia/Kolkata")
CRM_CURRENCY = env("CRM_CURRENCY", default="INR")
# Number of reverse proxies in front of Django whose X-Forwarded-For entries are trusted.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)
# Adopt an incoming X-Request-ID as our correlation id only when a trusted edge proxy sets
# (overwrites) it. Otherwise a client could spoof or replay ids that end up in audit records;
# the client's value is then only logged as `client_request_id`.
TRUST_INCOMING_REQUEST_ID = env.bool("TRUST_INCOMING_REQUEST_ID", default=False)
# Window during which repeated admin access to the same user's workspace is audited once.
WORKSPACE_ACCESS_AUDIT_WINDOW_S = env.int("WORKSPACE_ACCESS_AUDIT_WINDOW_S", default=15 * 60)

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
    "DEFAULT_PAGINATION_CLASS": "arkray.core.pagination.DefaultCursorPagination",
    # No OPTIONS metadata: it would describe serializers to anyone who asks.
    "DEFAULT_METADATA_CLASS": None,
    "PAGE_SIZE": 25,
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
CELERY_TASK_ROUTES = {"core.outbox.relay": {"queue": "outbox"}}
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BEAT_SCHEDULE = {
    "outbox-relay": {
        "task": "core.outbox.relay",
        "schedule": 5.0,
        # A relay tick that cannot run within 10s is dropped rather than piling up.
        "options": {"expires": 10},
    },
    "core-housekeeping": {
        "task": "core.housekeeping",
        "schedule": 60.0 * 60,  # purge expired idempotency records
        "options": {"expires": 30 * 60},
    },
    "identity-housekeeping": {
        "task": "identity.housekeeping",
        "schedule": 60.0 * 60,  # purge old throttle evidence and expired sessions
        "options": {"expires": 30 * 60},
    },
}

# --- Transactional outbox ------------------------------------------------------------------
# Maximum events per Celery queue that may be dispatched-but-unfinished at once. This is what
# keeps broker queues bounded: the durable backlog lives in PostgreSQL, not in Redis.
OUTBOX_MAX_IN_FLIGHT = {"default": 200, "email": 50, "ai": 50}
OUTBOX_RELAY_BATCH_SIZE = 100
# While a claimed event's message waits in the broker. The message expires with it; after
# that the event is re-claimed under a new token (the old message is a no-op if it runs).
OUTBOX_DISPATCH_LEASE_SECONDS = 1800
# Once a worker starts the event. Must exceed CELERY_TASK_TIME_LIMIT.
OUTBOX_LEASE_SECONDS = 300

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
    },
}
