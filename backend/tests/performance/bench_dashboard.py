# ruff: noqa: T201
# A diagnostics command (prints its report), not application code:
# mypy: ignore-errors
"""Dashboard benchmark: EXPLAIN ANALYZE of the exact SQL the dashboard runs, and the whole
`dashboard.selectors.dashboard()` call, on realistic and growth volumes.

Not a test (pytest doesn't collect it). Run against a benchmark database:

    DATABASE_URL=postgres://.../arkray_bench_act DJANGO_SETTINGS_MODULE=config.settings.test \
        uv run python tests/performance/bench_dashboard.py [--grow] [--verbose]

Without --grow it measures what the database holds (arkray_bench_act: 100,000 leads, 50,000
opportunities, 403,000 activities, 61 users; tests/performance/bench_activities.py).

--grow extends a copy of the 2M-activity database (CREATE DATABASE arkray_bench_dash
TEMPLATE arkray_bench_2m) to growth scale: 440 more users (501), 900,000 more leads
(1,000,000: two years of history at ~1,230 a day, so today has some; 3 % archived; one owner
with 66,700), and 250,000 more opportunities (300,000: 60 % open, 25 % won, 15 % lost, 3 %
archived), then VACUUM ANALYZE (what autovacuum keeps up in production).

--churn N first edits N random leads, each in its own committed transaction (as the app's
edits are: updated_at and version change, never a HOT update), and measures right away,
before autovacuum catches up: the lead figures are index-only scans, so they depend on the
visibility map that every edit erodes (Phase 5 performance review, P1). It CHANGES the
database: run it on a copy (CREATE DATABASE ... TEMPLATE arkray_bench_dash).

Measured for the heaviest owner (most leads), a typical owner and the organisation: every
query the dashboard executes, verbatim, EXPLAIN ANALYZEd (best of three), then the whole
selector call (best of five, wall clock, inside its REPEATABLE READ snapshot). Results:
docs/dashboard.md#performance.
"""

from __future__ import annotations

import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django

django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.utils import timezone  # noqa: E402

from arkray.activities.models import Activity  # noqa: E402
from arkray.core.access import AccessScope  # noqa: E402
from arkray.dashboard import selectors  # noqa: E402
from arkray.identity.models import User  # noqa: E402
from arkray.leads.models import Lead  # noqa: E402
from arkray.pipeline.models import Opportunity, Pipeline  # noqa: E402
from tests.factories import UserFactory  # noqa: E402

NEW_USERS, NEW_LEADS, NEW_OPPORTUNITIES = 440, 900_000, 250_000
EXECUTION_TIME = re.compile(r"Execution Time: ([0-9.]+) ms")
INDEX_SCAN = re.compile(r"(?:Index|Index Only) Scan(?: Backward)? using (\w+)")
BITMAP_SCAN = re.compile(r"Bitmap Index Scan on (\w+)")
SEQ_SCAN = re.compile(r"Seq Scan on (\w+)")
# (marker, second marker, label), checked in order: the activity lists join leads too.
LABELS = (
    ("activities_activity", "COUNT", "activity figures"),
    ("= 'meeting'", "LIMIT", "next meetings"),
    ("= 'task'", "LIMIT", "next open tasks"),
    ("pipeline_opportunity", "SUM", "pipeline totals"),
    ("leads_lead", "COUNT", "lead figures"),
    ("leads_lead", "LIMIT", "today's newest leads"),
)


