"""Structured logging.

Every line is a single JSON object (newlines and control characters are escaped by the
JSON encoder, which also neutralises log-injection attempts). Request/job identifiers are
attached automatically from the execution context.

Rules (see docs/observability.md): never log request bodies, headers, cookies, passwords,
tokens, or free-text CRM content. Log identifiers, not personal data.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from .context import get_context

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


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and key not in _DROPPED_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
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
        # ensure_ascii escapes every non-ASCII character, including U+2028/U+2029/U+0085,
        # which some log shippers treat as line breaks.
        return json.dumps(payload, default=str, ensure_ascii=True)


def _escape(value: object) -> str:
    """Render control and line-separator characters visibly (one event = one line)."""
    return str(value).encode("unicode_escape").decode("ascii")


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={_escape(v)}" for k, v in _extra_fields(record).items())
        line = f"{record.levelname:<7} {record.name}: {_escape(record.getMessage())}"
        if extras:
            line = f"{line} | {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line
