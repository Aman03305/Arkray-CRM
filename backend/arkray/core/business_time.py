"""Business days ([ADR-0012](../../../docs/adr/0012-business-day-time-zone.md)).

Storage and APIs speak UTC; "today", "due today", "this day's meetings" and date filters
mean a calendar day in the organisation's time zone (`CRM_TIME_ZONE`, Asia/Kolkata by
default). This is the one place that turns such a day into the UTC instants queries use,
so every module, the dashboard and Ask Arkray agree on where a day starts: 00:15 IST on
3 October belongs to 3 October even though it is still 2 October in UTC.

A day is the half-open range [midnight, next midnight): an instant at exactly midnight
belongs to the day it starts.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings


def business_zone() -> ZoneInfo:
    return ZoneInfo(settings.CRM_TIME_ZONE)


def business_midnight(day: date) -> datetime:
    """The instant `day` starts in the business time zone (aware)."""
    return datetime.combine(day, time.min, tzinfo=business_zone())


def business_date(moment: datetime) -> date:
    """The business day an aware instant falls on."""
    if moment.tzinfo is None:
        raise ValueError("A business date needs an aware datetime.")
    return moment.astimezone(business_zone()).date()


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a business day, as aware instants."""
    return business_midnight(day), business_midnight(day + timedelta(days=1))


def today_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the business day that contains `now`."""
    return business_day_bounds(business_date(now))
