"""Business days in Asia/Kolkata (ADR-0012): the UTC boundary cases."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from django.test import override_settings

from arkray.core.business_time import (
    business_date,
    business_day_bounds,
    business_midnight,
    today_bounds,
)

IST_MIDNIGHT_3_OCT = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)


def test_00_15_ist_belongs_to_the_new_business_day_while_utc_is_still_yesterday():
    moment = datetime(2026, 10, 2, 18, 45, tzinfo=UTC)  # 00:15 IST on 3 Oct
    assert moment.date() == date(2026, 10, 2)
    assert business_date(moment) == date(2026, 10, 3)


def test_a_day_is_half_open_from_midnight_to_midnight():
    start, end = business_day_bounds(date(2026, 10, 3))
    assert start == IST_MIDNIGHT_3_OCT
    assert end == datetime(2026, 10, 3, 18, 30, tzinfo=UTC)
    assert business_date(start) == date(2026, 10, 3)  # midnight belongs to the day it starts
    assert business_date(datetime(2026, 10, 3, 18, 29, 59, tzinfo=UTC)) == date(2026, 10, 3)
    assert business_date(end) == date(2026, 10, 4)


def test_today_bounds_contain_now():
    now = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)  # 04:30 IST on 3 Oct
    assert today_bounds(now) == business_day_bounds(date(2026, 10, 3))


def test_midnight_is_aware_and_in_the_business_zone():
    assert business_midnight(date(2026, 10, 3)).utcoffset().total_seconds() == 5.5 * 3600


def test_naive_moments_are_refused():
    with pytest.raises(ValueError, match="aware"):
        business_date(datetime(2026, 10, 3, 0, 15))  # noqa: DTZ001 — the case under test


@override_settings(CRM_TIME_ZONE="Europe/London")
def test_another_organisation_zone_is_one_setting_away():
    start, _ = business_day_bounds(date(2026, 10, 3))
    assert start == datetime(2026, 10, 2, 23, 0, tzinfo=UTC)  # BST
