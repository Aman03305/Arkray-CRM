"""Structured logging.

Every line is a single JSON object (newlines and control characters are escaped by the
JSON encoder, which also neutralises log-injection attempts). Request/job identifiers are
attached automatically from the execution context.

Rules (see docs/observability.md#what-a-log-line-may-hold): never log request bodies,
headers, cookies, passwords, tokens, or free-text CRM content. Log identifiers, not personal
data. Enforced here, not only by convention (privacy remediation P2-2):

- **Allowlisted fields.** A record's extra fields are written only when their name is in
  `SAFE_FIELDS` (ids, codes, counts, timings). Anything else (Celery's `data` with its
  traceback and task arguments, Django's `request`, a field someone adds later) is dropped
  and only its *name* is listed under `withheld`, so a missing field is visible, never its
  value. Values must be scalars, or lists/dicts of them, of bounded size.
- **Exceptions by type and place.** Tracebacks keep every frame (file, line, function, code
  line) and every exception's type, but no exception's *message* unless LOG_EXCEPTION_MESSAGES
  is on (local development only): PostgreSQL's carry row values ("Failing row contains
  (...)", "Key (email)=(...)"), and other messages quote whatever a caller passed
  ("invalid literal for int() with base 10: '<value>'"). Database errors add their SQLSTATE
  and constraint name instead (R63).
- **Messages.** Arkray's own loggers log event names (constant strings). Other libraries'
  messages are rendered with their arguments sanitised (`_third_party_message`): Celery's
  task-failure line ("Task %(name)s[%(id)s] ...: %(exc)s") keeps the task and id but never
  the exception's text; gunicorn's error log loses query strings.
- **The client address** goes on the access-log line only (`http_request`), not on every
  line a request writes.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import UUID

import psycopg
from django.db import Error as DjangoDatabaseError

from .context import get_context

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime", "taskName"}
)

# The extra fields a log line may carry: identifiers, codes, counts, timings and flags.
# tests/security/test_log_redaction.py checks that every field the code logs is listed (a
# new one must be added here, deliberately) and that nothing else is ever written.
SAFE_FIELDS = frozenset(
    {
        # execution context (core.context); client_ip only on the access line
        "correlation_id",
        "client_ip",
        "user_id",
        "subject_user_id",
        "task_id",
        "task_name",
        "support_session_id",
        "support_target_id",
        # access log (core.middleware) and Django's own request logging
        "method",
        "path",
        "route",
        "status",
        "status_code",
        "duration_ms",
        "client_request_id",
        # application events
        "aborted",
        "abandoned",
        "account_token_id",
        "access_windows",
        "attachment_id",
        "attempt",
        "backend",
        "cache_read_tokens",
        "checked",
        "complete",
        "conversations",
        "cooldown_s",
        "count",
        "dependency",
        "details_expired",
        "error",
        "error_class",
        "error_code",
        "errors",
        "event_ids",
        "exc_type",
        "exit_code",
        "export_id",
        "extension",
        "failures",
        "gauge",
        "healthy",
        "host",
        "input_tokens",
        "kind",
        "latency_ms",
        "listed",
        "login",
        "mode",
        "model",
        "no_longer_scanned",
        "operation",
        "outbox_event_id",
        "outcome",
        "output_tokens",
        "pending_scans",
        "problems",
        "provider_request_id",
        "purged",
        "purpose",
        "question_id",
        "queued_for_scanning",
        "reason",
        "redacted",
        "redacted_reasons",
        "repaired",
        "requeued",
        "retry_in_s",
        "round",
        "security",
        "sessions",
        "source",
        "storage_failures",
        "stop_reason",
        "superseded",
        "support_sessions_expired",
        "target_user_id",
        "throttle_events",
        "tool",
        "tools",
        "topic",
        "unsupported_numbers",
        "workspace_kind",
        # Ask Arkray's answer metadata (ai.service)
        "records",
        # attachment reconciliation (activities.reconcile: Report.counts())
        "missing",
        "orphaned",
        "pending",
        "pending_scan",
        "failed",
        "restorable",
        "size_mismatch",
        "hash_mismatch",
        # privacy jobs (audit retention, erasure ledger, exports, custom field purge)
        "ledger_seq",
        "applied",
        "entries",
        "rows",
        "held",
        "opportunities",
        # gunicorn
        "worker_pid",
    }
)
# Never written whatever the policy says: Django's request (query string, headers), Celery's
# failure context (`data`: traceback, args, kwargs).
_DROPPED_ATTRS = frozenset({"request", "data"})
_MAX_TEXT = 500
_MAX_ITEMS = 50
_WITHHELD = "[withheld]"


class ContextFilter(logging.Filter):
    """Copies execution-context identifiers onto each record (the client address only onto
    the access line, which sets it itself)."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_context()
        if context is not None:
            for key, value in context.log_fields().items():
                if key == "client_ip":
                    continue
                if not hasattr(record, key):
                    setattr(record, key, value)
        return True


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    """An exception and every one it was raised from or during, once each."""
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending += [current.__cause__, current.__context__]


