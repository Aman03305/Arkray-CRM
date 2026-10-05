"""Ask Arkray's tools: the only way a language model reaches CRM data (ADR-0008).

- **Allowlist.** The model may call exactly the tools `definitions(scope)` lists. Any other
  name is an error result; there is no SQL, ORM, shell, file or HTTP tool.
- **Scope-bound.** Every tool runs with the request's AccessScope as a Python argument and
  reads through the owning module's scoped selectors. No tool has an owner, user or
  workspace parameter. Organisation-wide tools (`team_breakdown`) are *absent* from every
  other scope's list, not merely refused.
- **Untrusted arguments.** Strict schemas constrain the model, and every argument is
  validated again here (types, enums, bounds, unknown keys). A record reference outside the
  scope is "not found", exactly like an id that doesn't exist.
- **Read-only and bounded.** A fixed handful of bounded queries per call (lists at most
  MAX_LIMIT rows, text clipped), never a write.
- **Facts come from here.** Counts, amounts and dates are computed by the CRM modules
  (pipeline.metrics for money, Decimal end to end) and formatted by the server
  (ai.formatting); the model only repeats them. Records returned carry a `ref`
  ("lead:<uuid>") that an answer may cite; only refs registered here become links.
- **User-written text is data.** Descriptions, notes and retrieved passages are returned
  under `untrusted_text`, which the system prompt tells the model never to follow.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings

from arkray.activities import selectors as activity_selectors
from arkray.activities.models import Activity, ActivityStatus, ActivityType
from arkray.activities.selectors import ActivityFilters
from arkray.core.access import AccessScope
from arkray.core.business_time import business_date
from arkray.core.errors import NotFoundError
from arkray.core.ranking import SearchQuery
from arkray.identity import selectors as identity_selectors
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from arkray.leads.selectors import LeadFilters
from arkray.pipeline import instruments
from arkray.pipeline import selectors as pipeline_selectors
from arkray.pipeline.models import Opportunity, Pipeline, Stage
from arkray.pipeline.selectors import OpportunityFilters
from arkray.search import selectors as search_selectors

from . import formatting as fmt
from . import retrieval

logger = logging.getLogger(__name__)

MAX_LIMIT = 20
DEFAULT_LIMIT = 10
TEXT_LIMIT = 1500  # characters of a description or note in a record's details
PREVIEW_LIMIT = 240
RECENT_ACTIVITY_LIMIT = 8
STAGE_HISTORY_LIMIT = 5
REF_KINDS = ("lead", "opportunity", "task", "meeting", "note")


class ToolError(Exception):
    """A tool call that can't be answered as asked; the message goes back to the model."""


# --- what the tools hand to the answer -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RecordRef:
    kind: str
    id: UUID
    label: str
    detail: str = ""

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True, slots=True)
class Fact:
    label: str
    value: str  # display form, formatted by the server
    kind: str  # "count" | "money"
    raw: str  # exact value: an integer or a decimal string


@dataclass(frozen=True, slots=True)
class Citation:
    ref: str
    kind: str
    label: str
    snippet: str
    when: str
    source_hash: str = ""  # the record's content when quoted (stored, never serialised)


@dataclass
class ToolContext:
    scope: AccessScope
    now: datetime
    records: dict[str, RecordRef] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    retrieval_unavailable: bool = False
    retrieval_dropped: int = 0
    # Per-question budgets (settings): retrieved passage text, and all tool output together.
    retrieved_chars: int = 0
    result_chars: int = 0

    def cite(self, kind: str, record_id: UUID, label: str, detail: str = "") -> str:
        record = RecordRef(kind, record_id, label or kind.capitalize(), detail)
        self.records.setdefault(record.ref, record)
        return record.ref

    def count(self, label: str, value: int) -> None:
        self.facts.append(Fact(label, f"{value:,}", "count", str(value)))

    def amount(self, label: str, value: Decimal) -> None:
        shown = fmt.money(value)
        self.facts.append(Fact(label, shown["display"], "money", shown["amount"]))


