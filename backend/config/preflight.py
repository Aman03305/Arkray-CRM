"""Preflight: a live, read-only check of a staging or production environment's dependencies,
run inside the backend image with the deployment's own environment before it takes traffic
(`manage.py preflight`; docs/deployment.md#preflight).

Production settings refuse an unsafe *configuration* at startup (config/settings/production.py);
this checks that what the configuration points at is really there and really as safe:
the database's and Redis's passwords by the whole production credential rule
(config/credentials.py; settings refuse only the published development values), PostgreSQL
(TLS actually in use, version, extensions, the runtime role, migrations, R63's
logging, timeouts, the connection budget), both Redis instances (TLS, a password actually
required, eviction and persistence, two instances), SMTP (TLS with certificate and host name
verified, AUTH), object storage (private, versioned, encrypted, Object Lock for the erasure
ledger, a probe written, read back and removed under the liveness probe's prefix, never in the
ledger's bucket), ClamAV (PING, signature age, the EICAR test file found), the erasure ledger
(read, verified, gate open), Celery's routing (each documented worker answers and consumes
exactly its queues) and, given `--base-url`, the edge (HSTS, security headers, the HTTP
redirect, /health not routed, no server version).

Like config/metrics.py, a composition root: it needs every module at once (activities'
storage and scanner, core's ledger, the Celery app), which no layer of `arkray` may import
together.

Rules every check follows:

- **Read-only** where the deployment's data lives: nothing is written but a small probe object
  in the attachments' and exports' stores (under PROBE_PREFIX, which the reconciliation
  ignores, removed with every version), and an email only with `--send-test-email`. Never a
  write to the erasure ledger: Object Lock could make a probe undeletable.
- **Bounded**: every network call has its own timeout, and each group of checks runs on a
  thread with a deadline (`--timeout`), so a dependency that hangs costs one FAIL, not the run.
- **No secret is ever printed**: URLs are shown without their password, and every line is
  redacted against every secret the settings hold before it is shown.
- Each check is a small function; the decisions are pure functions of what was observed
  (`judge_*`), unit-tested with fakes (tests/integration/test_preflight.py).
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import socket
import ssl
import stat
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import url2pathname

from django.conf import settings

from arkray.core import transport

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
STATUSES = (PASS, WARN, FAIL, SKIP)

# The connections the documented starting topology holds (docs/deployment.md#operations:
# 2 api replicas x WEB_CONCURRENCY 4, and the workers' Celery pools: worker-outbox 2,
# worker-default 2, worker-index 1, worker-ai 2), plus beat. `--connections` states the real
# plan; docs/reliability.md#connection-budget has the rule.
PLANNED_CONNECTIONS = 2 * 4 + 2 + 2 + 1 + 2 + 1
# max_connections minus what is kept for a superuser (3) and migrations and operations (5).
RESERVED_CONNECTIONS = 8
POSTGRES_MAJOR = 16
EXTENSIONS = frozenset({"vector", "pg_trgm", "btree_gin"})
# The broker must never drop a message; the cache should evict
# (docs/deployment.md#production-topology).
BROKER_POLICIES = frozenset({"noeviction"})
CACHE_POLICIES = frozenset(
    {"allkeys-lru", "allkeys-lfu", "volatile-lru", "volatile-lfu", "volatile-ttl", "allkeys-random"}
)
# Each worker's name (`--hostname <name>@%h`; Celery's default `celery@` for worker-default)
# and exactly the queues it consumes (docs/deployment.md#production-topology).
WORKERS: dict[str, frozenset[str]] = {
    "outbox": frozenset({"outbox", "email"}),
    "celery": frozenset({"default"}),
    "index": frozenset({"ai_index"}),
    "ai": frozenset({"ai"}),
}
CLAMAV_MAX_SIGNATURE_AGE_S = 3 * 24 * 60 * 60  # infrastructure/clamav/healthcheck.sh
HSTS_MIN_SECONDS = 31_536_000
PROBE_BODY = b"Arkray CRM preflight probe\n"
CLEAN_SAMPLE = b"Arkray CRM preflight: an ordinary text file, nothing to find here.\n"
# The EICAR anti-malware test file, assembled at run time: the repository never holds it, so
# antivirus on a developer machine has nothing to quarantine (as tests/drills/clamav_drill.py).
_EICAR_PARTS = (
    "X5O!P%@AP[4",
    "\\PZX54(P^)7CC)7}$",
    "EICAR-STANDARD-",
    "ANTIVIRUS-TEST-",
    "FILE!$H+H*",
)


@dataclass(frozen=True, slots=True)
class Result:
    check: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class Options:
    groups: tuple[str, ...] = ()  # empty: every group
    timeout_s: float = 30.0  # each group's deadline
    send_test_email: str = ""
    base_url: str = ""
    http_url: str = ""
    edge_ca_file: str = ""
    connections: int = PLANNED_CONNECTIONS

    def network_timeout(self, cap: float) -> float:
        """One network call's timeout: `cap`, within the group's deadline."""
        return max(min(cap, self.timeout_s), 0.5)


# --- running ------------------------------------------------------------------------------------
def run(options: Options) -> list[Result]:
    """Every group asked for, in order, each within its deadline; every line redacted."""
    redact = Redactor.from_settings()
    results: list[Result] = []
    for name in options.groups or tuple(GROUPS):
        results += bounded(name, GROUPS[name], options)
    return [Result(r.check, r.status, redact(r.detail)) for r in results]


def bounded(
    name: str, group: Callable[[Options], Iterable[Result]], options: Options
) -> list[Result]:
    """The group's results within `options.timeout_s`, on a daemon thread (a call that hangs
    in C, a DNS lookup or a TLS handshake, can't be interrupted; the process exits without
    waiting for it). Results given before a hang or a crash are kept."""
    found: list[Result] = []
    failed: list[BaseException] = []

    def work() -> None:
        from django.db import connections

        try:
            for result in group(options):
                found.append(result)
        except Exception as error:  # noqa: BLE001 — reported as the group's failure below
            failed.append(error)
        finally:
            connections.close_all()  # this thread's own

    thread = threading.Thread(target=work, name=f"preflight-{name}", daemon=True)
    thread.start()
    thread.join(options.timeout_s)
    if thread.is_alive():
        return [
            *list(found),
            Result(
                name,
                FAIL,
                f"no answer within {options.timeout_s:g} s: something it waits for hangs"
                " (the lines above finished; --timeout gives it longer)",
            ),
        ]
    if failed:
        return [*found, Result(name, FAIL, f"the check itself failed: {reason(failed[0])}")]
    return found


def reason(error: BaseException) -> str:
    """An error's type and the first line of its message, shortened (redacted later)."""
    message = str(error).strip().split("\n", 1)[0][:240]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


class Redactor:
    """Replaces every secret the settings hold (and its percent-encoded form) in a line."""

    def __init__(self, secrets: Iterable[str]) -> None:
        values = {value for value in secrets if value and len(value) >= 4}
        values |= {quote(value, safe="") for value in values}
        self._secrets = sorted(values, key=len, reverse=True)

    @classmethod
    def from_settings(cls) -> Redactor:
        found: list[str] = [
            settings.SECRET_KEY,
            *settings.SECRET_KEY_FALLBACKS,
            settings.ERASURE_LEDGER_KEY,
            *settings.ERASURE_LEDGER_VERIFY_KEYS,
            settings.METRICS_TOKEN,
            settings.ANTHROPIC_API_KEY,
            settings.EMAIL_HOST_PASSWORD,
            str(settings.DATABASES["default"].get("PASSWORD") or ""),
        ]
        for url in (settings.CELERY_BROKER_URL, settings.REDIS_CACHE_URL):
            password = urlsplit(url).password
            found += [password, unquote(password)] if password else []
        for alias in ("attachments", "exports"):
            options = settings.STORAGES.get(alias, {}).get("OPTIONS", {})
            found += [str(options.get("secret_key") or ""), str(options.get("access_key") or "")]
        return cls(found)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text


