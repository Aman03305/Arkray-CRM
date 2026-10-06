"""The language-model adapter (docs/rag-architecture.md#llm-usage).

`AnthropicProvider` calls Claude through the official Anthropic Python SDK (Messages API,
beta namespace for the refusal fallback): adaptive thinking with an explicit effort, strict
client tools, a cached system prompt, a per-call timeout inside the question's budget, and
`max_retries` from settings (the circuit breaker is the outer layer). SDK errors are mapped
to `ProviderError` with whether they should count toward the breaker; nothing about the
request or response content is logged.

`ScriptedProvider` (tests) replays a fixed script of tool calls and answers and records
every request it receives, so tests can assert exactly what would have reached a provider.

Everything about which model and how is in settings (AI_*); nothing else names a model.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from django.conf import settings

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MIN_ATTEMPT_S = 1.0  # don't start a model call with less time than this left
# Phase 10 drill: a retry followed its failure at once, and a 429 that asked for 30 s was
# retried immediately (and failed again, adding to the provider's load). A retry now waits a
# jittered RETRY_BACKOFF_S..2x, or what a 429's Retry-After asks if that fits the question's
# remaining time; if it doesn't, the question falls back now instead of waiting in vain.
RETRY_BACKOFF_S = 0.5


class ProviderError(Exception):
    """The model call failed. `counts` says whether it is a provider-health failure (it
    trips the breaker) rather than a bug in our request; `retryable` whether another
    attempt can help (a rejected key can't); `retry_after` is the wait the provider asked
    for, if it said."""

    def __init__(
        self,
        kind: str,
        *,
        counts: bool,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.counts = counts
        self.retryable = retryable
        self.retry_after = retry_after


RETRY_AFTER_MAX_S = 24 * 60 * 60


def _retry_after(exc: Any) -> float | None:
    """Seconds from a response's Retry-After header: None without one; delta-seconds or an
    HTTP-date (RFC 9110) as seconds from now; infinity for a value present but unusable
    (negative, a day or more, unparseable), which means "don't retry" rather than "retry
    soon" (Phase 10 review)."""
    try:
        raw = exc.response.headers.get("retry-after")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        try:
            value = max(0.0, (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return math.inf
    return value if 0 <= value < RETRY_AFTER_MAX_S else math.inf


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass(frozen=True, slots=True)
class Completion:
    content: list[Any]  # the assistant content blocks, replayed verbatim in the next turn
    stop_reason: str
    text: str
    tool_calls: list[ToolCall]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    provider_request_id: str = ""  # the provider's id for this call (support, tracing)


class ChatProvider(Protocol):
    model_name: str

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout: float,
        allow_tools: bool,
    ) -> Completion: ...


# Loggers that would write request bodies at DEBUG (also pinned in settings.LOGGING).
QUIET_LOGGERS = ("anthropic", "httpx", "httpx2", "httpcore", "httpcore2", "urllib3")


class AnthropicProvider:
    def __init__(self) -> None:
        import anthropic

        # Importing the SDK applies ANTHROPIC_LOG, which can set its loggers to DEBUG: they
        # would log whole requests (questions, notes, tool results). Pin them again.
        for name in QUIET_LOGGERS:
            logger_ = logging.getLogger(name)
            logger_.setLevel(max(logger_.level, logging.WARNING))
        self._anthropic = anthropic
        self.model_name = settings.AI_CHAT_MODEL
        # Retries are ours (see complete): the SDK's own would each get the full timeout
        # and could overrun the question's budget.
        self._client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            base_url=settings.AI_LLM_BASE_URL,  # explicit: never ANTHROPIC_BASE_URL
            max_retries=0,
            timeout=settings.AI_LLM_TIMEOUT_S,
        )
        # Where this process sends model calls, once (the host only, never the key).
        logger.info(
            "ai_provider_configured",
            extra={"host": urlsplit(settings.AI_LLM_BASE_URL).hostname, "model": self.model_name},
        )

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout: float,
        allow_tools: bool,
    ) -> Completion:
        """One completion within `timeout` seconds in all: a provider-health failure
        (429, 5xx, timeout, connection, malformed) is retried up to AI_LLM_MAX_RETRIES times
        while time is left, after a jittered back-off or the Retry-After the provider asked
        for; a rejected key (401/403) counts toward the breaker but isn't retried; the
        deadline is never extended."""
        deadline = time.monotonic() + timeout
        for attempt in range(settings.AI_LLM_MAX_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining < MIN_ATTEMPT_S:
                raise ProviderError("timeout", counts=True)
            try:
                return self._complete_once(
                    system=system,
                    tools=tools,
                    messages=messages,
                    timeout=remaining,
                    allow_tools=allow_tools,
                )
            except ProviderError as exc:
                if not (exc.counts and exc.retryable) or attempt == settings.AI_LLM_MAX_RETRIES:
                    raise
                wait = exc.retry_after
                if wait is None:
                    wait = RETRY_BACKOFF_S * (1 + random.random())  # noqa: S311 — jitter
                if deadline - time.monotonic() - wait < MIN_ATTEMPT_S:
                    raise  # it would wait past the question's budget: fall back now
                time.sleep(wait)
        raise ProviderError("timeout", counts=True)  # unreachable: the loop returns or raises

    def _complete_once(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout: float,
        allow_tools: bool,
    ) -> Completion:
        anthropic = self._anthropic
        try:
            params: dict[str, Any] = {
                "model": self.model_name,
                "max_tokens": settings.AI_CHAT_MAX_TOKENS,
                "system": [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                "tools": tools,
                "tool_choice": {"type": "auto" if allow_tools else "none"},
                "messages": messages,
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": settings.AI_CHAT_EFFORT},
                "betas": [FALLBACK_BETA],
                "fallbacks": "default",
                "timeout": timeout,
            }
            response = self._client.beta.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise ProviderError(
                "rate_limited", counts=True, retry_after=_retry_after(exc)
            ) from None
        except anthropic.APITimeoutError:
            raise ProviderError("timeout", counts=True) from None
        except anthropic.APIConnectionError:
            raise ProviderError("connection", counts=True) from None
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                # 500, 503 and 529 (overloaded) alike: the provider's health, retried after
                # what it asks for (503 and 529 used to land below as "rejected" requests).
                kind = "overloaded" if exc.status_code == 529 else "server_error"
                raise ProviderError(kind, counts=True, retry_after=_retry_after(exc)) from None
            # 4xx other than 429: our request or our credentials. Logged (status only), not
            # a provider outage, but the question can't be answered by the model. A rejected
            # key counts toward the breaker (every question would fail) without a retry.
            logger.error("ai_provider_rejected", extra={"status": exc.status_code})
            counts = exc.status_code in {401, 403}
            raise ProviderError(
                f"status_{exc.status_code}", counts=counts, retryable=False
            ) from None
        except anthropic.AnthropicError:
            raise ProviderError("sdk_error", counts=True) from None
        except Exception as exc:  # noqa: BLE001 — e.g. invalid JSON in a 200 from a proxy
            raise _bad_response(exc) from None
        try:
            text = "".join(block.text for block in response.content if block.type == "text")
            calls = [
                ToolCall(block.id, block.name, block.input)
                for block in response.content
                if block.type == "tool_use"
            ]
            usage = response.usage
            return Completion(
                content=list(response.content),
                stop_reason=str(response.stop_reason),
                text=text,
                tool_calls=calls,
                model=response.model,
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                provider_request_id=str(getattr(response, "_request_id", "") or "")[:100],
            )
        except Exception as exc:  # noqa: BLE001 — not a Messages response (an HTML page, say)
            raise _bad_response(exc) from None


def _bad_response(exc: Exception) -> ProviderError:
    """Whatever came back wasn't a usable Messages response (Phase 9 review: an HTML 200
    from a proxy crashed the worker, so the question waited for its expiry and the breaker
    never opened). Counted like an outage; logged by type only, never content."""
    logger.warning("ai_provider_bad_response", extra={"error": type(exc).__name__})
    return ProviderError("bad_response", counts=True)


# --- tests -------------------------------------------------------------------------------------
@dataclass
class ScriptedProvider:
    """Each step is either a list of (tool name, arguments) calls, a final answer text, or
    a ProviderError to raise. Records every request (system, tool names, messages)."""

    steps: list[Any]
    model_name: str = "scripted-model"
    requests: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout: float,
        allow_tools: bool,
    ) -> Completion:
        self.requests.append(
            {
                "system": system,
                "tools": [t["name"] for t in tools],
                "tool_definitions": tools,
                "messages": [dict(m) for m in messages],
                "allow_tools": allow_tools,
            }
        )
        if not self.steps:
            return Completion([{"type": "text", "text": ""}], "end_turn", "", [], self.model_name)
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        if isinstance(step, tuple) and step and step[0] == "refusal":
            return Completion([], "refusal", "", [], self.model_name)
        if isinstance(step, tuple) and step and step[0] == "stop":  # ("stop", reason, text)
            _, reason, text = step
            return Completion([{"type": "text", "text": text}], reason, text, [], self.model_name)
        if isinstance(step, list) and allow_tools:
            calls = [
                ToolCall(f"call_{len(self.requests)}_{i}", n, a) for i, (n, a) in enumerate(step)
            ]
            content = [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
            ]
            return Completion(content, "tool_use", "", calls, self.model_name)
        text = step if isinstance(step, str) else ""
        return Completion([{"type": "text", "text": text}], "end_turn", text, [], self.model_name)

    def sent_text(self) -> str:
        """Everything that would have been sent to the provider, as one string."""
        import json

        return json.dumps(self.requests, default=str)


_override = threading.local()


@contextmanager
def override_provider(provider: ChatProvider | None) -> Iterator[None]:
    """Use `provider` for questions answered in this thread (tests)."""
    previous = getattr(_override, "provider", _MISSING)
    _override.provider = provider
    try:
        yield
    finally:
        if previous is _MISSING:
            del _override.provider
        else:
            _override.provider = previous


_MISSING = object()
_shared: dict[str, AnthropicProvider] = {}
_shared_lock = threading.Lock()


def get_provider() -> ChatProvider | None:
    """The configured model, or None when Ask Arkray runs without one."""
    override = getattr(_override, "provider", _MISSING)
    if override is not _MISSING:
        return cast("ChatProvider | None", override)
    if (
        not settings.AI_ENABLED
        or settings.AI_LLM_PROVIDER != "anthropic"
        or not settings.ANTHROPIC_API_KEY
    ):
        return None
    with _shared_lock:
        key = settings.AI_CHAT_MODEL
        if key not in _shared:
            _shared[key] = AnthropicProvider()
        return _shared[key]