# --- argument validation ---------------------------------------------------------------------
def _check_keys(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolError(f"Unknown argument(s): {', '.join(sorted(unknown))}.")


def _choice(
    args: dict[str, Any], name: str, allowed: tuple[str, ...], default: str | None
) -> str | None:
    value = args.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ToolError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _instrument(args: dict[str, Any]) -> str | None:
    """One of the instruments (pipeline.instruments), in the list's spelling whatever the
    letter case or spacing it was asked in."""
    value = args.get("instrument")
    if value is None:
        return None
    known = instruments.canonical(value) if isinstance(value, str) else None
    if known is None:
        raise ToolError(f"instrument must be one of: {', '.join(instruments.INSTRUMENTS)}.")
    return known


def _limit(args: dict[str, Any]) -> int:
    value = args.get("limit", DEFAULT_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise ToolError(f"limit must be a whole number from 1 to {MAX_LIMIT}.")
    return int(value)


def _text(args: dict[str, Any], name: str, *, required: bool, max_length: int = 200) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise ToolError(f"{name} is required.")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ToolError(f"{name} must be text of 1 to {max_length} characters.")
    return value.strip()


def _ref(value: str, kinds: tuple[str, ...] = REF_KINDS) -> tuple[str, UUID]:
    kind, _, raw_id = value.partition(":")
    if kind not in kinds:
        raise ToolError(f"A reference looks like kind:id, kind one of {', '.join(kinds)}.")
    try:
        return kind, UUID(raw_id)
    except ValueError:
        raise ToolError("That reference's id is not valid.") from None


# --- shared row shapes -------------------------------------------------------------------------
def _person(ctx: ToolContext, user: Any) -> str | None:
    """Owner names only where they tell the user something: organisation-wide answers."""
    return user.full_name if ctx.scope.is_organization_wide and user is not None else None


def _lead_link(ctx: ToolContext, lead: Lead | None) -> dict[str, str] | None:
    """A related lead, only if it is visible in this scope (the search serializers' rule)."""
    if lead is None or not ctx.scope.permits_owner(lead.owner_id):
        return None
    return {
        "ref": ctx.cite("lead", lead.pk, lead.display_name),
        "name": fmt.user_text(lead.display_name),
    }


def _pipelines(ctx: ToolContext) -> list[Pipeline]:
    """The pipelines this workspace may see (docs/pipeline.md#pipelines): active ones, then
    archived ones. Never another user's personal pipeline that holds none of its deals."""
    return [
        *pipeline_selectors.visible_pipelines(ctx.scope),
        *pipeline_selectors.visible_pipelines(ctx.scope, archived=True),
    ]


def _stage_names(ctx: ToolContext) -> dict[UUID, Stage]:
    return {stage.pk: stage for pipeline in _pipelines(ctx) for stage in pipeline.stages.all()}


def _named_pipeline(ctx: ToolContext, name: str) -> Pipeline:
    pipelines = _pipelines(ctx)
    matched = [p for p in pipelines if p.name.casefold() == name.casefold()]
    if not matched:
        names = ", ".join(sorted({p.name for p in pipelines if p.is_active}))
        raise ToolError(f"No pipeline is called {name!r}. Pipelines: {names}.")
    if len(matched) > 1:
        # Names are unique per owner only: never answer for an arbitrary one of them
        # (enhancement review: a ₹0 shared pipeline answered for the user's own).
        owners = ", ".join(
            "shared" if p.owner is None else f"{p.owner.full_name}'s" for p in matched
        )
        raise ToolError(
            f"Several pipelines are called {name!r} ({owners}). Ask in the workspace of the"
            " one you mean, or about all pipelines."
        )
    return matched[0]


def _opportunity_row(
    ctx: ToolContext, found: Opportunity, stages: dict[UUID, Stage]
) -> dict[str, Any]:
    stage = stages.get(found.stage_id)
    row: dict[str, Any] = {
        "ref": ctx.cite("opportunity", found.pk, found.title, stage.name if stage else ""),
        "title": fmt.label(found.title),
        "stage": stage.name if stage else None,
        "status": found.status,
        "value": fmt.money(found.value),
        "probability": fmt.percent(found.probability),
        # Computed by PostgreSQL in the list query (pipeline.metrics.WEIGHTED_VALUE).
        "weighted_value": fmt.money(found.weighted_value),  # type: ignore[attr-defined]
        "expected_close": fmt.day(found.expected_close_date),
        "lead": _lead_link(ctx, found.lead),
    }
    if found.account_name:
        row["account"] = fmt.label(found.account_name)
    if found.customer_name and found.customer_name != found.account_name:
        row["customer"] = fmt.label(found.customer_name)
    if found.instrument_name:
        row["instrument"] = fmt.label(found.instrument_name)
    if found.negotiated_price is not None:
        # The latest recorded negotiated price (authoritative, from the price history).
        row["negotiated_price"] = fmt.money(found.negotiated_price)
    if found.closed_at is not None:
        row["closed"] = fmt.day(found.closed_at)
    owner = _person(ctx, found.owner)
    if owner:
        row["owner"] = owner
    return row


def _activity_row(ctx: ToolContext, activity: Activity) -> dict[str, Any]:
    kind = str(activity.type)
    label = activity.title or kind.capitalize()
    row: dict[str, Any] = {
        "ref": ctx.cite(kind, activity.pk, label),
        "type": kind,
        "title": fmt.label(activity.title or None),
        "status": activity.status,
        "lead": _lead_link(ctx, activity.lead),
    }
    if activity.type == ActivityType.TASK:
        row["priority"] = activity.priority
        row["due"] = fmt.when(activity.due_at) if activity.due_at else None
        row["overdue"] = bool(
            activity.status == ActivityStatus.OPEN and activity.due_at and activity.due_at < ctx.now
        )
    elif activity.type == ActivityType.MEETING:
        row["starts"] = fmt.when(activity.starts_at)
        row["ends"] = fmt.when(activity.ends_at)
    else:
        row["written"] = fmt.when(activity.created_at)
    preview = getattr(activity, "text_preview", None)
    if preview:
        row["untrusted_text"] = fmt.clip(preview, PREVIEW_LIMIT)
    owner = _person(ctx, activity.owner)
    if owner:
        row["owner"] = owner
    return row


def _lead_row(ctx: ToolContext, lead: Lead) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ref": ctx.cite("lead", lead.pk, lead.display_name, lead.organization_name),
        "name": fmt.label(lead.display_name),
        "organisation": fmt.label(lead.organization_name or None),
        "job_title": fmt.label(lead.job_title or None),
        "status": lead.status.name,
        "source": lead.source.name if lead.source is not None else None,
        "rating": lead.rating,
        "created": fmt.day(lead.created_at),
        "last_contacted": fmt.when(lead.last_contacted_at) if lead.last_contacted_at else "never",
    }
    owner = _person(ctx, lead.owner)
    if owner:
        row["owner"] = owner
    return row


# --- the tools -----------------------------------------------------------------------------------
def get_pipeline_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"pipeline"})
    name = _text(args, "pipeline", required=False, max_length=100)
    chosen = _named_pipeline(ctx, name) if name is not None else None
    totals = pipeline_selectors.pipeline_totals(
        ctx.scope, OpportunityFilters(pipeline_id=chosen.pk if chosen else None)
    )
    ctx.amount("Pipeline value", totals.pipeline_value)
    ctx.amount("Weighted pipeline", totals.weighted_pipeline)
    ctx.count("Open opportunities", totals.open_count)
    pipelines = []
    for pipeline in [chosen] if chosen is not None else _pipelines(ctx):
        breakdown = pipeline_selectors.stage_breakdown(ctx.scope, pipeline)
        # A retired pipeline still holding opportunities is listed too: the totals count
        # them, so the breakdown must add up to them (whole-software audit).
        if not pipeline.is_active and not any(row.count for row in breakdown):
            continue
        pipelines.append(
            {
                "pipeline": pipeline.name if pipeline.is_active else f"{pipeline.name} (retired)",
                "stages": [
                    {
                        "stage": row.stage.name,
                        "category": row.stage.category,
                        "type": row.stage.stage_type,
                        "opportunities": row.count,
                        "value": fmt.money(row.total_value),
                        "weighted_value": fmt.money(row.weighted_value),
                    }
                    for row in breakdown
                ],
            }
        )
    return {
        "pipeline": chosen.name if chosen else "all pipelines",
        "pipeline_value": fmt.money(totals.pipeline_value),
        "weighted_pipeline": fmt.money(totals.weighted_pipeline),
        "open_opportunities": totals.open_count,
        "definitions": (
            "Pipeline value is the sum of the values of open, non-archived opportunities. "
            "Weighted pipeline is the sum of value x probability over the same opportunities. "
            "Won and lost opportunities never count."
        ),
        "by_stage": pipelines,
    }


