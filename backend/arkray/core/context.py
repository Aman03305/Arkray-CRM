"""Per-request / per-job execution context.

Carries the correlation ID (and other safe identifiers) through logs, audit events and
outbox events without threading them through every function signature.
"""

from __future__ import annotations

import dataclasses
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    correlation_id: str
    client_ip: str | None = None
    user_id: str | None = None
    subject_user_id: str | None = None
    task_id: str | None = None
    task_name: str | None = None

    def log_fields(self) -> dict[str, str]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


_current: ContextVar[ExecutionContext | None] = ContextVar("arkray_execution_context", default=None)


def get_context() -> ExecutionContext | None:
    return _current.get()


def bind_context(context: ExecutionContext) -> Token[ExecutionContext | None]:
    return _current.set(context)


def update_context(
    *,
    correlation_id: str | None = None,
    user_id: str | None = None,
    subject_user_id: str | None = None,
) -> None:
    """Add identifiers once they become known (e.g. the user after authentication)."""
    current = _current.get()
    if current is not None:
        _current.set(
            dataclasses.replace(
                current,
                correlation_id=correlation_id or current.correlation_id,
                user_id=user_id or current.user_id,
                subject_user_id=subject_user_id or current.subject_user_id,
            )
        )


def reset_context(token: Token[ExecutionContext | None]) -> None:
    _current.reset(token)


def current_correlation_id() -> str:
    context = _current.get()
    return context.correlation_id if context else ""