def display_url(url: str) -> str:
    """A URL to show: its password replaced, its query left out (paths to CA files only)."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.password is not None:
        user = parts.username or ""
        netloc = f"{user}:***@{netloc.rpartition('@')[2]}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _exempt(host: str) -> bool:
    """transport's rule: loopback, or a private name BACKEND_TLS_PRIVATE_HOSTS lists."""
    private = {name.strip().lower() for name in settings.BACKEND_TLS_PRIVATE_HOSTS}
    return transport.is_loopback(host) or host.strip().lower() in private


# --- credentials --------------------------------------------------------------------------------
def check_credentials(options: Options) -> Iterator[Result]:
    """The database's and both Redis instances' passwords by the whole production rule
    (config/credentials.py; adversarial review of 73ff1e3, M4): production settings refuse
    only the development and placeholder values at startup. Offline: nothing is contacted."""
    yield judge_credential(
        "credentials.database",
        "DATABASE_URL",
        str(settings.DATABASES["default"].get("PASSWORD") or ""),
    )
    for role, setting in (("broker", "CELERY_BROKER_URL"), ("cache", "REDIS_CACHE_URL")):
        url = str(getattr(settings, setting))
        if urlsplit(url).scheme not in {"redis", "rediss", "unix"}:
            yield Result(f"credentials.{role}", SKIP, f"{setting} isn't a Redis server")
            continue
        password = unquote(urlsplit(url).password or "")
        yield judge_credential(f"credentials.{role}", setting, password, required=True)


def judge_credential(check: str, setting: str, password: str, *, required: bool = False) -> Result:
    from config import credentials

    if not password:
        if required:
            return Result(check, FAIL, f"{setting} has no password: anyone who reaches it is in")
        return Result(
            check,
            SKIP,
            f"no password in {setting}: the server authenticates this client another way"
            " (a client certificate, the platform's identity)",
        )
    problem = credentials.weakness(password)
    if problem:
        return Result(
            check,
            FAIL,
            f"{setting}'s password {problem}: generate one for this environment"
            " (config/credentials.py)",
        )
    return Result(
        check,
        PASS,
        f"{setting}'s password: {credentials.MIN_LENGTH}+ characters, not a development or"
        " placeholder value, not predictable",
    )


# --- PostgreSQL ---------------------------------------------------------------------------------
def check_database(options: Options) -> Iterator[Result]:
    from django.db import DatabaseError, connections

    from arkray.core import metrics, privileges

    connection = connections["default"]
    database = connection.settings_dict
    where = (
        f"{database.get('HOST') or 'the local socket'}:{database.get('PORT') or 5432}"
        f"/{database.get('NAME')} as {database.get('USER')}"
    )
    try:
        connection.ensure_connection()
    except DatabaseError as error:
        yield Result("database.connect", FAIL, f"{where}: {reason(error)}")
        return
    yield Result("database.connect", PASS, where)
    raw: Any = connection.connection
    # DB_TRANSACTION_POOLER's mark in the settings (config/settings/base.py): no session state.
    pooler = bool(database.get("DISABLE_SERVER_SIDE_CURSORS"))
    with connection.cursor() as cursor:
        cursor.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        row = cursor.fetchone()
        yield judge_database_tls(
            required=database_tls_required(database),
            in_use=bool(raw.pgconn.ssl_in_use),
            sslmode=str(raw.info.get_parameters().get("sslmode", "prefer")),
            protocol=str(row[1]) if row and row[0] else None,
            pooler=pooler,
        )
        cursor.execute("SHOW server_version_num")
        yield judge_version(int(cursor.fetchone()[0]))
        cursor.execute("SELECT extname FROM pg_extension")
        yield judge_extensions({str(name) for (name,) in cursor.fetchall()})
        cursor.execute("SELECT current_user")
        role = str(cursor.fetchone()[0])
        yield judge_role(
            role, privileges.runtime_problems(cursor), required=settings.DB_REQUIRE_RESTRICTED_ROLE
        )
        yield judge_statement_logging(privileges.logs_failed_statements(cursor))
        timeouts: dict[str, str] = {}
        for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"):
            cursor.execute(f"SHOW {name}")
            timeouts[name] = str(cursor.fetchone()[0])
        yield judge_timeouts(timeouts, configured_statement_timeout_ms(database), pooler=pooler)
    yield judge_migrations(*migration_state(connection))
    gauges = {gauge.name: gauge for gauge in metrics.database()}
    yield judge_connections(
        max_connections=int(gauges["arkray_db_max_connections"].samples[0][1]),
        in_use=int(gauges["arkray_db_server_connections"].samples[0][1]),
        planned=options.connections,
        pooler=pooler,
    )


def database_tls_required(database: Mapping[str, Any]) -> bool:
    """Whether transport's rule requires TLS for where this connection goes: its problem
    report for the same settings without an sslmode is empty only for a Unix socket,
    loopback or a listed private host (the rule, its libpq fallbacks included, stays in
    one place)."""
    options = {k: v for k, v in (database.get("OPTIONS") or {}).items() if k != "sslmode"}
    probe = {**database, "OPTIONS": options}
    return transport.database_problem(probe, settings.BACKEND_TLS_PRIVATE_HOSTS) is not None


def judge_database_tls(
    *, required: bool, in_use: bool, sslmode: str, protocol: str | None, pooler: bool
) -> Result:
    name = "database.tls"
    server = f", the server reports {protocol}" if protocol else ""
    if pooler and in_use:
        server = " (to the pooler; pg_stat_ssl describes the pooler's own connection)"
    if in_use and required and sslmode != "verify-full":
        return Result(
            name, FAIL, f"encrypted but sslmode={sslmode}: the certificate isn't verified"
        )
    if in_use:
        return Result(name, PASS, f"TLS in use, sslmode={sslmode}{server}")
    if required:
        return Result(name, FAIL, "the connection is NOT encrypted (docs/security.md#backend-tls)")
    return Result(
        name,
        SKIP,
        "plain TCP, allowed: a Unix socket, loopback or a listed private host"
        " (docs/security.md#backend-tls)",
    )


def judge_version(server_version_num: int) -> Result:
    major = server_version_num // 10_000
    found = f"PostgreSQL {major}.{server_version_num % 10_000}"
    if major == POSTGRES_MAJOR:
        return Result("database.version", PASS, found)
    if major > POSTGRES_MAJOR:
        return Result(
            "database.version",
            WARN,
            f"{found}: released and tested on {POSTGRES_MAJOR}; backup.sh needs pg_dump {major}",
        )
    return Result("database.version", FAIL, f"{found}: the deployment needs {POSTGRES_MAJOR}")


def judge_extensions(installed: Collection[str]) -> Result:
    missing = sorted(EXTENSIONS - set(installed))
    if missing:
        return Result(
            "database.extensions",
            FAIL,
            f"missing {', '.join(missing)}: infrastructure/postgres/roles.sql creates them",
        )
    return Result("database.extensions", PASS, ", ".join(sorted(EXTENSIONS)))


def judge_role(role: str, problems: list[str], *, required: bool) -> Result:
    if not problems:
        return Result("database.role", PASS, f"{role}: restricted (R74)")
    shown = "; ".join(problems[:3])
    if len(problems) > 3:
        shown += f"; and {len(problems) - 3} more (manage.py check --deploy --database default)"
    detail = f"{role} may not run the application: {shown}"
    if required:
        return Result("database.role", FAIL, detail)
    return Result("database.role", WARN, f"{detail} (DB_REQUIRE_RESTRICTED_ROLE is off)")