_OPPORTUNITY_SORTS = {
    "value_desc": ("-value", "-id"),
    "closing_soonest": ("expected_close_sort", "created_at", "id"),
    "newest": ("-created_at", "-id"),
    "recently_closed": ("-closed_sort", "-id"),
}
_CLOSING = ("this_month", "next_month", "this_quarter", "past_due")


def _month_bounds(day: date, months_ahead: int) -> tuple[date, date]:
    month_index = day.year * 12 + day.month - 1 + months_ahead
    first = date(month_index // 12, month_index % 12 + 1, 1)
    following = date((month_index + 1) // 12, (month_index + 1) % 12 + 1, 1)
    return first, following - timedelta(days=1)


def list_opportunities(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(
        args,
        {"status", "stage", "stage_type", "pipeline", "closing", "instrument", "sort", "limit"},
    )
    status = _choice(args, "status", ("open", "won", "lost"), None)
    instrument = _instrument(args)
    closing = _choice(args, "closing", _CLOSING, None)
    sort = _choice(args, "sort", tuple(_OPPORTUNITY_SORTS), "value_desc") or "value_desc"
    limit = _limit(args)
    stage_name = _text(args, "stage", required=False, max_length=50)
    stage_type = _choice(args, "stage_type", ("open", "negotiation", "won", "lost"), None)
    pipeline_name = _text(args, "pipeline", required=False, max_length=100)
    stages = _stage_names(ctx)
    if closing is not None and status is None:
        status = "open"  # an expected close date only matters for open deals
    filters = OpportunityFilters(status=status)
    today = business_date(ctx.now)
    if closing == "past_due":
        filters = OpportunityFilters(status="open", expected_close_to=today - timedelta(days=1))
    elif closing is not None:
        first, last = (
            _month_bounds(today, 1) if closing == "next_month" else _month_bounds(today, 0)
        )
        if closing == "this_quarter":
            quarter_start = (today.month - 1) // 3 * 3 + 1
            first = date(today.year, quarter_start, 1)
            last = _month_bounds(first, 2)[1]
        filters = OpportunityFilters(
            status=status, expected_close_from=first, expected_close_to=last
        )
    queryset = pipeline_selectors.opportunity_list(ctx.scope, filters)
    if instrument is not None:
        # The structured field (one of the instruments), never a text match on the title;
        # letter case aside, as an opportunity from before the list may spell it.
        queryset = queryset.filter(instrument_name__iexact=instrument)
    if pipeline_name is not None:
        queryset = queryset.filter(pipeline_id=_named_pipeline(ctx, pipeline_name).pk)
    if stage_type is not None:
        # The stage's meaning, never its name: "Negotiation" renamed "Commercial discussion"
        # is still a negotiation stage (docs/pipeline.md#negotiation).
        typed = [s.pk for s in stages.values() if s.stage_type == stage_type]
        queryset = queryset.filter(stage_id__in=typed)
        if status is None:
            queryset = queryset.filter(status="open" if stage_type == "negotiation" else stage_type)
    if stage_name is not None:
        matched = [s for s in stages.values() if s.name.casefold() == stage_name.casefold()]
        if not matched:
            names = sorted({s.name for s in stages.values() if s.is_active})
            raise ToolError(f"No stage is called {stage_name!r}. Stages: {', '.join(names)}.")
        queryset = queryset.filter(stage_id__in=[s.pk for s in matched])
        categories = {s.category for s in matched}
        if status is None and len(categories) == 1:
            queryset = queryset.filter(status=categories.pop())
    total = queryset.count()
    rows = list(queryset.order_by(*_OPPORTUNITY_SORTS[sort])[:limit])
    return {
        "total_matching": total,
        "shown": len(rows),
        "opportunities": [_opportunity_row(ctx, row, stages) for row in rows],
    }


def get_lead_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, set())
    summary = lead_selectors.lead_summary(ctx.scope, now=ctx.now)
    ctx.count("Total leads", summary.total)
    ctx.count("New leads today", summary.new_today)
    return {
        "total_leads": summary.total,
        "new_leads_today": summary.new_today,
        "today": fmt.day(business_date(ctx.now)),
        "by_status": [
            {"status": row.name, "category": row.category, "leads": row.count}
            for row in lead_selectors.status_breakdown(ctx.scope)
        ],
        "definitions": "Archived leads are never counted. Today is the business day.",
    }


_LEAD_SORTS = {
    "newest": ("-created_at", "-id"),
    "oldest": ("created_at", "id"),
    "longest_without_contact": ("last_contacted_sort", "id"),
    "recently_contacted": ("-last_contacted_sort", "-id"),
    "name": ("display_name", "id"),
}
_CREATED = ("today", "yesterday", "last_7_days", "this_month", "last_30_days")


def list_leads(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"status", "created", "sort", "limit"})
    created = _choice(args, "created", _CREATED, None)
    sort = _choice(args, "sort", tuple(_LEAD_SORTS), "newest") or "newest"
    limit = _limit(args)
    status_text = _text(args, "status", required=False, max_length=50)
    status_key = None
    if status_text is not None:
        statuses = lead_selectors.statuses()
        match = [
            s for s in statuses if status_text.casefold() in (s.key.casefold(), s.name.casefold())
        ]
        if not match:
            raise ToolError(
                f"No lead status is called {status_text!r}. Statuses: "
                f"{', '.join(s.name for s in statuses)}."
            )
        status_key = match[0].key
    today = business_date(ctx.now)
    created_from = created_to = None
    if created == "today":
        created_from = created_to = today
    elif created == "yesterday":
        created_from = created_to = today - timedelta(days=1)
    elif created == "last_7_days":
        created_from, created_to = today - timedelta(days=6), today
    elif created == "this_month":
        created_from, created_to = today.replace(day=1), today
    elif created == "last_30_days":
        created_from, created_to = today - timedelta(days=29), today
    queryset = lead_selectors.lead_list(
        ctx.scope, LeadFilters(status=status_key, created_from=created_from, created_to=created_to)
    )
    total = queryset.count()
    rows = list(queryset.order_by(*_LEAD_SORTS[sort])[:limit])
    return {
        "total_matching": total,
        "shown": len(rows),
        "leads": [_lead_row(ctx, lead) for lead in rows],
    }


def get_activity_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, set())
    summary = activity_selectors.activity_summary(ctx.scope, now=ctx.now)
    ctx.count("Open tasks", summary.open_tasks)
    ctx.count("Overdue tasks", summary.overdue_tasks)
    ctx.count("Tasks due today", summary.tasks_due_today)
    ctx.count("Meetings today", summary.meetings_today)
    ctx.count("Upcoming meetings", summary.upcoming_meetings)
    return {
        "open_tasks": summary.open_tasks,
        "overdue_tasks": summary.overdue_tasks,
        "tasks_due_today": summary.tasks_due_today,
        "meetings_today": summary.meetings_today,
        "upcoming_meetings": summary.upcoming_meetings,
        "today": fmt.day(business_date(ctx.now)),
        "definitions": (
            "Overdue: open tasks due before now. Meetings today: scheduled or completed "
            "meetings starting today. Upcoming: scheduled meetings from now on."
        ),
    }


