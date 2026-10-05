"""Outbox handlers for attachments (registered from ActivitiesConfig.ready). Thin: the logic
lives in attachments.py. A storage or scanner outage raises, so the outbox retries with
backoff and finally marks the event dead (an alert), never losing the work."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from arkray.core import outbox

from . import attachments


@outbox.handler(attachments.TOPIC_PURGE, queue="default")
def purge_attachment(payload: dict[str, Any]) -> None:
    attachments.purge(UUID(str(payload["attachment_id"])))


@outbox.handler(attachments.TOPIC_SCAN, queue="default", max_attempts=12)
def scan_attachment(payload: dict[str, Any]) -> None:
    attachments.scan(UUID(str(payload["attachment_id"])))