def judge_statement_logging(logs_failed_statements: bool) -> Result:
    if logs_failed_statements:
        return Result(
            "database.logging",
            WARN,
            "PostgreSQL logs failing statements or rows, values included: set"
            " log_min_error_statement=panic and log_error_verbosity=terse (R63, arkray.W002)",
        )
    return Result(
        "database.logging", PASS, "log_min_error_statement=panic, log_error_verbosity=terse"
    )


_DURATION = re.compile(r"(\d+)\s*(us|ms|s|min|h|d)?")
_UNIT_MS = {"us": 0.001, "ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}


def duration_ms(shown: str) -> float | None:
    """PostgreSQL's SHOW form of a duration ("10s", "1min", "0") in milliseconds."""
    found = _DURATION.fullmatch(shown.strip())
    if found is None:
        return None
    return int(found[1]) * _UNIT_MS[found[2] or "ms"]


def configured_statement_timeout_ms(database: Mapping[str, Any]) -> float | None:
    """What the settings send at connect (-c statement_timeout=...); None behind a pooler."""
    options = str((database.get("OPTIONS") or {}).get("options") or "")
    found = re.search(r"statement_timeout=(\d+)", options)
    return float(found[1]) if found else None


def judge_timeouts(shown: Mapping[str, str], expected_ms: float | None, *, pooler: bool) -> Result:
    name = "database.timeouts"
    detail = ", ".join(f"{key} {value}" for key, value in shown.items())
    statement = duration_ms(shown.get("statement_timeout", "0"))
    where = (
        "on the role (docs/deployment.md#database-poolers)" if pooler else "DB_STATEMENT_TIMEOUT_MS"
    )
    if not statement:
        return Result(name, FAIL, f"{detail}: no statement timeout; set it {where}")
    if expected_ms is not None and statement != expected_ms:
        return Result(name, WARN, f"{detail}: the settings ask for {expected_ms:g} ms")
    if any(not duration_ms(value) for value in shown.values()):
        return Result(name, WARN, f"{detail}: a timeout is off")
    return Result(name, PASS, detail)


def migration_state(connection: Any) -> tuple[list[str], list[str]]:
    """(migrations this image has that the database hasn't applied, migrations the database
    has applied that this image doesn't know: a newer release ran against it)."""
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    loader = executor.loader
    pending = [
        f"{migration.app_label}.{migration.name}"
        for migration, _ in executor.migration_plan(loader.graph.leaf_nodes())
    ]
    replaced = {key for migration in loader.replacements.values() for key in migration.replaces}
    unknown = sorted(
        f"{app}.{name}"
        for app, name in loader.applied_migrations
        if app in loader.migrated_apps
        and (app, name) not in loader.graph.nodes
        and (app, name) not in replaced
    )
    return pending, unknown


def judge_migrations(pending: list[str], unknown: list[str]) -> Result:
    if pending:
        return Result(
            "database.migrations",
            FAIL,
            f"{len(pending)} not applied (first {pending[0]}): run the migrate job"
            " (docs/deployment.md#release-process)",
        )
    if unknown:
        return Result(
            "database.migrations",
            WARN,
            f"the database has {len(unknown)} this image doesn't (first {unknown[0]}): a newer"
            " release migrated it",
        )
    return Result("database.migrations", PASS, "every migration applied")


def judge_connections(*, max_connections: int, in_use: int, planned: int, pooler: bool) -> Result:
    budget = max_connections - RESERVED_CONNECTIONS
    target = int(budget * 0.8)
    detail = (
        f"max_connections {max_connections}: a budget of {budget}, 80% is {target};"
        f" planned {planned}, {in_use} in use now"
    )
    if pooler:
        return Result(
            "database.connections", PASS, f"{detail} (behind a pooler: its pool size counts)"
        )
    if planned > target or in_use > target:
        return Result(
            "database.connections",
            WARN,
            f"{detail}: raise max_connections or put PgBouncer in front"
            " (docs/reliability.md#connection-budget)",
        )
    return Result("database.connections", PASS, detail)


# --- Redis --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RedisIdentity:
    address: str  # host:port, or the socket path
    db: int
    run_id: str | None  # INFO server's run_id: the same process whatever the name


def check_redis(options: Options) -> Iterator[Result]:
    import redis

    identities: dict[str, RedisIdentity] = {}
    for role, url, policies in (
        ("broker", settings.CELERY_BROKER_URL, BROKER_POLICIES),
        ("cache", settings.REDIS_CACHE_URL, CACHE_POLICIES),
    ):
        name = f"redis.{role}"
        scheme = urlsplit(url).scheme
        if scheme not in {"redis", "rediss", "unix"}:
            yield Result(name, SKIP, f"{scheme}:// isn't a Redis server")
            continue
        client = redis_client(url, options)
        try:
            client.ping()
        except redis.RedisError as error:
            yield Result(f"{name}.ping", FAIL, f"{display_url(url)}: {reason(error)}")
            continue
        try:
            yield Result(f"{name}.ping", PASS, f"{display_url(url)} answered PING")
            yield judge_redis_tls(name, url)
            yield judge_redis_auth(name, unauthenticated_ping(url, options))
            config = redis_config(client, ("maxmemory-policy", "appendonly", "save"))
            yield judge_redis_policy(name, role, config, policies)
            yield judge_redis_persistence(name, role, config)
            identities[role] = redis_identity(client, url)
        finally:
            client.close()
    if len(identities) == 2:
        yield judge_redis_separate(identities["broker"], identities["cache"])


def redis_client(url: str, options: Options, *, password: bool = True) -> Any:
    """A client as the application connects: the URL's TLS parameters, the broker's
    verification (transport.redis_ssl_options), no retries, a timeout on everything."""
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    if not password:
        parts = urlsplit(url)
        url = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    timeout = options.network_timeout(5.0)
    kwargs: dict[str, Any] = {
        "socket_connect_timeout": timeout,
        "socket_timeout": timeout,
        "retry": Retry(NoBackoff(), 0),
        **(transport.redis_ssl_options(url) or {}),
    }
    return redis.Redis.from_url(url, **kwargs)


def judge_redis_tls(name: str, url: str) -> Result:
    parts = urlsplit(url)
    if parts.scheme == "rediss":
        return Result(f"{name}.tls", PASS, "rediss://: TLS, the certificate and host name verified")
    if parts.scheme == "unix" or _exempt(parts.hostname or ""):
        return Result(
            f"{name}.tls",
            SKIP,
            f"plain TCP to {parts.hostname or 'a socket'}, allowed: loopback or a listed"
            " private host (docs/security.md#backend-tls)",
        )
    return Result(f"{name}.tls", FAIL, f"{parts.hostname}: NOT encrypted (rediss:// required)")


def unauthenticated_ping(url: str, options: Options) -> str:
    """What the server answers a PING without the password: "refused", "answered" or why it
    couldn't tell. Harmless: no AUTH is attempted, so no wrong password is ever counted."""
    import redis

    client = redis_client(url, options, password=False)
    try:
        client.ping()
    except redis.AuthenticationError:
        return "refused"
    except redis.ResponseError as error:
        if str(error).upper().startswith(("NOAUTH", "NOPERM", "WRONGPASS")):
            return "refused"
        return reason(error)
    except redis.RedisError as error:
        return reason(error)
    finally:
        client.close()
    return "answered"


def judge_redis_auth(name: str, unauthenticated: str) -> Result:
    if unauthenticated == "refused":
        return Result(f"{name}.auth", PASS, "a PING without the password is refused")
    if unauthenticated == "answered":
        return Result(
            f"{name}.auth",
            FAIL,
            "answers WITHOUT a password: anyone who reaches it reads the queued tasks and the"
            " cache (docs/deployment.md#production-topology)",
        )
    return Result(
        f"{name}.auth", WARN, f"couldn't tell whether it needs a password: {unauthenticated}"
    )


def redis_config(client: Any, names: Iterable[str]) -> dict[str, str] | None:
    """CONFIG GET for each name; None where the server forbids CONFIG (managed Redis)."""
    import redis

    found: dict[str, str] = {}
    try:
        for name in names:
            found.update({str(k): str(v) for k, v in client.config_get(name).items()})
    except redis.ResponseError:
        return None
    return found


def judge_redis_policy(
    name: str, role: str, config: Mapping[str, str] | None, policies: Collection[str]
) -> Result:
    check = f"{name}.policy"
    if config is None or "maxmemory-policy" not in config:
        return Result(
            check,
            WARN,
            "unknown: CONFIG isn't allowed (managed Redis); check maxmemory-policy in the"
            f" provider's console ({' or '.join(sorted(policies)[:2])})",
        )
    policy = config["maxmemory-policy"]
    if policy in policies:
        return Result(check, PASS, f"maxmemory-policy {policy}")
    if role == "broker":
        return Result(
            check,
            FAIL,
            f"maxmemory-policy {policy}: the broker would drop queued tasks (noeviction)",
        )
    return Result(
        check,
        WARN,
        f"maxmemory-policy {policy}: a full cache refuses writes instead of evicting (allkeys-lru)",
    )


def judge_redis_persistence(name: str, role: str, config: Mapping[str, str] | None) -> Result:
    check = f"{name}.persistence"
    if config is None or "appendonly" not in config:
        return Result(check, WARN, "unknown: CONFIG isn't allowed (managed Redis)")
    appendonly, save = config["appendonly"], config.get("save", "")
    if role == "broker":
        if appendonly == "yes":
            return Result(check, PASS, "appendonly yes")
        return Result(
            check,
            WARN,
            "appendonly no: a restart loses the queued messages (the outbox re-creates them,"
            " slowly; AOF is the documented setting)",
        )
    if appendonly == "no" and not save.strip():
        return Result(check, PASS, "nothing persisted")
    return Result(
        check,
        WARN,
        f"appendonly {appendonly}, save {save!r}: the cache writes to disk, and its rate-limit"
        " keys hold client addresses (--save '' --appendonly no)",
    )


def redis_identity(client: Any, url: str) -> RedisIdentity:
    import redis

    kwargs = client.connection_pool.connection_kwargs
    address = str(kwargs.get("path") or f"{kwargs.get('host')}:{kwargs.get('port')}")
    try:
        run_id: str | None = str(client.info("server").get("run_id") or "") or None
    except redis.ResponseError:
        run_id = None
    return RedisIdentity(address, int(kwargs.get("db") or 0), run_id)


def judge_redis_separate(broker: RedisIdentity, cache: RedisIdentity) -> Result:
    same = (
        broker.run_id == cache.run_id
        if broker.run_id and cache.run_id
        else broker.address == cache.address
    )
    if not same:
        return Result("redis.separate", PASS, "the broker and the cache are separate instances")
    if broker.db == cache.db:
        return Result(
            "redis.separate",
            WARN,
            f"the broker and the cache are the SAME database ({broker.address}/{broker.db}):"
            " their keys mix and one eviction policy serves both",
        )
    return Result(
        "redis.separate",
        WARN,
        f"one instance ({broker.address}, databases {broker.db} and {cache.db}): one eviction"
        " policy serves both; production runs two (docs/deployment.md#production-topology)",
    )


# --- SMTP ---------------------------------------------------------------------------------------
def check_smtp(options: Options) -> Iterator[Result]:
    import smtplib

    from django.core.mail import EmailMessage

    from arkray.core.mail import VerifiedSMTPBackend

    if settings.EMAIL_BACKEND != "arkray.core.mail.VerifiedSMTPBackend":
        yield Result(
            "smtp",
            SKIP,
            f"{settings.EMAIL_BACKEND}: no SMTP server to check (production refuses a backend"
            " that doesn't deliver)",
        )
        return
    host, port = str(settings.EMAIL_HOST), int(settings.EMAIL_PORT)
    where = f"{host}:{port}"
    backend = VerifiedSMTPBackend(
        fail_silently=False, timeout=options.network_timeout(float(settings.EMAIL_TIMEOUT))
    )
    try:
        backend.open()
    except ssl.SSLCertVerificationError as error:
        yield Result(
            "smtp.connect",
            FAIL,
            f"{where}: the certificate isn't trusted for {host} ({error.verify_message})",
        )
        return
    except smtplib.SMTPNotSupportedError:
        yield Result("smtp.connect", FAIL, f"{where} doesn't offer STARTTLS")
        return
    except smtplib.SMTPAuthenticationError as error:
        yield Result(
            "smtp.connect", FAIL, f"{where}: the credentials were refused ({error.smtp_code})"
        )
        return
    except OSError as error:  # smtplib.SMTPException included
        yield Result("smtp.connect", FAIL, f"{where}: {reason(error)}")
        return
    try:
        server: Any = backend.connection
        server.ehlo_or_helo_if_needed()
        yield Result("smtp.connect", PASS, f"{where}: EHLO answered")
        sock = server.sock
        yield judge_smtp_tls(
            encrypted=isinstance(sock, ssl.SSLSocket),
            protocol=sock.version() if isinstance(sock, ssl.SSLSocket) else None,
            implicit=bool(settings.EMAIL_USE_SSL),
            required=transport.smtp_problem(
                host, use_tls=False, use_ssl=False, private_hosts=settings.BACKEND_TLS_PRIVATE_HOSTS
            )
            is not None,
        )
        yield judge_smtp_auth(bool(settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD))
        if not options.send_test_email:
            yield Result("smtp.send", SKIP, "nothing sent (--send-test-email <address> sends one)")
            return
        message = EmailMessage(
            subject="Arkray CRM preflight",
            body="A test message from `manage.py preflight`. Nothing to do.\n",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[options.send_test_email],
            connection=backend,
        )
        try:
            message.send()
        except OSError as error:
            yield Result("smtp.send", FAIL, f"to {options.send_test_email}: {reason(error)}")
            return
        yield Result(
            "smtp.send",
            PASS,
            f"sent to {options.send_test_email} from {settings.DEFAULT_FROM_EMAIL}: check that it"
            " arrives and that SPF, DKIM and DMARC pass",
        )
    finally:
        backend.close()


def judge_smtp_tls(
    *, encrypted: bool, protocol: str | None, implicit: bool, required: bool
) -> Result:
    if encrypted:
        how = "implicit TLS" if implicit else "STARTTLS"
        return Result(
            "smtp.tls", PASS, f"{how} ({protocol}), the certificate and host name verified"
        )
    if required:
        return Result("smtp.tls", FAIL, "NOT encrypted: the emails carry one-time account links")
    return Result(
        "smtp.tls",
        SKIP,
        "plain SMTP, allowed: loopback or a listed private host (docs/security.md#backend-tls)",
    )


def judge_smtp_auth(credentials: bool) -> Result:
    if credentials:
        return Result("smtp.auth", PASS, "authenticated")
    return Result(
        "smtp.auth",
        WARN,
        "no credentials: an open relay, or one that trusts this network (docs/deployment.md#email:"
        " authenticated submission)",
    )


# --- object storage ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Answer:
    """What a bucket configuration call returned: "ok" (with the response), "absent" (the
    configuration doesn't exist) or "unknown" (with the error code: no permission, or a store
    that doesn't implement the call)."""

    state: str
    response: dict[str, Any] = field(default_factory=dict)
    code: str = ""


def ask(call: Callable[[], dict[str, Any]], absent: Collection[str] = ()) -> Answer:
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return Answer("ok", call())
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "")) or "error"
        return Answer("absent" if code in absent else "unknown", code=code)
    except BotoCoreError as error:
        return Answer("unknown", code=type(error).__name__)