_TASK_FILTERS = ("overdue", "due_today", "open", "upcoming")


def list_tasks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"filter", "limit"})
    which = _choice(args, "filter", _TASK_FILTERS, None)
    if which is None:
        raise ToolError(f"filter is required: one of {', '.join(_TASK_FILTERS)}.")
    limit = _limit(args)
    today = business_date(ctx.now)
    task, open_ = ActivityType.TASK, ActivityStatus.OPEN
    filters = {
        "overdue": ActivityFilters(overdue=True),
        "due_today": ActivityFilters(type=task, status=open_, date_from=today, date_to=today),
        "open": ActivityFilters(type=task, status=open_),
        "upcoming": ActivityFilters(type=task, status=open_, upcoming=True),
    }[which]
    queryset = activity_selectors.activity_list(ctx.scope, filters, now=ctx.now)
    total = queryset.count()
    rows = list(queryset.order_by("schedule_sort", "id")[:limit])
    return {
        "filter": which,
        "total_matching": total,
        "shown": len(rows),
        "tasks": [_activity_row(ctx, task) for task in rows],
    }


_MEETING_RANGES = (
    "today",
    "tomorrow",
    "this_week",
    "next_7_days",
    "past_7_days",
    "upcoming",
)


def list_meetings(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"range", "limit"})
    which = _choice(args, "range", _MEETING_RANGES, None)
    if which is None:
        raise ToolError(f"range is required: one of {', '.join(_MEETING_RANGES)}.")
    limit = _limit(args)
    today = business_date(ctx.now)
    if which == "upcoming":
        # The dashboard's "upcoming meetings": scheduled, starting from now on (not this
        # morning's, held or missed), here within the next 7 days (whole-software audit).
        first, last = today, today + timedelta(days=6)
        filters = ActivityFilters(type=ActivityType.MEETING, upcoming=True, date_to=last)
        return _meetings(ctx, which, first, last, filters, limit)
    first, last = {
        "today": (today, today),
        "tomorrow": (today + timedelta(days=1), today + timedelta(days=1)),
        "this_week": (
            today - timedelta(days=today.weekday()),
            today + timedelta(days=6 - today.weekday()),
        ),
        "next_7_days": (today, today + timedelta(days=6)),
        "past_7_days": (today - timedelta(days=7), today - timedelta(days=1)),
    }[which]
    filters = ActivityFilters(
        type=ActivityType.MEETING, cancelled=False, date_from=first, date_to=last
    )
    return _meetings(ctx, which, first, last, filters, limit)


