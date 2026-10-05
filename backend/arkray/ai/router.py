"""The deterministic question router: Ask Arkray's fast path (docs/rag-architecture.md).

Common structured questions ("What is my pipeline value?", "How many overdue tasks do I
have?", "What meetings do I have today?", "Which deals are in negotiation?") are answered
by running the matching tool and a fixed template: no language model, no cost, the same
answer every time, in milliseconds, and still available when the AI provider, the broker
or the AI workers are down.

Deliberately strict: a question matches an intent only if *every* word in it is either one
of the intent's words or generic filler ("what", "my", "do", "I", "have", ...). Anything
more specific ("...for Acme", "...last quarter", "...does Rahul have") is not routed here;
the language model (or, without one, retrieval) handles it. A wrong fast answer would be
worse than a slower right one.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arkray.core.access import AccessScope
from arkray.pipeline import selectors as pipeline_selectors

FILLER = frozenset(
    [
        "a",
        "all",
        "an",
        "any",
        "are",
        "at",
        "can",
        "could",
        "current",
        "currently",
        "did",
        "do",
        "does",
        "for",
        "get",
        "give",
        "have",
        "has",
        "how",
        "i",
        "is",
        "it",
        "just",
        "list",
        "many",
        "me",
        "much",
        "my",
        "now",
        "number",
        "of",
        "our",
        "please",
        "right",
        "see",
        "show",
        "tell",
        "the",
        "there",
        "total",
        "we",
        "what",
        "whats",
        "which",
        "would",
        "you",
        "your",
        "currently",
        "count",
    ]
)

_APOSTROPHES = str.maketrans({chr(0x2019): "'", chr(0x2018): "'"})
_NON_WORD = re.compile(r"[^\w\s']+", re.UNICODE)


def tokens(question: str) -> list[str]:
    text = question.translate(_APOSTROPHES).casefold()
    text = text.replace("what's", "whats").replace("today's", "today").replace("'s", "")
    return _NON_WORD.sub(" ", text).replace("'", " ").split()


@dataclass(frozen=True, slots=True)
class Route:
    intent: str
    calls: tuple[tuple[str, dict[str, Any]], ...]  # tool name and arguments, in order
    stage: str | None = None  # deals_in_stage: the stage name as configured
    pipeline: str | None = None  # pipeline_named: the pipeline's name as configured


@dataclass(frozen=True, slots=True)
class _Intent:
    name: str
    groups: tuple[frozenset[str], ...]  # each group: at least one word must appear
    extra: frozenset[str]  # other words this intent allows
    calls: tuple[tuple[str, dict[str, Any]], ...]


def _words(*groups: str) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(group.split()) for group in groups)


TASKS = "task tasks todo todos"
MEETINGS = "meeting meetings appointment appointments"
DEALS = "deal deals opportunity opportunities"
LEADS = "lead leads"

# Most specific first: the first intent whose words cover the question wins.
INTENTS: tuple[_Intent, ...] = (
    _Intent(
        "weighted_pipeline",
        _words("weighted", "pipeline"),
        frozenset(["value", "worth", "amount", "forecast", "open"]),
        (("get_pipeline_summary", {}),),
    ),
    _Intent(
        "pipeline_by_stage",
        _words(f"pipeline {DEALS}", "stage stages"),
        frozenset(["by", "per", "each", "in", "breakdown", "across"]),
        (("get_pipeline_summary", {}),),
    ),
    _Intent(
        # "How many open opportunities do I have?": answered from the pipeline summary (open
        # ones, which is what it counts and says). Without a model it went to note search
        # (whole-software audit).
        "open_deal_count",
        _words(DEALS, "how many number count total"),
        frozenset(["open", "active"]),
        (("get_pipeline_summary", {}),),
    ),
    _Intent(
        # "How many deals are in negotiation?": negotiation stages by their type (domain
        # data), whatever they are called (docs/pipeline.md#negotiation).
        "deals_in_negotiation",
        _words("negotiation negotiating negotiations"),
        frozenset([*DEALS.split(), "in", "at", "under", "stage", "stages", "open"]),
        (("list_opportunities", {"stage_type": "negotiation", "limit": 10}),),
    ),
    _Intent(
        "deals_closing_this_month",
        _words(DEALS, "closing close", "month"),
        frozenset(["this", "expected", "to", "due", "open", "in"]),
        (
            (
                "list_opportunities",
                {"closing": "this_month", "sort": "closing_soonest", "limit": 10},
            ),
        ),
    ),
    _Intent(
        "pipeline_value",
        _words("pipeline"),
        frozenset(["value", "worth", "amount", "size", "open"]),
        (("get_pipeline_summary", {}),),
    ),
    _Intent(
        "new_leads_today",
        _words("new", LEADS, "today"),
        frozenset(["got", "came", "created", "added", "in"]),
        (("get_lead_summary", {}), ("list_leads", {"created": "today", "limit": 10})),
    ),
    _Intent(
        "lead_count",
        # Only a count: "show my leads" or "active leads" want something else.
        _words(LEADS, "how many number count total"),
        frozenset(),
        (("get_lead_summary", {}),),
    ),
    _Intent(
        "overdue_tasks",
        _words("overdue late", TASKS),
        frozenset(["pending", "open"]),
        (("list_tasks", {"filter": "overdue", "limit": 10}),),
    ),
    _Intent(
        "tasks_due_today",
        _words(TASKS, "today"),
        frozenset(["due", "open", "pending", "for"]),
        (("list_tasks", {"filter": "due_today", "limit": 10}),),
    ),
    _Intent(
        "open_tasks",
        _words(TASKS),
        frozenset(["open", "pending", "outstanding"]),
        (("list_tasks", {"filter": "open", "limit": 10}),),
    ),
    _Intent(
        "meetings_today",
        _words(MEETINGS, "today"),
        frozenset(["scheduled", "for"]),
        (("list_meetings", {"range": "today", "limit": 10}),),
    ),
    _Intent(
        "meetings_tomorrow",
        _words(MEETINGS, "tomorrow"),
        frozenset(["scheduled", "for"]),
        (("list_meetings", {"range": "tomorrow", "limit": 10}),),
    ),
    _Intent(
        "upcoming_meetings",
        # "Upcoming" only: "next week" and "my next meeting" mean something narrower.
        _words(MEETINGS, "upcoming"),
        frozenset(["scheduled"]),
        (("list_meetings", {"range": "upcoming", "limit": 10}),),
    ),
)


def _covers(words: list[str], intent: _Intent) -> bool:
    allowed = FILLER | intent.extra | frozenset().union(*intent.groups)
    return all(word in allowed for word in words) and all(
        group & set(words) for group in intent.groups
    )


def _stage_route(words: list[str], stage_names: list[str]) -> Route | None:
    """ "Which deals are in negotiation?": a configured stage's name, deal words and filler."""
    if not set(words) & set(DEALS.split()):
        return None
    for name in sorted(stage_names, key=len, reverse=True):
        stage_words = tokens(name)
        if not stage_words:
            continue
        for at in range(len(words) - len(stage_words) + 1):
            if words[at : at + len(stage_words)] == stage_words:
                rest = words[:at] + words[at + len(stage_words) :]
                allowed = FILLER | frozenset(DEALS.split()) | frozenset(["in", "at", "stage"])
                # "in Negotiation", "at the Won stage": the stage named as a place, so "new
                # deals" (recent ones) is not read as the stage "New".
                placed = bool({"in", "at", "stage"} & set(rest))
                if placed and all(word in allowed for word in rest):
                    return Route(
                        "deals_in_stage",
                        (("list_opportunities", {"stage": name, "limit": 10}),),
                        stage=name,
                    )
    return None