def check_storage(options: Options) -> Iterator[Result]:
    from django.core.files.storage import storages

    from arkray.core import ledger, storage_purge

    # (endpoint, bucket) -> (the alias that checked it, versioned, reachable): the exports
    # normally share the attachments' bucket under their own prefix.
    checked: dict[tuple[str, str], tuple[str, bool, bool]] = {}
    for alias in ("attachments", "exports"):
        store: Any = storages[alias]
        name = f"storage.{alias}"
        if not storage_purge.is_s3(store):
            directory = list(check_directory(name, str(store.location)))
            yield from directory
            if all(result.status != FAIL for result in directory):
                yield probe_store(name, store, versioned=False)
            continue
        client = store.connection.meta.client
        key = (str(client.meta.endpoint_url), str(store.bucket_name))
        if key in checked:
            first, versioned, reachable = checked[key]
            yield Result(f"{name}.bucket", PASS, f"the {first}' bucket, checked above")
        else:
            facts = bucket_facts(client, store.bucket_name)
            yield from judge_bucket(name, store.bucket_name, facts, ledger_bucket=False)
            versioned = (
                facts.get("versioning", Answer("unknown")).response.get("Status") == "Enabled"
            )
            reachable = facts["reachable"].state == "ok"
            checked[key] = (alias, versioned, reachable)
        if reachable:
            yield probe_store(name, store, versioned=versioned)
    url = settings.ERASURE_LEDGER_URL
    if url.startswith("s3://"):
        ledger_store = ledger.S3Store(url)
        facts = bucket_facts(ledger_store.client, ledger_store.bucket, object_lock=True)
        yield from judge_bucket("storage.ledger", ledger_store.bucket, facts, ledger_bucket=True)
    elif url.startswith("file://"):
        yield from check_directory("storage.ledger", url2pathname(urlsplit(url).path))
    else:
        yield Result("storage.ledger", SKIP, "no erasure ledger configured (development, tests)")


