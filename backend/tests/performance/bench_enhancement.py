# ruff: noqa: T201, E402
# mypy: ignore-errors
# A diagnostics command (prints its report), not application code: it EXPLAINs SQL the
# application itself just ran, verbatim.
"""Product enhancement phase benchmark: the new access patterns (user pipelines, the board
over a personal pipeline, negotiated prices, deal notes with files, the security events
feed, organisation-wide pipeline lists and totals) at growth volume.

Not a test (pytest doesn't collect it). On a copy of the RAG benchmark database migrated to
this phase's schema:

    CREATE DATABASE arkray_bench_enh TEMPLATE arkray_bench_rag;   -- then `manage.py migrate`
    DATABASE_URL=.../arkray_bench_enh uv run python tests/performance/bench_enhancement.py --seed
    DATABASE_URL=.../arkray_bench_enh uv run python tests/performance/bench_enhancement.py

--seed adds, in one transaction: 3 personal pipelines per sales user (1,500; custom stage
probabilities and a negotiation stage each), 30 % of the 300,000 opportunities moved into
their owner's personal pipelines, two negotiated prices per deal in negotiation, ~300,000
attachment rows on 200,000 notes (10 % of second files deleted), the audit log grown to
~2,000,000 events with 6,000 security events; then VACUUM ANALYZE.

The measurement sends each request through the whole stack (as the heaviest owner, the owner
of the most opportunities, or an administrator organisation-wide), captures its SQL and
EXPLAIN (ANALYZE, BUFFERS)es every statement (best of three), flagging sequential scans of
large tables. Results: docs/database.md#product-enhancement-phase-access-patterns.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
os.environ.setdefault("API_THROTTLE_USER", "1000000/min")

import django

django.setup()
time.monotonic = time.perf_counter  # per-query timings at full resolution (Windows)

from django.db import connection
from django.test.utils import CaptureQueriesContext

from arkray.identity.models import Role, User
from tests.helpers import signed_in

SEED = """
SET LOCAL statement_timeout = 0;
CREATE TEMP TABLE sales_users ON COMMIT DROP AS
  SELECT id, row_number() OVER (ORDER BY id) AS n FROM identity_user WHERE role = 'sales_user';

INSERT INTO pipeline_pipeline (id, created_at, updated_at, key, name, is_default, is_active,
                               owner_id, created_by_id, version)
SELECT gen_random_uuid(), now(), now(), 'p' || u.n || '_' || g,
       (ARRAY['Tenders', 'Distributors', 'Service contracts'])[g], false, true, u.id, u.id, 1
FROM sales_users u, generate_series(1, 3) g;

INSERT INTO pipeline_stage (id, created_at, updated_at, key, name, position, probability,
                            category, is_active, pipeline_id, is_negotiation)
SELECT gen_random_uuid(), now(), now(), s.key, s.name, s.pos, s.prob, s.cat, true, p.id, s.neg
FROM pipeline_pipeline p,
     (VALUES ('lead', 'Lead', 0, 10, 'open', false),
             ('demo', 'Demo', 1, 30, 'open', false),
             ('commercial', 'Commercial discussion', 2, 65, 'open', true),
             ('po', 'PO expected', 3, 85, 'open', false),
             ('won', 'Won', 4, 100, 'won', false),
             ('lost', 'Lost', 5, 0, 'lost', false)) AS s(key, name, pos, prob, cat, neg)
WHERE p.owner_id IS NOT NULL;

-- The seeded shared stages sit at positions 10..60.
WITH chosen AS (
  SELECT o.id, o.owner_id, s.position / 10 - 1 AS pos, 1 + (abs(hashtext(o.id::text)) % 3) AS which
  FROM pipeline_opportunity o JOIN pipeline_stage s ON s.id = o.stage_id
  WHERE abs(hashtext(o.id::text || 'x')) % 10 < 3
), target AS (
  SELECT c.id, ns.id AS stage_id, np.id AS pipeline_id, ns.probability
  FROM chosen c
  JOIN pipeline_pipeline np ON np.owner_id = c.owner_id
       AND np.name = (ARRAY['Tenders', 'Distributors', 'Service contracts'])[c.which]
  JOIN pipeline_stage ns ON ns.pipeline_id = np.id AND ns.position = c.pos
)
UPDATE pipeline_opportunity o
SET pipeline_id = t.pipeline_id, stage_id = t.stage_id,
    probability = CASE WHEN o.probability_overridden THEN o.probability ELSE t.probability END
FROM target t WHERE o.id = t.id;