def _database_cause(exc: BaseException) -> psycopg.Error | None:
    return next((e for e in _chain(exc) if isinstance(e, psycopg.Error)), None)


def database_error_fields(exc: BaseException) -> dict[str, str]:
    """What identifies a database error without quoting data: SQLSTATE and constraint."""
    cause = _database_cause(exc)
    if cause is None:
        return {}
    fields = {"db_sqlstate": cause.sqlstate or ""}
    if cause.diag.constraint_name:
        fields["db_constraint"] = cause.diag.constraint_name
    return fields


def _is_database_error(exc: BaseException) -> bool:
    return isinstance(exc, DjangoDatabaseError | psycopg.Error)


def _safe_message(exc: BaseException) -> str:
    fields = database_error_fields(exc)
    described = ", ".join(f"{key[3:]}={value}" for key, value in fields.items() if value)
    return f"[database error message withheld{': ' + described if described else ''}]"


def safe_exception_text(exc: BaseException) -> str:
    """`Type: message`, a database error's message withheld."""
    message = _safe_message(exc) if _is_database_error(exc) else str(exc)
    return f"{type(exc).__name__}: {message}"


def _messages_allowed() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "LOG_EXCEPTION_MESSAGES", False))


def _exception_line(exc: BaseException, *, messages: bool) -> str:
    name = f"{type(exc).__module__}.{type(exc).__qualname__}".removeprefix("builtins.")
    if _is_database_error(exc):
        return f"{name}: {_safe_message(exc)}"
    if messages:
        return "".join(traceback.format_exception_only(exc)).rstrip("\n")
    return f"{name}: [message withheld]"


def _render(exc: BaseException, *, messages: bool, seen: set[int]) -> list[str]:
    seen.add(id(exc))
    lines: list[str] = []
    cause, context = exc.__cause__, exc.__context__
    if cause is not None and id(cause) not in seen:
        lines += _render(cause, messages=messages, seen=seen)
        lines.append("\nThe above exception was the direct cause of the following exception:\n\n")
    elif context is not None and not exc.__suppress_context__ and id(context) not in seen:
        lines += _render(context, messages=messages, seen=seen)
        lines.append("\nDuring handling of the above exception, another exception occurred:\n\n")
    if exc.__traceback__ is not None:
        lines.append("Traceback (most recent call last):\n")
        lines += traceback.format_tb(exc.__traceback__)
    lines.append(_exception_line(exc, messages=messages) + "\n")
    return lines


def format_exception(exc_info: ExcInfo, *, messages: bool | None = None) -> str:
    """The traceback as Python prints it (frames and exception types), every message in the
    chain withheld unless LOG_EXCEPTION_MESSAGES (and a database error's always)."""
    allowed = _messages_allowed() if messages is None else messages
    return "".join(_render(exc_info[1], messages=allowed, seen=set()))