PIPELINE_VALUE_WORDS = frozenset(["pipeline", "value", "worth", "amount", "size", "open"])


def _pipeline_route(words: list[str], pipeline_names: list[str]) -> Route | None:
    """ "What is the value of my Government Tender pipeline?": a pipeline's name, value words
    and filler."""
    if "pipeline" not in words:
        return None
    for name in sorted(pipeline_names, key=len, reverse=True):
        name_words = [w for w in tokens(name) if w != "pipeline"]
        if not name_words:
            continue
        for at in range(len(words) - len(name_words) + 1):
            if words[at : at + len(name_words)] == name_words:
                rest = words[:at] + words[at + len(name_words) :]
                if all(word in FILLER | PIPELINE_VALUE_WORDS for word in rest):
                    return Route(
                        "pipeline_named",
                        (("get_pipeline_summary", {"pipeline": name}),),
                        pipeline=name,
                    )
    return None


def route(
    question: str,
    *,
    stage_names: Callable[[], list[str]] | None = None,
    pipeline_names: Callable[[], list[str]] | None = None,
) -> Route | None:
    """`stage_names` and `pipeline_names` give the names the asker's workspace may see (its
    pipelines): another user's personal pipeline never shapes the routing."""
    words = tokens(question)
    if not words or len(words) > 14:
        return None
    for intent in INTENTS:
        if _covers(words, intent):
            return Route(intent.name, intent.calls)
    named = _pipeline_route(words, pipeline_names() if pipeline_names is not None else [])
    if named is not None:
        return named
    return _stage_route(words, stage_names() if stage_names is not None else [])


def active_stage_names(scope: AccessScope) -> list[str]:
    return sorted(
        {
            stage.name
            for pipeline in pipeline_selectors.visible_pipelines(scope)
            for stage in pipeline.stages.all()
            if stage.is_active
        }
    )


def pipeline_names(scope: AccessScope) -> list[str]:
    """Names that identify one visible pipeline. A name several share (names are unique per
    owner only) isn't routed: the tool asks which one is meant."""
    visible = pipeline_selectors.visible_pipelines(scope)
    counts = Counter(pipeline.name.casefold() for pipeline in visible)
    return sorted(p.name for p in visible if counts[p.name.casefold()] == 1)
