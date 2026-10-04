"""The activity figures Phase 5's dashboard will show (selectors.activity_summary and
upcoming_meetings): exact definitions, the business day in Asia/Kolkata, scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arkray.activities.models import ActivityStatus
from arkray.activities.selectors import (
    ActivityFilters,
    ActivitySummary,
    activity_list,
    activity_summary,
    upcoming_meetings,
)
from arkray.core.access import AccessScope
from tests.factories import LeadFactory, MeetingFactory, TaskFactory, UserFactory

pytestmark = pytest.mark.django_db

# 00:15 IST on 3 October 2026 (18:45 UTC on 2 October).
NOW = datetime(2026, 10, 2, 18, 45, tzinfo=UTC)
IST_DAY_START = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)
IST_DAY_END = datetime(2026, 10, 3, 18, 30, tzinfo=UTC)


def meeting(lead, start, status=ActivityStatus.SCHEDULED, **extra):
    return MeetingFactory(
        lead=lead, starts_at=start, ends_at=start + timedelta(minutes=30), status=status, **extra
    )


def test_the_definitions_at_a_utc_boundary(user_a):
    lead = LeadFactory(owner=user_a)
    # Tasks: due today (IST), earlier today, yesterday (IST, but 2 Oct UTC too), tomorrow,
    # undated, completed today, archived overdue.
    TaskFactory(lead=lead, due_at=IST_DAY_END - timedelta(minutes=1))  # today, later
    TaskFactory(lead=lead, due_at=IST_DAY_START)  # today, at midnight: due today AND overdue
    TaskFactory(lead=lead, due_at=IST_DAY_START - timedelta(minutes=1))  # yesterday: overdue
    TaskFactory(lead=lead, due_at=IST_DAY_END)  # tomorrow
    TaskFactory(lead=lead)  # undated: open, never overdue
    TaskFactory(lead=lead, due_at=NOW, status=ActivityStatus.COMPLETED)
    TaskFactory(lead=lead, due_at=NOW - timedelta(days=9), archived_at=NOW)
    # Meetings: earlier today (completed), later today, tomorrow, yesterday, cancelled today.
    meeting(lead, IST_DAY_START + timedelta(minutes=5), ActivityStatus.COMPLETED)
    meeting(lead, NOW + timedelta(hours=10))
    meeting(lead, IST_DAY_END + timedelta(hours=1))
    meeting(lead, IST_DAY_START - timedelta(hours=1))  # yesterday, still scheduled
    meeting(lead, NOW + timedelta(hours=2), ActivityStatus.CANCELLED)

    assert activity_summary(AccessScope.own(user_a.pk), now=NOW) == ActivitySummary(
        open_tasks=5,
        tasks_due_today=2,
        overdue_tasks=2,
        meetings_today=2,  # the completed one and the later one; not the cancelled one
        upcoming_meetings=2,  # later today and tomorrow
    )


def test_every_count_has_a_list_showing_exactly_those_activities(user_a):
    """The Activities page's shortcuts (frontend) open these filters; each must list the rows
    its figure counts, so a number never disagrees with the list it opens (review)."""
    test_the_definitions_at_a_utc_boundary(user_a)
    scope = AccessScope.own(user_a.pk)
    today = IST_DAY_START.astimezone(UTC).date() + timedelta(days=1)  # 3 Oct in IST
    shortcuts = {
        "open_tasks": ActivityFilters(type="task", status="open"),
        "tasks_due_today": ActivityFilters(
            type="task", status="open", date_from=today, date_to=today
        ),
        "overdue_tasks": ActivityFilters(overdue=True),
        "meetings_today": ActivityFilters(
            type="meeting", cancelled=False, date_from=today, date_to=today
        ),
        "upcoming_meetings": ActivityFilters(type="meeting", upcoming=True),
    }
    summary = activity_summary(scope, now=NOW)
    for figure, filters in shortcuts.items():
        assert activity_list(scope, filters, now=NOW).count() == getattr(summary, figure), figure


def test_counts_are_scoped(admin, user_a, user_b):
    TaskFactory(lead=LeadFactory(owner=user_a))
    TaskFactory(lead=LeadFactory(owner=user_b))
    TaskFactory(lead=LeadFactory(owner=user_b))
    assert activity_summary(AccessScope.own(user_a.pk), now=NOW).open_tasks == 1
    assert activity_summary(AccessScope.for_user(admin.pk, user_b.pk), now=NOW).open_tasks == 2
    assert activity_summary(AccessScope.organization(admin.pk), now=NOW).open_tasks == 3


def test_an_empty_workspace_counts_zero():
    assert activity_summary(AccessScope.own(UserFactory().pk), now=NOW) == ActivitySummary(
        0, 0, 0, 0, 0
    )


def test_upcoming_meetings_are_the_next_scheduled_ones(user_a):
    lead = LeadFactory(owner=user_a)
    later = meeting(lead, NOW + timedelta(days=2))
    soon = meeting(lead, NOW + timedelta(hours=1))
    meeting(lead, NOW - timedelta(hours=1))
    meeting(lead, NOW + timedelta(hours=3), ActivityStatus.CANCELLED)
    meeting(lead, NOW + timedelta(hours=4), archived_at=NOW)
    found = upcoming_meetings(AccessScope.own(user_a.pk), now=NOW, limit=5)
    assert [m.pk for m in found] == [soon.pk, later.pk]
    with pytest.raises(ValueError, match="range"):
        upcoming_meetings(AccessScope.own(user_a.pk), now=NOW, limit=500)


def test_next_open_tasks_are_the_start_of_the_tasks_tab(user_a):
    """The dashboard's "tasks requiring attention" are the first rows of the Activities
    page's Tasks tab (open, soonest due first): overdue first, undated last."""
    from arkray.activities.selectors import ORDERINGS, next_open_tasks
    from arkray.core.keyset import KeysetPaginator

    lead = LeadFactory(owner=user_a)
    undated = TaskFactory(lead=lead)
    later = TaskFactory(lead=lead, due_at=NOW + timedelta(days=3))
    overdue = TaskFactory(lead=lead, due_at=NOW - timedelta(days=2))
    today = TaskFactory(lead=lead, due_at=NOW + timedelta(hours=2))
    TaskFactory(lead=lead, due_at=NOW - timedelta(days=5), status=ActivityStatus.COMPLETED)
    TaskFactory(lead=lead, due_at=NOW - timedelta(days=6), archived_at=NOW)
    TaskFactory(lead=LeadFactory(), due_at=NOW - timedelta(days=9))  # someone else's
    scope = AccessScope.own(user_a.pk)

    found = next_open_tasks(scope, now=NOW, limit=5)

    assert found == [overdue, today, later, undated]
    tab = KeysetPaginator(ORDERINGS["scheduled"], page_size=3, binding=None).paginate(
        activity_list(scope, ActivityFilters(type="task", status="open"), now=NOW), None
    )
    assert next_open_tasks(scope, now=NOW, limit=3) == tab.items
    with pytest.raises(ValueError, match="range"):
        next_open_tasks(scope, now=NOW, limit=0)


def test_next_open_tasks_break_ties_like_the_tasks_tab(user_a):
    """Undated tasks (and tasks due at the same moment) are ordered by id, as the Tasks tab's
    keyset ordering (schedule_sort, id) does, so the rows shown are its first rows."""
    from arkray.activities.selectors import next_open_tasks

    lead = LeadFactory(owner=user_a)
    undated = [TaskFactory(lead=lead) for _ in range(4)]
    same_time = [TaskFactory(lead=lead, due_at=NOW + timedelta(hours=1)) for _ in range(3)]
    found = next_open_tasks(AccessScope.own(user_a.pk), now=NOW, limit=5)
    by_id = lambda tasks: sorted(tasks, key=lambda t: t.pk)  # noqa: E731
    assert found == [*by_id(same_time), *by_id(undated)[:2]]
