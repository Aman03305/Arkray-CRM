"""Structured logging.

Every line is a single JSON object (newlines and control characters are escaped by the
JSON encoder, which also neutralises log-injection attempts). Request/job identifiers are
attached automatically from the execution context.

Rules (see docs/observability.md): never log request bodies, headers, cookies, passwords,
tokens, or free-text CRM content. Log identifiers, not personal data.

Database errors are logged by type, SQLSTATE and constraint name, never their message:
PostgreSQL's carries row values ("Failing row contains (...)", "Key (email)=(...)") into
the exception text, so a traceback of an unexpected IntegrityError would have put a
person's data in the log (whole-software audit, R63).
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from types import TracebackType

import psycopg
from django.db import Error as DjangoDatabaseError

from .context import get_context

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime", "taskName"}
)
# Attributes Django attaches to log records that may contain personal data (e.g. the
# full request object with its query string). They are dropped, never serialised.
_DROPPED_ATTRS = frozenset({"request"})


class ContextFilter(logging.Filter):
    """Copies execution-context identifiers onto each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_context()
        if context is not None:
            for key, value in context.log_fields().items():
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


def format_exception(exc_info: ExcInfo) -> str:
    """The traceback as Python prints it, with every database error's message in the chain
    replaced by `_safe_message` (the text is replaced where Python printed it)."""
    text = "".join(traceback.format_exception(*exc_info))
    for exc in _chain(exc_info[1]):
        message = str(exc)
        if message and _is_database_error(exc):
            text = text.replace(message, _safe_message(exc))
    return text


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and key not in _DROPPED_ATTRS and not key.startswith("_")
    }


class _Formatter(logging.Formatter):
    """Tracebacks without database errors' messages (`format_exception`)."""

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
            "msg": record.getMessage(),
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
        line = f"{record.levelname:<7} {record.name}: {_escape(record.getMessage())}"
        if extras:
            line = f"{line} | {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line
