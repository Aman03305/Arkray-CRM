"""Recording what reaches the model and what the CRM's tools return
(docs/ai-evaluation.md#what-is-recorded).

`RecordingProvider` wraps whichever provider answers (ScriptedProvider for the self-test,
the configured AnthropicProvider for the real run, `DryRunProvider` for a dry run) and is
installed with `llm.override_provider`, so the service path is exactly production's: the
service builds the request, the wrapper records it and passes it on. For every call it keeps
the payload as sent (system prompt, tool definitions, messages: a deep copy taken before the
call, since the service appends to the same list afterwards), the completion (stop reason,
text, tool calls, model, usage), its latency and its cost.

It is also where the run's hard caps live: before a call reaches a real provider, the
number of calls so far and the spend so far plus this call's worst case are checked against
`--max-model-calls` and `--max-cost-usd`; a call that would cross either is never sent, and
the run stops (`aborted`). The worst case can't be exceeded: one input token per byte of the
request (a token is never shorter than a byte) plus the API's own framing, nothing cached,
the whole output budget used, all at the most expensive model's prices (a server-side
fallback may answer on another model). The refusal is a ProviderError that doesn't count
toward the breaker, so the question in flight falls back to retrieval like any other failed
call.

`capture_tool_runs` records every tool execution (the model's, the router's, retrieval's),
including results never sent because the question stopped first.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from arkray.ai import tools
from arkray.ai.llm import ChatProvider, Completion, ProviderError

from .pricing import PriceTable, Usage

# Tokens the API adds around a request (tool-use instructions, message framing): generous.
FRAMING_TOKENS = 2_000


def jsonable(value: Any) -> Any:
    """A JSON-safe deep copy: SDK content blocks (pydantic models) as their dicts."""
    return json.loads(json.dumps(value, default=_encode, ensure_ascii=False))


def _encode(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return str(value)


@dataclass
class ModelCall:
    case: str
    turn: int
    request: dict[str, Any]  # system, tools, messages, allow_tools, timeout: as sent
    request_chars: int
    outcome: str  # ok | error | injected | blocked | not_sent
    latency_s: float = 0.0
    error: str = ""
    stop_reason: str = ""
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    price_assumed: bool = False
    provider_request_id: str = ""

    @property
    def sent(self) -> bool:
        """Whether this request left the process (a provider received it)."""
        return self.outcome in {"ok", "error"}


@dataclass
class ToolRun:
    case: str
    turn: int
    name: str
    arguments: Any
    is_error: bool
    content: str


class DryRunProvider:
    """Sends nothing: the recorder keeps the request, then the question falls back to
    retrieval (a ProviderError that doesn't count toward the breaker)."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout: float,
        allow_tools: bool,
    ) -> Completion:
        raise ProviderError("dry_run", counts=False)


@dataclass(frozen=True, slots=True)
class Limits:
    max_calls: int
    max_cost_usd: float


