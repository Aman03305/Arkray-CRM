# 0012. "Today" is computed in the organisation's time zone

Status: Accepted
Date: 2026-09-30

## Context
"New leads today", "tasks due today", "today's meetings" and "overdue" all depend on where the
day starts. Storing UTC and comparing against UTC midnight would be off by 5.5 hours for an
India-based team.

## Decision
- Storage and APIs use UTC (`USE_TZ=True`, `TIME_ZONE="UTC"`).
- Business day boundaries use `CRM_TIME_ZONE` (default `Asia/Kolkata`). One server-side helper
  computes them and passes them to queries as UTC instants (`[today_start, today_end)`).
- Date-only fields (`due_date`, `expected_close_date`) compare against "today" in that zone.
- The browser displays timestamps in the viewer's local time.
- Naive datetimes are prevented: ruff `DTZ` rules, and runtime warnings are errors in tests.

## Consequences
- Users, admins and Ask Arkray all see the same "today".
- Per-user time zones would be a localised change to the helper (pass the subject's zone).

## Alternatives considered
- Client-computed day boundaries: inconsistent between users, and unusable by Ask Arkray.
- Per-user time zones in v1: complexity without a stated need.
