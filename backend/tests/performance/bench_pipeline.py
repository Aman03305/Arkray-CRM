# ruff: noqa: T201
# A diagnostics command (prints its report), not application code:
# mypy: ignore-errors
"""Pipeline benchmark: EXPLAIN ANALYZE of the exact SQL the API runs, on a realistic volume.

Not a test (pytest doesn't collect it). Run against an empty, migrated scratch database:

    DATABASE_URL=postgres://.../arkray_bench DJANGO_SETTINGS_MODULE=config.settings.test \
        uv run python tests/performance/bench_pipeline.py [--seed] [--verbose]

--seed builds the dataset: 100,000 leads and 300,000 opportunities over 60 owners, one of
them with ~20,000 opportunities (the rest ~4,740 each); 60 % open, 25 % won, 15 % lost,
3 % archived, 20 % without an expected close date. Every query the selectors execute is
captured verbatim and EXPLAIN ANALYZEd (best of three). Results: docs/database.md.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django

django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from arkray.core.access import AccessScope  # noqa: E402
from arkray.core.keyset import KeysetPaginator  # noqa: E402
from arkray.identity.models import User  # noqa: E402
from arkray.pipeline import selectors  # noqa: E402
from arkray.pipeline.models import Opportunity, Pipeline  # noqa: E402
from arkray.pipeline.selectors import ORDERINGS, OpportunityFilters  # noqa: E402
from tests.factories import AdminFactory, UserFactory  # noqa: E402

OWNERS = 60
EXECUTION_TIME = re.compile(r"Execution Time: ([0-9.]+) ms")
INDEX_SCAN = re.compile(r"(?:Index|Index Only) Scan(?: Backward)? using (\w+) on pipeline_opp")
BITMAP_SCAN = re.compile(r"Bitmap Index Scan on (pipeline_\w+)")


def seed() -> None:
    owners = [UserFactory() for _ in range(OWNERS)]
    AdminFactory()
    ids = [str(o.pk) for o in owners]
    stages = {s.key: str(s.pk) for s in Pipeline.objects.get(is_default=True).stages.all()}
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        # 100,000 leads: owner 1 gets 6,700 (~20,000 opportunities); the other 59 share the
        # rest evenly (~1,580 leads, ~4,740 opportunities each).
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
        # Three opportunities per lead, owned by the lead's owner; stage by (n % 20):
        # 12 open slots (new 4, qualified 3, proposal 3, negotiation 2), 5 won, 3 lost.
        cursor.execute(
            """
            WITH picks AS (
                SELECT l.id AS lead_id, l.owner_id, row_number() OVER () AS n
                FROM leads_lead l CROSS JOIN generate_series(1, 3)
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
            stages,
        )
        cursor.execute("ANALYZE leads_lead")
        cursor.execute("ANALYZE pipeline_opportunity")


def explain(sql: str, params=None, runs: int = 3) -> tuple[float, set[str], str]:
    """Best-of-`runs` execution time, the opportunity indexes used, and the plan."""
    best, text = float("inf"), ""
    for _ in range(runs):
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", params)
            text = "\n".join(row[0] for row in cursor.fetchall())
        best = min(best, float(EXECUTION_TIME.search(text).group(1)))
    used = set(INDEX_SCAN.findall(text)) | {f"bitmap {i}" for i in BITMAP_SCAN.findall(text)}
    if "Seq Scan on pipeline_opportunity" in text:
        used.add("SEQ SCAN")
    return best, used, text


def run_captured(label: str, call, *, verbose: bool) -> None:
    """Run `call`, then EXPLAIN ANALYZE every opportunity query it executed, verbatim."""
    with CaptureQueriesContext(connection) as captured:
        call()
    for number, query in enumerate(captured.captured_queries, start=1):
        sql = query["sql"]
        if "pipeline_opportunity" not in sql:
            continue
        runtime, used, text = explain(sql)
        tag = f"{label} [{number}]"
        print(f"{runtime:9.2f} ms  {tag:<58} {', '.join(sorted(used)) or '-'}")
        if verbose:
            print(text)