INSERT INTO pipeline_negotiation_price (price, currency, stage_name, source,
                                        opportunity_version, occurred_at, actor_id,
                                        opportunity_id, stage_id, subject_user_id)
SELECT round(o.value * (0.85 + g * 0.05), 2), 'INR', s.name,
       CASE g WHEN 1 THEN 'stage_entry' ELSE 'revision' END, o.version,
       now() - (3 - g) * interval '2 days', o.owner_id, o.id, s.id, NULL
FROM pipeline_opportunity o JOIN pipeline_stage s ON s.id = o.stage_id, generate_series(1, 2) g
WHERE s.is_negotiation AND o.status = 'open';

UPDATE pipeline_opportunity o
SET negotiated_price = round(o.value * 0.95, 2), negotiated_at = now() - interval '2 days'
FROM pipeline_stage s
WHERE s.id = o.stage_id AND s.is_negotiation AND o.status = 'open';

INSERT INTO activities_attachment (id, original_name, extension, content_type, size, sha256,
                                   storage_key, state, scan_status, created_at, stored_at,
                                   deleted_at, purged_at, deleted_by_id, note_id, uploaded_by_id)
SELECT gen_random_uuid(), 'Quotation ' || g || '.pdf', 'pdf', 'application/pdf', 100000 + g,
       md5(a.id::text || g) || md5(g || a.id::text), 'bench/' || a.id || '/' || g, 'stored',
       'not_scanned', a.created_at, a.created_at,
       CASE WHEN g = 2 AND abs(hashtext(a.id::text)) % 10 = 0 THEN now() END, NULL, NULL,
       a.id, coalesce(a.created_by_id, a.owner_id)
FROM (SELECT id, created_at, created_by_id, owner_id FROM activities_activity
      WHERE type = 'note' ORDER BY id LIMIT 200000) a,
     generate_series(1, 1 + (abs(hashtext(a.id::text)) % 2)) g;

INSERT INTO audit_event (occurred_at, actor_type, actor_id, action, target_type, target_id,
                         subject_user_id, request_id, ip_address, metadata, support_session_id)
SELECT now() - (g * interval '13 seconds'), 'user', u.id,
       (ARRAY['lead.updated', 'opportunity.updated', 'note.created', 'opportunity.stage_changed',
              'auth.login', 'workspace.accessed', 'activity.completed', 'ai.question'])[1 + g % 8],
       'lead', g::text, NULL, 'bench', NULL, '{}'::jsonb, NULL
FROM generate_series(1, 2000000) g JOIN sales_users u ON u.n = 1 + g % 500;

INSERT INTO audit_event (occurred_at, actor_type, actor_id, action, target_type, target_id,
                         subject_user_id, request_id, ip_address, metadata, support_session_id)
SELECT now() - (g * interval '71 minutes'), 'user', u.id,
       (ARRAY['auth.password_changed', 'auth.password_set_by_admin', 'user.created',
              'support_session.started', 'support_session.ended',
              'auth.password_reset_completed'])[1 + g % 6],
       'user', u.id::text, u.id, 'bench', NULL, '{}'::jsonb, NULL