def bucket_facts(client: Any, bucket: str, *, object_lock: bool = False) -> dict[str, Answer]:
    """The bucket's configuration, as far as the credentials may read it. Nothing more is
    asked of a bucket that doesn't answer HEAD (each call would wait out its timeouts)."""
    reachable = ask(lambda: client.head_bucket(Bucket=bucket))
    if reachable.state != "ok":
        return {"reachable": reachable}
    facts = {
        "reachable": reachable,
        "public_block": ask(
            lambda: client.get_public_access_block(Bucket=bucket),
            absent={"NoSuchPublicAccessBlockConfiguration"},
        ),
        "policy": ask(
            lambda: client.get_bucket_policy_status(Bucket=bucket), absent={"NoSuchBucketPolicy"}
        ),
        "versioning": ask(lambda: client.get_bucket_versioning(Bucket=bucket)),
        "encryption": ask(
            lambda: client.get_bucket_encryption(Bucket=bucket),
            absent={"ServerSideEncryptionConfigurationNotFoundError"},
        ),
        "lifecycle": ask(
            lambda: client.get_bucket_lifecycle_configuration(Bucket=bucket),
            absent={"NoSuchLifecycleConfiguration"},
        ),
    }
    if object_lock:
        facts["object_lock"] = ask(
            lambda: client.get_object_lock_configuration(Bucket=bucket),
            absent={"ObjectLockConfigurationNotFoundError"},
        )
    return facts


def _unknown(check: str, answer: Answer, what: str) -> Result:
    return Result(
        check,
        WARN,
        f"{what} unknown ({answer.code}): the application's credentials may not read it (least"
        " privilege) or the store doesn't support it; check it in the provider's console",
    )


def judge_bucket(
    name: str, bucket: str, facts: Mapping[str, Answer], *, ledger_bucket: bool
) -> Iterator[Result]:
    reachable = facts["reachable"]
    if reachable.state != "ok":
        yield Result(f"{name}.bucket", FAIL, f"{bucket}: not reachable ({reachable.code})")
        return
    yield Result(f"{name}.bucket", PASS, f"{bucket} reachable")
    yield judge_public(f"{name}.public", facts["public_block"], facts["policy"])
    yield judge_versioning(f"{name}.versioning", facts["versioning"], ledger_bucket=ledger_bucket)
    yield judge_encryption(f"{name}.encryption", facts["encryption"])
    if ledger_bucket:
        yield judge_object_lock(f"{name}.object_lock", facts["object_lock"])
    elif (
        facts["versioning"].response.get("Status") == "Enabled"
        and not settings.ATTACHMENT_S3_PURGE_VERSIONS
    ):
        yield judge_lifecycle(f"{name}.lifecycle", facts["lifecycle"])


_BLOCKS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")


def judge_public(check: str, block: Answer, policy: Answer) -> Result:
    if policy.state == "ok" and policy.response.get("PolicyStatus", {}).get("IsPublic"):
        return Result(check, FAIL, "the bucket policy makes it PUBLIC: the files must be private")
    if block.state == "ok":
        settings_ = block.response.get("PublicAccessBlockConfiguration", {})
        off = [name for name in _BLOCKS if not settings_.get(name)]
        if not off:
            return Result(check, PASS, "Block Public Access on (all four settings)")
        return Result(
            check,
            WARN,
            f"Block Public Access partly off ({', '.join(off)}): nothing makes it public now,"
            " nothing stops it either (an account-level block may apply)",
        )
    if block.state == "absent":
        return Result(
            check,
            WARN,
            "no bucket-level Block Public Access: nothing makes it public now, nothing stops it"
            " either (an account-level block may apply)",
        )
    return _unknown(check, block, "Block Public Access")


def judge_versioning(check: str, answer: Answer, *, ledger_bucket: bool) -> Result:
    if answer.state != "ok":
        return _unknown(check, answer, "versioning")
    status = answer.response.get("Status")
    if status == "Enabled":
        return Result(check, PASS, "versioning enabled")
    why = (
        "versioning and Object Lock keep ledger entries from being removed or rewritten"
        if ledger_bucket
        else "a deleted or damaged file can't be recovered, and the bucket's backups rely on"
        " versions and replication (R93)"
    )
    return Result(check, WARN, f"versioning {status or 'never enabled'}: {why}")