def grow() -> None:
    heavy = Lead.objects.values("owner_id").annotate(n=Count("id")).order_by("-n")[0]["owner_id"]
    others = [UserFactory(email=f"grown{i}@bench.example") for i in range(NEW_USERS)]
    others += list(User.objects.filter(role="sales_user").exclude(pk=heavy))
    ids = [str(heavy)] + [str(u.pk) for u in others if u.pk != heavy]
    stages = {s.key: str(s.pk) for s in Pipeline.objects.get(is_default=True).stages.all()}
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        cursor.execute("SET max_parallel_workers_per_gather = 0")  # 64 MB /dev/shm in Docker
        # 900,000 leads, one every 70 seconds back from now (two years); every 15th for the
        # heavy owner (60,000 more: 66,700), the rest round-robin over the other 499 users.
        cursor.execute(
            """
            INSERT INTO leads_lead (
                id, first_name, last_name, organization_name, job_title, email, phone, mobile,
                alternate_phone, phone_keys, address_line_1, address_line_2, city, state,
                postal_code, country, status_key, source_key, rating, owner_id, created_by_id,
                last_contacted_at, description, archived_at, version, created_at, updated_at)
            SELECT gen_random_uuid(), 'Grown', 'Person ' || n, 'Grown Clinic ' || (n %% 997),
                   '', 'grown' || n || '@clinic.example', '', '', '', '{}', '', '', '', '', '',
                   '', 'qualified', NULL, NULL, o.id, o.id, NULL, '',
                   CASE WHEN n %% 33 = 0 THEN now() END, 1,
                   now() - (n * 70 || ' seconds')::interval,
                   now() - (n * 70 || ' seconds')::interval
            FROM generate_series(1, %s) AS n
            CROSS JOIN LATERAL (
                SELECT (%s::uuid[])[CASE WHEN n %% 15 = 0 THEN 1
                                         ELSE 2 + (n %% (cardinality(%s::uuid[]) - 1)) END]
                    AS id
            ) o
            """,
            [NEW_LEADS, ids, ids],
        )
        # 250,000 opportunities on the grown leads, owned by the lead's owner; stage by
        # (n % 20): 12 open slots, 5 won, 3 lost (as tests/performance/bench_pipeline.py).
        cursor.execute(
            """
            WITH picks AS (
                SELECT l.id AS lead_id, l.owner_id, row_number() OVER () AS n
                FROM leads_lead l WHERE l.first_name = 'Grown' LIMIT %(count)s
            ), staged AS (
                SELECT p.*, CASE
                    WHEN n %% 20 < 4 THEN %(new)s::uuid
                    WHEN n %% 20 < 7 THEN %(qualified)s::uuid
                    WHEN n %% 20 < 10 THEN %(proposal)s::uuid
                    WHEN n %% 20 < 12 THEN %(negotiation)s::uuid
                    WHEN n %% 20 < 17 THEN %(won)s::uuid
                    ELSE %(lost)s::uuid END AS stage_id
                FROM picks p
            )
            INSERT INTO pipeline_opportunity (
                id, title, lead_id, owner_id, pipeline_id, stage_id, status, value, probability,
                probability_overridden, expected_close_date, description, lost_reason,
                closed_at, created_by_id, archived_at, version, created_at, updated_at)
            SELECT gen_random_uuid(), 'Deal ' || n, s.lead_id, s.owner_id, st.pipeline_id,
                   s.stage_id, st.category, ((n * 7919) %% 5000000) + 0.50, st.probability,
                   false,
                   CASE WHEN n %% 5 = 0 THEN NULL
                        ELSE DATE '2026-01-01' + ((n * 13) %% 730)::int END,
                   '', '',
                   CASE WHEN st.category <> 'open'
                        THEN now() - ((n %% 500000) || ' seconds')::interval END,
                   s.owner_id,
                   CASE WHEN n %% 33 = 0 THEN now() END,
                   1,
                   now() - ((n * 3) || ' seconds')::interval,
                   now() - ((n * 5 %% 900000) || ' seconds')::interval
            FROM staged s JOIN pipeline_stage st ON st.id = s.stage_id
            """,
            {"count": NEW_OPPORTUNITIES, **stages},
        )
    with connection.cursor() as cursor:
        cursor.execute("SET max_parallel_maintenance_workers = 0")
        for table in ("leads_lead", "pipeline_opportunity", "identity_user"):
            cursor.execute(f"VACUUM ANALYZE {table}")


def churn(edits: int) -> None:
    """`edits` random leads edited one by one, each committed on its own."""
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        cursor.execute(
            """
            CREATE OR REPLACE PROCEDURE bench_churn(n int) LANGUAGE plpgsql AS $$
            DECLARE ids uuid[];
            BEGIN
              SELECT array_agg(id) INTO ids
              FROM (SELECT id FROM leads_lead ORDER BY random() LIMIT n) picked;
              FOR i IN 1..n LOOP
                UPDATE leads_lead SET updated_at = now(), version = version + 1
                WHERE id = ids[i];
                COMMIT;
              END LOOP;
            END $$
            """,
            [],
        )
        cursor.execute("CALL bench_churn(%s)", [edits])
        cursor.execute("DROP PROCEDURE bench_churn(int)", [])


