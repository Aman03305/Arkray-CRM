# ruff: noqa: T201, E501, S104, S107, S324, S501, S603, S607, DTZ006, RUF005
# mypy: ignore-errors
# A diagnostics command (prints its report), not application code: random choices pick
# workload steps, not secrets; long lines are SQL and report formats; it talks to a local stack
# with a self-signed certificate over IPv4 (local_address 0.0.0.0 picks the address family, it
# binds no server), runs fixed docker/powershell commands, hashes emails only to sample users
# deterministically, and prints local wall-clock times.
"""Capacity test: N concurrent *active* CRM users against the production-shaped stack
(nginx TLS -> gunicorn / Next.js, PostgreSQL, Redis, Celery), docs/reliability.md#capacity.

An *active user* is one signed-in person using the CRM at human pace: every virtual user
loops "think (gamma-distributed, mean --think-mean seconds) -> do one thing". One thing is
a page view (the several API calls the real page makes at once: dashboard, board, a deal
with its history and notes, ...) or a smaller write (a note, a task, a meeting, a stage
move, a new opportunity). It is NOT a closed loop without pauses (that measures how fast
one connection can hammer, see load_test.py), so "100 users" means 100 people clicking, not
100 threads in a tight loop. `--think-mean 0.3` gives a stress run for headroom.

Sub-commands (all talk HTTP to --base; only `prepare`, `verify` and the monitors use SQL):

    prepare   pick N salespeople (+ admins) in the benchmark database, give them one known
              password hash (the copy only), write the account file
    run       log everybody in (paced under the 20/min sign-in limit; sessions are saved and
              reused), then ramp 10/25/50/75/100 users, warm up, hold, quiesce, and verify:
              duplicates, orphans, lost updates, impossible states, dashboard arithmetic,
              cross-user leaks (tokens in every response), cross-user attack probes
    races     targeted concurrency: racing stage moves, negotiation entries and price
              revisions, same-key creates at 2/20/100 parallel, same key + other payload
    search    Global Search alone: p50/p95/p99 per term class (rare, common, instrument,
              customer, Unicode, pathological) at --vus concurrent users
    ask       Ask Arkray alone (structured and retrieval, provider disabled)
    isolate   cache/workspace isolation: two users, an admin switching users, support
              sessions entering and leaving, all concurrent, every response token-checked

Every virtual user owns a canary token (ZQ007Z for user 7) written into the customer names
and notes it creates; every response read by a user is scanned for tokens, and a token that
is not the viewed workspace's owner's is a cross-user leak. The numbers describe THIS
machine, this topology and this data, not a universal capacity.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

API = "/api/v1"
TOKEN_RE = re.compile(r"ZQ(\d{3})Z")
CURSOR_RE = re.compile(r"gAAAAA[A-Za-z0-9_\-=%]+")
INSTRUMENTS = ["Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T", "PCBA with Printer"]
SEARCH_RARE = "ZQ{token}Z"
SEARCH_COMMON = ["analyser", "follow", "quotation", "price", "hospital", "pune", "demo", "call"]
SEARCH_UNICODE = ["Müller", "São Paulo", "naïve café", "Zoë", "डॉक्टर"]
SEARCH_PATHOLOGICAL = ["a", "the", "%", "e", "in", "--", "o"]
SEARCH_LONG = "x" * 300

# What a page view is made of: (name, weight). The weights are the product owner's load mix.
ACTIONS = [
    ("dashboard", 20),
    ("lead_list", 15),
    ("lead_detail", 10),
    ("pipeline_board", 15),
    ("opportunity_detail", 10),
    ("search", 10),
    ("tasks_meetings", 5),
    ("notes", 5),
    ("writes", 5),
    ("other", 5),
]
ACTION_NAMES = [name for name, weight in ACTIONS for _ in range(weight)]


def percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return float("nan")
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def now() -> float:
    return time.perf_counter()


# --- recording --------------------------------------------------------------------------------
@dataclass
class Sample:
    at: float
    stage: str
    name: str
    status: int
    ms: float
    user: int


class Recorder:
    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self.stage = "setup"
        self.leaks: list[str] = []
        self.probes: Counter[str] = Counter()
        self.probe_leaks: list[str] = []
        self.dashboard_mismatches: list[str] = []
        self.notes: list[str] = []
        self.failures: list[dict[str, Any]] = []
        self.sessions_ended: list[str] = []

    def add(self, name: str, status: int, ms: float, user: int) -> None:
        self.samples.append(Sample(time.time(), self.stage, name, status, ms, user))

    @staticmethod
    def classify(status: int, expected: tuple[int, ...]) -> str:
        if status == 429:
            return "throttled"
        if status in expected:
            return "ok"
        if status == 0:
            return "network_or_timeout"
        if status >= 500:
            return "server_error"
        return "unexpected_4xx"

    def summary(self, stage: str | None, names: list[str] | None = None) -> dict[str, Any]:
        """Per request name: n, p50, p95, p99, max, statuses; plus totals."""
        rows: dict[str, list[float]] = defaultdict(list)
        statuses: dict[str, Counter[int]] = defaultdict(Counter)
        for s in self.samples:
            if (stage is not None and s.stage != stage) or (names and s.name not in names):
                continue
            if s.status == 429:
                statuses[s.name][429] += 1
                continue
            rows[s.name].append(s.ms)
            statuses[s.name][s.status] += 1
        out: dict[str, Any] = {}
        for name, values in sorted(rows.items()):
            ordered = sorted(values)
            out[name] = {
                "n": len(ordered),
                "p50": round(statistics.median(ordered), 1),
                "p95": round(percentile(ordered, 0.95), 1),
                "p99": round(percentile(ordered, 0.99), 1),
                "max": round(ordered[-1], 1),
                "statuses": dict(statuses[name]),
            }
        return out


# --- one virtual user -------------------------------------------------------------------------
class Run:
    """State shared by every virtual user of one run."""

    def __init__(self, args: argparse.Namespace, accounts: dict[str, Any]) -> None:
        self.args = args
        self.accounts = accounts
        self.rec = Recorder()
        self.run_id = uuid.uuid4().hex[:6]
        self.target_active = 0
        self.stop = asyncio.Event()
        self.pool: dict[int, dict[str, list[Any]]] = {}  # other users' ids, for attack probes
        self.vus: list[VU] = []
        self.creates: list[dict[str, Any]] = []
        self.started_db = ""  # DB clock at the start (set by prepare_run)
        self.think_mean = args.think_mean


class VU:
    def __init__(self, run: Run, account: dict[str, Any], position: int) -> None:
        self.run = run
        self.account = account
        self.position = position
        self.index = account["index"]  # token number; -1 for admins
        self.is_admin = account["role"] == "admin"
        self.token = f"ZQ{self.index:03d}Z" if self.index >= 0 else ""
        base = run.args.base
        transport = httpx.AsyncHTTPTransport(
            verify=False,
            local_address="0.0.0.0",  # IPv4 only: "localhost" tries ::1 first on this host
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=6),
            retries=0,
        )
        self.client = httpx.AsyncClient(
            base_url=base,
            transport=transport,
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers={"Origin": base, "Referer": base + "/", "Host": "localhost:8443"},
        )
        self.ws = "all" if self.is_admin else "me"
        self.view_as: dict[str, Any] | None = None  # an admin looking into a user's workspace
        self.own: dict[str, Any] = {}  # an administrator's own ids while viewing a user
        self.leads: list[str] = []
        self.cursor: str | None = None
        self.opps: dict[str, dict[str, Any]] = {}  # id -> {version, stage, lead, pipeline}
        self.pipeline: dict[str, Any] | None = None
        self.open_stages: list[str] = []
        self.neg_stage: str | None = None
        self.serial = 0
        self.expected_leads: int | None = None
        self.new_creates = 0
        self.moves_ok: Counter[str] = Counter()
        self.version_expected: dict[str, int] = {}
        self.notes_ok = 0
        self.tasks_ok = 0
        self.meetings_ok = 0
        self.patches_ok: Counter[str] = Counter()
        self.negotiations_ok = 0
        self.errors: Counter[str] = Counter()
        self.signed_in = False
        self.need_login = False

    # -- plumbing --------------------------------------------------------------------------
    def csrf(self) -> str:
        for name in ("__Host-arkray_csrftoken", "arkray_csrftoken"):
            value = self.client.cookies.get(name)
            if value:
                return value
        return ""

    def workspace(self) -> str:
        if self.view_as is not None:
            return self.view_as["id"]
        return self.ws

    def expected_token(self) -> str | None:
        """The one token this view may contain; None = anything (organisation-wide)."""
        if self.view_as is not None:
            return f"ZQ{self.view_as['index']:03d}Z"
        if self.is_admin:
            return None
        return self.token

    def path(self, tail: str, workspace: str | None = None) -> str:
        return f"{API}/workspaces/{workspace or self.workspace()}/{tail}"

    async def call(
        self,
        name: str,
        method: str,
        url: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        scan: bool = True,
        token_override: str | None = "-",
    ) -> httpx.Response | None:
        allowed_at_send = self.expected_token() if token_override == "-" else token_override
        merged = dict(headers or {})
        if method != "GET":
            merged["X-CSRFToken"] = self.csrf()
        started = now()
        try:
            response = await self.client.request(
                method, url, json=body, params=params, headers=merged
            )
            status = response.status_code
        except httpx.HTTPError as error:
            self.run.rec.add(name, 0, (now() - started) * 1000, self.position)
            self.errors[f"{name}:{type(error).__name__}"] += 1
            return None
        elapsed = (now() - started) * 1000
        self.run.rec.add(name, status, elapsed, self.position)
        if status == 401 and name not in ("auth_me", "login") and not self.need_login:
            self.need_login = True
            self.run.rec.sessions_ended.append(
                f"{self.account['email']} at {self.run.rec.stage}: {name}"
            )
        kind = Recorder.classify(status, expected)
        if kind not in ("ok", "throttled"):
            self.errors[f"{name}:{status}"] += 1
            if len(self.run.rec.failures) < 200:
                self.run.rec.failures.append(
                    {
                        "name": name,
                        "status": status,
                        "ms": round(elapsed),
                        "stage": self.run.rec.stage,
                        "who": self.account["email"],
                        "admin": self.is_admin,
                        "view_as": bool(self.view_as),
                        "url": url[:100],
                        "params": {k: str(v)[:40] for k, v in (params or {}).items()},
                        "body": response.text[:160],
                    }
                )
        if scan and status < 400:
            allowed = allowed_at_send
            if allowed is not None:
                text = response.text
                if "search" in name:  # the response echoes the query (and its terms): not data
                    try:
                        data = response.json()
                        data.pop("query", None)
                        data.pop("terms", None)
                        text = json.dumps(data)
                    except ValueError:
                        pass
                self.scan(name, url, text, allowed)
        return response

    def scan(self, name: str, url: str, text: str, allowed: str) -> None:
        # Page links carry sealed cursors: random URL-safe base64 (Fernet, "gAAAAA..."), which
        # contain a "ZQnnnZ" by chance about once in 25,000 board responses (the first final
        # run's two hits: one was ZQ415Z, a token no user has). Only data is scanned.
        text = CURSOR_RE.sub("", text)
        for found in set(TOKEN_RE.findall(text)):
            token = f"ZQ{found}Z"
            if token != allowed:
                self.run.rec.leaks.append(
                    f"{self.account['email']} ({allowed}) saw {token} in {name} {url[:90]}"
                )

    async def login(self, saved: dict[str, str] | None) -> bool:
        if saved:
            for key, value in saved.items():
                # http.cookiejar files a host without a dot as "<host>.local"
                self.client.cookies.set(key, value, domain="localhost.local")
            me = await self.call("auth_me", "GET", f"{API}/auth/me", scan=False)
            if me is not None and me.status_code == 200:
                self.signed_in = True
                return True
            self.client.cookies.clear()
        await self.call("auth_csrf", "GET", f"{API}/auth/csrf", scan=False, expected=(200, 204))
        # Everyone signs in from this one address, under the 20-a-minute sign-in limit (R77):
        # wait as told, as long as it takes (15 minutes at most).
        deadline = time.time() + 900
        while time.time() < deadline:
            response = await self.call(
                "login",
                "POST",
                f"{API}/auth/login",
                body={"email": self.account["email"], "password": self.run.accounts["password"]},
                scan=False,
                expected=(200,),
            )
            if response is None:
                await asyncio.sleep(2)
                continue
            if response.status_code == 429:
                await asyncio.sleep(
                    float(response.headers.get("Retry-After", "20")) + random.random()
                )
                continue
            if response.status_code == 403:  # CSRF cookie lost: fetch a new one and retry
                await self.call(
                    "auth_csrf", "GET", f"{API}/auth/csrf", scan=False, expected=(200, 204)
                )
                continue
            self.signed_in = response.status_code == 200
            return self.signed_in
        return False

    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self.client.cookies.jar}

    # -- priming ---------------------------------------------------------------------------
    async def prime(self) -> None:
        board = await self.call("setup_board", "GET", self.path("pipeline-board"))
        if board is not None and board.status_code == 200:
            data = board.json()
            self.pipeline = data.get("pipeline")
            for column in data.get("columns", []):
                stage = column["stage"]
                if stage.get("category") == "open":
                    if stage.get("is_negotiation"):
                        self.neg_stage = stage["id"]
                    else:
                        self.open_stages.append(stage["id"])
                for card in column.get("cards", [])[:6]:
                    self.opps[card["id"]] = {
                        "version": card["version"],
                        "stage": stage["id"],
                        "lead": (card.get("lead") or {}).get("id"),
                    }
                    self.version_expected[card["id"]] = card["version"]
        page = await self.call("setup_leads", "GET", self.path("leads"), params={"page_size": 50})
        if page is not None and page.status_code == 200:
            data = page.json()
            self.leads = [row["id"] for row in data["results"]]
            nxt = data.get("next")
            if nxt and "cursor=" in nxt:
                self.cursor = unquote(re.search(r"cursor=([^&]+)", nxt).group(1))
        dash = await self.call("setup_dashboard", "GET", self.path("dashboard"))
        if dash is not None and dash.status_code == 200:
            self.expected_leads = leads_total(dash.json())
        self.run.pool[self.position] = {
            "opps": list(self.opps)[:5],
            "leads": self.leads[:5],
            "user_id": self.account["id"],
        }

    # -- page views --------------------------------------------------------------------------
    async def page(self, name: str, calls: list[Any]) -> None:
        started = now()
        results = await asyncio.gather(*calls)
        worst = max((r.status_code for r in results if r is not None), default=0)
        failed = any(r is None for r in results)
        self.run.rec.add(
            f"page:{name}", 0 if failed else worst, (now() - started) * 1000, self.position
        )

    def get(self, name: str, tail: str, **params: Any) -> Any:
        return self.call(name, "GET", self.path(tail), params=params or None)

    async def do_dashboard(self) -> None:
        await self.page("dashboard", [self.get("dashboard", "dashboard")])

    async def do_lead_list(self) -> None:
        calls = [self.get("lead_list", "leads", page_size=25)]
        if self.cursor and random.random() < 0.3:
            calls.append(self.get("lead_list_page2", "leads", page_size=50, cursor=self.cursor))
        await self.page("lead_list", calls)

    async def do_lead_detail(self) -> None:
        if not self.leads:
            return await self.do_dashboard()
        lead = random.choice(self.leads)
        await self.page(
            "lead_detail",
            [
                self.get("lead_detail", f"leads/{lead}"),
                self.get("lead_timeline", f"leads/{lead}/timeline"),
            ],
        )

    async def do_pipeline_board(self) -> None:
        await self.page(
            "pipeline_board",
            [
                self.get("pipeline_board", "pipeline-board"),
                self.get("pipelines", "pipelines"),
                self.get("pipeline_summary", "pipeline-summary"),
            ],
        )

    async def do_opportunity_detail(self) -> None:
        if not self.opps:
            return await self.do_pipeline_board()
        opp = random.choice(list(self.opps))
        lead = self.opps[opp].get("lead")
        calls = [
            self.get("opportunity_detail", f"opportunities/{opp}"),
            self.get("opportunity_history", f"opportunities/{opp}/history", page_size=25),
            self.get("negotiated_prices", f"opportunities/{opp}/negotiated-prices", page_size=50),
        ]
        if lead:
            calls.append(self.get("lead_timeline", f"leads/{lead}/timeline"))
        await self.page("opportunity_detail", calls)

    def search_term(self) -> str:
        roll = random.random()
        if roll < 0.45:
            return random.choice(SEARCH_COMMON)
        if roll < 0.7 and self.token:
            return self.token
        if roll < 0.8:
            return random.choice(INSTRUMENTS)
        if roll < 0.9:
            return random.choice(SEARCH_UNICODE)
        return f"LT{self.run.run_id}"

    async def do_search(self) -> None:
        await self.page("search", [self.get("search", "search", q=self.search_term())])

    async def do_tasks_meetings(self) -> None:
        if random.random() < 0.7:
            await self.page(
                "tasks_meetings",
                [
                    self.get(
                        "activities_open_tasks",
                        "activities",
                        type="task",
                        status="open",
                        page_size=25,
                    ),
                    self.get("activity_summary", "activity-summary"),
                    self.get(
                        "activities_upcoming_meetings",
                        "activities",
                        type="meeting",
                        upcoming="true",
                        page_size=25,
                    ),
                ],
            )
            return
        await self.create_activity("meeting" if random.random() < 0.4 else "task")

    async def do_notes(self) -> None:
        if random.random() < 0.5 and self.leads:
            lead = random.choice(self.leads)
            await self.page("notes", [self.get("lead_timeline", f"leads/{lead}/timeline")])
            return
        await self.create_activity("note")

    async def create_activity(self, kind: str) -> None:
        if not self.opps:
            return
        opp = random.choice(list(self.opps))
        stamp = datetime.now(UTC) + timedelta(days=random.randint(1, 6), hours=random.randint(0, 8))
        body: dict[str, Any] = {"type": kind, "opportunity": opp}
        if kind == "note":
            body["description"] = f"Load test note {self.token}: discussed pricing, follow up."
        elif kind == "task":
            body["title"] = f"Follow up {self.token}"
            body["description"] = "Call back about the quotation."
            body["due_at"] = stamp.isoformat()
        else:
            body["title"] = f"Demo {self.token}"
            body["starts_at"] = stamp.isoformat()
            body["ends_at"] = (stamp + timedelta(hours=1)).isoformat()
        started = now()
        response = await self.call(
            f"create_{kind}",
            "POST",
            self.path("activities"),
            body=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
            expected=(201,),
        )
        if response is not None and response.status_code == 201:
            if kind == "note":
                self.notes_ok += 1
            elif kind == "task":
                self.tasks_ok += 1
            else:
                self.meetings_ok += 1
        self.run.rec.add(
            f"page:create_{kind}",
            response.status_code if response else 0,
            (now() - started) * 1000,
            self.position,
        )

    async def do_writes(self) -> None:
        roll = random.random()
        if self.is_admin:
            return await self.do_dashboard()
        if roll < 0.4:
            await self.create_opportunity()
        elif roll < 0.8:
            await self.move_stage()
        elif roll < 0.9:
            await self.negotiate()
        else:
            await self.patch_lead()

    def opportunity_body(self, serial: int) -> dict[str, Any]:
        return {
            "customer_name": f"LT{self.run.run_id} {self.index:03d}-{serial} {self.token}",
            "account_name": f"{self.token} Diagnostics {serial}",
            "instrument_name": random.choice(INSTRUMENTS),
            "value": f"{random.randint(100, 4000) * 1000}.00",
            "expected_cpt": "Rs 4.5 per test",
            "description": "Created by the capacity test.",
        }

    async def create_opportunity(self, *, retry: bool = True) -> None:
        self.serial += 1
        key = str(uuid.uuid4())
        body = self.opportunity_body(self.serial)
        logical = {
            "vu": self.position,
            "user": self.account["id"],
            "key": key,
            "name": body["customer_name"],
            "opp": None,
            "replays": 0,
            "created": 0,
        }
        self.run.creates.append(logical)
        started = now()
        response = await self.call(
            "create_opportunity",
            "POST",
            self.path("opportunities"),
            body=body,
            headers={"Idempotency-Key": key},
            expected=(201,),
        )
        if response is not None and response.status_code == 201:
            data = response.json()
            logical["opp"] = data["id"]
            logical["created"] += 1
            if response.headers.get("Idempotent-Replayed") != "true":
                self.new_creates += 1
            self.opps[data["id"]] = {
                "version": data["version"],
                "stage": stage_of(data),
                "lead": lead_of(data),
            }
            self.version_expected[data["id"]] = data["version"]
        # A network retry of the same logical submission (timeout and resend, or a double
        # click): same key, same body. Must replay the first result, never create again.
        if retry and (response is None or random.random() < 0.06):
            mode = random.random()
            if mode < 0.5 or response is None:
                again = [self.retry_create(body, key, logical)]
            else:
                again = [
                    self.retry_create(body, key, logical),
                    self.retry_create(body, key, logical),
                ]
            await asyncio.gather(*again)
        self.run.rec.add(
            "page:create_opportunity",
            response.status_code if response else 0,
            (now() - started) * 1000,
            self.position,
        )
        await self.check_dashboard_after_create()

    async def retry_create(self, body: dict[str, Any], key: str, logical: dict[str, Any]) -> None:
        response = await self.call(
            "create_opportunity_retry",
            "POST",
            self.path("opportunities"),
            body=body,
            headers={"Idempotency-Key": key},
            expected=(201,),
        )
        if response is not None and response.status_code == 201:
            data = response.json()
            if logical["opp"] is None:
                logical["opp"] = data["id"]
                if response.headers.get("Idempotent-Replayed") != "true":
                    self.new_creates += 1
            elif logical["opp"] != data["id"]:
                self.run.rec.notes.append(f"retry of {key} returned another opportunity")
            logical["replays"] += 1 if response.headers.get("Idempotent-Replayed") == "true" else 0
            if response.headers.get("Idempotent-Replayed") != "true" and logical["created"]:
                self.run.rec.notes.append(f"retry of {key} was not flagged as a replay")

    async def check_dashboard_after_create(self) -> None:
        """Read-your-write and no double counting: this workspace's lead total is exactly
        the baseline plus this user's distinct creates (nobody else writes into it)."""
        if self.expected_leads is None or self.view_as is not None:
            return
        response = await self.get("dashboard_check", "dashboard")
        if response is None or response.status_code != 200:
            return
        total = leads_total(response.json())
        want = self.expected_leads + self.new_creates
        if total != want:
            self.run.rec.dashboard_mismatches.append(
                f"{self.account['email']}: total leads {total}, expected {want}"
            )

    async def move_stage(self) -> None:
        if not self.opps or len(self.open_stages) < 2:
            return await self.do_pipeline_board()
        opp = random.choice(list(self.opps))
        info = self.opps[opp]
        targets = [s for s in self.open_stages if s != info["stage"]]
        if not targets:
            return
        target = random.choice(targets)
        response = await self.call(
            "move_stage",
            "POST",
            self.path(f"opportunities/{opp}/move"),
            body={"stage": target, "version": info["version"]},
            expected=(200, 409),
        )
        if response is not None and response.status_code == 200:
            data = response.json()
            info.update(version=data["version"], stage=target)
            self.moves_ok[opp] += 1
            self.version_expected[opp] = data["version"]
        elif response is not None and response.status_code == 409:
            await self.refresh_opp(opp)

    async def refresh_opp(self, opp: str) -> None:
        response = await self.get("opportunity_detail", f"opportunities/{opp}")
        if response is not None and response.status_code == 200:
            data = response.json()
            self.opps[opp].update(version=data["version"], stage=stage_of(data))

    async def negotiate(self) -> None:
        if not self.neg_stage or not self.opps:
            return await self.move_stage()
        opp = random.choice(list(self.opps))
        info = self.opps[opp]
        if info["stage"] == self.neg_stage:
            # a revision of the agreed price
            response = await self.call(
                "negotiation_revision",
                "POST",
                self.path(f"opportunities/{opp}/negotiated-prices"),
                body={
                    "version": info["version"],
                    "price": f"{random.randint(100, 4000) * 1000}.37",
                    "agreed_cpt": "Rs 4.2 per test",
                },
                expected=(201, 409),
            )
            if response is not None and response.status_code in (201, 409):
                await self.refresh_opp(opp)
            return
        response = await self.call(
            "negotiation_entry",
            "POST",
            self.path(f"opportunities/{opp}/move"),
            body={
                "stage": self.neg_stage,
                "version": info["version"],
                "negotiated_price": f"{random.randint(100, 4000) * 1000}.25",
                "agreed_cpt": "Rs 4.0 per test",
            },
            expected=(200, 409),
        )
        if response is not None and response.status_code == 200:
            data = response.json()
            info.update(version=data["version"], stage=self.neg_stage)
            self.moves_ok[opp] += 1
            self.negotiations_ok += 1
            self.version_expected[opp] = data["version"]
        elif response is not None and response.status_code == 409:
            await self.refresh_opp(opp)

    async def patch_lead(self) -> None:
        if not self.leads:
            return
        lead = random.choice(self.leads)
        current = await self.get("lead_detail", f"leads/{lead}")
        if current is None or current.status_code != 200:
            return
        response = await self.call(
            "patch_lead",
            "PATCH",
            self.path(f"leads/{lead}"),
            body={
                "version": current.json()["version"],
                "city": random.choice(["Pune", "Mumbai", "Delhi"]),
            },
            expected=(200, 409),
        )
        if response is not None and response.status_code == 200:
            self.patches_ok[lead] += 1

    async def do_other(self) -> None:
        roll = random.random()
        today = datetime.now(UTC).date()
        if roll < 0.25:
            await self.page("me", [self.call("auth_me", "GET", f"{API}/auth/me", scan=False)])
        elif roll < 0.5:
            await self.page(
                "options",
                [
                    self.call(
                        "opportunity_options",
                        "GET",
                        f"{API}/config/opportunity-options",
                        scan=False,
                    )
                ],
            )
        elif roll < 0.8:
            await self.page(
                "calendar",
                [
                    self.get(
                        "calendar_range",
                        "activities",
                        date_from=str(today - timedelta(days=7)),
                        date_to=str(today + timedelta(days=21)),
                        page_size=100,
                    )
                ],
            )
        else:
            await self.page("pipelines", [self.get("pipelines", "pipelines")])

    async def probe(self) -> None:
        """An attack: reach another user's data. Every one must be refused (403/404)."""
        others = [p for key, p in self.run.pool.items() if key != self.position and p["opps"]]
        if not others or self.is_admin:
            return
        victim = random.choice(others)
        kind = random.choice(["opp", "dash", "all", "lead", "move", "timeline", "admin", "search"])
        if kind == "opp":
            response = await self.call(
                "probe_opp",
                "GET",
                self.path(f"opportunities/{random.choice(victim['opps'])}"),
                expected=(403, 404),
                scan=False,
            )
        elif kind == "dash":
            response = await self.call(
                "probe_dash",
                "GET",
                self.path("dashboard", victim["user_id"]),
                expected=(403, 404),
                scan=False,
            )
        elif kind == "all":
            response = await self.call(
                "probe_all", "GET", self.path("dashboard", "all"), expected=(403, 404), scan=False
            )
        elif kind == "lead":
            response = await self.call(
                "probe_lead",
                "GET",
                self.path(f"leads/{random.choice(victim['leads'])}"),
                expected=(403, 404),
                scan=False,
            )
        elif kind == "move":
            response = await self.call(
                "probe_move",
                "POST",
                self.path(f"opportunities/{random.choice(victim['opps'])}/move"),
                body={"stage": str(uuid.uuid4()), "version": 1},
                expected=(403, 404, 422),
                scan=False,
            )
        elif kind == "timeline":
            response = await self.call(
                "probe_timeline",
                "GET",
                self.path(f"leads/{random.choice(victim['leads'])}/timeline"),
                expected=(403, 404),
                scan=False,
            )
        elif kind == "admin":
            response = await self.call(
                "probe_admin", "GET", f"{API}/admin/users", expected=(403, 404), scan=False
            )
        else:
            response = await self.call(
                "probe_search",
                "GET",
                self.path("search"),
                params={"q": f"ZQ{random.randrange(100):03d}Z"},
                scan=True,
            )
            if response is not None and response.status_code == 200:
                self.run.rec.probes["probe_search"] += 1
            return
        self.run.rec.probes[f"probe_{kind}"] += 1
        if response is not None and response.status_code < 400:
            self.run.rec.probe_leaks.append(
                f"{self.account['email']} reached {kind} of another user: {response.status_code}"
            )

    # -- the loop --------------------------------------------------------------------------
    def think(self) -> float:
        mean = self.run.think_mean
        if mean <= 0:
            return 0.0
        return min(max(random.gammavariate(2.0, mean / 2.0), 0.15 * mean), 6 * mean)

    async def switch_view(self) -> None:
        """An administrator moves between the organisation and individual users."""
        if not self.is_admin:
            return
        if not self.own:
            self.own = {"leads": self.leads, "opps": self.opps, "cursor": self.cursor}
        targets = [v for v in self.run.vus if not v.is_admin and v.opps]
        if targets and random.random() < 0.4:
            target = random.choice(targets)
            self.view_as = {"id": target.account["id"], "index": target.index}
            self.leads, self.opps, self.cursor = list(target.leads), dict(target.opps), None
        else:
            self.view_as = None
            self.leads, self.opps, self.cursor = (
                self.own["leads"],
                self.own["opps"],
                self.own["cursor"],
            )

    async def loop(self) -> None:
        await asyncio.sleep(random.random() * min(self.run.think_mean, 3))
        if not self.signed_in:  # never signed in: not a user of this run (reported)
            return
        while not self.run.stop.is_set():
            if self.position >= self.run.target_active:
                await asyncio.sleep(0.5)
                continue
            if self.need_login:
                self.client.cookies.clear()
                self.need_login = False
                if not await self.login(None):
                    return
            if self.is_admin:
                await self.switch_view()
            name = random.choice(ACTION_NAMES)
            if random.random() < 0.02:
                await self.probe()
            else:
                try:
                    await getattr(self, f"do_{name}")()
                except (KeyError, ValueError, IndexError, TypeError) as error:
                    self.errors[f"client:{type(error).__name__}"] += 1
            await asyncio.sleep(self.think())


