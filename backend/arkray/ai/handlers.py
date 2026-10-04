"""Outbox handlers for the semantic index (registered from AiConfig.ready), queue
`ai_index`. Thin: logic lives in ai.indexing. Failures (the embedding model unavailable)
are retried by the outbox with backoff, then dead; there is no second retry layer."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.conf import settings

from arkray.core import outbox
from arkray.core.outbox import PermanentFailure

from . import indexing
from .sources import SourceModule


def _module(payload: dict[str, Any]) -> SourceModule:
    try:
        return SourceModule(str(payload["source"]))
    except (KeyError, ValueError):
        raise PermanentFailure("Unknown source module.") from None


def _uuid(value: Any) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise PermanentFailure("Malformed identifier.") from None


@outbox.handler(indexing.TOPIC_INDEX_SOURCE, queue="ai_index")
def index_source(payload: dict[str, Any]) -> None:
    if not settings.AI_INDEXING_ENABLED:
        return  # turned off after it was queued; reconciliation catches up when it is back
    indexing.index_source(_module(payload), _uuid(payload.get("id")))


@outbox.handler(indexing.TOPIC_INDEX_LEAD, queue="ai_index")
def index_lead(payload: dict[str, Any]) -> None:
    if not settings.AI_INDEXING_ENABLED:
        return
    indexing.index_lead(_uuid(payload.get("lead_id")))


@outbox.handler(indexing.TOPIC_RECONCILE, queue="ai_index", max_attempts=4)
def reconcile_batch(payload: dict[str, Any]) -> None:
    if not settings.AI_INDEXING_ENABLED:
        return
    after = payload.get("after")
    indexing.continue_reconciliation(_module(payload), _uuid(after) if after else None)