class RecordingProvider:
    def __init__(
        self,
        inner: ChatProvider,
        *,
        prices: PriceTable,
        limits: Limits | None = None,
        sends: bool = False,
    ) -> None:
        """`sends`: whether `inner` sends requests to a real provider (the caps apply)."""
        self.inner = inner
        self.model_name = inner.model_name
        self.prices = prices
        self.limits = limits
        self.sends = sends
        self.calls: list[ModelCall] = []
        self.tool_runs: list[ToolRun] = []
        self.case = ""
        self.turn = 0
        self.spent_usd = 0.0
        self.aborted = ""  # why the run stopped, if it did
        self._failures_to_inject = 0

    def begin(self, case: str, turn: int, inner: ChatProvider | None = None) -> None:
        self.case, self.turn = case, turn
        if inner is not None:
            self.inner = inner
            self.model_name = inner.model_name

    def inject_failures(self, count: int) -> None:
        """The next `count` calls fail as an outage would (breaker case), without sending."""
        self._failures_to_inject = count

    @property
    def calls_sent(self) -> int:
        return sum(1 for call in self.calls if call.sent)

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout: float,
        allow_tools: bool,
    ) -> Completion:
        request = jsonable(
            {
                "system": system,
                "tools": tools,
                "messages": messages,
                "allow_tools": allow_tools,
                "timeout": round(timeout, 3),
            }
        )
        serialised = json.dumps(request, ensure_ascii=False)
        chars = len(serialised)
        call = ModelCall(self.case, self.turn, request, chars, outcome="not_sent")
        self.calls.append(call)
        if self._failures_to_inject:
            self._failures_to_inject -= 1
            call.outcome, call.error = "injected", "injected_outage"
            raise ProviderError("injected_outage", counts=True)
        if self.aborted:
            call.outcome, call.error = "blocked", "run_aborted"
            raise ProviderError("evaluation_aborted", counts=False)
        if self.sends and self.limits is not None:
            refusal = self._over_limits(len(serialised.encode("utf-8")) + FRAMING_TOKENS)
            if refusal:
                self.aborted = refusal
                call.outcome, call.error = "blocked", refusal
                raise ProviderError("evaluation_budget", counts=False)
        started = time.perf_counter()
        try:
            completion = self.inner.complete(
                system=system,
                tools=tools,
                messages=messages,
                timeout=timeout,
                allow_tools=allow_tools,
            )
        except ProviderError as exc:
            call.latency_s = time.perf_counter() - started
            call.outcome = "not_sent" if exc.kind == "dry_run" else "error"
            call.error = exc.kind
            raise
        call.latency_s = time.perf_counter() - started
        call.outcome = "ok"
        call.stop_reason = completion.stop_reason
        call.text = completion.text
        call.tool_calls = [
            {"id": c.id, "name": c.name, "input": jsonable(c.input)} for c in completion.tool_calls
        ]
        call.model = completion.model
        call.provider_request_id = completion.provider_request_id
        call.usage = Usage(
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cache_read_tokens=completion.cache_read_tokens,
            cache_write_tokens=completion.cache_write_tokens,
        )
        cost = self.prices.cost(completion.model or self.model_name, call.usage)
        # A scripted model's zero usage costs nothing, whatever its (unknown) name.
        call.cost_usd, call.price_assumed = cost.usd, cost.assumed and cost.usd > 0
        self.spent_usd += cost.usd
        return completion

    def worst_case(self, input_tokens: int) -> float:
        """The most the next call can cost (see the module docstring)."""
        ceiling = self.prices.ceiling()
        max_output = int(settings.AI_CHAT_MAX_TOKENS)
        return (input_tokens * ceiling.input + max_output * ceiling.output) / 1_000_000

    def _over_limits(self, input_tokens: int) -> str:
        assert self.limits is not None  # noqa: S101 — checked by the caller
        if self.calls_sent >= self.limits.max_calls:
            return f"max_model_calls ({self.limits.max_calls}) reached"
        worst = self.worst_case(input_tokens)
        if self.spent_usd + worst > self.limits.max_cost_usd:
            return (
                f"max_cost_usd ({self.limits.max_cost_usd:.2f}) would be exceeded: spent "
                f"{self.spent_usd:.4f}, next call up to {worst:.4f}"
            )
        return ""

    # --- reading the record ----------------------------------------------------------------
    def calls_of(self, case: str, turn: int | None = None) -> list[ModelCall]:
        return [c for c in self.calls if c.case == case and (turn is None or c.turn == turn)]

    def tool_runs_of(self, case: str, turn: int | None = None) -> list[ToolRun]:
        return [r for r in self.tool_runs if r.case == case and (turn is None or r.turn == turn)]


@contextmanager
def capture_tool_runs(recorder: RecordingProvider) -> Iterator[None]:
    """Record every `tools.execute` (the module attribute the service calls) while active."""
    original = tools.execute

    def recording_execute(ctx: tools.ToolContext, name: str, arguments: Any) -> tools.ToolOutcome:
        outcome = original(ctx, name, arguments)
        recorder.tool_runs.append(
            ToolRun(
                recorder.case,
                recorder.turn,
                name,
                jsonable(arguments),
                outcome.is_error,
                outcome.content,
            )
        )
        return outcome

    tools.execute = recording_execute
    try:
        yield
    finally:
        tools.execute = original