def main() -> None:
    if "--seed" in sys.argv:
        started = time.monotonic()
        seed()
        print(f"seeded in {time.monotonic() - started:.0f} s")
    counts = dict(Opportunity.objects.values_list("status").annotate(n=Count("id")))
    print("opportunities:", Opportunity.objects.count(), counts)
    per_owner = Opportunity.objects.values("owner_id").annotate(n=Count("id"))
    heavy = per_owner.order_by("-n").first()
    typical = per_owner.order_by("n").first()
    print("heaviest owner:", heavy["n"], " lightest:", typical["n"])
    admin = User.objects.filter(role="admin").first()
    pipeline = selectors.pipeline_for_board(None)
    verbose = "--verbose" in sys.argv
    october = OpportunityFilters(
        status="open", expected_close_from=date(2026, 10, 1), expected_close_to=date(2026, 10, 31)
    )

    def page(scope, filters, ordering, cursor=None):
        paginator = KeysetPaginator(ORDERINGS[ordering], page_size=25, binding=None)
        return paginator.paginate(selectors.opportunity_list(scope, filters), cursor)

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
        run_captured(
            "board: [1] stage aggregates [2] totals [3] cards",
            lambda scope=scope: selectors.board(
                scope, pipeline, OpportunityFilters(), binding_for=None
            ),
            verbose=verbose,
        )
        run_captured(
            "summary",
            lambda scope=scope: selectors.pipeline_totals(scope, OpportunityFilters()),
            verbose=verbose,
        )
        for name in ORDERINGS:
            run_captured(
                f"list {name}",
                lambda scope=scope, name=name: page(scope, OpportunityFilters(), name),
                verbose=verbose,
            )
        for stage in pipeline.stages.all():
            run_captured(
                f"stage list {stage.key} (the board's next page)",
                lambda scope=scope, stage=stage: page(
                    scope,
                    OpportunityFilters(pipeline_id=pipeline.pk, stage_id=stage.pk),
                    selectors.BOARD_ORDERING[stage.category].name,
                ),
                verbose=verbose,
            )
        run_captured(
            "list archived",
            lambda scope=scope: page(scope, OpportunityFilters(archived=True), "-created_at"),
            verbose=verbose,
        )
        run_captured(
            "list open, closing in October",
            lambda scope=scope: page(scope, october, "expected_close"),
            verbose=verbose,
        )
        listing = selectors.opportunity_list(scope, OpportunityFilters())
        deep = listing.order_by("-value", "-id")[min(5000, listing.count() - 30)]
        cursor = KeysetPaginator(ORDERINGS["-value"], page_size=25, binding=None)._cursor(
            deep, "next"
        )
        run_captured(
            "list -value, deep cursor",
            lambda scope=scope, cursor=cursor: page(scope, OpportunityFilters(), "-value", cursor),
            verbose=verbose,
        )

    lead_id = Opportunity.objects.values_list("lead_id", flat=True).first()
    org = AccessScope.organization(admin.pk)
    print("\n== one lead ==")
    run_captured(
        "lead page: its opportunities",
        lambda: page(org, OpportunityFilters(lead_id=lead_id), "-created_at"),
        verbose=verbose,
    )
    run_captured(
        "reassignment: its open opportunities",
        lambda: list(
            Opportunity.objects.filter(lead_id=lead_id, status="open").order_by("id").only("id")
        ),
        verbose=verbose,
    )
    run_captured(
        "conversion guard",
        lambda: Opportunity.objects.filter(lead_id=lead_id).exists(),
        verbose=verbose,
    )
    runtime, used, _ = explain(
        "SELECT 1 FROM pipeline_opportunity WHERE lead_id = %s AND open_owner_id = %s",
        [lead_id, heavy["owner_id"]],
    )
    label = "ownership FK check on a lead owner change"
    print(f"{runtime:9.2f} ms  {label:<58} {', '.join(sorted(used))}")


if __name__ == "__main__":
    main()