def stage_of(opportunity: dict[str, Any]) -> str | None:
    return opportunity.get("stage_id") or (opportunity.get("stage") or {}).get("id")


def lead_of(opportunity: dict[str, Any]) -> str | None:
    lead = opportunity.get("lead")
    return lead if isinstance(lead, str) else (lead or {}).get("id")


def leads_total(dashboard: dict[str, Any]) -> int | None:
    leads = dashboard.get("leads") or {}
    for key in ("total", "total_leads", "count"):
        if key in leads:
            return int(leads[key])
    return None


# --- monitors ----------------------------------------------------------------------------------
class Monitor:
    """Samples the stack every few seconds: container CPU/memory, PostgreSQL connections,
    lock waits, deadlocks, Redis, Celery queue depth, outbox age, host load."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rows: list[dict[str, Any]] = []
        self.stop = threading.Event()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def finish(self) -> None:
        self.stop.set()
        self.thread.join(timeout=30)

    def loop(self) -> None:
        import psycopg
        import redis as redis_lib

        pg = psycopg.connect(self.args.pg, autocommit=True)
        rds = None
        if self.args.redis_url:
            rds = redis_lib.Redis.from_url(self.args.redis_url, socket_timeout=3)
        base_deadlocks = None
        containers = self.args.containers.split(",")
        while not self.stop.is_set():
            started = time.time()
            row: dict[str, Any] = {"t": started}
            try:
                with pg.cursor() as cur:
                    cur.execute(
                        "SELECT coalesce(state,'none'), count(*) FROM pg_stat_activity"
                        " WHERE datname = %s GROUP BY 1",
                        [self.args.db_name],
                    )
                    states = dict(cur.fetchall())
                    row["db_conn_total"] = sum(states.values())
                    row["db_conn_active"] = states.get("active", 0)
                    row["db_conn_idle"] = states.get("idle", 0)
                    row["db_conn_idle_in_tx"] = states.get("idle in transaction", 0)
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend'"
                    )
                    row["db_server_conn"] = cur.fetchone()[0]
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=%s"
                        " AND wait_event_type='Lock'",
                        [self.args.db_name],
                    )
                    row["db_lock_waiters"] = cur.fetchone()[0]
                    cur.execute(
                        "SELECT coalesce(max(extract(epoch from now()-xact_start)),0)"
                        " FROM pg_stat_activity WHERE datname=%s AND state IN"
                        " ('active','idle in transaction') AND pid<>pg_backend_pid()",
                        [self.args.db_name],
                    )
                    row["db_longest_tx_s"] = float(cur.fetchone()[0])
                    cur.execute(
                        "SELECT deadlocks, xact_commit, xact_rollback, temp_files, temp_bytes,"
                        " blks_read, blks_hit FROM pg_stat_database WHERE datname=%s",
                        [self.args.db_name],
                    )
                    d = cur.fetchone()
                    if base_deadlocks is None:
                        base_deadlocks = d[0]
                    row["db_deadlocks"] = d[0] - base_deadlocks
                    row["db_commits"], row["db_rollbacks"] = d[1], d[2]
                    row["db_temp_files"], row["db_temp_bytes"] = d[3], d[4]
                    row["db_blks_read"], row["db_blks_hit"] = d[5], d[6]
            except Exception as error:  # a sampling failure is a data point, not a crash
                row["db_error"] = type(error).__name__
                with contextlib.suppress(Exception):  # reconnect next time round
                    pg = psycopg.connect(self.args.pg, autocommit=True)
            if rds is not None:
                try:
                    t0 = time.perf_counter()
                    rds.ping()
                    row["redis_ping_ms"] = (time.perf_counter() - t0) * 1000
                    info = rds.info()
                    row["redis_clients"] = info.get("connected_clients")
                    row["redis_ops"] = info.get("instantaneous_ops_per_sec")
                    row["redis_mem_mb"] = round(info.get("used_memory", 0) / 1048576, 1)
                    for queue in ("outbox", "default", "email", "ai_index", "ai"):
                        row[f"queue_{queue}"] = rds.llen(queue)
                except Exception as error:
                    row["redis_error"] = type(error).__name__
            try:
                out = subprocess.run(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}",
                    ]
                    + containers,
                    capture_output=True,
                    text=True,
                    timeout=25,
                ).stdout
                for line in out.splitlines():
                    name, cpu, mem = line.split("|")
                    short = (
                        name.replace("arkray-load-", "").replace("arkray-", "").rsplit("-", 1)[0]
                    )
                    row[f"cpu_{short}"] = float(cpu.strip("%"))
                    row[f"mem_{short}"] = parse_mem(mem.split("/")[0])
            except Exception as error:
                row["docker_error"] = type(error).__name__
            if self.args.metrics_url:
                try:
                    text = httpx.get(
                        f"{self.args.metrics_url}/health/metrics",
                        headers={
                            "Authorization": f"Bearer {self.args.metrics_token}",
                            "Host": "localhost",
                        },
                        timeout=6,
                    ).text
                    for line in text.splitlines():
                        if line.startswith("arkray_outbox_oldest_due_seconds"):
                            row["outbox_oldest_due_s"] = max(
                                row.get("outbox_oldest_due_s", 0), float(line.split()[-1])
                            )
                        elif (
                            line.startswith("arkray_outbox_events{") and 'status="pending"' in line
                        ):
                            row["outbox_pending"] = row.get("outbox_pending", 0) + float(
                                line.split()[-1]
                            )
                        elif line.startswith("arkray_outbox_events{") and 'status="dead"' in line:
                            row["outbox_dead"] = row.get("outbox_dead", 0) + float(line.split()[-1])
                except Exception as error:
                    row["metrics_error"] = type(error).__name__
            row["host_cpu"], row["host_mem_free_mb"] = host_load()
            self.rows.append(row)
            self.stop.wait(max(0.0, self.args.monitor_every - (time.time() - started)))

    def summarise(self, since: float | None = None, until: float | None = None) -> dict[str, Any]:
        rows = [
            r
            for r in self.rows
            if (since is None or r["t"] >= since) and (until is None or r["t"] <= until)
        ]
        out: dict[str, Any] = {"samples": len(rows)}
        keys = sorted(
            {k for r in rows for k, v in r.items() if isinstance(v, int | float) and k != "t"}
        )
        for key in keys:
            values = sorted(float(r[key]) for r in rows if key in r)
            if values:
                out[key] = {
                    "min": round(values[0], 1),
                    "avg": round(sum(values) / len(values), 1),
                    "p95": round(percentile(values, 0.95), 1),
                    "max": round(values[-1], 1),
                }
        errors = Counter(k for r in rows for k in r if k.endswith("_error"))
        if errors:
            out["sampling_errors"] = dict(errors)
        return out

    def trend(self, key: str) -> list[float]:
        """First, middle, last third averages of a series: a monotonic rise is a leak."""
        values = [float(r[key]) for r in self.rows if key in r]
        if len(values) < 6:
            return []
        third = len(values) // 3
        parts = [values[:third], values[third : 2 * third], values[2 * third :]]
        return [round(sum(p) / len(p), 1) for p in parts]


import threading  # noqa: E402


def parse_mem(text: str) -> float:
    text = text.strip()
    for unit, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("B", 1 / 1048576)):
        if text.endswith(unit):
            return round(float(text[: -len(unit)]) * factor, 1)
    return 0.0


def host_load() -> tuple[float, float]:
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$c=(Get-Counter '\\Processor(_Total)\\% Processor Time' -ErrorAction Stop).CounterSamples.CookedValue;"
                "$m=(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1024;"
                "'{0:N1} {1:N0}' -f $c,$m",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.split()
        return float(out[0].replace(",", "")), float(out[1].replace(",", ""))
    except Exception:
        return float("nan"), float("nan")


# --- commands ----------------------------------------------------------------------------------
def load_accounts(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_prepare(args: argparse.Namespace) -> None:
    import psycopg
    from django.conf import settings
    from django.contrib.auth.hashers import make_password

    if not settings.configured:
        settings.configure(
            PASSWORD_HASHERS=["django.contrib.auth.hashers.Argon2PasswordHasher"],
        )
    password = args.password
    hashed = make_password(password)
    with psycopg.connect(args.pg, autocommit=True) as conn, conn.cursor() as cur:
        # A realistic population, not the heaviest owners only: the `--heavy` biggest owners
        # (the benchmark data is skewed: the top owner holds 60,000 leads) plus a
        # deterministic pseudo-random sample of everyone else.
        cur.execute(
            "SELECT u.id::text, u.email, (SELECT count(*) FROM leads_lead l WHERE l.owner_id=u.id) AS n"
            " FROM identity_user u WHERE u.role='sales_user' AND u.status='active'"
            " AND EXISTS (SELECT 1 FROM pipeline_opportunity o WHERE o.owner_id=u.id)"
            " ORDER BY n DESC, u.email"
        )
        ranked = cur.fetchall()
        heavy = ranked[: args.heavy]
        rest = sorted(ranked[args.heavy :], key=lambda r: hashlib.md5(r[1].encode()).hexdigest())
        picked = heavy + rest[: max(args.vus - len(heavy), 0)]
        users = [(u[0], u[1]) for u in picked]
        sizes = sorted(u[2] for u in picked)
        print(
            f"leads per picked user: min {sizes[0]}, median {sizes[len(sizes) // 2]}, max {sizes[-1]}"
        )
        cur.execute(
            "SELECT id::text, email FROM identity_user WHERE role='admin' AND status='active' ORDER BY email LIMIT %s",
            [args.admins],
        )
        admins = cur.fetchall()
        ids = [u[0] for u in users] + [a[0] for a in admins]
        cur.execute(
            "UPDATE identity_user SET password=%s, password_change_required=false, password_changed_at=now(),"
            " session_epoch = session_epoch WHERE id = ANY(%s::uuid[])",
            [hashed, ids],
        )
    accounts = {
        "password": password,
        "users": [
            {"index": i, "id": u[0], "email": u[1], "role": "sales_user"}
            for i, u in enumerate(users)
        ],
        "admins": [{"index": -1, "id": a[0], "email": a[1], "role": "admin"} for a in admins],
    }
    Path(args.out).write_text(json.dumps(accounts, indent=1), encoding="utf-8")
    print(f"{len(users)} salespeople and {len(admins)} administrators prepared -> {args.out}")


def vu_roster(run: Run, vus: int) -> list[VU]:
    """`vus` virtual users: the administrators (about 1 in 33, at least one) spread among
    the salespeople so every ramp step has the same mix."""
    admins = run.accounts["admins"]
    n_admin = min(len(admins), max(1, round(vus * 0.03))) if admins else 0
    sales = run.accounts["users"][: vus - n_admin]
    roster: list[VU] = []
    admin_slots = {round((i + 1) * vus / (n_admin + 1)) for i in range(n_admin)}
    s_i = a_i = 0
    for position in range(vus):
        if position in admin_slots and a_i < n_admin:
            account = admins[a_i % len(admins)]
            a_i += 1
        else:
            account = sales[s_i % len(sales)]
            s_i += 1
        roster.append(VU(run, account, position))
    return roster


async def login_all(run: Run, roster: list[VU], sessions_path: Path | None) -> None:
    saved: dict[str, dict[str, str]] = {}
    if sessions_path and sessions_path.exists():
        saved = json.loads(sessions_path.read_text(encoding="utf-8"))
    sem = asyncio.Semaphore(6)
    ok = 0

    async def one(vu: VU) -> None:
        nonlocal ok
        async with sem:
            if await vu.login(saved.get(vu.account["email"])):
                await vu.prime()
                ok += 1

    await asyncio.gather(*(one(v) for v in roster))
    print(f"{ok} of {len(roster)} virtual users signed in and primed")
    if sessions_path:
        sessions_path.write_text(
            json.dumps({v.account["email"]: v.cookies() for v in roster}), encoding="utf-8"
        )


async def run_load(args: argparse.Namespace) -> dict[str, Any]:
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    stage_sizes = [int(s) for s in args.stages.split(",")]
    total = max([*stage_sizes, args.vus])
    roster = vu_roster(run, total)
    run.vus = roster
    sessions = Path(args.sessions) if args.sessions else None
    t_login = time.time()
    await login_all(run, roster, sessions)
    print(f"sign-in phase {time.time() - t_login:.0f} s")
    import psycopg

    with psycopg.connect(args.pg, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT now()")
        run.started_db = cur.fetchone()[0]
    monitor = Monitor(args)
    monitor.start()
    run.rec.stage = "setup_canary"
    # canary data: every salesperson gets a deal and a note carrying their token
    for v in roster:
        if not v.is_admin:
            await v.create_opportunity(retry=False)
            await v.create_activity("note")
    tasks = [asyncio.create_task(v.loop()) for v in roster]
    marks: dict[str, tuple[float, float]] = {}
    for size in stage_sizes:
        label = f"{size}vu"
        run.target_active = size
        run.rec.stage = label
        t0 = time.time()
        print(f"stage {label}: {args.stage_seconds} s")
        await asyncio.sleep(args.stage_seconds)
        marks[label] = (t0, time.time())
    if args.vus and args.vus > stage_sizes[-1]:
        stage_sizes.append(args.vus)
    run.target_active = args.vus
    run.rec.stage = "warmup"
    print(f"warm-up at {args.vus}: {args.warmup} s")
    await asyncio.sleep(args.warmup)
    run.rec.stage = "hold"
    t_hold = time.time()
    if args.marker:  # a drill script waits for this file, then injects its fault
        Path(args.marker).write_text(str(t_hold), encoding="utf-8")
    print(f"hold at {args.vus} users: {args.hold_seconds} s")
    await asyncio.sleep(args.hold_seconds)
    marks["hold"] = (t_hold, time.time())
    run.stop.set()
    run.rec.stage = "drain"
    await asyncio.gather(*tasks, return_exceptions=True)
    print("quiescing 30 s (queues drain)")
    drain_end = time.time() + 30
    while time.time() < drain_end:
        await asyncio.sleep(2)
    marks["post"] = (time.time() - 30, time.time())
    monitor.finish()
    report = build_report(run, monitor, marks)
    report["verification"] = await verify(run, roster)
    for v in roster:
        await v.client.aclose()
    return report


def table(summary: dict[str, Any], names: list[str] | None = None) -> str:
    lines = [f"{'request':28} {'n':>7} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}  statuses"]
    for name, row in summary.items():
        if names and name not in names:
            continue
        lines.append(
            f"{name:28} {row['n']:7d} {row['p50']:8.1f} {row['p95']:8.1f} {row['p99']:8.1f} {row['max']:8.1f}  {row['statuses']}"
        )
    return "\n".join(lines)


def aggregate(rec: Recorder, stage: str, *, pages: bool) -> dict[str, Any]:
    samples = [
        s
        for s in rec.samples
        if s.stage == stage and s.status != 429 and s.name.startswith("page:") == pages
    ]
    if not samples:
        return {}
    ordered = sorted(s.ms for s in samples)
    errors = sum(1 for s in samples if s.status == 0 or s.status >= 500)
    unexpected = sum(
        1
        for s in samples
        if 400 <= s.status < 500
        and s.status not in (409,)
        and not s.name.startswith("probe")
        and s.name not in ("negotiation_revision",)
    )
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(percentile(ordered, 0.95), 1),
        "p99": round(percentile(ordered, 0.99), 1),
        "max": round(ordered[-1], 1),
        "server_or_network_errors": errors,
        "unexpected_4xx": unexpected,
        "throttled_429": sum(1 for s in rec.samples if s.stage == stage and s.status == 429),
    }


def build_report(
    run: Run, monitor: Monitor, marks: dict[str, tuple[float, float]]
) -> dict[str, Any]:
    rec = run.rec
    report: dict[str, Any] = {
        "run_id": run.run_id,
        "args": {
            k: v for k, v in vars(run.args).items() if k not in ("metrics_token", "pg", "redis_url")
        },
    }
    stages: dict[str, Any] = {}
    for stage, (t0, t1) in marks.items():
        if stage == "post":
            continue
        duration = t1 - t0
        api = aggregate(rec, stage, pages=False)
        pages = aggregate(rec, stage, pages=True)
        api["requests_per_s"] = round(api.get("n", 0) / duration, 1) if duration else 0
        error_rate = (
            100
            * (api.get("server_or_network_errors", 0) + api.get("unexpected_4xx", 0))
            / max(api.get("n", 1), 1)
        )
        stages[stage] = {
            "seconds": round(duration),
            "api": api,
            "pages": pages,
            "error_rate_pct": round(error_rate, 3),
            "monitor": monitor.summarise(t0, t1),
        }
        stages[stage]["endpoints"] = rec.summary(stage)
    report["stages"] = stages
    report["monitor_trends"] = {
        k: monitor.trend(k)
        for k in sorted(
            {
                k
                for r in monitor.rows
                for k in r
                if k.startswith(("mem_", "db_conn", "redis_clients", "redis_mem"))
            }
        )
    }
    post = marks.get("post")
    if post:
        report["post_load"] = monitor.summarise(*post)
    report["leaks"] = rec.leaks[:50]
    report["leak_count"] = len(rec.leaks)
    report["probes"] = dict(rec.probes)
    report["probe_leaks"] = rec.probe_leaks[:50]
    report["dashboard_mismatches"] = rec.dashboard_mismatches[:50]
    report["dashboard_mismatch_count"] = len(rec.dashboard_mismatches)
    report["notes"] = rec.notes[:50]
    report["failures"] = rec.failures[:60]
    report["sessions_ended"] = rec.sessions_ended[:50]
    report["virtual_users_signed_in"] = sum(1 for v in run.vus if v.signed_in)
    report["client_errors"] = dict(sum((v.errors for v in run.vus), Counter()))
    report["monitor_rows"] = len(monitor.rows)
    report["timeline"] = timeline(rec)
    report["monitor_series"] = [
        {
            k: (round(v, 1) if isinstance(v, float) else v)
            for k, v in row.items()
            if k
            in (
                "t",
                "db_conn_total",
                "db_conn_active",
                "db_lock_waiters",
                "redis_clients",
                "redis_ping_ms",
                "queue_outbox",
                "queue_default",
                "queue_email",
                "queue_ai_index",
                "queue_ai",
                "outbox_pending",
                "outbox_oldest_due_s",
                "host_cpu",
                "cpu_backend-1",
                "cpu_arkray-postgres",
                "mem_backend-1",
            )
        }
        for row in monitor.rows[:: max(1, len(monitor.rows) // 120)]
    ]
    return report


async def verify(run: Run, roster: list[VU]) -> dict[str, Any]:
    """After the run, from the database: duplicates, orphans, lost updates, impossible
    states, dashboard arithmetic."""
    import psycopg

    out: dict[str, Any] = {}
    t0 = run.started_db
    logical = list(run.creates)
    created = [c for c in logical if c["opp"]]
    out["logical_creates_attempted"] = len(logical)
    out["logical_creates_succeeded"] = len(created)
    out["retries_replayed"] = sum(c["replays"] for c in logical)
    with psycopg.connect(run.args.pg, autocommit=True) as conn, conn.cursor() as cur:
        prefix = f"LT{run.run_id} %"
        cur.execute(
            "SELECT count(*) FROM pipeline_opportunity WHERE created_at >= %s AND customer_name LIKE %s",
            [t0, prefix],
        )
        out["opportunities_created"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM leads_lead WHERE created_at >= %s", [t0])
        out["leads_created"] = cur.fetchone()[0]
        out["duplicate_logical_creates"] = _scalar(
            cur,
            "SELECT count(*) FROM (SELECT customer_name FROM pipeline_opportunity WHERE created_at >= %s AND customer_name LIKE %s GROUP BY 1 HAVING count(*)>1) d",
            [t0, prefix],
        )
        out["orphan_leads"] = _scalar(
            cur,
            "SELECT count(*) FROM leads_lead l WHERE l.created_at >= %s AND NOT EXISTS (SELECT 1 FROM pipeline_opportunity o WHERE o.lead_id=l.id)",
            [t0],
        )
        out["opportunities_without_lead_owner_match"] = _scalar(
            cur,
            "SELECT count(*) FROM pipeline_opportunity o JOIN leads_lead l ON l.id=o.lead_id WHERE o.created_at >= %s AND l.owner_id <> o.owner_id",
            [t0],
        )
        out["idempotency_records"] = _scalar(
            cur,
            "SELECT count(*) FROM core_idempotency_record WHERE created_at >= %s AND operation='pipeline.create_opportunity'",
            [t0],
        )
        out["distinct_keys_used"] = len({c["key"] for c in logical})
        ids = [c["opp"] for c in created]
        out["created_ids_missing_in_db"] = (
            len(set(ids))
            - _scalar(
                cur,
                "SELECT count(*) FROM pipeline_opportunity WHERE id = ANY(%s::uuid[])",
                [list(set(ids))],
            )
            if ids
            else 0
        )
        out["one_audit_event_per_create"] = (
            _scalar(
                cur,
                "SELECT count(*) FROM (SELECT target_id FROM audit_event WHERE created_at >= %s AND action='opportunity.created' GROUP BY 1 HAVING count(*)<>1) d",
                [t0],
            )
            if _has_column(cur, "audit_event", "created_at")
            else "n/a"
        )
        # lost updates: version of each moved opportunity vs this run's successful moves
        lost = 0
        checked = 0
        for v in roster:
            for opp, expected in v.version_expected.items():
                cur.execute("SELECT version FROM pipeline_opportunity WHERE id=%s", [opp])
                row = cur.fetchone()
                if row is None:
                    continue
                checked += 1
                if row[0] < expected:
                    lost += 1
        out["versions_checked"] = checked
        out["lost_updates"] = lost
        # a stage move writes exactly one history row: compare with the run's successful moves
        moved = {opp: n for v in roster for opp, n in v.moves_ok.items()}
        mismatches = 0
        for opp, n in moved.items():
            cur.execute(
                "SELECT count(*) FROM pipeline_stage_history WHERE opportunity_id=%s AND occurred_at >= %s AND from_stage_id IS NOT NULL",
                [opp, t0],
            )
            if cur.fetchone()[0] != n:
                mismatches += 1
        out["moves_with_history_mismatch"] = mismatches
        out["moved_opportunities"] = len(moved)
        out["impossible_states"] = {
            "open_with_closed_at": _scalar(
                cur,
                "SELECT count(*) FROM pipeline_opportunity WHERE status='open' AND closed_at IS NOT NULL AND updated_at >= %s",
                [t0],
            ),
            "closed_without_closed_at": _scalar(
                cur,
                "SELECT count(*) FROM pipeline_opportunity WHERE status<>'open' AND closed_at IS NULL AND updated_at >= %s",
                [t0],
            ),
            "negative_value": _scalar(
                cur,
                "SELECT count(*) FROM pipeline_opportunity WHERE value < 0 AND updated_at >= %s",
                [t0],
            ),
            "negotiation_stage_without_price": _scalar(
                cur,
                "SELECT count(*) FROM pipeline_opportunity o JOIN pipeline_stage s ON s.id=o.stage_id WHERE s.is_negotiation AND o.negotiated_price IS NULL AND o.updated_at >= %s",
                [t0],
            ),
        }
        out["deadlocks"] = _scalar(
            cur, "SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()", []
        )
    out["dashboard_math"] = await dashboard_math(run, roster)
    return out


def _scalar(cur: Any, sql: str, params: list[Any]) -> Any:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _has_column(cur: Any, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        [table, column],
    )
    return cur.fetchone() is not None


async def dashboard_math(run: Run, roster: list[VU]) -> dict[str, Any]:
    """After the writes stop: each user's dashboard figures equal an independent SQL
    computation over the authoritative tables."""
    import psycopg

    mismatches: list[str] = []
    checked = 0
    sales = [v for v in roster if not v.is_admin and v.signed_in]
    with psycopg.connect(run.args.pg, autocommit=True) as conn, conn.cursor() as cur:
        for v in sales:
            response = await v.get("dashboard_final", "dashboard")
            if response is None or response.status_code != 200:
                mismatches.append(
                    f"{v.account['email']}: dashboard {response.status_code if response else 'none'}"
                )
                continue
            data = response.json()
            uid = v.account["id"]
            truth = {}
            cur.execute(
                "SELECT count(*) FROM leads_lead WHERE owner_id=%s AND archived_at IS NULL", [uid]
            )
            truth["leads_total"] = cur.fetchone()[0]
            cur.execute(
                "SELECT coalesce(sum(value),0), coalesce(round(sum(value*probability*0.01),2),0), count(*)"
                " FROM pipeline_opportunity WHERE owner_id=%s AND archived_at IS NULL AND status='open'",
                [uid],
            )
            value, weighted, open_count = cur.fetchone()
            truth.update(pipeline_value=value, weighted=weighted, open_count=open_count)
            cur.execute(
                "SELECT count(*) FROM activities_activity WHERE owner_id=%s AND archived_at IS NULL AND type='task' AND status='open'",
                [uid],
            )
            truth["open_tasks"] = cur.fetchone()[0]
            got = flatten_dashboard(data)
            for key, want in truth.items():
                have = got.get(key)
                if have is None:
                    continue
                if Decimal(str(have)) != Decimal(str(want)):
                    mismatches.append(f"{v.account['email']}: {key} api={have} sql={want}")
            checked += 1
    return {
        "users_checked": checked,
        "mismatches": mismatches[:30],
        "mismatch_count": len(mismatches),
    }


def flatten_dashboard(data: dict[str, Any]) -> dict[str, Any]:
    leads = data.get("leads") or {}
    pipeline = data.get("pipeline") or {}
    activities = data.get("activities") or {}
    return {
        "leads_total": leads.get("total"),
        "pipeline_value": pipeline.get("pipeline_value") or pipeline.get("value"),
        "weighted": pipeline.get("weighted_pipeline") or pipeline.get("weighted"),
        "open_count": pipeline.get("open_count") or pipeline.get("open_opportunities"),
        "open_tasks": activities.get("open_tasks"),
    }


# --- targeted concurrency ---------------------------------------------------------------------
async def cmd_races(args: argparse.Namespace) -> dict[str, Any]:
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    run.think_mean = 0
    results: dict[str, Any] = {}
    sales = [v for v in vu_roster(run, 6) if not v.is_admin][:2]
    admin = next(v for v in vu_roster(run, 6) if v.is_admin)
    owner = sales[0]
    await login_all(run, [owner, admin], Path(args.sessions) if args.sessions else None)
    import psycopg

    conn = psycopg.connect(args.pg, autocommit=True)
    cur = conn.cursor()

    def count(sql: str, params: list[Any]) -> int:
        cur.execute(sql, params)
        return cur.fetchone()[0]

    # R1: 20 parallel moves of one opportunity, same version
    opp = next(iter(owner.opps))
    info = owner.opps[opp]
    history_before = count(
        "SELECT count(*) FROM pipeline_stage_history WHERE opportunity_id=%s", [opp]
    )
    targets = [s for s in owner.open_stages if s != info["stage"]]
    reqs = [
        owner.call(
            "race_move",
            "POST",
            owner.path(f"opportunities/{opp}/move"),
            body={"stage": targets[i % len(targets)], "version": info["version"]},
            expected=(200, 409),
        )
        for i in range(20)
    ]
    responses = await asyncio.gather(*reqs)
    codes = Counter(r.status_code for r in responses if r is not None)
    history_after = count(
        "SELECT count(*) FROM pipeline_stage_history WHERE opportunity_id=%s", [opp]
    )
    version_after = count("SELECT version FROM pipeline_opportunity WHERE id=%s", [opp])
    cur.execute("SELECT stage_id::text FROM pipeline_opportunity WHERE id=%s", [opp])
    final_stage = cur.fetchone()[0]
    winners = {
        r.json().get("stage_id") or (r.json().get("stage") or {}).get("id")
        for r in responses
        if r is not None and r.status_code == 200
    }
    # Moves to the stage the deal is already in answer 200 (idempotent): the proof is ONE
    # transition (one history row, one version step) and every success naming the same stage.
    results["move_race_same_version"] = {
        "codes": dict(codes),
        "history_rows_added": history_after - history_before,
        "version_delta": version_after - info["version"],
        "distinct_stages_reported_ok": len(winners),
        "pass": history_after - history_before == 1
        and version_after - info["version"] == 1
        and winners == {final_stage}
        and set(codes) <= {200, 409},
    }
    await owner.refresh_opp(opp)
    # R2: owner and administrator race the same version
    info = owner.opps[opp]
    targets = [s for s in owner.open_stages if s != info["stage"]]
    body1 = {"stage": targets[0], "version": info["version"]}
    body2 = {"stage": targets[-1], "version": info["version"]}
    r_owner, r_admin = await asyncio.gather(
        owner.call(
            "race_move",
            "POST",
            owner.path(f"opportunities/{opp}/move"),
            body=body1,
            expected=(200, 409),
        ),
        admin.call(
            "race_move_admin",
            "POST",
            f"{API}/workspaces/{owner.account['id']}/opportunities/{opp}/move",
            body=body2,
            expected=(200, 409),
            scan=False,
        ),
    )
    pair = sorted([r_owner.status_code if r_owner else 0, r_admin.status_code if r_admin else 0])
    results["move_race_owner_vs_admin"] = {"codes": pair, "pass": pair == [200, 409]}
    await owner.refresh_opp(opp)
    # R3: negotiation entry race with different prices and CPTs
    if owner.neg_stage:
        candidates = [o for o, i in owner.opps.items() if i["stage"] != owner.neg_stage]
        target = candidates[0]
        info = owner.opps[target]
        neg_before = count(
            "SELECT count(*) FROM pipeline_negotiation_price WHERE opportunity_id=%s", [target]
        )
        prices = [f"{1234567 + i}.89" for i in range(20)]
        reqs = [
            owner.call(
                "race_negotiate",
                "POST",
                owner.path(f"opportunities/{target}/move"),
                body={
                    "stage": owner.neg_stage,
                    "version": info["version"],
                    "negotiated_price": prices[i],
                    "agreed_cpt": f"CPT {i}",
                },
                expected=(200, 409),
            )
            for i in range(20)
        ]
        responses = await asyncio.gather(*reqs)
        codes = Counter(r.status_code for r in responses if r is not None)
        winner = next(
            (i for i, r in enumerate(responses) if r is not None and r.status_code == 200), None
        )
        loser_bodies = sorted(
            {r.text[:160] for r in responses if r is not None and r.status_code not in (200, 409)}
        )[:3]
        cur.execute(
            "SELECT negotiated_price::text, expected_cpt FROM pipeline_opportunity WHERE id=%s",
            [target],
        )
        stored_price = cur.fetchone()[0]
        cur.execute(
            "SELECT price::text, agreed_cpt, actor_id::text, occurred_at FROM pipeline_negotiation_price WHERE opportunity_id=%s ORDER BY occurred_at DESC, id DESC LIMIT 1",
            [target],
        )
        row = cur.fetchone()
        neg_after = count(
            "SELECT count(*) FROM pipeline_negotiation_price WHERE opportunity_id=%s", [target]
        )
        results["negotiation_entry_race"] = {
            "codes": dict(codes),
            "loser_bodies": loser_bodies,
            "history_rows_added": neg_after - neg_before,
            "winner_price_exact": winner is not None
            and row is not None
            and row[0] == prices[winner]
            and stored_price == prices[winner],
            "winner_cpt_exact": winner is not None
            and row is not None
            and row[1] == f"CPT {winner}",
            "actor_is_owner": row is not None and row[2] == owner.account["id"],
            "pass": codes.get(200) == 1
            and set(codes) <= {200, 400, 409}
            and neg_after - neg_before == 1
            and winner is not None
            and row is not None
            and row[0] == prices[winner]
            and row[1] == f"CPT {winner}",
        }
        await owner.refresh_opp(target)
        # R4: price revision race (same version, different prices)
        info = owner.opps[target]
        before = count(
            "SELECT count(*) FROM pipeline_negotiation_price WHERE opportunity_id=%s", [target]
        )
        reqs = [
            owner.call(
                "race_revision",
                "POST",
                owner.path(f"opportunities/{target}/negotiated-prices"),
                body={
                    "version": info["version"],
                    "price": f"{2000000 + i}.01",
                    "agreed_cpt": f"R{i}",
                },
                expected=(200, 201, 409),
            )
            for i in range(20)
        ]
        responses = await asyncio.gather(*reqs)
        codes = Counter(r.status_code for r in responses if r is not None)
        after = count(
            "SELECT count(*) FROM pipeline_negotiation_price WHERE opportunity_id=%s", [target]
        )
        results["price_revision_race"] = {
            "codes": dict(codes),
            "history_rows_added": after - before,
            "pass": codes.get(200, 0) + codes.get(201, 0) == 1
            and codes.get(409) == 19
            and after - before == 1,
        }
    # R5/R6/R7: same-key creates
    for n in (2, 20, 100):
        owner.serial += 1
        key = str(uuid.uuid4())
        body = owner.opportunity_body(owner.serial)
        t_before = count(
            "SELECT count(*) FROM pipeline_opportunity WHERE owner_id=%s", [owner.account["id"]]
        )
        l_before = count("SELECT count(*) FROM leads_lead WHERE owner_id=%s", [owner.account["id"]])
        reqs = [
            owner.call(
                "race_create",
                "POST",
                owner.path("opportunities"),
                body=body,
                headers={"Idempotency-Key": key},
                expected=(201,),
            )
            for _ in range(n)
        ]
        responses = [r for r in await asyncio.gather(*reqs) if r is not None]
        ids = {r.json()["id"] for r in responses if r.status_code == 201}
        replayed = sum(1 for r in responses if r.headers.get("Idempotent-Replayed") == "true")
        t_after = count(
            "SELECT count(*) FROM pipeline_opportunity WHERE owner_id=%s", [owner.account["id"]]
        )
        l_after = count("SELECT count(*) FROM leads_lead WHERE owner_id=%s", [owner.account["id"]])
        codes = Counter(r.status_code for r in responses)
        results[f"same_key_x{n}"] = {
            "codes": dict(codes),
            "distinct_ids": len(ids),
            "replayed": replayed,
            "opportunity_rows": t_after - t_before,
            "lead_rows": l_after - l_before,
            "pass": len(ids) == 1
            and t_after - t_before == 1
            and l_after - l_before == 1
            and replayed == codes.get(201, 0) - 1,
        }
        # the same key with a different body is refused and creates nothing
    other = dict(body, value="1.00")
    r = await owner.call(
        "race_create_other_payload",
        "POST",
        owner.path("opportunities"),
        body=other,
        headers={"Idempotency-Key": key},
        expected=(422,),
    )
    t_after2 = count(
        "SELECT count(*) FROM pipeline_opportunity WHERE owner_id=%s", [owner.account["id"]]
    )
    results["same_key_other_payload"] = {
        "status": r.status_code if r else 0,
        "extra_rows": t_after2 - t_after,
        "pass": bool(r) and r.status_code == 422 and t_after2 == t_after,
    }
    # a new key with the same legitimate payload is a new opportunity
    r = await owner.call(
        "race_create_new_key",
        "POST",
        owner.path("opportunities"),
        body=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        expected=(201,),
    )
    t_after3 = count(
        "SELECT count(*) FROM pipeline_opportunity WHERE owner_id=%s", [owner.account["id"]]
    )
    results["new_key_same_payload"] = {
        "status": r.status_code if r else 0,
        "rows": t_after3 - t_after2,
        "pass": bool(r) and r.status_code == 201 and t_after3 - t_after2 == 1,
    }
    # no key: refused
    r = await owner.call(
        "race_create_no_key",
        "POST",
        owner.path("opportunities"),
        body=owner.opportunity_body(99999),
        expected=(400, 422),
    )
    t_after4 = count(
        "SELECT count(*) FROM pipeline_opportunity WHERE owner_id=%s", [owner.account["id"]]
    )
    results["missing_key"] = {
        "status": r.status_code if r else 0,
        "extra_rows": t_after4 - t_after3,
        "pass": bool(r) and r.status_code in (400, 422) and t_after4 == t_after3,
    }
    results["all_pass"] = all(v.get("pass") for v in results.values() if isinstance(v, dict))
    results["deadlocks_total"] = count(
        "SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()", []
    )
    await owner.client.aclose()
    await admin.client.aclose()
    return results


# --- search and ask ----------------------------------------------------------------------------
async def cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    roster = vu_roster(run, args.vus)
    await login_all(run, roster, Path(args.sessions) if args.sessions else None)
    classes = {
        "rare_token": lambda v: v.token or "ZQ000Z",
        "common_word": lambda v: random.choice(SEARCH_COMMON),
        "instrument": lambda v: random.choice(INSTRUMENTS),
        "customer_name": lambda v: f"Diagnostics {random.randint(1, 30)}",
        "unicode": lambda v: random.choice(SEARCH_UNICODE),
        "pathological_common": lambda v: random.choice(SEARCH_PATHOLOGICAL),
        "two_words": lambda v: "analyser follow",
        "very_long": lambda v: SEARCH_LONG,
    }
    stop = asyncio.Event()

    async def worker(v: VU) -> None:
        while not stop.is_set():
            cls = random.choice(list(classes))
            await v.call(
                f"search_{cls}",
                "GET",
                v.path("search"),
                params={"q": classes[cls](v)},
                expected=(200, 400, 422),
            )
            await asyncio.sleep(v.think())

    run.rec.stage = "search"
    run.think_mean = args.think_mean
    tasks = [asyncio.create_task(worker(v)) for v in roster]
    monitor = Monitor(args)
    monitor.start()
    t0 = time.time()
    await asyncio.sleep(args.hold_seconds)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    monitor.finish()
    out = {
        "vus": args.vus,
        "seconds": round(time.time() - t0),
        "think_mean": args.think_mean,
        "endpoints": run.rec.summary("search"),
        "leaks": run.rec.leaks[:20],
        "leak_count": len(run.rec.leaks),
        "monitor": monitor.summarise(),
    }
    for v in roster:
        await v.client.aclose()
    return out


async def cmd_ask(args: argparse.Namespace) -> dict[str, Any]:
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    roster = vu_roster(run, args.vus)
    await login_all(run, roster, Path(args.sessions) if args.sessions else None)
    structured = [
        "What is my pipeline value?",
        "How many leads do I have?",
        "What are my overdue tasks?",
        "How many open opportunities do I have?",
    ]
    semantic = [
        "What concerns did customers raise about pricing?",
        "Who asked for a demo?",
        "Summarise follow ups about the quotation",
    ]
    stop = asyncio.Event()

    async def worker(v: VU) -> None:
        while not stop.is_set():
            kind = random.choice(["structured", "semantic"])
            question = random.choice(structured if kind == "structured" else semantic)
            began = now()
            response = await v.call(
                f"ask_{kind}",
                "POST",
                v.path("ask"),
                body={"question": question},
                expected=(200, 201, 202),
            )
            final = "none"
            if response is not None and response.status_code in (200, 201, 202):
                data = response.json()
                qid = data.get("id") or data.get("question", {}).get("id")
                for _ in range(40):
                    if data.get("status") in ("answered", "failed", "refused", "done", "completed"):
                        break
                    await asyncio.sleep(0.5)
                    poll = await v.call(
                        "ask_poll", "GET", v.path(f"ask/questions/{qid}"), expected=(200,)
                    )
                    if poll is None or poll.status_code != 200:
                        break
                    data = poll.json()
                final = str(data.get("status"))
                run.rec.add(f"ask_e2e_{kind}_{final}", 200, (now() - began) * 1000, v.position)
            await asyncio.sleep(max(v.think(), args.ask_gap))

    run.rec.stage = "ask"
    tasks = [asyncio.create_task(worker(v)) for v in roster]
    monitor = Monitor(args)
    monitor.start()
    t0 = time.time()
    await asyncio.sleep(args.hold_seconds)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    monitor.finish()
    out = {
        "vus": args.vus,
        "seconds": round(time.time() - t0),
        "endpoints": run.rec.summary("ask"),
        "leaks": run.rec.leaks[:20],
        "leak_count": len(run.rec.leaks),
        "monitor": monitor.summarise(),
    }
    for v in roster:
        await v.client.aclose()
    return out


# --- isolation ----------------------------------------------------------------------------------
async def cmd_isolate(args: argparse.Namespace) -> dict[str, Any]:
    """Users hammer their dashboards/boards/searches concurrently while one administrator
    switches between users, and another starts and ends support sessions; every response is
    token-scanned (the token expected is fixed BEFORE each request is sent) and every
    dashboard compared with the user's own SQL truth afterwards."""
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    roster = vu_roster(run, 12)
    users = [v for v in roster if not v.is_admin][:6]
    admin_accounts = accounts["admins"]
    switcher_admin = VU(run, admin_accounts[0], 90)
    support_admin = VU(run, admin_accounts[1 % len(admin_accounts)], 91)
    everyone = [*users, switcher_admin, support_admin]
    saved: dict[str, dict[str, str]] = {}
    sessions = Path(args.sessions) if args.sessions else None
    if sessions and sessions.exists():
        saved = json.loads(sessions.read_text(encoding="utf-8"))
    for v in everyone:
        if await v.login(saved.get(v.account["email"])):
            await v.prime()
    if sessions:
        saved.update({v.account["email"]: v.cookies() for v in everyone})
        sessions.write_text(json.dumps(saved), encoding="utf-8")
    run.vus = users
    stop = asyncio.Event()
    stats: Counter[str] = Counter()
    run.rec.stage = "isolate"

    async def hammer(v: VU) -> None:
        while not stop.is_set():
            await asyncio.gather(
                v.call("iso_dashboard", "GET", v.path("dashboard")),
                v.call("iso_board", "GET", v.path("pipeline-board")),
                v.call("iso_search", "GET", v.path("search"), params={"q": v.token}),
                v.call("iso_opps", "GET", v.path("opportunities"), params={"page_size": 25}),
            )
            stats["user_rounds"] += 1

    async def switcher() -> None:
        a = switcher_admin
        while not stop.is_set():
            target = random.choice(users)
            workspace, token = target.account["id"], target.token
            await asyncio.gather(
                a.call(
                    "iso_admin_dashboard",
                    "GET",
                    a.path("dashboard", workspace),
                    token_override=token,
                ),
                a.call(
                    "iso_admin_board",
                    "GET",
                    a.path("pipeline-board", workspace),
                    token_override=token,
                ),
                a.call(
                    "iso_admin_search",
                    "GET",
                    a.path("search", workspace),
                    params={"q": token},
                    token_override=token,
                ),
                a.call(
                    "iso_admin_opps",
                    "GET",
                    a.path("opportunities", workspace),
                    params={"page_size": 25},
                    token_override=token,
                ),
            )
            stats["admin_switches"] += 1
            await asyncio.gather(
                a.call(
                    "iso_admin_all_dashboard",
                    "GET",
                    a.path("dashboard", "all"),
                    token_override=None,
                ),
                a.call(
                    "iso_admin_all_search",
                    "GET",
                    a.path("search", "all"),
                    params={"q": "ZQ000Z"},
                    token_override=None,
                ),
            )

    async def support() -> None:
        """Enter and leave a support session for a user; while it lasts, that user's workspace
        opens (with only that user's data) and every other workspace and every identity route is
        refused."""
        a = support_admin
        while not stop.is_set():
            target, other = random.sample(users, 2)
            started = await a.call(
                "iso_support_start",
                "POST",
                f"{API}/admin/support-sessions",
                body={"user": target.account["id"], "reason": "capacity test"},
                expected=(200, 201),
            )
            if started is not None and started.status_code in (200, 201):
                stats["support_started"] += 1
                await asyncio.gather(
                    a.call(
                        "iso_support_dashboard",
                        "GET",
                        a.path("dashboard", target.account["id"]),
                        token_override=target.token,
                    ),
                    a.call(
                        "iso_support_search",
                        "GET",
                        a.path("search", target.account["id"]),
                        params={"q": target.token},
                        token_override=target.token,
                    ),
                    a.call(
                        "iso_support_other_workspace",
                        "GET",
                        a.path("dashboard", other.account["id"]),
                        expected=(403, 404),
                        scan=False,
                    ),
                    a.call(
                        "iso_support_all_workspace",
                        "GET",
                        a.path("dashboard", "all"),
                        expected=(403, 404),
                        scan=False,
                    ),
                    a.call(
                        "iso_support_admin_route",
                        "GET",
                        f"{API}/admin/users",
                        expected=(403, 404),
                        scan=False,
                    ),
                    a.call(
                        "iso_support_set_password",
                        "POST",
                        f"{API}/admin/users/{target.account['id']}/set-password",
                        body={"version": 1, "new_password": "Nope-Nope-12345!"},
                        expected=(403, 404),
                        scan=False,
                    ),
                    a.call(
                        "iso_support_change_email",
                        "POST",
                        f"{API}/admin/users/{target.account['id']}/change-email",
                        body={"version": 1, "email": "nope@example.test"},
                        expected=(403, 404),
                        scan=False,
                    ),
                )
                ended = await a.call(
                    "iso_support_end",
                    "DELETE",
                    f"{API}/admin/support-sessions/current",
                    expected=(200, 204),
                )
                if ended is not None and ended.status_code in (200, 204):
                    stats["support_ended"] += 1
                    # after the exit: the former subject's workspace is refused again
                    after = await a.call(
                        "iso_after_support_workspace",
                        "GET",
                        a.path("dashboard", target.account["id"]),
                        token_override=target.token,
                    )
                    stats["after_exit_ok"] += int(after is not None and after.status_code == 200)
            await asyncio.sleep(0.5)

    tasks = [asyncio.create_task(hammer(v)) for v in users] + [
        asyncio.create_task(switcher()),
        asyncio.create_task(support()),
    ]
    await asyncio.sleep(args.hold_seconds)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    math_check = await dashboard_math(run, users)
    refused = {
        k: v
        for k, v in run.rec.summary("isolate").items()
        if k.startswith("iso_support_") and k not in ("iso_support_dashboard", "iso_support_search")
    }
    out = {
        "rounds": dict(stats),
        "leaks": run.rec.leaks[:30],
        "leak_count": len(run.rec.leaks),
        "dashboard_math": math_check,
        "support_session_calls": refused,
        "endpoints": run.rec.summary("isolate"),
        "failures": run.rec.failures[:30],
        "client_errors": dict(sum((v.errors for v in everyone), Counter())),
    }
    for v in everyone:
        await v.client.aclose()
    return out