def _bounded(value: Any, depth: int = 0) -> Any:
    """A JSON-able copy of an allowlisted field's value: scalars, ids, short text, and
    lists/dicts of them; anything else by type only."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"
    if depth < 3 and isinstance(value, list | tuple | set | frozenset):
        return [_bounded(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if depth < 3 and isinstance(value, Mapping):
        return {
            str(key)[:64]: _bounded(item, depth + 1)
            for key, item in list(value.items())[:_MAX_ITEMS]
        }
    return f"[{type(value).__name__}]"


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    fields: dict[str, object] = {}
    withheld: list[str] = []
    for key, value in record.__dict__.items():
        if key in _STANDARD_ATTRS or key.startswith("_"):
            continue
        if key in SAFE_FIELDS and key not in _DROPPED_ATTRS:
            fields[key] = _bounded(value)
        else:
            withheld.append(key[:64])
    if withheld:
        fields["withheld"] = sorted(withheld)
    return fields


# Third-party log lines: which of a mapping's arguments may be shown (Celery's task lines),
# and the shape of a positional argument shown as is (a name, an id, a number, a path).
_SAFE_MAPPING_ARGS = frozenset({"name", "id", "runtime", "hostname"})
_TOKEN = re.compile(r"[\w.:/<>=+\-\[\]]{0,200}")
_OWN_LOGGERS = ("arkray", "config", "django")


def _safe_arg(value: Any, logger_name: str) -> Any:
    if value is None or isinstance(value, bool | int | float | UUID):
        return value
    if isinstance(value, BaseException):
        return f"[{type(value).__name__}]"
    text = str(value)
    if logger_name.startswith("gunicorn"):
        text = text.split("?", 1)[0]  # a URI's query string (search terms, R61)
    if "@" not in text and _TOKEN.fullmatch(text):
        return text
    return _WITHHELD


_QUERY = re.compile(r"\?\S*")


def _third_party_message(record: logging.LogRecord) -> str:
    template = str(record.msg)
    if record.name.startswith("gunicorn"):
        # Lines gunicorn formats itself ("Invalid request from ip=...: <request line>"): the
        # client address belongs on the access line only, and the rest is the client's text
        # (backend review P3).
        if template.startswith("Invalid request from"):
            return "Invalid request from a client (address and request withheld)."
        template = _QUERY.sub("?[query withheld]", template)
    args = record.args
    if not args:
        return template
    try:
        if isinstance(args, Mapping):
            safe_map = {
                key: (
                    str(value)[:100]
                    if key == "description"  # Celery's own words ("raised unexpected")
                    else _safe_arg(value, record.name)
                    if key in _SAFE_MAPPING_ARGS
                    else _WITHHELD
                )
                for key, value in args.items()
            }
            return template % safe_map
        safe = tuple(_safe_arg(value, record.name) for value in args)
        return template % safe
    except (TypeError, ValueError, KeyError):
        return template


def safe_message(record: logging.LogRecord) -> str:
    name = record.name
    own = any(name == prefix or name.startswith(f"{prefix}.") for prefix in _OWN_LOGGERS)
    # `django` and `django.*`, never a third-party `django_*` package (backend review P3).
    if own and not (name == "django.db" or name.startswith("django.db.")):
        # Arkray's event names, and Django's request lines (the path, never the query).
        return record.getMessage()
    return _third_party_message(record)


class _Formatter(logging.Formatter):
    """Tracebacks without exceptions' messages (`format_exception`)."""

    def formatException(self, ei: object) -> str:  # noqa: N802 — logging's name
        if isinstance(ei, tuple) and len(ei) == 3 and ei[1] is not None:
            return format_exception(ei)
        return super().formatException(ei)  # type: ignore[arg-type]


class JsonFormatter(_Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": safe_message(record),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
            if record.exc_info[1] is not None:
                payload.update(database_error_fields(record.exc_info[1]))
        # ensure_ascii escapes every non-ASCII character, including U+2028/U+2029/U+0085,
        # which some log shippers treat as line breaks.
        return json.dumps(payload, default=str, ensure_ascii=True)


def _escape(value: object) -> str:
    """Render control and line-separator characters visibly (one event = one line)."""
    return str(value).encode("unicode_escape").decode("ascii")


class ConsoleFormatter(_Formatter):
    """Human-readable single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={_escape(v)}" for k, v in _extra_fields(record).items())
        line = f"{record.levelname:<7} {record.name}: {_escape(safe_message(record))}"
        if extras:
            line = f"{line} | {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class GunicornErrorFilter(logging.Filter):
    """gunicorn's error log through the JSON formatter (gunicorn.conf.py): the worker's pid
    attached; query strings and exception messages are removed by the formatter
    (`_third_party_message`, `format_exception`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.worker_pid = record.process
        return True
