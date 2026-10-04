# ruff: noqa: T201, E402
# mypy: ignore-errors
# A diagnostics command (prints its report), not application code.
"""Phase 10 performance baseline: every major flow through the whole Django stack
(middleware, authentication, CSRF, permissions, serializers, the real SQL), in-process (no
network or proxy in the timings), against the 1M-lead benchmark database with Ask Arkray's
chunks (docs/reliability.md#performance-baseline):

    DATABASE_URL=.../arkray_bench_rag uv run python manage.py migrate
    DATABASE_URL=.../arkray_bench_rag uv run python tests/performance/bench_api.py [--runs 40]

For each flow and scope (a typical owner, the heaviest owner, an administrator in the
heaviest owner's workspace, the organisation): p50 / p95 / p99 latency in ms, queries per
request, the slowest single SQL statement and the response size. Throttles are lifted and
the production password hasher (Argon2) is used, so sign-in is timed as in production.
Writes (an opportunity moving between two stages, routed Ask questions) change the
benchmark database; nothing else does.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
for scope in ("ANON", "USER", "AUTH", "SEARCH", "ASK"):
    os.environ[f"API_THROTTLE_{scope}"] = "1000000/min"

import django

django.setup()

# Django times each query with time.monotonic(), which ticks every ~15.6 ms on Windows:
# per-query timings would read 0, 15 or 31 ms. The high-resolution counter instead.
time.monotonic = time.perf_counter

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from arkray.ai import retrieval
from arkray.core.access import AccessScope
from arkray.identity.models import Role, User
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity, Stage
from tests.helpers import signed_in

API = "/api/v1"
PASSWORD = "Bench-Phase10-2026!"


def pct(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def busiest_lead(owner: User | None) -> Lead:
    """A visible lead with the longest timeline (owner's, or anyone's for the organisation),
    so the detail and timeline flows return real content."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT t.lead_id FROM activities_timeline_entry t"
            " JOIN leads_lead l ON l.id = t.lead_id"
            " WHERE l.archived_at IS NULL AND (%s::uuid IS NULL OR l.owner_id = %s::uuid)"
            " GROUP BY 1 ORDER BY count(*) DESC LIMIT 1",
            [owner.pk if owner else None, owner.pk if owner else None],
        )
        return Lead.objects.get(pk=cursor.fetchone()[0])


def measure(label: str, call, runs: int) -> dict:
    call()
    call()  # warm-up: connection, caches, plans
    times, counts, slowest, sizes = [], [], [], []
    for _ in range(runs):
        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            response = call()
            times.append((time.perf_counter() - started) * 1000)
        assert response.status_code < 400, (label, response.status_code, response.content[:300])
        queries = [q for q in captured.captured_queries if q["sql"] not in ("BEGIN", "COMMIT")]
        counts.append(len(queries))
        slowest.append(max((float(q["time"]) * 1000 for q in queries), default=0.0))
        sizes.append(len(response.content))
    row = {
        "flow": label,
        "p50": statistics.median(times),
        "p95": pct(times, 0.95),
        "p99": pct(times, 0.99),
        "queries": max(counts),
        "slowest_sql": statistics.median(slowest),
        "bytes": int(statistics.median(sizes)),
    }
    print(
        f"{label:58} p50 {row['p50']:7.1f}  p95 {row['p95']:7.1f}  p99 {row['p99']:7.1f} ms"
        f"  | {row['queries']:2d} queries, slowest SQL {row['slowest_sql']:6.1f} ms"
        f"  | {row['bytes'] / 1024:6.1f} KB",
        flush=True,
    )
    return row


