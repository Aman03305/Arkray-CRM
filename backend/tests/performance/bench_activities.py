# ruff: noqa: T201
# A diagnostics command (prints its report), not application code:
# mypy: ignore-errors
"""Activities benchmark: EXPLAIN ANALYZE of the exact SQL the API runs, on a realistic volume.

Not a test (pytest doesn't collect it). Run against an empty, migrated scratch database:

    DATABASE_URL=postgres://.../arkray_bench_act DJANGO_SETTINGS_MODULE=config.settings.test \
        uv run python tests/performance/bench_activities.py [--seed] [--verbose]

--seed builds the dataset: 100,000 leads over 60 owners (one with 6,700 leads), 50,000
opportunities, 400,000 activities (45 % tasks: 40 % open, 50 % completed, 10 % cancelled,
a third undated, due dates spread over +-60 days; 30 % meetings: 30 % scheduled, 60 %
completed, 10 % cancelled; 25 % notes; 40 % of them on the lead's opportunity when it has
one; 3 % archived) and their timeline entries (creation, completion, lead and opportunity
creation: ~900,000), plus one lead with 3,000 notes (a long timeline). Every query the
selectors execute is captured verbatim and EXPLAIN ANALYZEd (best of three). Results:
docs/activities.md#performance and docs/database.md.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django

django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count, Q  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.utils import timezone  # noqa: E402

from arkray.activities import selectors  # noqa: E402
from arkray.activities.models import Activity  # noqa: E402
from arkray.activities.selectors import ORDERINGS, TIMELINE_ORDERING, ActivityFilters  # noqa: E402
from arkray.core.access import AccessScope  # noqa: E402
from arkray.core.business_time import business_date  # noqa: E402
from arkray.core.keyset import KeysetPaginator  # noqa: E402
from arkray.identity.models import User  # noqa: E402
from arkray.pipeline.models import Pipeline  # noqa: E402
from tests.factories import AdminFactory, UserFactory  # noqa: E402

OWNERS = 60
EXECUTION_TIME = re.compile(r"Execution Time: ([0-9.]+) ms")
INDEX_SCAN = re.compile(
    r"(?:Index|Index Only) Scan(?: Backward)? using (\w+) on (?:activities|leads|pipeline)"
)
BITMAP_SCAN = re.compile(r"Bitmap Index Scan on (\w+)")


def seed() -> None:
    owners = [UserFactory() for _ in range(OWNERS)]
    AdminFactory()
    ids = [str(o.pk) for o in owners]
    stage = Pipeline.objects.get(is_default=True).stages.get(key="new")
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        cursor.execute(
            """
            INSERT INTO leads_lead (
                id, first_name, last_name, organization_name, job_title, email, phone, mobile,
                alternate_phone, phone_keys, address_line_1, address_line_2, city, state,
                postal_code, country, status_key, source_key, rating, owner_id, created_by_id,
                last_contacted_at, description, archived_at, version, created_at, updated_at)
            SELECT gen_random_uuid(), 'Lead', 'Person ' || n, 'Clinic ' || (n %% 997), '',
                   'lead' || n || '@clinic.example', '', '', '', '{}', '', '', '', '', '', '',
                   'qualified', NULL, NULL, o.id, o.id, NULL, '', NULL, 1,
                   now() - (n || ' minutes')::interval, now() - (n || ' minutes')::interval
            FROM generate_series(1, 100000) AS n
            CROSS JOIN LATERAL (
                SELECT (%s::uuid[])[CASE WHEN n <= 6700 THEN 1 ELSE 2 + (n %% (%s - 1)) END]
                    AS id
            ) o
            """,
            [ids, OWNERS],
        )
        cursor.execute(
            """
            INSERT INTO pipeline_opportunity (
                id, title, lead_id, owner_id, pipeline_id, stage_id, status, value, probability,
                probability_overridden, expected_close_date, description, lost_reason,
                closed_at, created_by_id, archived_at, version, created_at, updated_at)
            SELECT gen_random_uuid(), 'Deal ' || n, l.id, l.owner_id, %s, %s, 'open', 1000,
                   %s, false, NULL, '', '', NULL, l.owner_id, NULL, 1, l.created_at,
                   l.created_at
            FROM (SELECT id, owner_id, created_at, row_number() OVER (ORDER BY id) AS n
                  FROM leads_lead) l
            WHERE n %% 2 = 0
            """,
            [stage.pipeline_id, stage.pk, stage.probability],
        )
        cursor.execute(
            """
            WITH picks AS (
                SELECT l.id AS lead_id, l.owner_id, o.id AS opportunity_id,
                       row_number() OVER () AS m
                FROM leads_lead l
                LEFT JOIN pipeline_opportunity o ON o.lead_id = l.id
                CROSS JOIN generate_series(1, 4)
            ), typed AS (
                SELECT p.*,
                       CASE WHEN m %% 20 < 9 THEN 'task' WHEN m %% 20 < 15 THEN 'meeting'
                            ELSE 'note' END AS type,
                       (m * 7) %% 10 AS s
                FROM picks p
            ), statused AS (
                SELECT t.*,
                       CASE t.type
                           WHEN 'task' THEN CASE WHEN s < 4 THEN 'open'
                                                 WHEN s < 9 THEN 'completed' ELSE 'cancelled' END
                           WHEN 'meeting' THEN CASE WHEN s < 3 THEN 'scheduled'
                                                    WHEN s < 9 THEN 'completed' ELSE 'cancelled' END
                       END AS status,
                       now() - ((m * 13) %% 31536000 || ' seconds')::interval AS created
                FROM typed t
            ), timed AS (
                SELECT s.*,
                       CASE WHEN type = 'task' AND m %% 3 <> 0
                            THEN now() + (((m * 37) %% 120) - 60 || ' days')::interval END AS due,
                       CASE WHEN type = 'meeting' THEN
                           CASE WHEN status = 'scheduled'
                                THEN now() + (((m * 53) %% 720) - 120 || ' hours')::interval
                                ELSE now() - ((m %% 4000) + 1 || ' hours')::interval END
                       END AS starts
                FROM statused s
            )
            INSERT INTO activities_activity (
                id, created_at, updated_at, type, title, description, status, priority, due_at,
                starts_at, ends_at, location, meeting_url, completed_at, cancelled_at,
                archived_at, version, cancelled_by_id, completed_by_id, created_by_id, lead_id,
                opportunity_id, owner_id)
            SELECT gen_random_uuid(), created, created, type,
                   CASE WHEN type = 'note' THEN '' ELSE 'Follow up ' || m END,
                   CASE WHEN type = 'note' THEN repeat('Spoke about the analyser. ', 12)
                        ELSE '' END,
                   status,
                   CASE WHEN type = 'task' THEN (ARRAY['low', 'normal', 'high'])[1 + m %% 3] END,
                   due, starts,
                   CASE WHEN starts IS NOT NULL THEN starts + interval '1 hour' END,
                   '', '',
                   CASE WHEN status = 'completed'
                        THEN COALESCE(starts + interval '1 hour', created + interval '1 day') END,
                   CASE WHEN status = 'cancelled' THEN created + interval '2 hours' END,
                   CASE WHEN m %% 33 = 0 THEN now() END,
                   1,
                   CASE WHEN status = 'cancelled' THEN owner_id END,
                   CASE WHEN status = 'completed' THEN owner_id END,
                   owner_id, lead_id,
                   CASE WHEN m %% 5 < 2 THEN opportunity_id END,
                   owner_id
            FROM timed
            """,
            [],  # with parameters, so "%%" is read as "%"
        )
        # One lead with a long history: 3,000 notes.
        cursor.execute(
            """
            INSERT INTO activities_activity (
                id, created_at, updated_at, type, title, description, status, priority,
                location, meeting_url, version, created_by_id, lead_id, owner_id)
            SELECT gen_random_uuid(), now() - (n || ' minutes')::interval, now(), 'note', '',
                   'Long history note ' || n, NULL, NULL, '', '', 1, l.owner_id, l.id, l.owner_id
            FROM (SELECT id, owner_id FROM leads_lead WHERE owner_id = %s LIMIT 1) l
            CROSS JOIN generate_series(1, 3000) n
            """,
            [ids[0]],
        )
        cursor.execute(
            """
            INSERT INTO activities_timeline_entry
                (lead_id, opportunity_id, activity_id, kind, actor_id, occurred_at, data)
            SELECT id, NULL::uuid, NULL::uuid, 'lead.created', owner_id, created_at,
                   jsonb_build_object('status', 'qualified', 'status_name', 'Qualified',
                                      'owner_id', owner_id::text)
            FROM leads_lead
            UNION ALL
            SELECT lead_id, id, NULL, 'opportunity.created', owner_id, created_at,
                   '{"stage": "New", "status": "open", "via_conversion": false}'::jsonb
            FROM pipeline_opportunity
            UNION ALL
            SELECT lead_id, opportunity_id, id,
                   CASE type WHEN 'task' THEN 'task.created' WHEN 'meeting'
                        THEN 'meeting.scheduled' ELSE 'note.added' END,
                   created_by_id, created_at, '{}'::jsonb
            FROM activities_activity
            UNION ALL
            SELECT lead_id, opportunity_id, id,
                   CASE type WHEN 'task' THEN 'task.completed' ELSE 'meeting.completed' END,
                   completed_by_id, completed_at, '{}'::jsonb
            FROM activities_activity WHERE status = 'completed'
            """
        )
        for table in (
            "leads_lead",
            "pipeline_opportunity",
            "activities_activity",
            "activities_timeline_entry",
        ):
            cursor.execute(f"ANALYZE {table}")


def explain(sql: str, params=None, runs: int = 3) -> tuple[float, set[str], str]:
    best, text = float("inf"), ""
    for _ in range(runs):
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", params)
            text = "\n".join(row[0] for row in cursor.fetchall())
        best = min(best, float(EXECUTION_TIME.search(text).group(1)))
    used = set(INDEX_SCAN.findall(text)) | {f"bitmap {i}" for i in BITMAP_SCAN.findall(text)}
    for table in ("activities_activity", "activities_timeline_entry"):
        if f"Seq Scan on {table}" in text:
            used.add(f"SEQ SCAN {table}")
    if re.search(r"\n\s*->\s+Sort|^Sort", text):
        used.add("sort")
    return best, used, text


def run_captured(label: str, call, *, verbose: bool) -> None:
    with CaptureQueriesContext(connection) as captured:
        call()
    for number, query in enumerate(captured.captured_queries, start=1):
        sql = query["sql"]
        if "activities_" not in sql:
            continue
        runtime, used, text = explain(sql)
        tag = f"{label} [{number}]"
        print(f"{runtime:9.2f} ms  {tag:<60} {', '.join(sorted(used)) or '-'}")
        if verbose:
            print(text)


def main() -> None:
    if "--seed" in sys.argv:
        started = time.monotonic()
        seed()
        print(f"seeded in {time.monotonic() - started:.0f} s")
    print(
        "activities:",
        Activity.objects.count(),
        dict(Activity.objects.values_list("type").annotate(n=Count("id"))),
    )
    from arkray.activities.models import TimelineEntry

    print("timeline entries:", TimelineEntry.objects.count())
    per_owner = Activity.objects.values("owner_id").annotate(n=Count("id"))
    heavy = per_owner.order_by("-n").first()
    typical = per_owner.order_by("n").first()
    print("heaviest owner:", heavy["n"], " lightest:", typical["n"])
    admin = User.objects.filter(role="admin").first()
    verbose = "--verbose" in sys.argv
    now = timezone.now()

    def page(scope, filters, ordering="-created_at", cursor=None):
        paginator = KeysetPaginator(
            selectors.ordering(ordering, scope, filters), page_size=25, binding=None
        )
        return paginator.paginate(selectors.activity_list(scope, filters, now=now), cursor)

    def day(offset: int):
        return business_date(now + timedelta(days=offset))

    shapes = [
        ("all, newest", ActivityFilters(), "-created_at"),
        ("all, oldest", ActivityFilters(), "created_at"),
        ("all, by due/start", ActivityFilters(), "scheduled"),
        ("tasks open, soonest due", ActivityFilters(type="task", status="open"), "scheduled"),
        ("tasks open, latest due", ActivityFilters(type="task", status="open"), "-scheduled"),
        (
            "tasks completed, newest",
            ActivityFilters(type="task", status="completed"),
            "-created_at",
        ),
        ("tasks any status, soonest", ActivityFilters(type="task"), "scheduled"),
        (
            "meetings scheduled, soonest",
            ActivityFilters(type="meeting", status="scheduled"),
            "scheduled",
        ),
        ("meetings any status, latest", ActivityFilters(type="meeting"), "-scheduled"),
        ("notes, newest", ActivityFilters(type="note"), "-created_at"),
        ("overdue tasks", ActivityFilters(overdue=True), "scheduled"),
        ("current work", ActivityFilters(current=True), "scheduled"),
        (
            "one week by due/start",
            ActivityFilters(date_from=now.date(), date_to=(now + timedelta(days=6)).date()),
            "scheduled",
        ),
        ("archived", ActivityFilters(archived=True), "-created_at"),
        # Rare or old matches in newest-first order (Phase 4 performance review, F1).
        (
            "meetings cancelled, newest",
            ActivityFilters(type="meeting", status="cancelled"),
            "-created_at",
        ),
        ("archived notes, newest", ActivityFilters(type="note", archived=True), "-created_at"),
        (
            "a past week, newest",
            ActivityFilters(date_from=day(-50), date_to=day(-44)),
            "-created_at",
        ),
        (
            "a past week, by due/start",
            ActivityFilters(date_from=day(-50), date_to=day(-44)),
            "scheduled",
        ),
        ("upcoming meetings list", ActivityFilters(type="meeting", upcoming=True), "scheduled"),
        (
            "today's meetings list",
            ActivityFilters(type="meeting", cancelled=False, date_from=day(0), date_to=day(0)),
            "scheduled",
        ),
    ]
    for who, owner_id in (
        ("heavy owner", heavy["owner_id"]),
        ("typical owner", typical["owner_id"]),
        ("organisation", None),
    ):
        scope = (
            AccessScope.organization(admin.pk)
            if owner_id is None
            else AccessScope.for_user(admin.pk, owner_id)
        )
        print(f"\n== {who} ==")
        for label, filters, ordering in shapes:
            run_captured(
                f"list {label}",
                lambda scope=scope, filters=filters, ordering=ordering: page(
                    scope, filters, ordering
                ),
                verbose=verbose,
            )
        if owner_id is None:
            for name, owner in (("heavy", heavy["owner_id"]), ("typical", typical["owner_id"])):
                for ordering in ("-created_at", "created_at"):
                    run_captured(
                        f"list owner filter ({name}), {ordering}",
                        lambda owner=owner, ordering=ordering, scope=scope: page(
                            scope, ActivityFilters(owner_id=owner), ordering
                        ),
                        verbose=verbose,
                    )
        run_captured(
            "summary",
            lambda scope=scope: selectors.activity_summary(scope, now=now),
            verbose=verbose,
        )
        run_captured(
            "upcoming meetings (5)",
            lambda scope=scope: selectors.upcoming_meetings(scope, now=now),
            verbose=verbose,
        )
        listing = selectors.activity_list(
            scope, ActivityFilters(type="task", status="open"), now=now
        )
        deep = listing.order_by("schedule_sort", "id")[min(3000, listing.count() - 30)]
        cursor = KeysetPaginator(ORDERINGS["scheduled"], page_size=25, binding=None)._cursor(
            deep, "next"
        )
        run_captured(
            "tasks open, deep cursor",
            lambda scope=scope, cursor=cursor: page(
                scope, ActivityFilters(type="task", status="open"), "scheduled", cursor
            ),
            verbose=verbose,
        )

    org = AccessScope.organization(admin.pk)
    long_lead = (
        Activity.objects.values("lead_id").annotate(n=Count("id")).order_by("-n").first()["lead_id"]
    )
    lead = Activity.objects.filter(lead_id=long_lead).values("lead_id", "owner_id").first()
    typical_lead = (
        Activity.objects.exclude(lead_id=long_lead).values_list("lead_id", flat=True).first()
    )
    opportunity_id = (
        Activity.objects.exclude(opportunity_id=None)
        .values_list("opportunity_id", flat=True)
        .first()
    )
    print("\n== one lead / opportunity ==")
    run_captured(
        "lead filter, newest",
        lambda: page(org, ActivityFilters(lead_id=typical_lead)),
        verbose=verbose,
    )
    run_captured(
        "lead page: current work",
        lambda: page(org, ActivityFilters(lead_id=typical_lead, current=True), "scheduled"),
        verbose=verbose,
    )
    run_captured(
        "opportunity filter",
        lambda: page(org, ActivityFilters(opportunity_id=opportunity_id)),
        verbose=verbose,
    )
    timeline_page = KeysetPaginator(TIMELINE_ORDERING, page_size=20, binding=None)
    for label, scope, lead_id in (
        ("timeline, typical lead (org)", org, typical_lead),
        ("timeline, 3,000-note lead (org)", org, long_lead),
        (
            "timeline, 3,000-note lead (owner)",
            AccessScope.for_user(admin.pk, lead["owner_id"]),
            long_lead,
        ),
    ):
        run_captured(
            label,
            lambda scope=scope, lead_id=lead_id: timeline_page.paginate(
                selectors.lead_timeline(scope, lead_id), None
            ),
            verbose=verbose,
        )
    entries = selectors.lead_timeline(org, long_lead).order_by("-occurred_at", "-id")
    deep_entry = entries[2500]
    deep_cursor = timeline_page._cursor(deep_entry, "next")
    run_captured(
        "timeline, 3,000-note lead, page 125",
        lambda: timeline_page.paginate(selectors.lead_timeline(org, long_lead), deep_cursor),
        verbose=verbose,
    )
    hidden_scope = AccessScope.for_user(admin.pk, typical["owner_id"])  # sees none of them
    try:
        selectors.lead_timeline(hidden_scope, long_lead)
    except Exception:
        print(f"{'':9}     timeline in a workspace that can't see the lead: not found (no scan)")
    run_captured(
        "opportunity timeline",
        lambda: timeline_page.paginate(selectors.opportunity_timeline(org, opportunity_id), None),
        verbose=verbose,
    )
    run_captured(
        "reassignment: current work lock",
        lambda: list(
            Activity.objects.filter(lead_id=typical_lead)
            .filter(Q(type="note") | Q(status__in=["open", "scheduled"]))
            .order_by("id")
            .only("id")
        ),
        verbose=verbose,
    )
    runtime, used, _ = explain(
        "SELECT 1 FROM activities_activity WHERE lead_id = %s AND current_owner_id = %s",
        [long_lead, lead["owner_id"]],
    )
    label = "ownership FK check on a lead owner change"
    print(f"{runtime:9.2f} ms  {label:<60} {', '.join(sorted(used))}")


if __name__ == "__main__":
    main()