def _meetings(
    ctx: ToolContext, which: str, first: date, last: date, filters: ActivityFilters, limit: int
) -> dict[str, Any]:
    queryset = activity_selectors.activity_list(ctx.scope, filters, now=ctx.now)
    total = queryset.count()
    order = ("-schedule_sort", "-id") if which == "past_7_days" else ("schedule_sort", "id")
    rows = list(queryset.order_by(*order)[:limit])
    return {
        "range": which,
        "from": fmt.day(first),
        "to": fmt.day(last),
        "total_matching": total,
        "shown": len(rows),
        "meetings": [_activity_row(ctx, meeting) for meeting in rows],
    }


def find_records(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"query"})
    text = _text(args, "query", required=True, max_length=100) or ""
    try:
        query = SearchQuery.parse(text)
    except ValueError as exc:
        raise ToolError(f"Search words need 3 letters or digits in a row ({exc}).") from None
    found = search_selectors.global_search(ctx.scope, query)
    stages = _stage_names(ctx)
    groups = (found.leads, found.opportunities, found.tasks, found.meetings, found.notes)
    return {
        "query": query.text,
        "shown": dict(
            zip(
                ("leads", "opportunities", "tasks", "meetings", "notes"),
                (len(group.items) for group in groups),
                strict=True,
            )
        ),
        "leads": [
            {
                "ref": ctx.cite("lead", lead.pk, lead.display_name, lead.organization_name),
                "name": fmt.label(lead.display_name),
                "organisation": fmt.label(lead.organization_name or None),
                "status": lead.status.name,
            }
            for lead in found.leads.items
        ],
        "opportunities": [
            {
                "ref": ctx.cite(
                    "opportunity",
                    o.pk,
                    o.title,
                    stages[o.stage_id].name if o.stage_id in stages else "",
                ),
                "title": fmt.label(o.title),
                "status": o.status,
                "stage": stages[o.stage_id].name if o.stage_id in stages else None,
                "lead": _lead_link(ctx, o.lead),
            }
            for o in found.opportunities.items
        ],
        "tasks": [_activity_row(ctx, a) for a in found.tasks.items],
        "meetings": [_activity_row(ctx, a) for a in found.meetings.items],
        "notes": [_activity_row(ctx, a) for a in found.notes.items],
        "more_available": any(
            group.has_more
            for group in (
                found.leads,
                found.opportunities,
                found.tasks,
                found.meetings,
                found.notes,
            )
        ),
    }


def get_record(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"ref"})
    kind, record_id = _ref(_text(args, "ref", required=True, max_length=60) or "")
    try:
        if kind == "lead":
            return _lead_details(ctx, record_id)
        if kind == "opportunity":
            return _opportunity_details(ctx, record_id)
        return _activity_details(ctx, kind, record_id)
    except NotFoundError:
        raise ToolError("No such record in this workspace.") from None


def _recent_activities(ctx: ToolContext, filters: ActivityFilters) -> list[dict[str, Any]]:
    rows = activity_selectors.activity_list(ctx.scope, filters, now=ctx.now).order_by(
        "-created_at", "-id"
    )[:RECENT_ACTIVITY_LIMIT]
    return [_activity_row(ctx, row) for row in rows]


def _lead_details(ctx: ToolContext, lead_id: UUID) -> dict[str, Any]:
    lead = lead_selectors.lead_detail(ctx.scope, lead_id)
    stages = _stage_names(ctx)
    opportunities = pipeline_selectors.opportunity_list(
        ctx.scope, OpportunityFilters(lead_id=lead_id)
    ).order_by("-created_at", "-id")[:RECENT_ACTIVITY_LIMIT]
    row = _lead_row(ctx, lead)
    row["untrusted_text"] = fmt.clip(lead.description, TEXT_LIMIT) if lead.description else None
    row["archived"] = lead.archived_at is not None
    row["opportunities"] = [_opportunity_row(ctx, o, stages) for o in opportunities]
    row["recent_activities"] = _recent_activities(ctx, ActivityFilters(lead_id=lead_id))
    return row


