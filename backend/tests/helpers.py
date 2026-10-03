"""Helpers shared by API and security tests."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.core import mail
from django.db import connections
from rest_framework.test import APIClient

from arkray.core import outbox
from arkray.core.domain_events import DomainEvent, subscribed
from arkray.identity.sessions import AUTH_AT, SEEN_AT

LINK_PATTERN = re.compile(
    r"https?://[^\s/]+/(?P<path>activate|reset-password)/(?P<secret>[A-Za-z0-9_-]{43})(?=\s|$)"
)


def signed_in(user: Any, client: APIClient | None = None) -> APIClient:
    """A client with a live session for `user` (as if they had just signed in)."""
    client = client or APIClient()
    client.force_login(user)
    session = client.session
    now = time.time()
    session[AUTH_AT] = now
    session[SEEN_AT] = now
    session.save()
    return client


def drain_outbox(rounds: int = 5) -> int:
    """Run the outbox relay with inline processing until nothing is due."""
    processed = 0
    for _ in range(rounds):
        dispatched = outbox.relay(
            dispatch=lambda event: outbox.process_event(event.pk, str(event.claim_token))
        )
        processed += dispatched
        if not dispatched:
            break
    return processed


def emailed_secrets(path: str) -> list[str]:
    """One-time secrets in links of sent emails, oldest first ("activate"/"reset-password")."""
    return [
        match["secret"]
        for message in mail.outbox
        for match in LINK_PATTERN.finditer(str(message.body))
        if match["path"] == path
    ]


def last_secret(path: str) -> str:
    found = emailed_secrets(path)
    assert found, f"no {path} link was emailed"
    return found[-1]


def without_request_id(response: Any) -> Any:
    body = response.json()
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body["error"].pop("request_id", None)
    return body


def run_concurrently(*calls: Callable[[], Any]) -> list[Any]:
    """Start every call at the same moment in its own thread (own DB connection).

    Returns each call's result, or the exception it raised.
    """
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(index: int, call: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = call()
        except BaseException as exc:
            results[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@contextmanager
def collected(event_type: type[DomainEvent]) -> Iterator[list[Any]]:
    """The domain events of `event_type` published inside the block, in order."""
    seen: list[Any] = []
    with subscribed(event_type, seen.append):
        yield seen
