# ruff: noqa: T201, E402
# mypy: ignore-errors
# A diagnostics command (prints its report), not application code: it EXPLAINs SQL the
# application itself just ran, verbatim.
"""Phase 10 database review: run a request through the whole stack, capture its SQL, and
EXPLAIN (ANALYZE, BUFFERS) the slowest statements:

    DATABASE_URL=.../arkray_bench_rag uv run python tests/performance/explain_hot.py \\
        --path /api/v1/workspaces/me/dashboard --as heaviest [--top 2]

`--as`: heaviest (the owner with most leads), admin (organisation scope via `all` paths).
"""

from __future__ import annotations

import argparse
import os
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--as", dest="who", default="heaviest", choices=["heaviest", "admin"])
    parser.add_argument("--top", type=int, default=2)
    args = parser.parse_args()
    if args.who == "admin":
        user = User.objects.filter(role=Role.ADMIN).first()
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT owner_id FROM leads_lead WHERE archived_at IS NULL"
                " GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            )
            user = User.objects.get(pk=cursor.fetchone()[0])
    client = signed_in(user)
    client.get(args.path)  # warm
    with CaptureQueriesContext(connection) as captured:
        response = client.get(args.path)
    print(f"{args.path} as {args.who}: {response.status_code}, {len(captured)} queries")
    queries = sorted(captured.captured_queries, key=lambda q: float(q["time"]), reverse=True)
    for query in queries[: args.top]:
        sql = query["sql"]
        print(f"\n--- {float(query['time']) * 1000:.1f} ms\n{sql[:600]}")
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
            print("\n".join(row[0] for row in cursor.fetchall()))


if __name__ == "__main__":
    main()
