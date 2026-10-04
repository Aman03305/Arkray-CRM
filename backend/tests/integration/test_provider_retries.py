"""Model-call retries back off, and honour a 429's Retry-After (Phase 10 drill: against a
fake provider, a 429 that asked for 30 s was retried at once, failing again and adding to
the provider's load)."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import anthropic
import httpx2
import pytest

from arkray.ai import llm
from arkray.ai.llm import AnthropicProvider, ProviderError


def provider_failing_with(*errors: ProviderError) -> tuple[AnthropicProvider, list[float]]:
    """A provider whose calls fail with `errors` in turn, then succeed; records when each
    call started."""
    provider = AnthropicProvider.__new__(AnthropicProvider)
    started: list[float] = []
    pending = list(errors)

    def once(**_kwargs: Any) -> Any:
        started.append(time.monotonic())
        if pending:
            raise pending.pop(0)
        return "answered"

    provider._complete_once = once  # type: ignore[method-assign]
    return provider, started


def complete(provider: AnthropicProvider, timeout: float) -> Any:
    return provider.complete(system="", tools=[], messages=[], timeout=timeout, allow_tools=True)


# Windows' monotonic clock ticks every 15.6 ms: a 0.3 s sleep can measure 0.297 s
# (Phase 10 review: the strict bound failed about three runs in four there).
CLOCK = 0.02


def status_error(
    cls: type[anthropic.APIStatusError], status: int, retry_after: str | None
) -> anthropic.APIStatusError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, headers=headers, request=request)
    return cls("provider error", response=response, body=None)


def rate_limited(retry_after: str | None) -> anthropic.APIStatusError:
    return status_error(anthropic.RateLimitError, 429, retry_after)


def test_a_rate_limit_asking_longer_than_the_question_has_left_falls_back_at_once(settings):
    settings.AI_LLM_MAX_RETRIES = 1
    provider, started = provider_failing_with(
        ProviderError("rate_limited", counts=True, retry_after=30.0)
    )
    began = time.monotonic()
    with pytest.raises(ProviderError, match="rate_limited"):
        complete(provider, timeout=10.0)
    assert len(started) == 1  # not retried into a certain second 429
    assert time.monotonic() - began < 0.5


def test_a_short_retry_after_is_waited_for_then_retried(settings):
    settings.AI_LLM_MAX_RETRIES = 1
    provider, started = provider_failing_with(
        ProviderError("rate_limited", counts=True, retry_after=0.3)
    )
    assert complete(provider, timeout=10.0) == "answered"
    assert len(started) == 2
    assert started[1] - started[0] >= 0.3 - CLOCK


def test_other_retries_back_off_with_jitter(settings, monkeypatch):
    settings.AI_LLM_MAX_RETRIES = 1
    monkeypatch.setattr(llm, "RETRY_BACKOFF_S", 0.2)
    provider, started = provider_failing_with(ProviderError("server_error", counts=True))
    assert complete(provider, timeout=10.0) == "answered"
    assert 0.2 - CLOCK <= started[1] - started[0] < 0.4 + 0.2  # backoff .. 2x backoff


def test_a_failure_that_does_not_count_is_never_retried(settings):
    settings.AI_LLM_MAX_RETRIES = 3
    provider, started = provider_failing_with(ProviderError("status_400", counts=False))
    with pytest.raises(ProviderError, match="status_400"):
        complete(provider, timeout=10.0)
    assert len(started) == 1


def test_a_rejected_key_counts_toward_the_breaker_without_a_retry(settings):
    """Phase 10 review: a revoked key cost two calls per question."""
    settings.AI_LLM_MAX_RETRIES = 3
    provider, started = provider_failing_with(
        ProviderError("status_401", counts=True, retryable=False)
    )
    with pytest.raises(ProviderError, match="status_401"):
        complete(provider, timeout=10.0)
    assert len(started) == 1


def http_date(seconds_from_now: float) -> str:
    return format_datetime(datetime.now(UTC) + timedelta(seconds=seconds_from_now), usegmt=True)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("30", 30.0),
        ("0.5", 0.5),
        ("0", 0.0),
        (None, None),  # no header: the jittered back-off
        # Present but unusable means "don't retry", not "retry soon" (Phase 10 review).
        ("soon", math.inf),
        ("-1", math.inf),
        ("86400", math.inf),
        ("999999", math.inf),
    ],
)
def test_retry_after_is_read_from_the_providers_429(header, expected):
    assert llm._retry_after(rate_limited(header)) == expected


def test_retry_after_may_be_an_http_date():
    assert 25 <= llm._retry_after(rate_limited(http_date(30))) <= 31
    assert llm._retry_after(rate_limited(http_date(-60))) == 0.0  # already passed: now
    assert llm._retry_after(rate_limited(http_date(3 * 86400))) == math.inf


def test_an_unusable_retry_after_falls_back_at_once(settings):
    settings.AI_LLM_MAX_RETRIES = 1
    provider, started = provider_failing_with(
        ProviderError("rate_limited", counts=True, retry_after=math.inf)
    )
    with pytest.raises(ProviderError):
        complete(provider, timeout=10.0)
    assert len(started) == 1


def test_the_sdks_429_carries_its_retry_after_into_the_provider_error(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"
    provider = AnthropicProvider()

    def create(**_kwargs: Any) -> Any:
        raise rate_limited("30")

    provider._client.beta.messages.create = create
    with pytest.raises(ProviderError) as raised:
        provider._complete_once(system="", tools=[], messages=[], timeout=5.0, allow_tools=True)
    assert (raised.value.kind, raised.value.retry_after) == ("rate_limited", 30.0)


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (anthropic.InternalServerError, 500, "server_error"),
        (anthropic.ServiceUnavailableError, 503, "server_error"),
        (anthropic.OverloadedError, 529, "overloaded"),
    ],
)
def test_provider_side_errors_are_health_failures_with_their_retry_after(
    settings, caplog, error, status, kind
):
    """Phase 10 review: 503 and 529 landed with the rejected requests (logged as our
    fault at ERROR, retried at once whatever Retry-After said)."""
    settings.ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"
    provider = AnthropicProvider()

    def create(**_kwargs: Any) -> Any:
        raise status_error(error, status, "7")

    provider._client.beta.messages.create = create
    with caplog.at_level("ERROR", logger="arkray.ai.llm"), pytest.raises(ProviderError) as raised:
        provider._complete_once(system="", tools=[], messages=[], timeout=5.0, allow_tools=True)
    assert (raised.value.kind, raised.value.counts, raised.value.retryable) == (kind, True, True)
    assert raised.value.retry_after == 7.0
    assert "ai_provider_rejected" not in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(("status", "counts"), [(401, True), (403, True), (400, False)])
def test_rejected_requests_are_never_retried(settings, status, counts):
    settings.ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"
    provider = AnthropicProvider()
    cls = {401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError}.get(
        status, anthropic.BadRequestError
    )

    def create(**_kwargs: Any) -> Any:
        raise status_error(cls, status, None)

    provider._client.beta.messages.create = create
    with pytest.raises(ProviderError) as raised:
        provider._complete_once(system="", tools=[], messages=[], timeout=5.0, allow_tools=True)
    assert (raised.value.counts, raised.value.retryable) == (counts, False)