# --- abuse ----------------------------------------------------------------------------------------
async def cmd_abuse(args: argparse.Namespace) -> dict[str, Any]:
    """Normal users keep working while ONE abusive user at a time hammers a path as fast as
    it can (8 parallel connections, no pauses): sign-in guessing, Global Search, opportunity
    creation, attachment upload, Ask Arkray. Reported per abuse: what the abuser got (accepted,
    throttled with 429, other) and what the victims experienced against their own baseline."""
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    roster = vu_roster(run, args.vus)
    run.vus = roster
    abuser_accounts = accounts["users"][-6:]
    abusers = [VU(run, a, 300 + i) for i, a in enumerate(abuser_accounts)]
    sessions = Path(args.sessions) if args.sessions else None
    saved: dict[str, dict[str, str]] = {}
    if sessions and sessions.exists():
        saved = json.loads(sessions.read_text(encoding="utf-8"))
    for v in [*roster, *abusers]:
        if await v.login(saved.get(v.account["email"])):
            await v.prime()
    if sessions:
        saved.update({v.account["email"]: v.cookies() for v in [*roster, *abusers]})
        sessions.write_text(json.dumps(saved), encoding="utf-8")
    run.target_active = args.vus
    tasks = [asyncio.create_task(v.loop()) for v in roster]
    results: dict[str, Any] = {}
    seconds = args.abuse_seconds
    stop_flags: list[asyncio.Event] = []

    async def hammer(kind: str, abuser: VU, stats: Counter[str], stop: asyncio.Event) -> None:
        note = None
        if kind == "upload":
            made = await abuser.call(
                "abuse_setup_note",
                "POST",
                abuser.path("activities"),
                body={
                    "type": "note",
                    "opportunity": next(iter(abuser.opps)),
                    "description": f"abuse {abuser.token}",
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
                expected=(201,),
            )
            note = made.json()["id"] if made is not None and made.status_code == 201 else None
        while not stop.is_set():
            started = now()
            if kind == "login":
                async with httpx.AsyncClient(
                    base_url=args.base,
                    verify=False,
                    transport=httpx.AsyncHTTPTransport(verify=False, local_address="0.0.0.0"),
                    headers={"Origin": args.base, "Host": "localhost:8443"},
                ) as anon:
                    await anon.get(f"{API}/auth/csrf")
                    token = anon.cookies.get("__Host-arkray_csrftoken") or ""
                    r = await anon.post(
                        f"{API}/auth/login",
                        json={
                            "email": f"nobody{random.randint(1, 5)}@example.test",
                            "password": "wrong-password-1234",
                        },
                        headers={"X-CSRFToken": token},
                    )
                    status = r.status_code
            elif kind == "search":
                r = await abuser.call(
                    "abuse_search",
                    "GET",
                    abuser.path("search"),
                    params={"q": random.choice(SEARCH_COMMON)},
                    expected=(200,),
                )
                status = r.status_code if r is not None else 0
            elif kind == "create_opportunity":
                abuser.serial += 1
                r = await abuser.call(
                    "abuse_create",
                    "POST",
                    abuser.path("opportunities"),
                    body=abuser.opportunity_body(abuser.serial),
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    expected=(201,),
                )
                status = r.status_code if r is not None else 0
            elif kind == "upload":
                if note is None:
                    return
                abuser.run.rec.stage = run.rec.stage
                r = await abuser.client.post(
                    abuser.path(f"activities/{note}/attachments"),
                    content=b"capacity test file\n" * 20,
                    headers={
                        "X-Filename": "abuse.txt",
                        "Content-Type": "application/octet-stream",
                        "X-CSRFToken": abuser.csrf(),
                    },
                )
                status = r.status_code
            else:
                r = await abuser.call(
                    "abuse_ask",
                    "POST",
                    abuser.path("ask"),
                    body={"question": "What is my pipeline value?"},
                    expected=(200, 201, 202),
                )
                status = r.status_code if r is not None else 0
            stats[str(status)] += 1
            stats["_ms_total"] += int((now() - started) * 1000)
            if status == 429 and "first_429_at" not in stats:
                stats["first_429_at"] = int(time.time())
            if status == 429:
                retry = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                if retry:
                    await asyncio.sleep(0)  # an abuser ignores Retry-After

    run.rec.stage = "baseline"
    print(f"baseline: {args.baseline_seconds} s")
    await asyncio.sleep(args.baseline_seconds)
    for i, kind in enumerate(["login", "search", "create_opportunity", "upload", "ask"]):
        stage = f"abuse_{kind}"
        run.rec.stage = stage
        stats: Counter[str] = Counter()
        stop = asyncio.Event()
        stop_flags.append(stop)
        abuser = abusers[i]
        t_start = int(time.time())
        loops = [asyncio.create_task(hammer(kind, abuser, stats, stop)) for _ in range(8)]
        print(f"{stage}: {seconds} s")
        await asyncio.sleep(seconds)
        stop.set()
        loop_results = await asyncio.gather(*loops, return_exceptions=True)
        loop_errors = sorted({repr(x)[:120] for x in loop_results if isinstance(x, BaseException)})
        total = sum(v for k, v in stats.items() if k.isdigit())
        results[stage] = {
            "requests": total,
            "per_minute": round(total * 60 / seconds),
            "statuses": {k: v for k, v in stats.items() if k.isdigit()},
            "seconds_to_first_429": (stats["first_429_at"] - t_start)
            if "first_429_at" in stats
            else None,
            "avg_ms": round(stats["_ms_total"] / max(total, 1)),
            "loop_errors": loop_errors,
        }
        run.rec.stage = "cooldown"
        await asyncio.sleep(args.cooldown_seconds)
    run.rec.stage = "recovery"
    await asyncio.sleep(args.baseline_seconds)
    run.stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    victims = {}
    for stage in [
        "baseline",
        *[f"abuse_{k}" for k in ("login", "search", "create_opportunity", "upload", "ask")],
        "recovery",
    ]:
        pages = aggregate(run.rec, stage, pages=True)
        api = aggregate(run.rec, stage, pages=False)
        victims[stage] = {
            "pages": pages,
            "api": {
                k: api.get(k)
                for k in (
                    "n",
                    "p50",
                    "p95",
                    "p99",
                    "server_or_network_errors",
                    "unexpected_4xx",
                    "throttled_429",
                )
            },
        }
    out = {
        "vus": args.vus,
        "abusers": results,
        "victims": victims,
        "leak_count": len(run.rec.leaks),
        "failures": run.rec.failures[:20],
    }
    for v in [*roster, *abusers]:
        await v.client.aclose()
    return out


# --- attachment storage failure drill -------------------------------------------------------------
async def cmd_attach(args: argparse.Namespace) -> dict[str, Any]:
    """The stack's attachment storage is an S3-compatible endpoint with a control port
    (`s3_control.py`). Normal users keep working while uploads and downloads run continuously and
    the store is made to fail in turn: refused, black-holed, slow, 403, 500, an object missing,
    a download that breaks mid-body. Per fault: what uploads/downloads answered and how long they
    took, and what the rest of the CRM experienced against its own baseline."""
    accounts = load_accounts(args.users)
    run = Run(args, accounts)
    roster = vu_roster(run, args.vus)
    run.vus = roster
    uploaders = [VU(run, a, 400 + i) for i, a in enumerate(accounts["users"][-8:])]
    sessions = Path(args.sessions) if args.sessions else None
    saved: dict[str, dict[str, str]] = {}
    if sessions and sessions.exists():
        saved = json.loads(sessions.read_text(encoding="utf-8"))
    for v in [*roster, *uploaders]:
        if await v.login(saved.get(v.account["email"])):
            await v.prime()
    run.target_active = args.vus
    tasks = [asyncio.create_task(v.loop()) for v in roster]
    control = args.s3_control
    stop = asyncio.Event()
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    lat: dict[str, list[float]] = defaultdict(list)
    state = {"phase": "baseline"}
    made: dict[int, list[str]] = defaultdict(list)
    uploaded: Counter[int] = Counter()

    async def notes_for(v: VU) -> str | None:
        r = await v.call(
            "attach_setup_note",
            "POST",
            v.path("activities"),
            body={
                "type": "note",
                "opportunity": next(iter(v.opps)),
                "description": f"attach drill {v.token}",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
            expected=(201,),
        )
        return r.json()["id"] if r is not None and r.status_code == 201 else None

    async def pump(v: VU, idx: int) -> None:
        note = await notes_for(v)
        n = 0
        while not stop.is_set() and note:
            n += 1
            started = now()
            if n % 2:
                try:
                    r = await v.client.post(
                        v.path(f"activities/{note}/attachments"),
                        content=(b"capacity drill line\n" * 40) + str(n).encode(),
                        headers={
                            "X-Filename": f"drill{idx}-{n}.txt",
                            "Content-Type": "application/octet-stream",
                            "X-CSRFToken": v.csrf(),
                        },
                        timeout=45,
                    )
                    status = r.status_code
                    if status == 201:
                        made[idx].append(r.json()["id"])
                        uploaded[idx] += 1
                        if uploaded[idx] % 9 == 0:
                            note = await notes_for(v)
                    elif status == 422:
                        note = await notes_for(v)
                except httpx.HTTPError:
                    status = 0
                kind = "upload"
            else:
                if not made[idx]:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    r = await v.client.get(
                        v.path(f"attachments/{random.choice(made[idx])}/download"), timeout=45
                    )
                    status = r.status_code
                except httpx.HTTPError:
                    status = 0
                kind = "download"
            ms = (now() - started) * 1000
            key = f"{state['phase']}|{kind}"
            stats[key][str(status)] += 1
            lat[key].append(ms)
            run.rec.add(f"attach_{kind}", status, ms, v.position)
            await asyncio.sleep(1.1)

    pumps = [asyncio.create_task(pump(v, i)) for i, v in enumerate(uploaders)]

    def s3(path: str) -> None:
        httpx.get(f"{control}{path}", timeout=5)

    scenarios = [
        (
            "refused",
            lambda: s3("/fault?kind=503"),
            "5xx answers (the closest to a dead service the fake offers)",
        ),
        ("black_hole", lambda: s3("/fault?kind=hang&seconds=60"), "accepted, never answered"),
        ("slow_5s", lambda: s3("/fault?kind=slow&seconds=5"), "every request answered after 5 s"),
        ("access_denied_403", lambda: s3("/fault?kind=403"), "IAM / bucket policy fault"),
        ("server_error_500", lambda: s3("/fault?kind=500"), "internal errors"),
        (
            "object_missing_404",
            lambda: s3("/fault?kind=404&methods=GET,HEAD"),
            "objects gone: downloads 404",
        ),
        (
            "download_breaks_mid_body",
            lambda: s3("/fault?kind=drop&methods=GET"),
            "connection closes mid-body",
        ),
    ]
    run.rec.stage = "baseline"
    await asyncio.sleep(args.baseline_seconds)
    results: dict[str, Any] = {}
    for name, inject, note in scenarios:
        state["phase"] = name
        run.rec.stage = name
        inject()
        await asyncio.sleep(args.abuse_seconds)
        s3("/heal")
        state["phase"] = f"recover_{name}"
        run.rec.stage = f"recover_{name}"
        await asyncio.sleep(args.cooldown_seconds)
        results[name] = {"fault": note}
    stop.set()
    run.stop.set()
    await asyncio.gather(*pumps, *tasks, return_exceptions=True)
    out: dict[str, Any] = {"vus": args.vus, "faults": results, "uploads_downloads": {}, "crm": {}}
    for key in sorted(stats):
        ordered = sorted(lat[key])
        out["uploads_downloads"][key] = {
            "statuses": dict(stats[key]),
            "p50": round(statistics.median(ordered)) if ordered else None,
            "p95": round(percentile(ordered, 0.95)) if ordered else None,
            "max": round(ordered[-1]) if ordered else None,
        }
    crm = Recorder()  # the rest of the CRM only: the uploads and downloads are reported above
    crm.samples = [x for x in run.rec.samples if not x.name.startswith(("attach_", "page:attach"))]
    for stage in ["baseline", *results, *[f"recover_{k}" for k in results]]:
        api = aggregate(crm, stage, pages=False)
        pages = aggregate(crm, stage, pages=True)
        out["crm"][stage] = {
            "api": {
                k: api.get(k)
                for k in ("n", "p50", "p95", "p99", "server_or_network_errors", "unexpected_4xx")
            },
            "pages": {k: pages.get(k) for k in ("n", "p50", "p95")},
        }
    out["timeline"] = timeline(run.rec)
    for v in [*roster, *uploaders]:
        await v.client.aclose()
    return out


# --- timeline ----------------------------------------------------------------------------------
def timeline(rec: Recorder, bucket: int = 10) -> list[dict[str, Any]]:
    """Requests, errors and p95 per `bucket` seconds of wall clock: the shape of a blip
    during a failure drill and the recovery after it."""
    rows: dict[int, list[Sample]] = defaultdict(list)
    for s in rec.samples:
        if not s.name.startswith("page:") and s.status != 429:
            rows[int(s.at // bucket)].append(s)
    out = []
    for key in sorted(rows):
        samples = rows[key]
        ordered = sorted(x.ms for x in samples)
        out.append(
            {
                "t": datetime.fromtimestamp(key * bucket).strftime("%H:%M:%S"),
                "stage": samples[0].stage,
                "n": len(samples),
                "errors": sum(1 for x in samples if x.status == 0 or x.status >= 500),
                "p50": round(statistics.median(ordered)),
                "p95": round(percentile(ordered, 0.95)),
                "max": round(ordered[-1]),
            }
        )
    return out


# --- main ---------------------------------------------------------------------------------------
def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", default="https://localhost:8443")
    parser.add_argument("--users", default="users.json")
    parser.add_argument("--sessions", default="")
    parser.add_argument(
        "--pg", default="postgres://arkray:arkray_dev_only_password@127.0.0.1:55432/arkray_load"
    )
    parser.add_argument("--db-name", default="arkray_load")
    parser.add_argument("--redis-url", default="")
    parser.add_argument("--metrics-url", default="http://127.0.0.1:8300")
    parser.add_argument("--metrics-token", default=os.environ.get("METRICS_TOKEN", ""))
    parser.add_argument(
        "--containers",
        default="arkray-load-backend-1,arkray-load-worker-1,arkray-load-worker-index-1,arkray-load-worker-ai-1,arkray-load-beat-1,arkray-load-redis-1,arkray-load-frontend-1,arkray-load-nginx-1,arkray-postgres-1",
    )
    parser.add_argument("--monitor-every", type=float, default=5.0)
    parser.add_argument("--think-mean", type=float, default=2.0)
    parser.add_argument("--out", default="")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument(
        "--pg", default="postgres://arkray:arkray_dev_only_password@127.0.0.1:55432/arkray_load"
    )
    p.add_argument("--vus", type=int, default=100)
    p.add_argument("--admins", type=int, default=3)
    p.add_argument("--heavy", type=int, default=15)
    p.add_argument("--password", required=True)
    p.add_argument("--out", default="users.json")
    r = sub.add_parser("run")
    common(r)
    r.add_argument("--vus", type=int, default=100)
    r.add_argument("--stages", default="10,25,50,75")
    r.add_argument("--stage-seconds", type=int, default=90)
    r.add_argument("--warmup", type=int, default=60)
    r.add_argument("--hold-seconds", type=int, default=600)
    r.add_argument("--marker", default="")
    for name in ("races", "search", "ask", "isolate", "abuse", "attach"):
        s = sub.add_parser(name)
        common(s)
        s.add_argument("--vus", type=int, default=100)
        s.add_argument("--hold-seconds", type=int, default=120)
        s.add_argument("--ask-gap", type=float, default=3.0)
        s.add_argument("--abuse-seconds", type=int, default=40)
        s.add_argument("--s3-control", default="http://127.0.0.1:9100")
        s.add_argument("--baseline-seconds", type=int, default=45)
        s.add_argument("--cooldown-seconds", type=int, default=20)
    args = parser.parse_args()
    if args.command == "prepare":
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        cmd_prepare(args)
        return
    runner = {
        "run": run_load,
        "races": cmd_races,
        "search": cmd_search,
        "ask": cmd_ask,
        "isolate": cmd_isolate,
        "abuse": cmd_abuse,
        "attach": cmd_attach,
    }[args.command]
    report = asyncio.run(runner(args))
    text = json.dumps(report, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"report -> {args.out}")
    print_summary(args.command, report)


def print_summary(command: str, report: dict[str, Any]) -> None:
    if command == "run":
        for stage, data in report["stages"].items():
            api, pages = data["api"], data["pages"]
            print(
                f"\n== {stage}: {data['seconds']} s, {api.get('n', 0)} API requests, {api.get('requests_per_s')} req/s,"
                f" errors {api.get('server_or_network_errors')}+{api.get('unexpected_4xx')} ({data['error_rate_pct']} %),"
                f" 429s {api.get('throttled_429')}"
            )
            print(
                f"   API  p50 {api.get('p50')} p95 {api.get('p95')} p99 {api.get('p99')} max {api.get('max')}"
            )
            print(f"   page p50 {pages.get('p50')} p95 {pages.get('p95')} p99 {pages.get('p99')}")
        print("\nverification:", json.dumps(report["verification"], indent=1, default=str))
        print(
            "leaks:",
            report["leak_count"],
            "probe leaks:",
            len(report["probe_leaks"]),
            "dashboard mismatches:",
            report["dashboard_mismatch_count"],
            "signed in:",
            report.get("virtual_users_signed_in"),
            "sessions ended:",
            len(report.get("sessions_ended", [])),
        )
    else:
        print(json.dumps(report, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