def _opportunity_details(ctx: ToolContext, opportunity_id: UUID) -> dict[str, Any]:
    found = pipeline_selectors.opportunity_detail(ctx.scope, opportunity_id)
    # The list query computes the weighted value in PostgreSQL (pipeline.metrics); the same
    # scope, so it finds exactly the opportunity just found (archived ones included).
    card = (
        pipeline_selectors.opportunity_list(
            ctx.scope, OpportunityFilters(archived=found.archived_at is not None)
        )
        .filter(pk=opportunity_id)
        .get()
    )
    stages = _stage_names(ctx)
    row = _opportunity_row(ctx, card, stages)
    row["archived"] = found.archived_at is not None
    row["untrusted_text"] = fmt.clip(found.description, TEXT_LIMIT) if found.description else None
    if found.lost_reason:
        row["lost_reason"] = {"untrusted_text": fmt.clip(found.lost_reason, TEXT_LIMIT)}
    history = pipeline_selectors.stage_history(ctx.scope, opportunity_id)[:STAGE_HISTORY_LIMIT]
    row["stage_history"] = [
        {"from": h.from_stage_name or None, "to": h.to_stage_name, "on": fmt.when(h.occurred_at)}
        for h in history
    ]
    row["opportunity_date"] = fmt.day(found.opportunity_date)
    if found.work_load:
        row["work_load"] = fmt.label(found.work_load)
    if found.expected_cpt:
        # Free text as the salesperson wrote it: the CRM defines no unit for CPT.
        row["expected_cpt"] = fmt.label(found.expected_cpt)
    row["negotiation_history"] = _negotiation_rows(ctx, opportunity_id)
    row["recent_activities"] = _recent_activities(
        ctx, ActivityFilters(opportunity_id=opportunity_id)
    )
    return row


NEGOTIATION_HISTORY_LIMIT = 20


def _negotiation_rows(ctx: ToolContext, opportunity_id: UUID) -> list[dict[str, Any]]:
    prices = pipeline_selectors.negotiation_history(ctx.scope, opportunity_id).order_by(
        "-occurred_at", "-id"
    )[:NEGOTIATION_HISTORY_LIMIT]
    return [
        {
            "price": fmt.money(price.price),
            "stage": fmt.label(price.stage_name),
            "recorded": fmt.when(price.occurred_at),
            # Whoever recorded it, by name, unless it was the person asking: an
            # administrator's price in a user's workspace is never "you" for the user, nor
            # the user's for the administrator (enhancement review).
            "by": "you" if price.actor_id == ctx.scope.actor_id else price.actor.full_name,
        }
        for price in prices
    ]


