"""Outbox handlers (registered from IdentityConfig.ready). Thin: logic lives in services.

Delivery uses the Phase 0 outbox exclusively: bounded attempts with backoff, then dead.
There is deliberately no second retry mechanism here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from arkray.core import outbox

from . import services


@outbox.handler(services.TOPIC_DELIVER_ACCOUNT_TOKEN, queue="email")
def deliver_account_token(payload: dict[str, Any]) -> None:
    services.deliver_account_token(UUID(str(payload["token_id"])))


@outbox.handler(services.TOPIC_PASSWORD_RESET_REQUESTED, queue="default")
def password_reset_requested(payload: dict[str, Any]) -> None:
    if "email" not in payload:
        return  # already processed and redacted by housekeeping
    requested_from = payload.get("requested_from")
    services.issue_password_reset(
        str(payload["email"]), str(requested_from) if requested_from else None
    )


@outbox.handler(services.TOPIC_EMAIL_CHANGED, queue="email")
def notify_email_changed(payload: dict[str, Any]) -> None:
    if "previous_email" not in payload:
        return  # already processed and redacted by housekeeping
    services.notify_email_changed(UUID(str(payload["user_id"])), str(payload["previous_email"]))