def main(runs: int) -> None:
    settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.Argon2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
    settings.AI_ENABLED = True
    settings.AI_LLM_PROVIDER = "none"
    settings.AI_EMBEDDING_PROVIDER = "local"
    # Owners with every kind of record (the benchmark seeded activities, notes and chunks for
    # 60 of its 500 salespeople): the heaviest, and the median of those as "typical".
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        cursor.execute(
            "SELECT owner_id, count(*) FROM leads_lead WHERE archived_at IS NULL AND owner_id IN"
            " (SELECT DISTINCT owner_id FROM ai_knowledge_chunk) GROUP BY 1 ORDER BY 2 DESC"
        )
        owners = cursor.fetchall()
        cursor.execute("RESET statement_timeout")
    heavy = User.objects.get(pk=owners[0][0])
    typical = User.objects.get(pk=owners[len(owners) // 2][0])
    admin = User.objects.filter(role=Role.ADMIN).first()
    print(
        f"leads={Lead.objects.count()} opportunities={Opportunity.objects.count()}"
        f" heavy owner {owners[0][1]} leads, typical {owners[len(owners) // 2][1]}",
        flush=True,
    )
    typical.set_password(PASSWORD)
    typical.save(update_fields=["password"])

    clients = {
        "typical owner": (signed_in(typical), "me", typical),
        "heaviest owner": (signed_in(heavy), "me", heavy),
        "admin in heaviest owner's workspace": (signed_in(admin), str(heavy.pk), heavy),
        "organisation (admin)": (signed_in(admin), "all", None),
    }
    rows = []

    # Sign-in: Argon2 + session + throttle bookkeeping.
    def login():
        return APIClient().post(
            f"{API}/auth/login", {"email": typical.email, "password": PASSWORD}, format="json"
        )

    rows.append(measure("login (Argon2)", login, max(10, runs // 2)))

    for scope_name, (client, segment, owner) in clients.items():
        ws = f"{API}/workspaces/{segment}"
        lead = busiest_lead(owner)
        first = client.get(f"{ws}/leads", {"page_size": 25}).json()
        cursor = parse_qs(urlsplit(first["next"]).query)["cursor"][0] if first.get("next") else None
        flows = [
            ("dashboard", lambda c=client, w=ws: c.get(f"{w}/dashboard")),
            ("lead list (25)", lambda c=client, w=ws: c.get(f"{w}/leads", {"page_size": 25})),
            (
                "lead list page 2 (sealed cursor)",
                lambda c=client, w=ws, k=cursor: c.get(
                    f"{w}/leads", {"page_size": 25, "cursor": k}
                ),
            ),
            (
                "lead list search q=Rahul",
                lambda c=client, w=ws: c.get(f"{w}/leads", {"q": "Rahul", "page_size": 25}),
            ),
            ("lead detail", lambda c=client, w=ws, ld=lead: c.get(f"{w}/leads/{ld.pk}")),
            (
                "lead timeline",
                lambda c=client, w=ws, ld=lead: c.get(f"{w}/leads/{ld.pk}/timeline"),
            ),
            ("pipeline board", lambda c=client, w=ws: c.get(f"{w}/pipeline-board")),
            (
                "opportunity list (25)",
                lambda c=client, w=ws: c.get(f"{w}/opportunities", {"page_size": 25}),
            ),
            (
                "activities list (25)",
                lambda c=client, w=ws: c.get(f"{w}/activities", {"page_size": 25}),
            ),
            (
                "global search q=follow quotation",
                lambda c=client, w=ws: c.get(f"{w}/search", {"q": "follow quotation"}),
            ),
            (
                "Ask Arkray structured (routed)",
                lambda c=client, w=ws: c.post(
                    f"{w}/ask", {"question": "What is the pipeline value?"}, format="json"
                ),
            ),
        ]
        for name, call in flows:
            rows.append(measure(f"{name} — {scope_name}", call, runs))

    admin_client = clients["organisation (admin)"][0]
    rows.append(
        measure(
            "admin users list",
            lambda: admin_client.get(f"{API}/admin/users", {"page_size": 25}),
            runs,
        )
    )

    # An opportunity moving between two open stages (history, timeline, audit, outbox).
    stages = list(
        Stage.objects.filter(pipeline__is_default=True, category="open").order_by("position")[:2]
    )
    moving = Opportunity.objects.filter(
        owner=typical, status="open", archived_at__isnull=True, stage__in=stages
    ).first()
    client = clients["typical owner"][0]
    state = {"index": 0}

    def move():
        moving.refresh_from_db(fields=["version", "stage_id"])
        target = stages[1] if moving.stage_id == stages[0].pk else stages[0]
        state["index"] += 1
        return client.post(
            f"{API}/workspaces/me/opportunities/{moving.pk}/move",
            {"stage": str(target.pk), "version": moving.version},
            format="json",
        )

    rows.append(measure("opportunity stage transition (write)", move, runs))

    # RAG retrieval (embedding + candidate SQL + live re-verification), typical owner.
    class Result:
        status_code = 200
        content = b""

    def rag():
        retrieval.retrieve(AccessScope.own(typical.pk), "What concerns did the customer raise?")
        return Result()

    rows.append(measure("RAG retrieval (embed + SQL + verify) — typical owner", rag, runs))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=40)
    main(parser.parse_args().runs)