def get_negotiation_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The negotiated prices of one opportunity, newest first: authoritative amounts from
    the append-only history, never computed."""
    _check_keys(args, {"ref"})
    _, record_id = _ref(_text(args, "ref", required=True, max_length=60) or "", ("opportunity",))
    try:
        found = pipeline_selectors.opportunity_detail(ctx.scope, record_id)
        rows = _negotiation_rows(ctx, record_id)
    except NotFoundError:
        raise ToolError("No such opportunity in this workspace.") from None
    if found.negotiated_price is not None:
        ctx.amount("Latest negotiated price", found.negotiated_price)
    return {
        "ref": ctx.cite("opportunity", found.pk, found.title),
        "latest": fmt.money(found.negotiated_price) if found.negotiated_price is not None else None,
        "history": rows,
        "shown": len(rows),
    }


def _activity_details(ctx: ToolContext, kind: str, activity_id: UUID) -> dict[str, Any]:
    activity = activity_selectors.activity_detail(ctx.scope, activity_id)
    if activity.type != kind:
        raise NotFoundError()
    row = _activity_row(ctx, activity)
    row["untrusted_text"] = (
        fmt.clip(activity.description, TEXT_LIMIT) if activity.description else None
    )
    # Never the location or the meeting link: they often carry addresses, dial-ins or
    # passcodes, and the model doesn't need them (the record is a click away).
    if activity.completed_at:
        row["completed"] = fmt.when(activity.completed_at)
    return row


def search_notes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(args, {"query", "about"})
    query = _text(args, "query", required=True, max_length=300) or ""
    about = _text(args, "about", required=False, max_length=60)
    lead_id = opportunity_id = None
    if about is not None:
        kind, record_id = _ref(about, ("lead", "opportunity"))
        try:
            if kind == "lead":
                lead_id = lead_selectors.lead_ref(ctx.scope, record_id).pk
            else:
                found_opportunity = pipeline_selectors.opportunity_ref(ctx.scope, record_id)
                # The lead narrows the candidates through its index; the opportunity filters.
                lead_id, opportunity_id = found_opportunity.lead_id, found_opportunity.pk
        except NotFoundError:
            raise ToolError("No such record in this workspace.") from None
    remaining = settings.AI_CONTEXT_MAX_CHARS - ctx.retrieved_chars
    if remaining <= 0:
        raise ToolError(
            "This question has used its retrieved-text budget; answer with what you have."
        )
    found = retrieval.retrieve(
        ctx.scope, query, lead_id=lead_id, opportunity_id=opportunity_id, max_chars=remaining
    )
    ctx.retrieval_dropped += found.dropped
    ctx.retrieved_chars += sum(len(p.text) for p in found.passages)
    if not found.available:
        ctx.retrieval_unavailable = True
        return {"available": False, "message": "Note search is temporarily unavailable."}
    leads = {
        lead.pk: lead
        for lead in ctx.scope.apply(
            Lead.objects.filter(pk__in={p.lead_id for p in found.passages})
        ).only("id", "owner_id", "display_name")
    }
    passages = []
    for passage in found.passages:
        ref = ctx.cite(passage.source_type.value, passage.source_id, passage.label)
        when = fmt.when(passage.occurred_at)
        text = passage.text  # sanitised as sliced (retrieval: user_text_slice)
        citation = Citation(
            ref=ref,
            kind=passage.source_type.value,
            label=passage.label,
            snippet=text[:PREVIEW_LIMIT],
            when=when["display"] if when else "",
            source_hash=passage.source_hash,
        )
        if citation not in ctx.citations:  # the same passage found twice is quoted once
            ctx.citations.append(citation)
        passages.append(
            {
                "ref": ref,
                "type": passage.source_type.value,
                "label": fmt.label(passage.label),
                "date": when,
                "lead": _lead_link(ctx, leads.get(passage.lead_id)),
                "relevance": passage.similarity,
                "untrusted_text": text,
            }
        )
    return {
        "available": True,
        "shown": len(passages),
        "passages": passages,
        "note": "Passages are the closest matches by meaning; some may be unrelated.",
    }


_TEAM_METRICS = ("pipeline", "leads", "overdue_tasks")


def team_breakdown(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.scope.is_organization_wide:  # never offered then; refused if called anyway
        raise ToolError("Unknown tool.")
    _check_keys(args, {"metric", "limit"})
    metric = _choice(args, "metric", _TEAM_METRICS, None)
    if metric is None:
        raise ToolError(f"metric is required: one of {', '.join(_TEAM_METRICS)}.")
    limit = _limit(args)
    rows: list[dict[str, Any]]
    if metric == "pipeline":
        found = pipeline_selectors.open_pipeline_by_owner(ctx.scope, limit=limit)
        people = identity_selectors.people(r.owner_id for r in found)
        rows = [
            {
                "user": people[r.owner_id].full_name if r.owner_id in people else None,
                "open_opportunities": r.open_count,
                "pipeline_value": fmt.money(r.pipeline_value),
                "weighted_pipeline": fmt.money(r.weighted_pipeline),
            }
            for r in found
        ]
    elif metric == "leads":
        counted = lead_selectors.lead_counts_by_owner(ctx.scope, now=ctx.now, limit=limit)
        people = identity_selectors.people(r.owner_id for r in counted)
        rows = [
            {
                "user": people[r.owner_id].full_name if r.owner_id in people else None,
                "leads": r.total,
                "new_leads_today": r.new_today,
            }
            for r in counted
        ]
    else:
        tasks = activity_selectors.task_counts_by_owner(ctx.scope, now=ctx.now, limit=limit)
        people = identity_selectors.people(r.owner_id for r in tasks)
        rows = [
            {
                "user": people[r.owner_id].full_name if r.owner_id in people else None,
                "open_tasks": r.open_tasks,
                "overdue_tasks": r.overdue_tasks,
            }
            for r in tasks
        ]
    return {"metric": metric, "users": rows, "shown": len(rows)}


# --- the registry ------------------------------------------------------------------------------
def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_LIMIT_PROPERTY = {
    "type": "integer",
    "description": f"How many rows to return, 1 to {MAX_LIMIT} (default {DEFAULT_LIMIT}).",
}


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]
    organisation_only: bool = False


TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_pipeline_summary",
        "Pipeline value, weighted pipeline and number of open opportunities in this "
        "workspace (every pipeline, or one by name), plus the number, value and weighted value "
        "of opportunities in each stage. Use for any question about pipeline totals or deals "
        "by stage.",
        _schema({"pipeline": {"type": "string", "description": "A pipeline's name (optional)."}}),
        get_pipeline_summary,
    ),
    Tool(
        "list_opportunities",
        "Opportunities (deals) in this workspace, filtered and sorted, with value, stage, "
        "probability, expected close date, customer, instrument and lead. Returns the total "
        "matching and up to `limit` rows.",
        _schema(
            {
                "status": {"type": "string", "enum": ["open", "won", "lost"]},
                "stage": {"type": "string", "description": "A stage name, e.g. Negotiation."},
                "stage_type": {
                    "type": "string",
                    "enum": ["open", "negotiation", "won", "lost"],
                    "description": "What the stage means, whatever it is called.",
                },
                "pipeline": {"type": "string", "description": "A pipeline's name."},
                "closing": {
                    "type": "string",
                    "enum": list(_CLOSING),
                    "description": "Expected close date window (open deals).",
                },
                "instrument": {
                    "type": "string",
                    "enum": list(instruments.INSTRUMENTS),
                    "description": "Only deals for this instrument.",
                },
                "sort": {"type": "string", "enum": list(_OPPORTUNITY_SORTS)},
                "limit": _LIMIT_PROPERTY,
            }
        ),
        list_opportunities,
    ),
    Tool(
        "get_negotiation_history",
        "The negotiated prices recorded for one opportunity (its reference, opportunity:<id>), "
        "newest first, with the stage, when and by whom. Use for 'what price did we last "
        "negotiate' questions; find the opportunity first with find_records.",
        _schema({"ref": {"type": "string"}}, ("ref",)),
        get_negotiation_history,
    ),
    Tool(
        "get_lead_summary",
        "Total leads, new leads today and leads per status in this workspace. The only "
        "source of lead counts: every opportunity created in the pipeline made its own lead, "
        "so never count opportunities as leads.",
        _schema({}),
        get_lead_summary,
    ),
    Tool(
        "list_leads",
        "Leads in this workspace, filtered by status or creation date and sorted. Returns the "
        "total matching and up to `limit` rows (no contact details).",
        _schema(
            {
                "status": {"type": "string", "description": "A lead status name, e.g. Qualified."},
                "created": {"type": "string", "enum": list(_CREATED)},
                "sort": {"type": "string", "enum": list(_LEAD_SORTS)},
                "limit": _LIMIT_PROPERTY,
            }
        ),
        list_leads,
    ),
    Tool(
        "get_activity_summary",
        "Open, overdue and due-today task counts and today's and upcoming meeting counts in "
        "this workspace.",
        _schema({}),
        get_activity_summary,
    ),
    Tool(
        "list_tasks",
        "Tasks in this workspace: overdue, due today, all open, or upcoming; soonest due first.",
        _schema(
            {"filter": {"type": "string", "enum": list(_TASK_FILTERS)}, "limit": _LIMIT_PROPERTY},
            ("filter",),
        ),
        list_tasks,
    ),
    Tool(
        "list_meetings",
        "Meetings in this workspace in a date range (business time zone), excluding "
        "cancelled; `upcoming`: scheduled meetings from now to the end of the 7th day.",
        _schema(
            {"range": {"type": "string", "enum": list(_MEETING_RANGES)}, "limit": _LIMIT_PROPERTY},
            ("range",),
        ),
        list_meetings,
    ),
    Tool(
        "find_records",
        "Find leads, opportunities, tasks, meetings and notes by name or words they contain "
        "(exact words, not meaning). Use it to turn a name like 'Rahul' or 'analyzer deal' "
        "into record references.",
        _schema({"query": {"type": "string", "description": "2 to 100 characters."}}, ("query",)),
        find_records,
    ),
    Tool(
        "get_record",
        "Details of one record by its reference (lead:<id>, opportunity:<id>, task:<id>, "
        "meeting:<id> or note:<id>), with a lead's or opportunity's recent activities.",
        _schema({"ref": {"type": "string"}}, ("ref",)),
        get_record,
    ),
    Tool(
        "search_notes",
        "Search the text of notes, meeting and task descriptions, and lead and opportunity "
        "descriptions by meaning. Use for what was discussed, concerns, context and summaries. "
        "Optionally restrict to one lead or opportunity (`about`: its reference).",
        _schema(
            {
                "query": {"type": "string", "description": "What to look for, in plain words."},
                "about": {"type": "string", "description": "lead:<id> or opportunity:<id>."},
            },
            ("query",),
        ),
        search_notes,
    ),
    Tool(
        "team_breakdown",
        "Organisation-wide figures per salesperson: open pipeline, leads, or open and overdue "
        "tasks, largest first.",
        _schema(
            {"metric": {"type": "string", "enum": list(_TEAM_METRICS)}, "limit": _LIMIT_PROPERTY},
            ("metric",),
        ),
        team_breakdown,
        organisation_only=True,
    ),
)
_BY_NAME = {tool.name: tool for tool in TOOLS}


def available(scope: AccessScope) -> list[Tool]:
    return [t for t in TOOLS if scope.is_organization_wide or not t.organisation_only]


def definitions(scope: AccessScope) -> list[dict[str, Any]]:
    """The tool list for the model: the same order and bytes for a given scope kind, so the
    prompt prefix stays cacheable."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.schema,
            "strict": True,
        }
        for tool in available(scope)
    ]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    content: str  # JSON text for the tool_result block
    is_error: bool