def explain(sql: str, runs: int = 3) -> tuple[float, set[str], str]:
    """Best-of-`runs` execution time, the scans used, and the plan."""
    best, text = float("inf"), ""
    for _ in range(runs):
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", [])
            text = "\n".join(row[0] for row in cursor.fetchall())
        best = min(best, float(EXECUTION_TIME.search(text).group(1)))
    used = set(INDEX_SCAN.findall(text)) | {f"bitmap {i}" for i in BITMAP_SCAN.findall(text)}
    used |= {f"SEQ SCAN {table}" for table in SEQ_SCAN.findall(text)}
    return best, used, text


def label(sql: str) -> str:
    for marker, kind, name in LABELS:
        if marker in sql and kind in sql:
            return name
    return sql[:40]


def round_trip() -> float:
    """The cost of one trivial statement (client <-> server latency), best of 200."""
    best = float("inf")
    with connection.cursor() as cursor:
        for _ in range(200):
            started = time.perf_counter()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            best = min(best, (time.perf_counter() - started) * 1000)
    return best


def measure(who: str, scope: AccessScope, *, verbose: bool) -> None:
    now = timezone.now()
    with CaptureQueriesContext(connection) as captured:
        figures = selectors.dashboard(scope, now=now)
    print(
        f"\n== {who} ==  leads {figures.leads.total:,} (new today {figures.leads.new_today:,}),"
        f" open opportunities {figures.pipeline.open_count:,}, open tasks"
        f" {figures.activities.open_tasks:,}, upcoming meetings"
        f" {figures.activities.upcoming_meetings:,}"
    )
    total = 0.0
    for query in captured.captured_queries:
        sql = query["sql"]
        if not sql.startswith("SELECT"):
            continue
        runtime, used, text = explain(sql)
        total += runtime
        print(f"{runtime:9.2f} ms  {label(sql):<22} {', '.join(sorted(used)) or '-'}")
        if verbose:
            print(text)
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        selectors.dashboard(scope, now=now)
        timings.append((time.perf_counter() - started) * 1000)
    median = statistics.median(timings)
    print(
        f"{total:9.2f} ms  sum of the queries (server)\n"
        f"{min(timings):9.2f} ms  whole selector, best of 5 (median {median:.2f})"
    )


def main() -> None:
    if "--grow" in sys.argv:
        started = time.monotonic()
        grow()
        print(f"grown in {time.monotonic() - started:.0f} s")
    if "--churn" in sys.argv:
        edits = int(sys.argv[sys.argv.index("--churn") + 1])
        started = time.monotonic()
        churn(edits)
        print(f"{edits:,} lead edits in {time.monotonic() - started:.0f} s (no VACUUM since)")
    print(
        f"users {User.objects.count():,}, leads {Lead.objects.count():,}, opportunities"
        f" {Opportunity.objects.count():,}, activities {Activity.objects.count():,}"
    )
    per_owner = Lead.objects.values("owner_id").annotate(n=Count("id")).order_by("-n")
    owners = list(per_owner)
    heavy, typical = owners[0], owners[len(owners) // 2]
    admin = User.objects.filter(role="admin").first()
    verbose = "--verbose" in sys.argv
    print(f"one round trip (SELECT 1): {round_trip():.2f} ms")
    for who, owner_id, n in (
        ("heaviest owner", heavy["owner_id"], heavy["n"]),
        ("typical owner", typical["owner_id"], typical["n"]),
        ("organisation", None, None),
    ):
        scope = (
            AccessScope.organization(admin.pk)
            if owner_id is None
            else AccessScope.for_user(admin.pk, owner_id)
        )
        measure(f"{who}{f' ({n:,} leads)' if n else ''}", scope, verbose=verbose)


if __name__ == "__main__":
    main()
