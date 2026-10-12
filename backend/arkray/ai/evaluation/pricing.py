"""What a model call costs (docs/ai-evaluation.md#cost-and-duration).

Prices are USD per million tokens, Anthropic first-party API rates, from the claude-api
skill's model table (cached 2026-10-06) and its prompt-caching economics: a cache write with
the default 5-minute lifetime costs 1.25x the input price; a cache read is a fraction of it
that differs by model ($0.20 on Claude Opus 5.5 and Claude Sonnet 5.5, $0.25 on Claude Fable
5.1; about 0.1x elsewhere). Prices change: confirm them on the pricing page
(https://platform.claude.com/docs/en/about-claude/pricing) before a run whose cost matters,
and pass `--pricing-file` (JSON: {"model": {"input": .., "output": .., "cache_write": ..,
"cache_read": ..}}) to override any of them.

A model the table doesn't know (a server-side fallback answered, say) is priced at the most
expensive model in the table, and the call is flagged: the estimate errs high, never low.

Usage fields (Messages API): `input_tokens` is the uncached remainder only; the prompt is
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`; `output_tokens`
includes thinking (adaptive thinking on Opus 5.5 is billed as output).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRICES_AS_OF = "2026-10-06"
PRICES_SOURCE = (
    "claude-api skill model table (Anthropic first-party API rates); "
    "https://platform.claude.com/docs/en/about-claude/pricing"
)
MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float  # 5-minute cache lifetime: 1.25x input
    cache_read: float

    def as_dict(self) -> dict[str, float]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
        }


PRICES: dict[str, Price] = {
    # The default model (AI_CHAT_MODEL): $4 / $20, cache reads $0.20 (0.05x).
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
    # Haiku 5.5's rates are for prompts up to 100K tokens (every Ask Arkray prompt).
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
    "claude-fable-5-1": Price(input=10.00, output=50.00, cache_write=12.50, cache_read=0.25),
    # Previous generation, cache reads at the general 0.1x.
    "claude-opus-5": Price(input=5.00, output=25.00, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-8": Price(input=5.00, output=25.00, cache_write=6.25, cache_read=0.50),
}


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Cost:
    usd: float
    assumed: bool  # the model wasn't in the table: priced at the most expensive one


class PriceTable:
    def __init__(self, prices: dict[str, Price] | None = None) -> None:
        self.prices = dict(PRICES if prices is None else prices)
        if not self.prices:
            raise ValueError("A price table needs at least one model.")

    @classmethod
    def from_file(cls, path: Path | None) -> PriceTable:
        """The built-in table, with the models of `path` (JSON) added or replaced."""
        prices = dict(PRICES)
        if path is not None:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("A pricing file is a JSON object of model -> prices.")
            for model, entry in raw.items():
                prices[str(model)] = _price(str(model), entry)
        return cls(prices)

    def ceiling(self) -> Price:
        """The most expensive model's prices (for models the table doesn't know)."""
        return max(self.prices.values(), key=lambda p: (p.output, p.input))

    def price(self, model: str) -> tuple[Price, bool]:
        known = self.prices.get(model)
        return (known, False) if known is not None else (self.ceiling(), True)

    def cost(self, model: str, usage: Usage) -> Cost:
        price, assumed = self.price(model)
        usd = (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * price.cache_write
        ) / MILLION
        return Cost(round(usd, 6), assumed)

    def worst_case(self, model: str, *, input_tokens: int, max_output_tokens: int) -> float:
        """An upper bound on one call before it is sent: every input token uncached and the
        whole output budget used."""
        price, _ = self.price(model)
        return (input_tokens * price.input + max_output_tokens * price.output) / MILLION


def _price(model: str, entry: Any) -> Price:
    if not isinstance(entry, dict):
        raise ValueError(f"Prices for {model} must be an object.")
    values: dict[str, float] = {}
    for key in ("input", "output", "cache_write", "cache_read"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ValueError(f"{model}: {key} must be a non-negative number (USD per MTok).")
        values[key] = float(value)
    return Price(**values)


# Characters per token for estimates made before anything is sent (dry runs, the spend cap):
# JSON with UUIDs, rupee amounts and Indian names tokenises worse than English prose, so this
# errs towards more tokens. The real run uses the provider's own usage figures.
CHARS_PER_TOKEN = 3.0
# Tokens the API adds for tool use and message framing beyond the visible characters.
REQUEST_OVERHEAD_TOKENS = 600


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN) + REQUEST_OVERHEAD_TOKENS