def judge_encryption(check: str, answer: Answer) -> Result:
    if answer.state == "ok":
        rules = answer.response.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        algorithms = sorted(
            {
                str(rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm"))
                for rule in rules
            }
        )
        return Result(check, PASS, f"default encryption {', '.join(algorithms) or 'on'}")
    if answer.state == "absent":
        return Result(
            check,
            WARN,
            "no default encryption: the application asks for AES256 on what it writes; nothing"
            " covers anything else",
        )
    return _unknown(check, answer, "default encryption")


def judge_object_lock(check: str, answer: Answer) -> Result:
    if answer.state == "absent":
        return Result(
            check,
            WARN,
            "no Object Lock: credentials that can delete could remove entries (the gate would"
            " then close, ahead); compliance mode is the documented setting"
            " (docs/privacy.md#restore-safe-erasure)",
        )
    if answer.state != "ok":
        return _unknown(check, answer, "Object Lock")
    configuration = answer.response.get("ObjectLockConfiguration", {})
    retention = configuration.get("Rule", {}).get("DefaultRetention", {})
    if configuration.get("ObjectLockEnabled") != "Enabled":
        return Result(check, WARN, "Object Lock not enabled (compliance mode is documented)")
    if not retention:
        return Result(
            check,
            WARN,
            "Object Lock enabled without a default retention: the ledger's entries aren't"
            " locked (it sets none of its own)",
        )
    mode = retention.get("Mode")
    period = retention.get("Days") or retention.get("Years")
    unit = "days" if retention.get("Days") else "years"
    if mode != "COMPLIANCE":
        return Result(
            check,
            WARN,
            f"{mode} mode, {period} {unit}: privileged credentials can still remove entries"
            " (compliance mode is documented)",
        )
    return Result(check, PASS, f"Object Lock: compliance mode, {period} {unit} by default")


def judge_lifecycle(check: str, answer: Answer) -> Result:
    if answer.state == "ok":
        rules = answer.response.get("Rules", [])
        if any(
            rule.get("Status") == "Enabled" and rule.get("NoncurrentVersionExpiration")
            for rule in rules
        ):
            return Result(check, PASS, "a rule expires earlier versions")
    elif answer.state == "unknown":
        return _unknown(check, answer, "the lifecycle rules")
    return Result(
        check,
        WARN,
        "versioned, without ATTACHMENT_S3_PURGE_VERSIONS or a rule expiring earlier versions:"
        " deleted and erased files stay readable for ever (docs/privacy.md#attachments)",
    )


def probe_store(name: str, store: Any, *, versioned: bool) -> Result:
    """Write, read back and remove a small object named under the liveness probe's prefix,
    which the attachments' reconciliation never takes for an orphan. Removed with every
    version where the credentials allow it (core.storage_purge)."""
    from django.core.files.base import ContentFile

    from arkray.activities.storage import PROBE_PREFIX
    from arkray.core import storage_purge

    probe = f"{PROBE_PREFIX}preflight-{uuid.uuid4().hex}"
    check = f"{name}.probe"
    try:
        stored = store.save(probe, ContentFile(PROBE_BODY))
    except Exception as error:  # noqa: BLE001 — a store's failure, reported
        return Result(check, FAIL, f"can't write: {reason(error)}")
    try:
        if stored != probe:
            return Result(check, FAIL, f"the store renamed the object ({stored!r})")
        try:
            with store.open(probe, "rb") as file:
                body = file.read()
        except Exception as error:  # noqa: BLE001 — a store's failure, reported
            return Result(check, FAIL, f"written, but can't read it back: {reason(error)}")
        if body != PROBE_BODY:
            return Result(check, FAIL, "read back something other than what was written")
    finally:
        try:
            removed = storage_purge.purge(store, stored).complete
        except storage_purge.PurgeFailed:
            removed = False
        if not removed:
            # A failure here is reported below, as a probe left behind.
            with contextlib.suppress(Exception):
                store.delete(stored)
    if not removed and versioned:
        return Result(
            check,
            WARN,
            f"written, read back and deleted; an earlier version of {probe} stays (the"
            " credentials can't delete versions: s3:ListBucketVersions, s3:DeleteObjectVersion)",
        )
    return Result(check, PASS, f"written, read back and removed ({PROBE_PREFIX}preflight-*)")


def check_directory(name: str, path: str) -> Iterator[Result]:
    """A private directory: there, writable by this user, nobody else's to read. Nothing is
    written here (the stores' probe is separate; never in the ledger's)."""
    directory = Path(path)
    if not directory.is_dir():
        yield Result(f"{name}.directory", FAIL, f"{path} doesn't exist: mount its volume there")
        return
    if not os.access(directory, os.W_OK | os.X_OK):
        yield Result(f"{name}.directory", FAIL, f"{path} isn't writable by this user")
        return
    yield Result(f"{name}.directory", PASS, f"{path} writable")
    getuid = getattr(os, "getuid", None)  # POSIX only: Windows has ACLs, not modes
    if getuid is None:
        yield Result(f"{name}.permissions", SKIP, "POSIX modes only (not checked on this system)")
        return
    found = directory.stat()
    yield judge_permissions(
        f"{name}.permissions", stat.S_IMODE(found.st_mode), found.st_uid, int(getuid())
    )


def judge_permissions(check: str, mode: int, owner_uid: int, my_uid: int) -> Result:
    """Owner-only (0700), owned by the app user: the image makes them so (backend/Dockerfile)."""
    owner = "" if owner_uid == my_uid else f", owned by uid {owner_uid}, not this user"
    if mode & 0o007:
        return Result(
            check, FAIL, f"mode {mode:04o}{owner}: anyone on the host can reach it (want 0700)"
        )
    if mode & 0o070 or owner:
        return Result(check, WARN, f"mode {mode:04o}{owner}: want 0700, the app user's")
    return Result(check, PASS, f"mode {mode:04o}, this user's")


# --- ClamAV -------------------------------------------------------------------------------------
def eicar() -> bytes:
    return "".join(_EICAR_PARTS).encode("ascii")


def clamd_address(value: str) -> tuple[str, int] | None:
    parts = urlsplit(value)
    if parts.scheme != "clamd" or not parts.hostname:
        return None
    try:
        return parts.hostname, parts.port or 3310
    except ValueError:
        return None


def clamd_command(address: tuple[str, int], command: bytes, timeout: float) -> str:
    """One of clamd's one-line commands (PING, VERSION): its reply, bounded in time and size.
    Scanning itself is activities.storage.scan's (the application's own client)."""
    with socket.create_connection(address, timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(b"z" + command + b"\0")
        reply = b""
        while not reply.endswith(b"\0") and len(reply) < 4096:
            part = connection.recv(4096)
            if not part:
                break
            reply += part
    return reply.rstrip(b"\0").decode("utf-8", "replace").strip()


def check_scanner(options: Options) -> Iterator[Result]:
    from arkray.activities import storage as attachments

    scanner = settings.ATTACHMENT_SCANNER
    if not scanner:
        yield Result(
            "scanner",
            WARN,
            "none configured: files are downloadable unscanned (production starts this way only"
            " with ATTACHMENT_SCANNER_WAIVED, R94)",
        )
        return
    address = clamd_address(scanner)
    if address is None:
        yield Result("scanner", FAIL, f"{scanner!r} isn't clamd://host:port")
        return
    where = f"{address[0]}:{address[1]}"
    timeout = options.network_timeout(10.0)
    try:
        reply = clamd_command(address, b"PING", timeout)
    except OSError as error:
        yield Result(
            "scanner.ping",
            FAIL,
            f"{where}: {reason(error)} (only the scanning worker reaches ClamAV: run preflight"
            " in worker-default, or --skip scanner elsewhere)",
        )
        return
    if reply != "PONG":
        yield Result("scanner.ping", FAIL, f"{where} answered {reply[:60]!r}, not PONG")
        return
    yield Result("scanner.ping", PASS, f"{where}: PONG")
    try:
        version = clamd_command(address, b"VERSION", timeout)
    except OSError as error:
        yield Result("scanner.signatures", FAIL, f"VERSION: {reason(error)}")
    else:
        yield judge_signatures(version, datetime.now(UTC))
    for check, sample, infected in (
        ("scanner.eicar", eicar(), True),
        ("scanner.clean", CLEAN_SAMPLE, False),
    ):
        try:
            verdict = attachments.scan(io.BytesIO(sample))
        except attachments.ScannerUnavailable as error:
            yield Result(check, FAIL, f"no verdict ({error.reason})")
            continue
        yield judge_verdict(
            check, clean=verdict.clean, signature=verdict.signature, infected=infected
        )


def judge_signatures(version: str, now: datetime) -> Result:
    """clamd's VERSION ("ClamAV 1.5.4/28150/Sun Oct 11 06:26:01 2026"): the daily
    database's build time, as infrastructure/clamav/healthcheck.sh reads it."""
    engine, _, rest = version.partition("/")
    database, _, built = rest.partition("/")
    try:
        when = datetime.strptime(built.strip(), "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError:
        return Result(
            "scanner.signatures", WARN, f"can't read the signatures' date: {version[:80]!r}"
        )
    age = now - when
    detail = f"{engine}, signatures {database} from {when:%Y-%m-%d %H:%M} UTC"
    if age.total_seconds() > CLAMAV_MAX_SIGNATURE_AGE_S:
        return Result(
            "scanner.signatures",
            WARN,
            f"{detail}: {age.days} days old, freshclam isn't updating them"
            " (docs/operations.md#clamav-signature-freshness)",
        )
    return Result("scanner.signatures", PASS, detail)


def judge_verdict(check: str, *, clean: bool, signature: str, infected: bool) -> Result:
    if infected and not clean:
        return Result(check, PASS, f"the EICAR test file was found ({signature})")
    if infected:
        return Result(
            check, FAIL, "the EICAR test file passed as clean: the scanner isn't scanning"
        )
    if clean:
        return Result(check, PASS, "an ordinary file passed as clean")
    return Result(check, FAIL, f"an ordinary file was reported infected ({signature})")


# --- the erasure ledger -------------------------------------------------------------------------
def check_ledger(options: Options) -> Iterator[Result]:
    from arkray.core import ledger, ledger_gate

    if not settings.ERASURE_LEDGER_URL:
        yield Result(
            "ledger",
            SKIP,
            "no erasure ledger configured (development and tests; production refuses to start"
            " without one)",
        )
        return
    status = ledger_gate.GATE.status()
    entries: int | None = None
    if status not in {"tampered", "unavailable"}:
        try:
            last = ledger.head()
            entries = last.seq if last else 0
        except ledger.LedgerError:
            entries = None
    yield judge_ledger_chain(status, entries, ledger.key_id(settings.ERASURE_LEDGER_KEY))
    yield judge_ledger_gate(status)
    yield judge_ledger_keys(len(settings.ERASURE_LEDGER_VERIFY_KEYS))


def judge_ledger_chain(status: str, entries: int | None, key_id: str) -> Result:
    if status == "tampered":
        return Result(
            "ledger.chain",
            FAIL,
            "an entry doesn't verify: altered, or signed with a key that isn't configured"
            " (docs/runbooks.md#erasure-ledger-behind)",
        )
    if status == "unavailable":
        return Result(
            "ledger.chain", FAIL, "can't be read: the volume or the bucket, its credentials"
        )
    count = "entries" if entries is None else f"{entries} entries"
    return Result("ledger.chain", PASS, f"{count}, every one verified (signing key id {key_id})")


def judge_ledger_gate(status: str) -> Result:
    from arkray.core import ledger_gate

    if status in ledger_gate.OPEN:
        return Result("ledger.gate", PASS, f"open ({status})")
    hints = {
        "behind": "erasures since the backup aren't applied: manage.py replay_erasures"
        " (docs/runbooks.md#restore-from-backup)",
        "ahead": "the database applied entries this ledger doesn't have: the wrong ledger?"
        " (docs/runbooks.md#erasure-ledger-behind)",
        "diverged": "the database and this ledger disagree: the wrong ledger?"
        " (docs/runbooks.md#erasure-ledger-behind)",
    }
    return Result(
        "ledger.gate", FAIL, f"closed ({status}): {hints.get(status, 'the API answers 503')}"
    )


def judge_ledger_keys(verify_keys: int) -> Result:
    if verify_keys:
        return Result(
            "ledger.keys",
            WARN,
            f"{verify_keys} verify key(s): a key rotation is under way; finish it"
            " (docs/runbooks.md#rotate-the-erasure-ledger-key)",
        )
    return Result("ledger.keys", PASS, "one key, no rotation under way")


# --- Celery -------------------------------------------------------------------------------------
def check_celery(options: Options) -> Iterator[Result]:
    from arkray.core import metrics
    from config.celery import app

    timeout = options.network_timeout(5.0)
    replies = app.control.ping(timeout=timeout) or []
    answered = {str(node) for reply in replies for node in reply}
    active = app.control.inspect(timeout=timeout).active_queues() or {}
    queues = {str(node): {str(q["name"]) for q in found} for node, found in active.items()}
    yield from judge_workers(
        answered,
        queues,
        ai=bool(settings.AI_ENABLED),
        indexing=bool(settings.AI_INDEXING_ENABLED),
    )
    overdue = 0.0
    for gauge in metrics.outbox():
        if gauge.name == "arkray_outbox_oldest_due_seconds":
            overdue = max([overdue, *(value for _, value in gauge.samples)])
    yield judge_beat(overdue)


def worker_name(node: str) -> str:
    return node.partition("@")[0]


def judge_workers(
    answered: Collection[str], queues: Mapping[str, Collection[str]], *, ai: bool, indexing: bool
) -> Iterator[Result]:
    nodes = set(answered) | set(queues)
    needed = {"outbox": True, "celery": True, "index": indexing, "ai": ai}
    for name, expected in WORKERS.items():
        check = f"celery.{name}"
        found = sorted(node for node in nodes if worker_name(node) == name)
        if not found:
            if needed[name]:
                yield Result(
                    check,
                    FAIL,
                    f"no {name}@ worker answered (it consumes {', '.join(sorted(expected))})",
                )
            else:
                yield Result(
                    check, SKIP, f"no {name}@ worker answered; Ask Arkray doesn't need it now"
                )
            continue
        wrong = [
            f"{node} consumes {', '.join(sorted(queues[node])) or 'nothing'}"
            for node in found
            if node in queues and set(queues[node]) != expected
        ]
        silent = [node for node in found if node not in answered or node not in queues]
        if wrong:
            yield Result(
                check, FAIL, f"{'; '.join(wrong)}: expected exactly {', '.join(sorted(expected))}"
            )
        elif silent:
            yield Result(check, WARN, f"{', '.join(silent)} answered only part of the inspection")
        else:
            yield Result(
                check,
                PASS,
                f"{len(found)} answered ({', '.join(found)}): {', '.join(sorted(expected))}",
            )
    for node in sorted(node for node in nodes if worker_name(node) not in WORKERS):
        consumed = ", ".join(sorted(queues.get(node, ()))) or "unknown queues"
        yield Result(
            f"celery.{worker_name(node)}",
            WARN,
            f"{node}, consuming {consumed}: a worker the topology doesn't name",
        )


def judge_beat(oldest_due_s: float) -> Result:
    if oldest_due_s > 60:
        return Result(
            "celery.beat",
            WARN,
            f"an outbox event is {oldest_due_s:.0f} s overdue: beat (the relay's tick) or"
            " worker-outbox isn't running",
        )
    return Result(
        "celery.beat",
        SKIP,
        "beat answers no ping (nothing overdue now); its health check is"
        " `manage.py ledger_status` in its container",
    )


# --- the edge -----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]  # lower-cased names; repeated headers joined with ", "
    tls: str | None = None


def fetch(url: str, *, timeout: float, context: ssl.SSLContext | None = None) -> Response:
    """One GET, redirects not followed, certificate and host name verified for https."""
    parts = urlsplit(url)
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            parts.hostname or "", parts.port, timeout=timeout, context=context
        )
    else:
        connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=timeout)
    try:
        connection.request("GET", parts.path or "/", headers={"User-Agent": "arkray-preflight"})
        # Before the response: a server that closes after it takes the socket with it.
        sock = connection.sock
        tls = sock.version() if isinstance(sock, ssl.SSLSocket) else None
        response = connection.getresponse()
        response.read(65536)
        headers: dict[str, str] = {}
        for key, value in response.getheaders():
            name = key.lower()
            headers[name] = f"{headers[name]}, {value}" if name in headers else value
        return Response(response.status, headers, tls)
    finally:
        connection.close()


def check_edge(options: Options) -> Iterator[Result]:
    if not options.base_url:
        yield Result("edge", SKIP, "--base-url https://<the public name> checks the edge")
        return
    base = options.base_url.rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        yield Result("edge", FAIL, f"--base-url must be https://<host> (got {base!r})")
        return
    context = ssl.create_default_context(cafile=options.edge_ca_file or None)
    timeout = options.network_timeout(10.0)
    try:
        login = fetch(f"{base}/login", timeout=timeout, context=context)
    except ssl.SSLCertVerificationError as error:
        yield Result(
            "edge.login", FAIL, f"{base}: the certificate isn't trusted ({error.verify_message})"
        )
        return
    except OSError as error:
        yield Result("edge.login", FAIL, f"{base}: {reason(error)}")
        return
    yield judge_login(login)
    yield judge_hsts("edge.hsts", login.headers)
    yield judge_page_headers(login.headers)
    yield judge_server_header(login.headers)
    for check, path, judge in (
        ("edge.api", "/api/v1/auth/me", judge_api),
        ("edge.health", "/health/ready", judge_health),
    ):
        try:
            yield judge(fetch(f"{base}{path}", timeout=timeout, context=context))
        except OSError as error:
            yield Result(check, FAIL, f"{path}: {reason(error)}")
    http_url = options.http_url or (f"http://{parts.hostname}" if parts.port in (None, 443) else "")
    if not http_url:
        yield Result(
            "edge.http", SKIP, "a non-standard https port: --http-url names the plain-HTTP one"
        )
        return
    try:
        plain = fetch(f"{http_url.rstrip('/')}/login", timeout=timeout)
    except OSError as error:
        yield Result(
            "edge.http",
            WARN,
            f"{http_url}: {reason(error)} (someone typing the bare name gets an error; the"
            " reference edge redirects)",
        )
        return
    yield judge_http_redirect(plain)


def judge_login(response: Response) -> Result:
    tls = f" over {response.tls}" if response.tls else ""
    if response.status == 200:
        return Result("edge.login", PASS, f"/login 200{tls}, the certificate verified")
    return Result("edge.login", FAIL, f"/login answered {response.status}{tls}")


def judge_hsts(check: str, headers: Mapping[str, str]) -> Result:
    value = headers.get("strict-transport-security", "")
    found = re.search(r"max-age=(\d+)", value)
    if not found:
        return Result(check, FAIL, "no Strict-Transport-Security header")
    if int(found[1]) < HSTS_MIN_SECONDS:
        return Result(check, FAIL, f"{value!r}: max-age under a year")
    return Result(check, PASS, value)


def judge_page_headers(headers: Mapping[str, str]) -> Result:
    """What the reference edge and the web tier send with every page (next.config.ts,
    src/proxy.ts, infrastructure/nginx/arkray.conf)."""
    csp = headers.get("content-security-policy", "")
    missing = [
        name
        for name, present in (
            ("X-Content-Type-Options: nosniff", headers.get("x-content-type-options") == "nosniff"),
            (
                "X-Frame-Options: DENY (or frame-ancestors 'none')",
                headers.get("x-frame-options", "").upper() == "DENY"
                or "frame-ancestors 'none'" in csp,
            ),
            ("Content-Security-Policy", bool(csp)),
            ("Referrer-Policy", bool(headers.get("referrer-policy"))),
        )
        if not present
    ]
    if missing:
        return Result("edge.headers", FAIL, f"missing {'; '.join(missing)}")
    notes = [
        name
        for name, present in (
            ("X-Request-ID (minted by the edge)", bool(headers.get("x-request-id"))),
            ("Cache-Control: no-store", "no-store" in headers.get("cache-control", "")),
        )
        if not present
    ]
    if notes:
        return Result("edge.headers", WARN, f"missing {'; '.join(notes)}")
    return Result(
        "edge.headers",
        PASS,
        "nosniff, DENY framing, a CSP, a Referrer-Policy, no-store, X-Request-ID",
    )


def judge_server_header(headers: Mapping[str, str]) -> Result:
    server = headers.get("server", "")
    if re.search(r"\d", server) or "/" in server:
        return Result(
            "edge.server", FAIL, f"Server: {server!r} names a version (server_tokens off)"
        )
    return Result("edge.server", PASS, f"Server: {server!r}" if server else "no Server header")


def judge_api(response: Response) -> Result:
    if response.status in (401, 403) and "json" in response.headers.get("content-type", ""):
        hsts = judge_hsts("edge.api", response.headers)
        if hsts.status != PASS:
            return Result("edge.api", FAIL, f"/api answered, but {hsts.detail}")
        return Result(
            "edge.api", PASS, f"/api reaches Django ({response.status} without a session)"
        )
    if response.status == 200:
        return Result("edge.api", FAIL, "/api/v1/auth/me answered 200 without a session")
    return Result(
        "edge.api",
        FAIL,
        f"/api/v1/auth/me answered {response.status}: /api isn't routed to Django"
        " (docs/deployment.md#reverse-proxy)",
    )


def judge_health(response: Response) -> Result:
    if response.status == 200:
        return Result(
            "edge.health",
            WARN,
            "/health/ready answers through the edge: probes and the scraper should reach the pods"
            " directly (docs/deployment.md#reverse-proxy)",
        )
    return Result("edge.health", PASS, f"/health/* not routed ({response.status})")


def judge_http_redirect(response: Response) -> Result:
    location = response.headers.get("location", "")
    if response.status in (301, 302, 307, 308) and location.startswith("https://"):
        return Result("edge.http", PASS, f"http:// redirects to https:// ({response.status})")
    if 200 <= response.status < 300:
        return Result(
            "edge.http", FAIL, f"http:// serves pages ({response.status}) instead of redirecting"
        )
    return Result(
        "edge.http",
        FAIL,
        f"http:// answered {response.status} {location!r}, not a redirect to https://",
    )


# --- output -------------------------------------------------------------------------------------
GROUPS: dict[str, Callable[[Options], Iterable[Result]]] = {
    "credentials": check_credentials,
    "database": check_database,
    "redis": check_redis,
    "smtp": check_smtp,
    "storage": check_storage,
    "scanner": check_scanner,
    "ledger": check_ledger,
    "celery": check_celery,
    "edge": check_edge,
}


def summary(results: Iterable[Result]) -> dict[str, int]:
    counts = Counter(result.status for result in results)
    return {status: counts[status] for status in STATUSES}


def as_table(results: list[Result]) -> str:
    width = max([len("CHECK"), *(len(result.check) for result in results)])
    lines = [f"{'CHECK':<{width}}  STATUS  DETAIL"]
    lines += [f"{r.check:<{width}}  {r.status:<6}  {r.detail}" for r in results]
    counts = summary(results)
    verdict = "FAILED" if counts[FAIL] else "passed"
    lines += ["", f"preflight {verdict}: " + ", ".join(f"{n} {s}" for s, n in counts.items())]
    return "\n".join(lines)


def as_json(results: list[Result]) -> str:
    counts = summary(results)
    return json.dumps(
        {"ok": not counts[FAIL], "summary": counts, "results": [asdict(r) for r in results]},
        indent=2,
    )