FROM generate_series(1, 6000) g JOIN sales_users u ON u.n = 1 + g % 500;
"""

BIG_TABLES = {
    "pipeline_opportunity",
    "activities_activity",
    "activities_attachment",
    "audit_event",
    "leads_lead",
    "pipeline_negotiation_price",
    "pipeline_stage_history",
}


def seed() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET max_parallel_workers_per_gather = 0", [])
        started = time.perf_counter()
        cursor.execute(f"BEGIN; {SEED} COMMIT;", [])
        print(f"seeded in {time.perf_counter() - started:.1f} s")
        for table in sorted(BIG_TABLES | {"pipeline_pipeline", "pipeline_stage"}):
            cursor.execute(f"VACUUM ANALYZE {table}", [])


def one(sql: str, *params: object) -> object:
    with connection.cursor() as cursor:
        cursor.execute(sql, list(params))
        row = cursor.fetchone()
    return row[0] if row else None


def explain(sql: str) -> tuple[float, str]:
    best, text = float("inf"), ""
    with connection.cursor() as cursor:
        for _ in range(3):
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", [])
            lines = [row[0] for row in cursor.fetchall()]
            runtime = float(re.search(r"Execution Time: ([\d.]+) ms", lines[-1]).group(1))
            if runtime < best:
                best, text = runtime, "\n".join(lines)
    return best, text


def measure(label: str, user: User, path: str, *, verbose: bool, only: str | None) -> None:
    if only and only not in label:
        return
    client = signed_in(user)
    client.get(path)  # warm the caches a real session would have
    with CaptureQueriesContext(connection) as captured:
        started = time.perf_counter()
        response = client.get(path)
        wall = (time.perf_counter() - started) * 1000
    statements = [
        q["sql"]
        for q in captured.captured_queries
        if q["sql"].lstrip().upper().startswith("SELECT") and "django_session" not in q["sql"]
    ]
    worst, worst_sql, flags = 0.0, "", []
    for sql in statements:
        runtime, text = explain(sql)
        for table in BIG_TABLES:
            if re.search(rf"Seq Scan on {table}\b", text):
                flags.append(f"SEQ SCAN {table}")
        if runtime > worst:
            worst, worst_sql = runtime, text
    print(
        f"{label:<44} {response.status_code} {len(captured):>3} queries "
        f"{wall:8.1f} ms wall  slowest statement {worst:7.2f} ms {' '.join(sorted(set(flags)))}"
    )
    if verbose:
        print(worst_sql, "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--only", help="measure only the requests whose label contains this")
    args = parser.parse_args()
    if args.seed:
        seed()
        return
    admin = User.objects.filter(role=Role.ADMIN).first()
    heaviest = User.objects.get(
        pk=one(
            "SELECT owner_id FROM pipeline_opportunity WHERE archived_at IS NULL"
            " GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
        )
    )
    personal = one(
        "SELECT o.pipeline_id FROM pipeline_opportunity o JOIN pipeline_pipeline p"
        " ON p.id = o.pipeline_id WHERE p.owner_id = %s GROUP BY 1 ORDER BY count(*) DESC LIMIT 1",
        heaviest.pk,
    )
    negotiating = one(
        "SELECT id FROM pipeline_stage WHERE pipeline_id = %s AND is_negotiation", personal
    )
    noted = one(
        "SELECT a.opportunity_id FROM activities_attachment t JOIN activities_activity a"
        " ON a.id = t.note_id WHERE a.opportunity_id IS NOT NULL AND a.archived_at IS NULL"
        " GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    )
    noted_owner = User.objects.get(
        pk=one("SELECT owner_id FROM pipeline_opportunity WHERE id = %s", noted)
    )
    priced = one(
        "SELECT opportunity_id FROM pipeline_negotiation_price n JOIN pipeline_opportunity o"
        " ON o.id = n.opportunity_id WHERE o.owner_id = %s LIMIT 1",
        heaviest.pk,
    )
    counts = one(
        "SELECT json_build_object('pipelines', (SELECT count(*) FROM pipeline_pipeline),"
        " 'opportunities', (SELECT count(*) FROM pipeline_opportunity),"
        " 'heaviest_opportunities',"
        " (SELECT count(*) FROM pipeline_opportunity WHERE owner_id = %s),"
        " 'prices', (SELECT count(*) FROM pipeline_negotiation_price),"
        " 'attachments', (SELECT count(*) FROM activities_attachment),"
        " 'audit_events', (SELECT count(*) FROM audit_event))",
        heaviest.pk,
    )
    print(f"volumes: {counts}\n")
    me = "/api/v1/workspaces/me"
    for label, user, path in [
        ("pipelines (heaviest owner)", heaviest, f"{me}/pipelines"),
        ("board, default pipeline (heaviest)", heaviest, f"{me}/pipeline-board"),
        (
            "board, personal pipeline (heaviest)",
            heaviest,
            f"{me}/pipeline-board?pipeline={personal}",
        ),
        (
            "list: personal negotiation stage (heaviest)",
            heaviest,
            f"{me}/opportunities?pipeline={personal}&stage={negotiating}",
        ),
        ("deal page (heaviest)", heaviest, f"{me}/opportunities/{priced}"),
        (
            "negotiated prices (heaviest)",
            heaviest,
            f"{me}/opportunities/{priced}/negotiated-prices",
        ),
        ("deal notes with files", noted_owner, f"{me}/opportunities/{noted}/notes"),
        ("dashboard (heaviest)", heaviest, f"{me}/dashboard"),
        ("pipelines (organisation, 1,501)", admin, "/api/v1/workspaces/all/pipelines"),
        ("board, default (organisation)", admin, "/api/v1/workspaces/all/pipeline-board"),
        ("dashboard (organisation)", admin, "/api/v1/workspaces/all/dashboard"),
        ("security events (2M audit rows)", admin, "/api/v1/admin/security-events"),
        ("user detail", admin, f"/api/v1/admin/users/{heaviest.pk}"),
    ]:
        measure(label, user, path, verbose=args.verbose, only=args.only)


if __name__ == "__main__":
    main()