def execute(ctx: ToolContext, name: str, arguments: Any) -> ToolOutcome:
    """Run one model-requested call. Never raises: errors become error results the model
    can recover from (an unexpected failure is logged, without arguments or content)."""
    tool = _BY_NAME.get(name)
    if tool is None or tool not in available(ctx.scope):
        return ToolOutcome(json.dumps({"error": "Unknown tool."}), True)
    if not isinstance(arguments, dict):
        return ToolOutcome(json.dumps({"error": "Arguments must be an object."}), True)
    if ctx.result_chars >= settings.AI_TOOL_RESULTS_MAX_CHARS:
        return ToolOutcome(json.dumps({"error": BUDGET_SPENT}), True)
    ctx.tools_used.append(name)
    try:
        result = tool.run(ctx, arguments)
    except ToolError as exc:
        return ToolOutcome(json.dumps({"error": str(exc)}), True)
    except Exception:  # a failed tool must not fail the question
        logger.exception("ai_tool_failed", extra={"tool": name})
        return ToolOutcome(json.dumps({"error": "The tool failed. Try another approach."}), True)
    content = json.dumps(result, ensure_ascii=False, default=str)
    if ctx.result_chars + len(content) > settings.AI_TOOL_RESULTS_MAX_CHARS:
        ctx.result_chars = settings.AI_TOOL_RESULTS_MAX_CHARS
        return ToolOutcome(json.dumps({"error": BUDGET_SPENT}), True)
    ctx.result_chars += len(content)
    return ToolOutcome(content, False)


BUDGET_SPENT = "This question has used its tool-output budget; answer with what you have."
