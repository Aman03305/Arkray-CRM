# ruff: noqa: T201
# An operator's drill (prints its progress and its result), not application code.
"""Resilience drills under load, repeatable on any environment
(docs/staging-validation.md#6-resilience-drills).

    python tests/drills/resilience_drill.py list
    python tests/drills/resilience_drill.py plan --drills all --prefix arkray-prod-
    python tests/drills/resilience_drill.py run --environment local --drills all \
        --users users.json --sessions sessions.json --pg postgres://... --vus 50
    python tests/drills/resilience_drill.py restore --drills all --prefix arkray-prod-

A steady background load runs for the whole series. Each drill measures a baseline, injects
ONE fault at a fixed offset (`--baseline` seconds in), holds it, restores it and watches the
recovery: what users saw (errors by status, latency p50/p95/p99 before, during and after),
the time to recovery (the first healthy readiness answer and the error rate back at the
baseline's, whichever comes later), the outbox backlog draining (pending, in flight, oldest,
dead: the metrics endpoint), whether any container restarted or was OOM-killed, and then,
with the load paused, the data: capacity_test.py's own `verify` (duplicates, orphans, lost
updates, impossible states, dashboard arithmetic against SQL), new cross-user leaks, and for
the worker kill every outbox event done exactly once and every note indexed. Each drill has
a written pass criterion (`list` prints them) and ends PASS, FAIL or INCONCLUSIVE (something
the criterion needs could not be measured: no `--pg`, no readiness probe, an operator who
didn't confirm, the load generator's own host stalling).

The load is capacity_test.py's virtual users (the same action mix, think times, canary
tokens and leak scanning, the same account file and saved sessions), driven from this
process rather than `capacity_test.py run` in a subprocess, because:
1. every request's completion time and status are at hand, so the windows, the statuses and
   the recovery are cut at the exact instants of the fault (`run`'s report keeps 10-second
   buckets of local clock times without statuses, nothing per request);
2. the fault is injected on the same clock, with no marker file between two processes;
3. the load pauses after each drill, so `verify` runs per drill on the same users' records
   (`run` verifies once, at its end, and couldn't attribute a problem to a drill);
4. the users sign in once for the whole series (the 20-a-minute sign-in limit makes 100
   sign-ins five minutes; saved sessions are reused exactly as capacity_test.py reuses them).

Fault drivers:
- `docker`: a compose stack's containers, `<prefix><service>-1` (`--prefix`; `--container
  service=name` for one named otherwise, such as the capacity runs' shared database). Every
  docker call is bounded by a timeout: a docker CLI can hang under load.
- `manual`: prints the operator's action and waits for Enter (or `--manual-timeout`), for
  managed services where the operator uses the console (an RDS reboot or failover,
  ElastiCache). `--manual-services postgres,redis,redis-cache` mixes the two.

A fault is always restored: its restore steps are owed before the first inject step runs (a
fault applied halfway is undone too), paid in a `finally` on any failure or Ctrl+C, again at
exit, and by the `restore` sub-command after a hard crash of this process.

Every report is labelled with `--environment` (required, also in every result cell): a local
run is evidence about the application, never about real hosting
(docs/staging-validation.md#local-validation-vs-real-hosting).
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import hashlib
import itertools
import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

BACKEND = Path(__file__).resolve().parents[2]
API = "/api/v1"
KIB = 1024
MIB = 1024 * KIB
ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
LOCAL_CAVEAT = (
    "Local run: evidence about the application on this machine's production-shaped stack, not"
    " about real hosting (network latency, managed failover, S3, TLS hops, platform proxies,"
    " noisy neighbours): docs/staging-validation.md#local-validation-vs-real-hosting."
)
# The containers whose restarts and OOM kills every drill watches (missing ones are skipped).
WATCH = (
    "backend,worker,worker-outbox,worker-index,worker-ai,beat,nginx,frontend,redis,"
    "redis-cache,postgres,clamav"
)
# Requests that had been running this long when the API was told to stop were inside a
# gunicorn worker, not still queued in its listen backlog (which a stop refuses like new ones).
INFLIGHT_MIN_AGE_S = 0.25
VERIFY_ALLOWANCE_S = 20.0
SCAN_TIMEOUT_S = 300.0
RESTORE_DRILL_TIMEOUT_S = 1800.0


# --- the catalogue ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    """One step of a fault: a verb on a compose service."""

    verb: str  # stop | start | pause | unpause | kill | kill-child
    service: str
    grace_s: int = 10  # `docker stop --time`: the service's own stop_grace_period

    def describe(self) -> str:
        return f"{self.verb} {self.service}"


VERBS = ("stop", "start", "pause", "unpause", "kill", "kill-child")


@dataclass(frozen=True)
class Criteria:
    """What a drill must show to pass. Windows: the fault (inject to restore), the recovery
    (the drill's `recovery_s` after the restore) and the baseline (`--baseline` before it)."""

    # Errors users saw (5xx, no answer, unexpected 4xx) in the fault and recovery windows: at
    # most this many percentage points above the baseline's rate, or at most `max_errors`.
    max_extra_error_pct: float | None = 0.1
    max_errors: int | None = None
    # Where errors are the expected behaviour: the only statuses (and envelope codes) allowed.
    error_statuses: frozenset[int] | None = None
    error_codes: frozenset[str] | None = None
    # p95 during the fault at most max(max_p95_ms, p95_factor x the baseline's p95).
    max_p95_ms: float | None = None
    p95_factor: float = 3.0
    # No single request in the fault and recovery windows longer than this (bounded waits).
    max_request_ms: float | None = None
    max_recovery_s: float | None = 60.0
    # "ready": readiness never answered 503; "unready": it did during the fault; "any".
    readiness: str = "ready"
    liveness_ok: bool = False
    # The outbox backlog ("pending", or "all": pending + in flight) back at the baseline's
    # level within `max_drain_s` of the restore, and no event dead.
    drain_scope: str | None = None
    max_drain_s: float = 180.0
    graceful_exit: bool = False
    workers_restored: bool = False
    inflight_ok: bool = False
    exactly_once: bool = False
    integrity: bool = True

    def describe(self) -> list[str]:
        lines: list[str] = []
        if self.max_errors is not None:
            lines.append(f"at most {self.max_errors} failed requests (fault and recovery)")
        elif self.max_extra_error_pct is not None:
            lines.append(
                "error rate (fault and recovery) at most the baseline's"
                f" + {self.max_extra_error_pct:g} percentage points"
            )
        if self.error_statuses is not None:
            allowed = "/".join(str(s) for s in sorted(self.error_statuses))
            codes = f" `{'`, `'.join(sorted(self.error_codes))}`" if self.error_codes else ""
            lines.append(f"errors only {allowed}{codes}")
        if self.max_p95_ms is not None:
            lines.append(
                f"p95 during the fault at most max({self.max_p95_ms:g} ms,"
                f" {self.p95_factor:g} x baseline)"
            )
        if self.max_request_ms is not None:
            lines.append(f"no request longer than {self.max_request_ms / 1000:g} s")
        if self.max_recovery_s is not None:
            lines.append(
                "recovered (readiness healthy, error rate at the baseline's) within"
                f" {self.max_recovery_s:g} s of the restore"
            )
        if self.readiness == "ready":
            lines.append("readiness never 503")
        elif self.readiness == "unready":
            lines.append("readiness 503 during the fault")
        if self.liveness_ok:
            lines.append("liveness 200 throughout")
        if self.drain_scope:
            lines.append(
                f"outbox backlog ({self.drain_scope}) back at the baseline's within"
                f" {self.max_drain_s:g} s of the restore, no event dead"
            )
        if self.graceful_exit:
            lines.append("the stopped service exited 0 (graceful)")
        if self.workers_restored:
            lines.append("the master replaced the killed process")
        if self.inflight_ok:
            lines.append("requests being served at the stop finished without an error")
        if self.exactly_once:
            lines.append(
                "every outbox event since the drill began done, none dead; every note written"
                " indexed (exactly once: the chunk table's unique key)"
            )
        if self.integrity:
            lines.append(
                "no duplicate, orphan, lost update, impossible state, dashboard mismatch or"
                " cross-user leak"
            )
        lines.append("no other container restarted, none OOM-killed")
        return lines


@dataclass(frozen=True)
class Drill:
    name: str
    title: str
    expectation: str
    criteria: Criteria
    service: str | None = None
    inject: tuple[Action, ...] = ()
    restore: tuple[Action, ...] = ()
    hold_s: float = 30.0
    recovery_s: float = 90.0
    drain_timeout_s: float = 180.0
    kind: str = "fault"  # fault | edge | scan | restore
    workload: str | None = None  # "uploads": files uploaded and fetched through the drill
    needs_load: bool = True

    def retarget(self, service: str) -> Drill:
        def swap(action: Action) -> Action:
            return replace(action, service=service) if action.service == self.service else action

        return replace(
            self,
            service=service,
            inject=tuple(swap(a) for a in self.inject),
            restore=tuple(swap(a) for a in self.restore),
        )

    def restarted(self) -> set[str]:
        """Services the fault itself stops, kills or starts: their restart is the drill."""
        return {
            a.service for a in (*self.inject, *self.restore) if a.verb in ("stop", "start", "kill")
        }

    def pass_criteria(self) -> list[str]:
        if self.kind == "scan":
            return [
                "the EICAR file blocked (422 `blocked by the virus scan`, never served)",
                "the clean file served with identical bytes",
                "the blocked file's object purged (reported; needs --pg)",
            ]
        if self.kind == "restore":
            return ["restore_drill.py reports PASS (the operator, for a managed database)"]
        lines = self.criteria.describe()
        if self.kind == "edge":
            lines.insert(
                0,
                "413 `file_too_large` for a JSON body over 1 MB and an upload over 11 MB; 429"
                " `rate_limited` for the sign-in burst, the 9th body and the 5th upload in flight",
            )
        if self.workload == "uploads":
            lines.insert(
                0,
                "uploads during the outage answered 201; none served before its scan; every"
                " file clean and served intact before the drain timeout",
            )
        return lines


CATALOGUE: tuple[Drill, ...] = (
    Drill(
        name="api-worker-kill",
        title="one gunicorn worker SIGKILLed inside the API container",
        expectation=(
            "The request in flight on that worker fails (nginx answers 503"
            " `service_unavailable`); gunicorn's master starts a replacement at once; nothing"
            " else notices (measured: 1 error, no latency effect)."
        ),
        service="backend",
        inject=(Action("kill-child", "backend"),),
        hold_s=10,
        recovery_s=60,
        criteria=Criteria(
            max_extra_error_pct=None,
            max_errors=2,
            error_statuses=frozenset({503}),
            max_recovery_s=15,
            workers_restored=True,
        ),
    ),
    Drill(
        name="redis-cache-stop",
        title="the cache Redis stopped for 30 s",
        expectation=(
            "Cache misses instead of errors behind a fail-fast breaker; request rates counted"
            " per process (R127); readiness `degraded` (still 200); no user errors, latency"
            " barely moves; readiness back to `ok` within the breaker's cool-down (15 s"
            " doubling to 120 s)."
        ),
        service="redis-cache",
        inject=(Action("stop", "redis-cache"),),
        restore=(Action("start", "redis-cache"),),
        hold_s=30,
        recovery_s=150,
        criteria=Criteria(max_p95_ms=1500, max_recovery_s=150),
    ),
    Drill(
        name="redis-pause",
        title="the cache Redis paused for 30 s (stalled, not refused)",
        expectation=(
            "Requests wait at most the cache's 1 s socket timeouts until each process's breaker"
            " opens, then run normally; no errors; no request waits for the whole pause"
            " (measured: p95 1.5 s for the first 10 s). `--target redis` stalls the broker"
            " instead (background work only)."
        ),
        service="redis-cache",
        inject=(Action("pause", "redis-cache"),),
        restore=(Action("unpause", "redis-cache"),),
        hold_s=30,
        recovery_s=150,
        criteria=Criteria(max_p95_ms=3000, max_request_ms=10000, max_recovery_s=150),
    ),
    Drill(
        name="redis-broker-stop",
        title="the broker Redis stopped for 30 s",
        expectation=(
            "Background work pauses; CRM writes still succeed (their outbox rows wait in"
            " PostgreSQL); readiness unaffected; beat and the workers reconnect by themselves;"
            " the backlog drains within the in-flight caps, none dead (measured: 406 events"
            " done 50 s after Redis returned)."
        ),
        service="redis",
        inject=(Action("stop", "redis"),),
        restore=(Action("start", "redis"),),
        hold_s=30,
        criteria=Criteria(max_p95_ms=1000, drain_scope="pending", max_drain_s=120),
    ),
    Drill(
        name="beat-restart",
        title="beat stopped (SIGTERM) for 30 s and started",
        expectation=(
            "No relay ticks while beat is down, so outbox events wait (pending); the CRM is"
            " unaffected; beat exits 0; after the start the backlog drains, none dead."
        ),
        service="beat",
        inject=(Action("stop", "beat"),),
        restore=(Action("start", "beat"),),
        hold_s=30,
        criteria=Criteria(
            max_p95_ms=1000, graceful_exit=True, drain_scope="pending", max_drain_s=120
        ),
    ),
    Drill(
        name="worker-restart",
        title="the relay worker stopped (SIGTERM, warm shutdown) for 60 s and started",
        expectation=(
            "Warm shutdown: the running task finishes and the worker exits 0 (2-3 s measured)."
            " The outbox relay runs on this worker (R128), so every queue's events wait in"
            " PostgreSQL; the CRM is unaffected; after the start the backlog drains (20"
            " events/s per queue at most), none dead (measured: 130 events, drained within"
            " 10 s). `--target worker` drills the default-queue worker (scans, exports)."
        ),
        service="worker-outbox",
        inject=(Action("stop", "worker-outbox", grace_s=130),),
        restore=(Action("start", "worker-outbox"),),
        hold_s=60,
        criteria=Criteria(
            max_p95_ms=1000, graceful_exit=True, drain_scope="pending", max_drain_s=120
        ),
    ),
    Drill(
        name="worker-kill",
        title="the indexing worker SIGKILLed mid-task, started 20 s later",
        expectation=(
            "Unacknowledged messages are redelivered (acks_late) and done after the start; the"
            " event the killed container was running waits out its 5-minute running lease,"
            " then runs again under a new claim (4 min 51 s in the whole-software audit);"
            " every note written during the drill is indexed exactly once (the chunk table's"
            " unique key); none dead; users unaffected. `--target worker` kills the"
            " default-queue worker."
        ),
        service="worker-index",
        inject=(Action("kill", "worker-index"),),
        restore=(Action("start", "worker-index"),),
        hold_s=20,
        drain_timeout_s=420,
        criteria=Criteria(max_p95_ms=1000, drain_scope="all", max_drain_s=420, exactly_once=True),
    ),
    Drill(
        name="api-restart",
        title="the API container stopped (SIGTERM) under load and started at once",
        expectation=(
            "gunicorn finishes the requests its workers are serving and exits 0 within the 35 s"
            " grace; until the new container answers, nginx answers 503 `service_unavailable`"
            " and nothing else; sessions survive (no 401); healthy within a minute. With two or"
            " more API instances behind a load balancer that drains on readiness, users should"
            " see no error at all."
        ),
        service="backend",
        inject=(Action("stop", "backend", grace_s=35),),
        restore=(Action("start", "backend"),),
        hold_s=0,
        criteria=Criteria(
            max_extra_error_pct=None,
            error_statuses=frozenset({503}),
            error_codes=frozenset({"service_unavailable"}),
            max_recovery_s=60,
            readiness="any",
            graceful_exit=True,
            inflight_ok=True,
            drain_scope="pending",
            max_drain_s=120,
        ),
    ),
    Drill(
        name="postgres-stop",
        title="PostgreSQL stopped for 45 s",
        expectation=(
            "The one hard dependency: the API answers 503 `service_unavailable` (Retry-After"
            " 30), never a 500 or a hung request; readiness 503 (the load balancer drains),"
            " liveness 200 (no restart loop); healthy again within 5-13 s of PostgreSQL"
            " returning, without restarting anything; nothing written is lost. Background work"
            " waits (an event whose worker failed before claiming it waits out its dispatch"
            " lease, SRE-5)."
        ),
        service="postgres",
        inject=(Action("stop", "postgres", grace_s=30),),
        restore=(Action("start", "postgres"),),
        hold_s=45,
        criteria=Criteria(
            max_extra_error_pct=None,
            error_statuses=frozenset({503}),
            error_codes=frozenset({"service_unavailable"}),
            max_request_ms=15000,
            max_recovery_s=30,
            readiness="unready",
            liveness_ok=True,
            drain_scope="pending",
            max_drain_s=180,
        ),
    ),
    Drill(
        name="postgres-stall",
        title="PostgreSQL paused for 45 s (stalled, not refused: R81)",
        expectation=(
            "Requests wait in their query until gunicorn's 30 s worker timeout and nginx"
            " answers 503 (its envelope for 502/504) at 30-35 s; readiness answers 503 within"
            " its 2 s deadline while a worker is free; everything resumes the moment the"
            " database does; only the timed-out gunicorn workers restart. No request waits"
            " longer than the proxy's 35 s."
        ),
        service="postgres",
        inject=(Action("pause", "postgres"),),
        restore=(Action("unpause", "postgres"),),
        hold_s=45,
        recovery_s=120,
        criteria=Criteria(
            max_extra_error_pct=None,
            error_statuses=frozenset({503}),
            error_codes=frozenset({"service_unavailable"}),
            max_request_ms=40000,
            max_recovery_s=45,
            readiness="any",
            drain_scope="pending",
            max_drain_s=180,
        ),
    ),
    Drill(
        name="edge-limits",
        title="one client over the edge's per-client limits",
        expectation=(
            "nginx refuses what is over its limits in the API's JSON envelope: a JSON body over"
            " 1 MB and an upload over 11 MB 413 `file_too_large`; a sign-in burst over 5/s"
            " (burst 30), a 9th request with a body in flight and a 5th upload in flight 429"
            " `rate_limited`; nginx is neither OOM-killed nor restarted; the users see no"
            " errors (sharing the abuser's address, as on one machine, their writes can meet"
            " the per-address limits too: their 429s are reported)."
        ),
        service="nginx",
        kind="edge",
        hold_s=0,
        recovery_s=60,
        criteria=Criteria(max_p95_ms=1000, max_recovery_s=None),
    ),
    Drill(
        name="attachment-scan",
        title="the EICAR test file and a clean file uploaded and scanned end to end",
        expectation=(
            'The EICAR file is blocked (422 "blocked by the virus scan", never served) and its'
            " object purged; the clean file becomes downloadable with identical bytes."
        ),
        service="worker",
        kind="scan",
        hold_s=0,
        recovery_s=0,
        needs_load=False,
        criteria=Criteria(max_extra_error_pct=None, max_recovery_s=None, integrity=False),
    ),
    Drill(
        name="clamav-stop",
        title="the ClamAV daemon stopped for 60 s while files are uploaded",
        expectation=(
            "Uploads are still accepted (201, being checked); a file not scanned yet can't be"
            " downloaded (422); the scan jobs retry with backoff; once clamd is back (about a"
            " minute to load its signatures) every file becomes clean and downloadable with"
            " identical bytes; the rest of the CRM is unaffected."
        ),
        service="clamav",
        inject=(Action("stop", "clamav", grace_s=30),),
        restore=(Action("start", "clamav"),),
        hold_s=60,
        drain_timeout_s=600,
        workload="uploads",
        criteria=Criteria(max_p95_ms=1000),
    ),
    Drill(
        name="backup-restore",
        title="encrypted backup, restore and erasure replay (tests/drills/restore_drill.py)",
        expectation=(
            "restore_drill.py PASS: a signed, encrypted backup restores; the API stays closed"
            " until the erasures are replayed; an altered or unsigned dump is refused"
            " (docs/runbooks.md#restore-drill)."
        ),
        service="postgres",
        kind="restore",
        hold_s=0,
        recovery_s=0,
        needs_load=False,
        criteria=Criteria(max_extra_error_pct=None, max_recovery_s=None, integrity=False),
    ),
)


def select_drills(spec: str, target: str = "") -> list[Drill]:
    by_name = {d.name: d for d in CATALOGUE}
    if spec.strip() == "all":
        chosen = list(CATALOGUE)
    else:
        names = [n.strip() for n in spec.split(",") if n.strip()]
        unknown = [n for n in names if n not in by_name]
        if unknown or not names:
            raise SystemExit(f"unknown drill {unknown or spec!r}; known: {', '.join(by_name)}")
        chosen = [by_name[n] for n in names]
    if target:
        if len(chosen) != 1:
            raise SystemExit("--target replaces one drill's service: select exactly one drill")
        chosen = [chosen[0].retarget(target)]
    return chosen


# --- fault drivers -----------------------------------------------------------------------------
class DriverError(RuntimeError):
    """A fault step that failed or didn't finish in time."""


class GateError(RuntimeError):
    """The stack isn't healthy enough to start the next drill."""


class Driver(Protocol):
    name: str

    def apply(self, action: Action) -> dict[str, Any]: ...

    def describe(self, action: Action) -> str: ...

    def inspect(self, services: Sequence[str]) -> dict[str, dict[str, Any] | None] | None: ...

    def children(self, service: str) -> int | None: ...

    def is_manual(self, service: str) -> bool: ...


Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def run_bounded(command: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Every docker call ends: a CLI that hangs (Docker Desktop does under load) is killed
    when its time is up, and the step counts as failed."""
    try:
        return subprocess.run(  # noqa: S603 - fixed commands
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise DriverError(f"{' '.join(command[1:3])} timed out after {timeout_s:g} s") from exc


# The children of PID 1 (gunicorn's workers, Celery's pool processes): the exec'd process
# itself has no parent inside the container, so it is never among them.
_CHILDREN = """
import os, random, signal
children = []
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            with open("/proc/" + pid + "/stat") as f:
                stat = f.read()
        except OSError:
            continue
        if stat.rsplit(")", 1)[1].split()[1] == "1":
            children.append(int(pid))
"""
KILL_CHILD = (
    _CHILDREN
    + """
if not children:
    raise SystemExit("no child process of PID 1")
victim = random.choice(children)
os.kill(victim, signal.SIGKILL)
print(victim, len(children))
"""
)
COUNT_CHILDREN = _CHILDREN + "\nprint(len(children))\n"
# Readiness, liveness and the metrics from inside the API container: what an orchestrator's
# probe sees, and the metrics token from the container's own environment.
PROBE = """
import json, os, time, urllib.error, urllib.request
def get(path, token=""):
    headers = {"Host": "localhost"}
    if token:
        headers["Authorization"] = "Bearer " + token
    url = "http://127.0.0.1:" + os.environ.get("PORT", "8000") + path
    started = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as r:
            status, body = r.status, r.read().decode()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read().decode()
    except Exception:
        status, body = 0, ""
    return [status, body, round((time.monotonic() - started) * 1000, 1)]
print(json.dumps({"ready": get("/health/ready"), "live": get("/health/live"),
                  "metrics": get("/health/metrics", os.environ.get("METRICS_TOKEN", ""))}))
"""


class DockerDriver:
    """Containers of a compose project on this host (the production-shaped stack, or a
    single-VM staging host running compose)."""

    name = "docker"

    def __init__(
        self,
        prefix: str,
        overrides: dict[str, str] | None = None,
        *,
        timeout_s: float = 60.0,
        runner: Runner = run_bounded,
        docker: str = "docker",
    ) -> None:
        self.prefix = prefix
        self.overrides = dict(overrides or {})
        self.timeout_s = timeout_s
        self.runner = runner
        self.docker = docker

    def container(self, service: str) -> str:
        return self.overrides.get(service, f"{self.prefix}{service}-1")

    def command(self, action: Action) -> list[str]:
        name = self.container(action.service)
        if action.verb == "stop":
            return [self.docker, "stop", "--time", str(action.grace_s), name]
        if action.verb in ("start", "pause", "unpause"):
            return [self.docker, action.verb, name]
        if action.verb == "kill":
            return [self.docker, "kill", "--signal", "KILL", name]
        if action.verb == "kill-child":
            return [self.docker, "exec", name, "python", "-c", KILL_CHILD]
        raise ValueError(f"unknown verb {action.verb!r}")

    def describe(self, action: Action) -> str:
        command = self.command(action)
        if action.verb == "kill-child":
            command = [*command[:-1], "'<SIGKILL one child of PID 1>'"]
        return " ".join(["docker", *command[1:]])

    def timeout_for(self, action: Action) -> float:
        # A stop waits up to its grace period before Docker kills: the CLI's bound is beyond it.
        return action.grace_s + self.timeout_s if action.verb == "stop" else self.timeout_s

    def apply(self, action: Action) -> dict[str, Any]:
        started = time.monotonic()
        result = self.runner(self.command(action), self.timeout_for(action))
        out: dict[str, Any] = {"action": action.describe(), "command": self.describe(action)}
        text = (result.stdout + result.stderr).strip()
        out["seconds"] = round(time.monotonic() - started, 1)
        if result.returncode != 0:
            # Restoring what needs no restoring isn't a failure (a container never paused).
            if action.verb == "unpause" and "is not paused" in text:
                out["note"] = "was not paused"
                return out
            raise DriverError(f"{out['command']}: exit {result.returncode}: {text[:300]}")
        if action.verb == "stop":
            state = (self.inspect([action.service]) or {}).get(action.service)
            out["exit_code"] = state["exit_code"] if state else None
        elif action.verb == "kill-child":
            fields = text.split()
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                out["killed_pid"], out["children_before"] = int(fields[0]), int(fields[1])
        return out

    def exec_python(self, service: str, script: str, timeout_s: float) -> str:
        command = [self.docker, "exec", self.container(service), "python", "-c", script]
        result = self.runner(command, timeout_s)
        if result.returncode != 0:
            raise DriverError(f"exec in {self.container(service)}: exit {result.returncode}")
        return result.stdout

    def children(self, service: str) -> int | None:
        try:
            return int(self.exec_python(service, COUNT_CHILDREN, self.timeout_s).strip())
        except (DriverError, ValueError):
            return None

    def inspect(self, services: Sequence[str]) -> dict[str, dict[str, Any] | None]:
        names = {self.container(s): s for s in services}
        found: dict[str, dict[str, Any] | None] = dict.fromkeys(services)
        if not names:
            return found
        template = (
            "{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}"
            "|{{.State.OOMKilled}}|{{.State.ExitCode}}"
        )
        try:
            result = self.runner(
                [self.docker, "inspect", "--format", template, *names], self.timeout_s
            )
        except DriverError:
            return found
        # A missing container makes the exit status 1; the others are still printed.
        for line in result.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 6 or parts[0].lstrip("/") not in names:
                continue
            found[names[parts[0].lstrip("/")]] = {
                "container": parts[0].lstrip("/"),
                "status": parts[1],
                "restarts": int(parts[2]),
                "started_at": parts[3],
                "oom_killed": parts[4] == "true",
                "exit_code": int(parts[5]),
            }
        return found

    def is_manual(self, service: str) -> bool:
        return False


MANUAL_STEPS = {
    ("stop", "postgres"): (
        "Make PostgreSQL REFUSE connections: stop the instance, or reboot it without failover"
        " (RDS: Reboot; Cloud SQL: Restart)."
    ),
    ("start", "postgres"): (
        "Bring PostgreSQL back (start it, or let the reboot finish) until the console shows it"
        " available."
    ),
    ("pause", "postgres"): (
        "STALL PostgreSQL (connections neither answered nor refused): a Multi-AZ failover"
        " (RDS: Reboot with failover), or a network rule that DROPS (not rejects) the"
        " application's traffic to port 5432."
    ),
    ("unpause", "postgres"): (
        "End the stall: remove the DROP rule, or wait until the failover has finished and the"
        " endpoint answers."
    ),
    ("stop", "redis"): (
        "Make the broker Redis unavailable (ElastiCache / Memorystore: reboot the node, or a"
        " network rule that REJECTS its port)."
    ),
    ("start", "redis"): "Bring the broker Redis back (the reboot finished, the rule removed).",
    ("stop", "redis-cache"): (
        "Make the cache Redis unavailable (reboot the node, or a REJECT rule on its port)."
    ),
    ("start", "redis-cache"): "Bring the cache Redis back.",
    ("pause", "redis-cache"): (
        "STALL the cache Redis: a network rule that DROPS (not rejects) the application's"
        " traffic to its port."
    ),
    ("unpause", "redis-cache"): "Remove the DROP rule from the cache Redis.",
}
MANUAL_VERBS = {
    "stop": "Stop",
    "start": "Start",
    "pause": "Stall (freeze, don't stop)",
    "unpause": "Unfreeze",
    "kill": "Kill (SIGKILL)",
    "kill-child": "Kill one worker process (SIGKILL) of",
}


def manual_step(action: Action) -> str:
    return MANUAL_STEPS.get(
        (action.verb, action.service),
        f"{MANUAL_VERBS[action.verb]} the {action.service} service (platform console or CLI).",
    )


class Prompter:
    """The operator's answers, from the terminal, each with a deadline: a missing answer is
    recorded as unconfirmed, never assumed."""

    def __init__(self, stream: Iterable[str] | None = None) -> None:
        self.stream: Iterable[str] = stream if stream is not None else sys.stdin
        self.lines: queue.Queue[str] = queue.Queue()
        self.reader: threading.Thread | None = None

    def _read(self) -> None:
        for line in self.stream:
            self.lines.put(line.rstrip("\r\n"))

    def ask(self, text: str, timeout_s: float) -> str | None:
        if self.reader is None:
            self.reader = threading.Thread(target=self._read, daemon=True, name="operator")
            self.reader.start()
        print(text, flush=True)
        try:
            return self.lines.get(timeout=timeout_s)
        except queue.Empty:
            return None


class ManualDriver:
    """The operator applies each step (managed services: the provider's console or CLI)."""

    name = "manual"

    def __init__(self, ask: Callable[[str, float], str | None], timeout_s: float) -> None:
        self.ask = ask
        self.timeout_s = timeout_s

    def describe(self, action: Action) -> str:
        return f"operator: {manual_step(action)}"

    def apply(self, action: Action) -> dict[str, Any]:
        started = time.monotonic()
        answer = self.ask(
            f"\n>>> {manual_step(action)}\n>>> Press Enter when done (unconfirmed after"
            f" {self.timeout_s:g} s).",
            self.timeout_s,
        )
        return {
            "action": action.describe(),
            "command": self.describe(action),
            "confirmed": answer is not None,
            "seconds": round(time.monotonic() - started, 1),
        }

    def inspect(self, services: Sequence[str]) -> None:
        return None

    def children(self, service: str) -> int | None:
        return None

    def is_manual(self, service: str) -> bool:
        return True


class RoutedDriver:
    """Docker for the containers, the operator for the managed services."""

    def __init__(
        self, docker: DockerDriver | None, manual: ManualDriver | None, manual_services: set[str]
    ) -> None:
        if docker is None and manual is None:
            raise ValueError("no driver")
        self.docker = docker
        self.manual = manual
        self.manual_services = manual_services
        self.name = "+".join(d.name for d in (docker, manual) if d is not None)

    def is_manual(self, service: str) -> bool:
        return self.docker is None or (self.manual is not None and service in self.manual_services)

    def _pick(self, service: str) -> DockerDriver | ManualDriver:
        if self.is_manual(service):
            assert self.manual is not None
            return self.manual
        assert self.docker is not None
        return self.docker

    def apply(self, action: Action) -> dict[str, Any]:
        return self._pick(action.service).apply(action)

    def describe(self, action: Action) -> str:
        return self._pick(action.service).describe(action)

    def inspect(self, services: Sequence[str]) -> dict[str, dict[str, Any] | None] | None:
        if self.docker is None:
            return None
        return self.docker.inspect([s for s in services if not self.is_manual(s)])

    def children(self, service: str) -> int | None:
        return self._pick(service).children(service)


# --- what users saw ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sample:
    """One API request a virtual user made: completed `at` (wall clock), its status (0: no
    answer) and duration."""

    at: float
    name: str
    status: int
    ms: float

    @property
    def started(self) -> float:
        return self.at - self.ms / 1000


# Requests that aren't the users' work: signing in, priming, the checks after a drill.
NOT_LOAD = ("login", "auth_csrf", "mfa_", "setup_", "dashboard_final")
# 4xx answers that belong to the mix (as capacity_test.aggregate counts them): a stale
# version's 409, the attack probes' refusals, a price revision racing a move.
EXPECTED_4XX = ("probe", "negotiation_revision")


def classify(sample: Sample) -> str:
    """ok | error | throttled | ignored (page totals, sign-in, priming, post-drill checks)."""
    if sample.name.startswith(("page:", *NOT_LOAD)):
        return "ignored"
    if sample.status == 429:
        return "throttled"
    if sample.status == 0 or sample.status >= 500:
        return "error"
    if 400 <= sample.status < 500 and sample.status != 409:
        return "ok" if sample.name.startswith(EXPECTED_4XX) else "error"
    return "ok"


def percentile(ordered: Sequence[float], q: float) -> float | None:
    if not ordered:
        return None
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))], 1)


def window_stats(samples: Iterable[Sample], start: float, end: float) -> dict[str, Any]:
    """The requests that completed in [start, end): count, errors by status, latency."""
    latencies: list[float] = []
    errors: Counter[int] = Counter()
    throttled = 0
    for sample in samples:
        if not start <= sample.at < end:
            continue
        kind = classify(sample)
        if kind == "ignored":
            continue
        if kind == "throttled":
            throttled += 1
            continue
        latencies.append(sample.ms)
        if kind == "error":
            errors[sample.status] += 1
    latencies.sort()
    failed = sum(errors.values())
    return {
        "seconds": round(end - start, 1),
        "requests": len(latencies),
        "errors": failed,
        "error_rate_pct": round(100 * failed / len(latencies), 3) if latencies else None,
        "errors_by_status": {str(k): v for k, v in sorted(errors.items())},
        "throttled_429": throttled,
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "max_ms": round(latencies[-1], 1) if latencies else None,
    }


def in_flight(samples: Iterable[Sample], at: float, min_age_s: float) -> list[Sample]:
    """Requests running at `at` that had been running for at least `min_age_s`."""
    return [
        s
        for s in samples
        if classify(s) in ("ok", "error") and s.started <= at - min_age_s and s.at > at
    ]


CODE = re.compile(r'"code"\s*:\s*"([a-z_]+)"')


def error_codes(failures: Iterable[dict[str, Any]], stages: set[str]) -> Counter[str]:
    """The API envelope's `code` of the failed requests capacity_test sampled (their bodies)."""
    codes: Counter[str] = Counter()
    for failure in failures:
        if failure.get("stage") in stages:
            found = CODE.search(str(failure.get("body", "")))
            codes[found.group(1) if found else "(no envelope)"] += 1
    return codes


def timeline(samples: Sequence[Sample], start: float, end: float, bucket_s: float) -> list[Any]:
    """Requests, errors and p95 per bucket: the shape of the blip, for the report."""
    window = [s for s in samples if start <= s.at < end]
    rows: list[Any] = []
    t = start
    while t < end:
        stats = window_stats(window, t, min(t + bucket_s, end))
        rows.append([round(t - start), stats["requests"], stats["errors"], stats["p95_ms"]])
        t += bucket_s
    return rows


# --- readiness, liveness and the metrics -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProbeReading:
    at: float
    kind: str  # ready | live
    status: int  # 0: no answer within the deadline
    state: str  # the body's "status": ok | degraded | unavailable
    ms: float


@dataclass(frozen=True, slots=True)
class MetricsReading:
    at: float
    values: dict[str, float] | None  # None: the scrape failed


METRIC = re.compile(r"^([a-z_]+)(?:\{([^}]*)\})?\s+(\S+)$")
LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse_metrics(text: str) -> dict[str, float]:
    """The outbox and dependency gauges of /health/metrics (config/metrics.py), summed over
    the queues (ages: the oldest of any queue)."""
    values = {
        "outbox_pending": 0.0,
        "outbox_in_flight": 0.0,
        "outbox_dead": 0.0,
        "outbox_oldest_due_s": 0.0,
        "outbox_oldest_undelivered_s": 0.0,
    }
    for line in text.splitlines():
        found = METRIC.match(line.strip())
        if not found:
            continue
        name, labels, raw = found.groups()
        try:
            value = float(raw)
        except ValueError:
            continue
        tags = dict(LABEL.findall(labels or ""))
        if name == "arkray_outbox_events" and f"outbox_{tags.get('status')}" in values:
            values[f"outbox_{tags['status']}"] += value
        elif name == "arkray_outbox_oldest_due_seconds":
            values["outbox_oldest_due_s"] = max(values["outbox_oldest_due_s"], value)
        elif name == "arkray_outbox_oldest_undelivered_seconds":
            values["outbox_oldest_undelivered_s"] = max(
                values["outbox_oldest_undelivered_s"], value
            )
        elif name in ("arkray_db_up", "arkray_broker_up", "arkray_cache_up"):
            values[name.removeprefix("arkray_")] = value
    return values


def body_state(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    return str(data.get("status", "")) if isinstance(data, dict) else ""


class ProbeLog:
    def __init__(self) -> None:
        self.probes: list[ProbeReading] = []
        self.metrics: list[MetricsReading] = []

    def answer(self, at: float, kind: str, status: int, body: str, ms: float) -> None:
        self.probes.append(ProbeReading(at, kind, status, body_state(body), round(ms, 1)))

    def latest_ready(self) -> ProbeReading | None:
        return next((p for p in reversed(self.probes) if p.kind == "ready"), None)


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


class HttpProbes:
    """Readiness and liveness every second and the metrics every five, straight at the API
    (`--health-url`: the internal address on staging, a published port locally; nginx doesn't
    route /health)."""

    def __init__(
        self,
        log: ProbeLog,
        url: str,
        host: str,
        token: str,
        every_s: float = 1.0,
        metrics_every_s: float = 5.0,
    ) -> None:
        self.log, self.url, self.host, self.token = log, url, host, token
        self.every_s, self.metrics_every_s = every_s, metrics_every_s

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(
            base_url=self.url, timeout=httpx.Timeout(2.5), headers={"Host": self.host}
        ) as client:
            await asyncio.gather(self._probes(client, stop), self._metrics(client, stop))

    @staticmethod
    async def _get(
        client: httpx.AsyncClient, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, str, float]:
        started = time.perf_counter()
        try:
            response = await client.get(path, headers=headers)
        except httpx.HTTPError:
            return 0, "", (time.perf_counter() - started) * 1000
        return response.status_code, response.text, (time.perf_counter() - started) * 1000

    async def _probes(self, client: httpx.AsyncClient, stop: asyncio.Event) -> None:
        while not stop.is_set():
            at = time.time()
            ready, live = await asyncio.gather(
                self._get(client, "/health/ready"), self._get(client, "/health/live")
            )
            self.log.answer(at, "ready", *ready)
            self.log.answer(at, "live", *live)
            await wait_or_stop(stop, self.every_s)

    async def _metrics(self, client: httpx.AsyncClient, stop: asyncio.Event) -> None:
        while not stop.is_set():
            at = time.time()
            status, body, _ = await self._get(
                client, "/health/metrics", {"Authorization": f"Bearer {self.token}"}
            )
            values = parse_metrics(body) if status == 200 else None
            self.log.metrics.append(MetricsReading(at, values))
            await wait_or_stop(stop, self.metrics_every_s)


class ExecProbes:
    """The same probes from inside the API container (`docker exec`), every five seconds:
    what an orchestrator's probe sees, with no port to publish. Coarser (an exec takes about a
    second), so recovery times are known to about 5 s; `--health-url` gives 1 s."""

    def __init__(
        self, log: ProbeLog, docker: DockerDriver, service: str = "backend", every_s: float = 5.0
    ) -> None:
        self.log, self.docker, self.service, self.every_s = log, docker, service, every_s

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            at = time.time()
            try:
                output = await asyncio.to_thread(self.docker.exec_python, self.service, PROBE, 15.0)
                data = json.loads(output.strip().splitlines()[-1])
            except (DriverError, ValueError, IndexError):
                data = None
            if data is None:
                self.log.answer(at, "ready", 0, "", 0.0)
                self.log.answer(at, "live", 0, "", 0.0)
                self.log.metrics.append(MetricsReading(at, None))
            else:
                for kind in ("ready", "live"):
                    status, body, ms = data[kind]
                    self.log.answer(at, kind, int(status), str(body), float(ms))
                status, body, _ = data["metrics"]
                values = parse_metrics(str(body)) if status == 200 else None
                self.log.metrics.append(MetricsReading(at, values))
            await wait_or_stop(stop, self.every_s)


# --- recovery and backlog ----------------------------------------------------------------------
@dataclass(frozen=True)
class Recovery:
    seconds: float | None  # restore to both signals; None: not within the observation window
    ready_at: float | None
    settled_at: float | None
    readiness_probed: bool
    note: str


def baseline_states(probes: Sequence[ProbeReading], start: float, end: float) -> frozenset[str]:
    """What healthy readiness looked like before the fault (normally {"ok"})."""
    seen = {
        p.state for p in probes if p.kind == "ready" and start <= p.at < end and p.status == 200
    }
    return frozenset(seen or {"ok"})


def settled_at(
    samples: Sequence[Sample],
    start: float,
    end: float,
    threshold_pct: float,
    bucket_s: float,
    sustain: int,
) -> float | None:
    """When users stopped noticing: the start of the first bucket after the last bad one (an
    error rate above `threshold_pct`, or no request completing at all), provided `sustain`
    good buckets follow before `end`. `start` if no bucket was bad; None if not settled."""
    window = [s for s in samples if start <= s.at < end]
    bad: list[bool] = []
    t = start
    while t + bucket_s <= end:
        stats = window_stats(window, t, t + bucket_s)
        bad.append(stats["requests"] == 0 or (stats["error_rate_pct"] or 0.0) > threshold_pct)
        t += bucket_s
    last_bad = max((i for i, b in enumerate(bad) if b), default=None)
    if last_bad is None:
        return start
    if len(bad) - (last_bad + 1) < sustain:
        return None
    return start + (last_bad + 1) * bucket_s


def ready_again_at(
    probes: Sequence[ProbeReading], start: float, end: float, healthy: frozenset[str]
) -> float | None:
    """The first healthy readiness answer after the last unhealthy one in [start, end);
    `start` if none was unhealthy; None if it never turned healthy again."""
    window = [p for p in probes if p.kind == "ready" and start <= p.at < end]
    ok = [p.status == 200 and p.state in healthy for p in window]
    last_bad = max((i for i, good in enumerate(ok) if not good), default=None)
    if last_bad is None:
        return start
    return next(
        (p.at for p, good in zip(window, ok, strict=True) if good and p.at > window[last_bad].at),
        None,
    )


def recovery(
    samples: Sequence[Sample],
    probes: Sequence[ProbeReading],
    *,
    fault_at: float,
    restored_at: float,
    end: float,
    threshold_pct: float,
    healthy: frozenset[str],
    bucket_s: float = 5.0,
    sustain: int = 3,
) -> Recovery:
    """Time from the restore until readiness is healthy again AND the users' error rate is
    back under the threshold, whichever comes later (0 if neither was disturbed)."""
    settled = settled_at(samples, fault_at, end, threshold_pct, bucket_s, sustain)
    probed = any(p.kind == "ready" and fault_at <= p.at < end for p in probes)
    ready = ready_again_at(probes, fault_at, end, healthy) if probed else None
    if settled is None:
        return Recovery(None, ready, None, probed, "users' error rate not back within the window")
    if probed and ready is None:
        return Recovery(None, None, settled, probed, "readiness not healthy within the window")
    point = max(settled, ready) if ready is not None else settled
    note = "" if probed else "user errors only: readiness not probed"
    return Recovery(round(max(0.0, point - restored_at), 1), ready, settled, probed, note)


def backlog_size(values: dict[str, float], scope: str) -> float:
    return values["outbox_pending"] + (values["outbox_in_flight"] if scope == "all" else 0.0)


def backlog_drain(
    readings: Sequence[MetricsReading],
    *,
    base_start: float,
    fault_at: float,
    restored_at: float,
    end: float,
    scope: str,
) -> dict[str, Any]:
    """The outbox backlog against its own baseline: when, after the restore, two scrapes in a
    row were back at the baseline's level (its peak + max(20, 20 %)) with the oldest due event
    no older than max(10 s, twice the baseline's), and how many events went dead."""
    good = [(m.at, m.values) for m in readings if m.values is not None and base_start <= m.at < end]
    base = [values for at, values in good if at < fault_at]
    if not base:
        return {"measured": False, "scope": scope}
    peak_base = max(backlog_size(v, scope) for v in base)
    ceiling = peak_base + max(20.0, 0.2 * peak_base)
    age = max(10.0, 2 * max(v["outbox_oldest_due_s"] for v in base))
    later = [(at, values) for at, values in good if at >= fault_at]

    def back(values: dict[str, float]) -> bool:
        return backlog_size(values, scope) <= ceiling and values["outbox_oldest_due_s"] <= age

    after = [(at, values) for at, values in later if at >= restored_at]
    drained = next((a[0] for a, b in itertools.pairwise(after) if back(a[1]) and back(b[1])), None)
    return {
        "measured": True,
        "scope": scope,
        "baseline_peak": peak_base,
        "ceiling": round(ceiling, 1),
        "peak": max((backlog_size(v, scope) for _, v in later), default=0.0),
        "peak_oldest_due_s": max((v["outbox_oldest_due_s"] for _, v in later), default=0.0),
        "peak_undelivered_s": max(
            (v["outbox_oldest_undelivered_s"] for _, v in later), default=0.0
        ),
        "seconds": None if drained is None else round(max(0.0, drained - restored_at), 1),
        "dead_added": good[-1][1]["outbox_dead"] - good[0][1]["outbox_dead"],
    }


# --- integrity ---------------------------------------------------------------------------------
# capacity_test.verify's counters that only ever grow over a run (since the run's start).
CUMULATIVE = (
    "duplicate_logical_creates",
    "orphan_leads",
    "opportunities_without_lead_owner_match",
    "created_ids_missing_in_db",
    "lost_updates",
    "moves_with_history_mismatch",
    "one_audit_event_per_create",
    "deadlocks",
)


def integrity_problems(current: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    """What capacity_test.verify found that wasn't there after the previous drill: its
    counters cover the whole run, so a problem is attributed to the drill that added it."""
    before = previous or {}
    problems: list[str] = []
    for key in CUMULATIVE:
        now, then = current.get(key), before.get(key, 0)
        if isinstance(now, int) and now > (then if isinstance(then, int) else 0):
            problems.append(f"{key} {then} -> {now}")
    then_states = before.get("impossible_states") or {}
    for name, count in (current.get("impossible_states") or {}).items():
        if isinstance(count, int) and count > then_states.get(name, 0):
            problems.append(f"impossible state {name}: {count}")
    mismatches = (current.get("dashboard_math") or {}).get("mismatch_count", 0)
    if mismatches:
        problems.append(f"dashboard figures differ from SQL for {mismatches} users")
    return problems


# --- evaluation ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: couldn't be measured
    detail: str
    required: bool = True


def verdict(checks: Sequence[Check]) -> str:
    if any(c.ok is False for c in checks):
        return "FAIL"
    if any(c.ok is None and c.required for c in checks):
        return "INCONCLUSIVE"
    return "PASS"


@dataclass(frozen=True)
class Timings:
    baseline_s: float = 60.0
    recovery_s: float | None = None  # replaces every drill's own
    drain_timeout_s: float | None = None  # replaces every drill's own
    quiesce_s: float = 10.0
    bucket_s: float = 5.0
    sustain: int = 3
    tolerance_pct: float = 1.0
    gate_timeout_s: float = 180.0
    stall_s: float = 10.0


@dataclass
class Observation:
    base_start: float
    fault_at: float
    restored_at: float
    recovery_end: float
    end: float
    fault: dict[str, Any]
    samples: list[Sample] | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    probes: list[ProbeReading] | None = None
    metrics: list[MetricsReading] | None = None
    watch_before: dict[str, dict[str, Any] | None] | None = None
    watch_after: dict[str, dict[str, Any] | None] | None = None
    integrity: dict[str, Any] | None = None
    exactly_once: dict[str, Any] | None = None
    stalls: list[tuple[float, float]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} %"


def fault_checks(fault: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    steps = fault.get("inject") or []
    if "error" in fault:
        checks.append(Check("fault applied", False, str(fault["error"])))
    elif steps:
        unconfirmed = [s["action"] for s in steps if s.get("confirmed") is False]
        detail = "; ".join(str(s.get("command", s["action"])) for s in steps)
        if unconfirmed:
            checks.append(Check("fault applied", None, f"not confirmed: {', '.join(unconfirmed)}"))
        else:
            checks.append(Check("fault applied", True, detail))
    restored = fault.get("restore") or []
    failed = [s for s in restored if "error" in s]
    unconfirmed = [s["action"] for s in restored if s.get("confirmed") is False]
    if failed:
        checks.append(Check("fault restored", False, "; ".join(str(s["error"]) for s in failed)))
    elif unconfirmed:
        checks.append(Check("fault restored", None, f"not confirmed: {', '.join(unconfirmed)}"))
    elif restored:
        detail = "; ".join(str(s.get("command", s["action"])) for s in restored)
        checks.append(Check("fault restored", True, detail))
    return checks


def user_checks(
    drill: Drill, obs: Observation, timings: Timings
) -> tuple[dict[str, Any], list[Check]]:
    c = drill.criteria
    samples = obs.samples or []
    probes = obs.probes or []
    before = window_stats(samples, obs.base_start, obs.fault_at)
    during = window_stats(samples, obs.fault_at, obs.restored_at)
    after = window_stats(samples, obs.restored_at, obs.recovery_end)
    impact = window_stats(samples, obs.fault_at, obs.recovery_end)
    base_rate = before["error_rate_pct"] or 0.0
    rec = recovery(
        samples,
        probes,
        fault_at=obs.fault_at,
        restored_at=obs.restored_at,
        end=obs.end,
        threshold_pct=base_rate + timings.tolerance_pct,
        healthy=baseline_states(probes, obs.base_start, obs.fault_at),
        bucket_s=timings.bucket_s,
        sustain=timings.sustain,
    )
    measured: dict[str, Any] = {
        "before": before,
        "during": during,
        "after": after,
        "fault_and_recovery": impact,
        "recovery": asdict(rec),
    }
    checks: list[Check] = []
    statuses = ", ".join(f"{k} x{v}" for k, v in impact["errors_by_status"].items()) or "none"
    detail = (
        f"{impact['errors']} of {impact['requests']} requests failed"
        f" ({pct(impact['error_rate_pct'])}; baseline {pct(before['error_rate_pct'])});"
        f" statuses: {statuses}; throttled (429): {impact['throttled_429']}"
    )
    if impact["requests"] == 0:
        checks.append(Check("user errors", None, "no request completed during the drill"))
    elif c.max_errors is not None:
        checks.append(Check("user errors", impact["errors"] <= c.max_errors, detail))
    elif c.max_extra_error_pct is not None:
        rate = impact["error_rate_pct"] or 0.0
        checks.append(Check("user errors", rate <= base_rate + c.max_extra_error_pct, detail))
    else:
        checks.append(Check("user errors", True, f"{detail} (expected; kinds below)", False))
    if c.error_statuses is not None:
        allowed = "/".join(str(s) for s in sorted(c.error_statuses))
        unexpected = [s for s in impact["errors_by_status"] if int(s) not in c.error_statuses]
        text = f"unexpected: {', '.join(unexpected)}" if unexpected else f"only {allowed}"
        checks.append(Check("error statuses", not unexpected, text))
    if c.error_codes is not None and impact["errors"]:
        stages = {f"{drill.name}:fault", f"{drill.name}:recovery"}
        codes = error_codes(obs.failures, stages)
        unexpected = sorted(set(codes) - c.error_codes)
        sampled = ", ".join(f"{k} x{v}" for k, v in codes.items())
        if not codes:
            checks.append(Check("error codes", None, "no error body sampled", required=False))
        else:
            checks.append(Check("error codes", not unexpected, f"sampled bodies: {sampled}"))
    if c.max_p95_ms is not None:
        limit = max(c.max_p95_ms, c.p95_factor * (before["p95_ms"] or 0.0))
        p95 = during["p95_ms"]
        text = (
            f"p95 {before['p95_ms']} ms before, {p95} ms during, {after['p95_ms']} ms after"
            f" (limit {limit:.0f} ms)"
        )
        checks.append(
            Check("latency during the fault", None if p95 is None else p95 <= limit, text)
        )
    if c.max_request_ms is not None:
        longest = impact["max_ms"]
        text = f"longest request {longest} ms (limit {c.max_request_ms:.0f} ms)"
        bounded = None if longest is None else longest <= c.max_request_ms
        checks.append(Check("bounded waits", bounded, text))
    if c.max_recovery_s is not None:
        if rec.seconds is None:
            text = rec.note
        else:
            text = f"{rec.seconds:g} s after the restore (limit {c.max_recovery_s:g} s)"
            text += f"; {rec.note}" if rec.note else ""
        recovered: bool | None = rec.seconds is not None and rec.seconds <= c.max_recovery_s
        if impact["requests"] == 0 or (recovered and not rec.readiness_probed):
            recovered = None  # half of the definition (readiness healthy again) wasn't measured
        checks.append(Check("recovery", recovered, text))
    if c.inflight_ok:
        served = in_flight(samples, obs.fault_at, INFLIGHT_MIN_AGE_S)
        failed = [s for s in served if classify(s) == "error"]
        every = in_flight(samples, obs.fault_at, 0.0)
        text = (
            f"{len(served)} requests had been served for {INFLIGHT_MIN_AGE_S} s or more at the"
            f" stop, {len(failed)} failed; {len(every)} were in flight in all, of which"
            f" {sum(1 for s in every if classify(s) == 'error')} failed (still queued ones are"
            " refused like new requests)"
        )
        checks.append(Check("requests in flight at the stop", not failed, text))
    return measured, checks


def probe_checks(c: Criteria, obs: Observation) -> list[Check]:
    if obs.probes is None:
        note = "readiness not probed (--health-url, or the docker driver's probe in the API)"
        required = c.readiness == "unready" or c.liveness_ok
        return [Check("readiness", None, note, required)]
    ready = [p for p in obs.probes if p.kind == "ready" and obs.base_start <= p.at < obs.end]
    seen = Counter(p.state or ("no answer" if p.status == 0 else str(p.status)) for p in ready)
    answers = ", ".join(f"{k} x{v}" for k, v in seen.items()) or "none"
    answered = [p for p in ready if p.status != 0]
    checks: list[Check] = []
    if c.readiness == "ready":
        refused = [p for p in ready if p.status == 503]
        ok = None if not answered else not refused
        checks.append(Check("readiness", ok, f"answers: {answers}", required=False))
    elif c.readiness == "unready":
        down = [p for p in ready if obs.fault_at <= p.at < obs.restored_at and p.status != 200]
        text = (
            f"unready {down[0].at - obs.fault_at:.1f} s after the fault; answers: {answers}"
            if down
            else f"never unready during the fault; answers: {answers}"
        )
        checks.append(Check("readiness", None if not ready else bool(down), text))
    else:
        checks.append(Check("readiness", True, f"answers: {answers} (not a criterion)", False))
    if c.liveness_ok:
        live = [
            p for p in obs.probes if p.kind == "live" and obs.fault_at <= p.at < obs.recovery_end
        ]
        bad = [p for p in live if p.status != 200]
        text = f"{len(live) - len(bad)} of {len(live)} liveness answers 200"
        checks.append(Check("liveness", None if not live else not bad, text))
    return checks


def backlog_checks(c: Criteria, obs: Observation, measured: dict[str, Any]) -> list[Check]:
    if not c.drain_scope:
        return []
    if not obs.metrics:
        note = "no metrics (--health-url with --metrics-token, or the docker driver's probe)"
        return [Check("backlog drained", None, note)]
    drain = backlog_drain(
        obs.metrics,
        base_start=obs.base_start,
        fault_at=obs.fault_at,
        restored_at=obs.restored_at,
        end=obs.end,
        scope=c.drain_scope,
    )
    measured["backlog"] = drain
    if not drain["measured"]:
        return [Check("backlog drained", None, "no metrics scraped during the baseline")]
    seconds = drain["seconds"]
    when = "not within the drill" if seconds is None else f"{seconds:g} s after the restore"
    text = (
        f"{c.drain_scope} peaked at {drain['peak']:.0f} (baseline up to"
        f" {drain['baseline_peak']:.0f}), oldest due {drain['peak_oldest_due_s']:.0f} s;"
        f" drained {when} (limit {c.max_drain_s:g} s); dead events added"
        f" {drain['dead_added']:.0f}"
    )
    ok = seconds is not None and seconds <= c.max_drain_s and drain["dead_added"] == 0
    return [Check("backlog drained", ok, text)]


def container_checks(drill: Drill, obs: Observation) -> list[Check]:
    c = drill.criteria
    checks: list[Check] = []
    steps = obs.fault.get("inject") or []
    if c.graceful_exit:
        code = next((s.get("exit_code") for s in steps if "exit_code" in s), None)
        ok = None if code is None else code == 0
        checks.append(Check("graceful exit", ok, f"exit code {code}"))
    if c.workers_restored:
        before = next((s.get("children_before") for s in steps if "children_before" in s), None)
        after = obs.fault.get("children_after")
        ok = None if before is None or after is None else after >= before
        checks.append(Check("process replaced", ok, f"{before} before the kill, {after} after"))
    if obs.watch_before is None or obs.watch_after is None:
        note = "no container state from this driver: check the platform's restart/OOM counters"
        checks.append(Check("no restarts", None, note, required=False))
        return checks
    expected = drill.restarted()
    changed: list[str] = []
    watched = 0
    for service, then in obs.watch_before.items():
        now = obs.watch_after.get(service)
        if then is None or now is None or then.get("status") != "running":
            continue
        watched += 1
        if now.get("oom_killed"):
            changed.append(f"{service} OOM-killed")
        elif service not in expected and (
            now["restarts"] != then["restarts"]
            or now["started_at"] != then["started_at"]
            or now["status"] != "running"
        ):
            changed.append(f"{service} restarted or {now['status']}")
    text = "; ".join(changed) or f"{watched} containers watched, none restarted or OOM-killed"
    checks.append(Check("no restarts", not changed, text))
    return checks


def data_checks(drill: Drill, obs: Observation) -> list[Check]:
    c = drill.criteria
    checks: list[Check] = []
    if c.integrity and obs.integrity is not None:
        if not obs.integrity.get("checked"):
            note = obs.integrity.get("error") or "not checked: no --pg"
            checks.append(Check("integrity", None, str(note)))
        else:
            problems = list(obs.integrity.get("problems") or [])
            leaks = obs.integrity.get("leaks") or {}
            problems += [f"{k}: {v} new" for k, v in leaks.items() if v]
            text = "; ".join(problems) or (
                "no duplicate, orphan, lost update, impossible state, dashboard mismatch or leak"
            )
            checks.append(Check("integrity", not problems, text))
    if c.exactly_once:
        found = obs.exactly_once
        if found is None:
            checks.append(Check("exactly once", None, "not checked: no --pg"))
        else:
            events = found.get("events") or {}
            unfinished = {k: v for k, v in events.items() if k != "done"}
            unindexed = found.get("notes_unindexed")
            text = (
                f"outbox events since the drill began: {events or 'none'}"
                f" ({found.get('retried', 0)} ran more than once, as a kill makes them);"
                f" notes written {found.get('notes')}, not indexed {unindexed}"
            )
            if not found.get("indexing"):
                text += " (no indexing events: Ask Arkray's indexing is off here)"
            checks.append(Check("exactly once", not unfinished and not unindexed, text))
    return checks


def edge_checks(edge: dict[str, Any] | None) -> list[Check]:
    if edge is None:
        return [Check("edge probes", None, "not run")]
    wanted = (
        ("json_too_large", 413, "file_too_large", "JSON body over 1 MB refused"),
        ("upload_too_large", 413, "file_too_large", "upload over 11 MB refused"),
        ("sign_in_burst", 429, "rate_limited", "sign-in burst limited"),
        ("bodies_in_flight", 429, "rate_limited", "requests with a body in flight limited"),
        ("uploads_in_flight", 429, "rate_limited", "uploads in flight limited"),
    )
    checks: list[Check] = []
    for key, status, code, label in wanted:
        batch = edge.get(key)
        if batch is None:
            checks.append(Check(label, None, "not run (no signed-in helper or note)"))
            continue
        refused = int(batch["statuses"].get(str(status), 0))
        enveloped = int(batch["codes"].get(f"{status}:{code}", 0))
        text = (
            f"{refused} of {batch['requests']} answered {status} ({enveloped} with `{code}`);"
            f" all: {batch['statuses']}"
        )
        checks.append(Check(label, refused >= 1 and enveloped == refused, text))
    return checks


def upload_checks(uploads: dict[str, Any] | None, obs: Observation) -> list[Check]:
    if uploads is None:
        return [Check("uploads", None, "no upload helper signed in")]
    records = uploads.get("uploads") or []
    during = [
        r for r in records if obs.fault_at <= r["at"] < obs.restored_at and r["status"] != 429
    ]
    accepted = [r for r in during if r["status"] == 201]
    text = f"{len(accepted)} of {len(during)} uploads during the fault answered 201"
    checks = [Check("uploads accepted", None if not during else len(accepted) == len(during), text)]
    early = [
        d
        for r in records
        if obs.fault_at <= r["at"] < obs.restored_at
        for d in r["downloads"]
        if d[1] == 200 and d[0] < obs.restored_at
    ]
    refused = sum(
        1
        for r in records
        if obs.fault_at <= r["at"] < obs.restored_at
        for d in r["downloads"]
        if d[2] == "scanning"
    )
    text = f"{len(early)} served before a scan; {refused} refused 422 while being checked"
    checks.append(Check("unscanned files not served", not early, text))
    stored = [r for r in records if r["status"] == 201]
    blocked = [r for r in stored if r.get("blocked")]
    waiting = [r for r in stored if r["clean_at"] is None]
    last = max((r["clean_at"] for r in stored if r["clean_at"] is not None), default=None)
    when = (
        ""
        if last is None
        else f", the last {max(0.0, last - obs.restored_at):.0f} s after the restore"
    )
    text = (
        f"{len(stored) - len(waiting)} of {len(stored)} files clean and served intact{when};"
        f" {len(blocked)} blocked, {len(waiting) - len(blocked)} still being checked"
    )
    checks.append(Check("scans completed", bool(stored) and not waiting, text))
    return checks


def scan_checks(scan: dict[str, Any] | None) -> list[Check]:
    if scan is None or "error" in scan:
        return [Check("scan end to end", None, str((scan or {}).get("error", "not run")))]
    eicar, clean = scan.get("eicar") or {}, scan.get("clean") or {}
    return [
        Check(
            "EICAR blocked",
            eicar.get("outcome") == "blocked",
            f"upload {eicar.get('upload')}, outcome {eicar.get('outcome')} after"
            f" {eicar.get('seconds')} s",
        ),
        Check(
            "clean file served intact",
            clean.get("outcome") == "clean",
            f"upload {clean.get('upload')}, outcome {clean.get('outcome')} after"
            f" {clean.get('seconds')} s",
        ),
        Check(
            "blocked file's object purged",
            eicar.get("purged"),
            f"purged: {eicar.get('purged')} (from the database; None without --pg)",
            required=False,
        ),
    ]


def restore_checks(result: dict[str, Any] | None) -> list[Check]:
    if result is None or "error" in result:
        return [Check("restore drill", None, str((result or {}).get("error", "not run")))]
    outcome = result.get("result")
    who = "reported by the operator" if result.get("operator_reported") else "restore_drill.py"
    ok = None if outcome is None else str(outcome) == "PASS"
    return [Check("restore drill", ok, f"{outcome} ({who}); report {result.get('report', '-')}")]


def stall_check(obs: Observation, timings: Timings) -> Check:
    longest = max((s for at, s in obs.stalls if obs.base_start <= at < obs.end), default=0.0)
    if longest >= timings.stall_s:
        text = (
            f"this machine stalled {longest:.0f} s during the drill (sleep, or overloaded): its"
            " timings aren't trustworthy; run it again"
        )
        return Check("load generator steady", None, text)
    return Check("load generator steady", True, f"longest stall {longest:.1f} s", False)


def evaluate(
    drill: Drill, obs: Observation, timings: Timings
) -> tuple[dict[str, Any], list[Check]]:
    measured: dict[str, Any] = {}
    checks = fault_checks(obs.fault)
    if drill.kind in ("fault", "edge"):
        if obs.samples is not None:
            measured, user = user_checks(drill, obs, timings)
            checks += user
        checks += probe_checks(drill.criteria, obs)
        checks += backlog_checks(drill.criteria, obs, measured)
        checks += container_checks(drill, obs)
    checks += data_checks(drill, obs)
    if drill.kind == "edge":
        checks += edge_checks(obs.extra.get("edge"))
    if drill.workload == "uploads":
        checks += upload_checks(obs.extra.get("uploads"), obs)
    if drill.kind == "scan":
        checks += scan_checks(obs.extra.get("scan"))
    if drill.kind == "restore":
        checks += restore_checks(obs.extra.get("restore"))
    checks.append(stall_check(obs, timings))
    return measured, checks


# --- the runner ----------------------------------------------------------------------------------
class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class Load(Protocol):
    def set_phase(self, label: str) -> None: ...

    def set_active(self, active: bool) -> None: ...

    def samples(self) -> list[Sample]: ...

    def forget(self) -> None: ...

    def take_failures(self) -> list[dict[str, Any]]: ...

    def leak_counts(self) -> dict[str, int]: ...

    async def verify(self) -> dict[str, Any] | None: ...


class Extras(Protocol):
    async def start(self, drill: Drill) -> Any: ...

    async def finish(self, handle: Any, deadline: float) -> dict[str, Any]: ...

    async def edge(self) -> dict[str, Any]: ...

    async def scan(self) -> dict[str, Any]: ...

    async def restore_drill(self, drill: Drill) -> dict[str, Any]: ...


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


class DrillRunner:
    """Runs one drill at a time against the driver, the load and the probes it is given."""

    def __init__(
        self,
        *,
        environment: str,
        driver: Driver,
        clock: Clock,
        timings: Timings,
        load: Load | None = None,
        probe_log: ProbeLog | None = None,
        database: Any = None,
        extras: Extras | None = None,
        watch: Sequence[str] = (),
        say: Callable[[str], None] = print,
    ) -> None:
        self.environment = environment
        self.driver = driver
        self.clock = clock
        self.timings = timings
        self.load = load
        self.probe_log = probe_log
        self.database = database
        self.extras = extras
        self.watch = list(watch)
        self.say = say
        self.owed: list[Action] = []
        self.events: list[dict[str, str]] = []
        self.stalls: list[tuple[float, float]] = []
        self.verified_before: dict[str, Any] | None = None
        self.leaks_seen: dict[str, int] = {}

    def event(self, text: str) -> None:
        self.events.append({"at": iso(self.clock.now()), "event": text})
        self.say(f"  {text}")

    # -- the fault and what it owes ------------------------------------------------------------
    async def inject(self, drill: Drill) -> list[dict[str, Any]]:
        # Owed BEFORE the first step: a fault applied halfway (the stop went through, then the
        # CLI timed out) is restored all the same.
        self.owed.extend(drill.restore)
        done: list[dict[str, Any]] = []
        for action in drill.inject:
            done.append(await asyncio.to_thread(self.driver.apply, action))
            self.event(f"fault: {done[-1].get('command', action.describe())}")
        return done

    async def restore_owed(self) -> list[dict[str, Any]]:
        """Pays every owed restore step; one that fails stays owed (exit and `restore` retry)."""
        results: list[dict[str, Any]] = []
        for action in list(self.owed):
            try:
                results.append(await asyncio.to_thread(self.driver.apply, action))
            except DriverError as exc:
                results.append({"action": action.describe(), "error": str(exc)})
                self.event(f"RESTORE FAILED: {exc}")
                continue
            self.owed.remove(action)
            self.event(f"restored: {results[-1].get('command', action.describe())}")
        return results

    def restore_owed_now(self) -> list[dict[str, Any]]:
        """The same, synchronously, for exit paths (an interrupt, an exception, atexit)."""
        results: list[dict[str, Any]] = []
        for action in list(self.owed):
            try:
                results.append(self.driver.apply(action))
            except DriverError as exc:
                results.append({"action": action.describe(), "error": str(exc)})
                print(f"!!! could not restore {action.describe()}: {exc}", flush=True)
                continue
            self.owed.remove(action)
            print(f"restored {action.describe()}", flush=True)
        return results

    # -- around a drill -------------------------------------------------------------------------
    async def gate(self, drill: Drill) -> None:
        """Starts a drill only on a healthy stack: nothing owed, readiness answering 200."""
        if self.owed:
            owed = ", ".join(a.describe() for a in self.owed)
            raise GateError(f"a previous fault is still owed a restore: {owed}")
        if self.probe_log is None:
            return
        deadline = self.clock.now() + self.timings.gate_timeout_s
        while True:
            latest = self.probe_log.latest_ready()
            if latest is not None and latest.status == 200:
                return
            if self.clock.now() >= deadline:
                raise GateError(f"readiness not 200 before {drill.name}: {latest}")
            await self.clock.sleep(5.0)

    async def watch_host(self, stop: asyncio.Event) -> None:
        """Notices this machine stalling (sleep, or an overloaded event loop): every timing
        taken across a stall is suspect."""
        last = time.time()
        while not stop.is_set():
            await wait_or_stop(stop, 1.0)
            now = time.time()
            if now - last > 6.0:
                self.stalls.append((last, round(now - last - 1.0, 1)))
            last = now

    async def _inspect(self) -> dict[str, dict[str, Any] | None] | None:
        if not self.watch:
            return None
        try:
            return await asyncio.to_thread(self.driver.inspect, self.watch)
        except DriverError:
            return None

    async def _sql(self, name: str, *args: Any) -> Any:
        if self.database is None:
            return None
        try:
            call = getattr(self.database, name)
            return await asyncio.wait_for(asyncio.to_thread(call, *args), 120)
        except Exception as exc:  # a failed check is a data point, never a crash
            self.event(f"database check {name} failed: {type(exc).__name__}")
            return None

    async def _integrity(self) -> dict[str, Any] | None:
        if self.load is None:
            return None
        try:
            verified = await asyncio.wait_for(self.load.verify(), 300)
        except Exception as exc:
            return {"checked": False, "error": f"verify failed: {type(exc).__name__}: {exc}"}
        leaks = self.load.leak_counts()
        new = {k: v - self.leaks_seen.get(k, 0) for k, v in leaks.items()}
        self.leaks_seen = leaks
        if verified is None:
            return {"checked": False, "leaks": new}
        problems = integrity_problems(verified, self.verified_before)
        self.verified_before = verified
        return {"checked": True, "problems": problems, "leaks": new, "verify": verified}

    async def _exactly_once(self, since: Any, until: Any) -> dict[str, Any] | None:
        if since is None or until is None:
            return None
        events = await self._sql("outbox_between", since, until)
        notes = await self._sql("notes_unindexed", since, until)
        if events is None or notes is None:
            return None
        return {**events, **notes}

    async def _wait_drained(self, drill: Drill, obs: Observation, deadline: float) -> None:
        scope = drill.criteria.drain_scope
        if not scope or self.probe_log is None:
            return
        while self.clock.now() < deadline:
            drain = backlog_drain(
                self.probe_log.metrics,
                base_start=obs.base_start,
                fault_at=obs.fault_at,
                restored_at=obs.restored_at,
                end=self.clock.now(),
                scope=scope,
            )
            if not drain["measured"] or drain["seconds"] is not None:
                return
            await self.clock.sleep(5.0)

    def _phase(self, drill: Drill, phase: str) -> None:
        if self.load is not None:
            self.load.set_phase(f"{drill.name}:{phase}")

    async def run(self, drill: Drill) -> dict[str, Any]:
        clock, load, timings = self.clock, self.load, self.timings
        self.say(f"\n== {drill.name} ({self.environment}): {drill.title}")
        result: dict[str, Any] = {
            "drill": drill.name,
            "environment": self.environment,
            "fault": drill.title,
            "service": drill.service,
            "expected": drill.expectation,
            "criteria": drill.pass_criteria(),
            "started_at": iso(clock.now()),
        }
        timed = drill.kind in ("fault", "edge")
        if load is not None:
            load.set_active(drill.needs_load)
            load.take_failures()
            load.forget()  # the previous drill's requests: evaluated, no longer needed
        watch_before = await self._inspect() if timed else None
        db_start = await self._sql("now") if drill.criteria.exactly_once else None
        self._phase(drill, "baseline")
        handle = await self.extras.start(drill) if self.extras and drill.workload else None
        base_start = clock.now()
        if timed:
            await clock.sleep(timings.baseline_s)
        fault_at = clock.now()
        self._phase(drill, "fault")
        fault: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        try:
            if drill.kind == "edge":
                extra["edge"] = await self.extras.edge() if self.extras else None
            elif drill.kind == "scan":
                extra["scan"] = await self.extras.scan() if self.extras else None
            elif drill.kind == "restore":
                extra["restore"] = await self.extras.restore_drill(drill) if self.extras else None
            else:
                fault["inject"] = await self.inject(drill)
                await clock.sleep(drill.hold_s)
        except DriverError as exc:
            fault["error"] = str(exc)
            self.event(f"FAULT FAILED: {exc}")
        finally:
            try:
                restored = await self.restore_owed()
            except BaseException:
                self.restore_owed_now()
                raise
            if restored:
                fault["restore"] = restored
        restored_at = clock.now()
        self._phase(drill, "recovery")
        if drill.criteria.workers_restored and drill.service:
            fault["children_after"] = await asyncio.to_thread(self.driver.children, drill.service)
        db_restored = await self._sql("now") if drill.criteria.exactly_once else None
        if timed:
            await clock.sleep(
                drill.recovery_s if timings.recovery_s is None else timings.recovery_s
            )
        recovery_end = clock.now()
        drain_s = (
            drill.drain_timeout_s if timings.drain_timeout_s is None else timings.drain_timeout_s
        )
        obs = Observation(base_start, fault_at, restored_at, recovery_end, recovery_end, fault)
        await self._wait_drained(drill, obs, restored_at + drain_s)
        if handle is not None and self.extras is not None:
            extra["uploads"] = await self.extras.finish(handle, restored_at + drain_s)
        obs.end = clock.now()
        if load is not None:
            load.set_active(False)
        self._phase(drill, "verify")
        if timed:
            await clock.sleep(timings.quiesce_s)
        if drill.needs_load and drill.criteria.integrity:
            obs.integrity = await self._integrity()
        if drill.criteria.exactly_once:
            obs.exactly_once = await self._exactly_once(db_start, db_restored)
        obs.watch_before = watch_before
        obs.watch_after = await self._inspect() if timed else None
        if load is not None:
            obs.failures = load.take_failures()
            if drill.needs_load:
                obs.samples = load.samples()
        if self.probe_log is not None:
            obs.probes = list(self.probe_log.probes)
            obs.metrics = list(self.probe_log.metrics)
        obs.stalls = list(self.stalls)
        obs.extra = extra
        return self.finish(drill, result, obs)

    def finish(self, drill: Drill, result: dict[str, Any], obs: Observation) -> dict[str, Any]:
        measured, checks = evaluate(drill, obs, self.timings)
        outcome = verdict(checks)
        result.update(
            {
                "verdict": outcome,
                "checks": [asdict(c) for c in checks],
                "measured": measured,
                "windows": {
                    "baseline_from": iso(obs.base_start),
                    "fault_at": iso(obs.fault_at),
                    "restored_at": iso(obs.restored_at),
                    "recovery_until": iso(obs.recovery_end),
                    "observed_until": iso(obs.end),
                },
                "fault_steps": obs.fault,
                "extra": obs.extra,
                "integrity": obs.integrity,
                "exactly_once": obs.exactly_once,
                "containers_before": obs.watch_before,
                "containers_after": obs.watch_after,
                "finished_at": iso(self.clock.now()),
            }
        )
        if obs.samples:
            result["timeline"] = timeline(
                obs.samples, obs.base_start, obs.end, self.timings.bucket_s
            )
        if obs.probes:
            result["readiness_changes"] = readiness_changes(obs.probes, obs.base_start, obs.end)
        for check in checks:
            mark = {True: "PASS", False: "FAIL", None: "n/a "}[check.ok]
            self.say(f"  [{mark}] {check.name}: {check.detail}")
        self.say(f"  => {drill.name}: {outcome} ({self.environment})")
        return result


def readiness_changes(probes: Sequence[ProbeReading], start: float, end: float) -> list[Any]:
    """[seconds into the drill, status, state] each time readiness changed."""
    changes: list[Any] = []
    last: tuple[int, str] | None = None
    for p in probes:
        if p.kind == "ready" and start <= p.at < end and (p.status, p.state) != last:
            last = (p.status, p.state)
            changes.append([round(p.at - start, 1), p.status, p.state])
    return changes


# --- the real load, database and drill work ------------------------------------------------------
def capacity_module() -> Any:
    """capacity_test.py, imported (not run): its virtual users are this drill's load."""
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from tests.performance import capacity_test

    return capacity_test


class CapacityLoad:
    """capacity_test.py's virtual users, in this process (the module docstring says why)."""

    def __init__(self, options: argparse.Namespace) -> None:
        self.ct = capacity_module()
        self.args = argparse.Namespace(
            base=options.base,
            users=options.users,
            sessions=options.sessions,
            pg=options.pg,
            think_mean=options.think_mean,
            host=options.host,
            insecure=options.insecure,
            ca_file=options.ca_file,
        )
        self.accounts = self.ct.load_accounts(options.users)
        self.run = self.ct.Run(self.args, self.accounts)
        self.wanted = options.vus
        self.roster: list[Any] = []
        self.helper_vus: list[Any] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self._converted: list[Sample] = []

    async def sign_in(self, vus: list[Any]) -> int:
        """Signs `vus` in, saved sessions first (as capacity_test does), and keeps every saved
        session in the file, not only these."""
        path = Path(self.args.sessions) if self.args.sessions else None
        saved: dict[str, Any] = {}
        if path is not None and path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
        gate = asyncio.Semaphore(6)

        async def one(vu: Any) -> bool:
            async with gate:
                if await vu.login(saved.get(vu.account["email"])):
                    await vu.prime()
                    return True
                return False

        signed = sum(await asyncio.gather(*(one(v) for v in vus)))
        if path is not None:
            saved.update({v.account["email"]: v.cookies() for v in vus if v.signed_in})
            path.write_text(json.dumps(saved), encoding="utf-8")
        return signed

    async def start(self) -> None:
        self.roster = self.ct.vu_roster(self.run, self.wanted)
        self.run.vus = self.roster
        started = time.time()
        signed = await self.sign_in(self.roster)
        took = time.time() - started
        print(f"{signed} of {len(self.roster)} virtual users signed in ({took:.0f} s)", flush=True)
        if not signed:
            raise SystemExit("no virtual user could sign in")
        if self.args.pg:
            self.run.started_db = await asyncio.to_thread(Database(self.args.pg).now)
        # Canary data, as capacity_test's run: every salesperson's token in a deal and a note.
        self.run.rec.stage = "setup_canary"
        for vu in self.roster:
            if vu.signed_in and not vu.is_admin:
                await vu.create_opportunity(retry=False)
                await vu.create_activity("note")
        self.run.target_active = 0
        self.tasks = [asyncio.create_task(vu.loop()) for vu in self.roster]

    async def helpers(self, count: int) -> list[Any]:
        """Signed-in users outside the load (the last accounts of the file), for uploads."""
        if count and len(self.helper_vus) < count:
            accounts = self.accounts["users"][-count:]
            fresh = [self.ct.VU(self.run, a, 900 + i) for i, a in enumerate(accounts)]
            await self.sign_in(fresh)
            self.helper_vus = [v for v in fresh if v.signed_in]
        return self.helper_vus[:count]

    def set_phase(self, label: str) -> None:
        self.run.rec.stage = label

    def set_active(self, active: bool) -> None:
        self.run.target_active = len(self.roster) if active else 0

    def samples(self) -> list[Sample]:
        raw = self.run.rec.samples
        self._converted.extend(
            Sample(s.at, s.name, s.status, s.ms) for s in raw[len(self._converted) :]
        )
        return list(self._converted)

    def forget(self) -> None:
        self.run.rec.samples.clear()
        self._converted.clear()

    def take_failures(self) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = self.run.rec.failures
        self.run.rec.failures = []
        return failures

    def leak_counts(self) -> dict[str, int]:
        return {"leaks": len(self.run.rec.leaks), "probe_leaks": len(self.run.rec.probe_leaks)}

    async def verify(self) -> dict[str, Any] | None:
        if not self.args.pg or not self.run.started_db:
            return None
        result: dict[str, Any] = await self.ct.verify(self.run, self.roster)
        return result

    async def stop(self) -> None:
        self.run.stop.set()
        self.run.target_active = 0
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for vu in [*self.roster, *self.helper_vus]:
            with contextlib.suppress(Exception):
                await vu.client.aclose()


class Database:
    """Read-only SQL for the checks: a short connection each, bounded (connect 5 s; statements
    60 s), so a database that is down or stalled costs a failed check, never a hang."""

    def __init__(self, url: str) -> None:
        self.url = url

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        import psycopg

        with (
            psycopg.connect(self.url, autocommit=True, connect_timeout=5) as conn,
            conn.cursor() as cursor,
        ):
            cursor.execute("SET statement_timeout = '60s'")
            cursor.execute(sql, list(params))
            return list(cursor.fetchall())

    def now(self) -> datetime:
        value: datetime = self._rows("SELECT now()")[0][0]
        return value

    def deadlocks(self) -> int:
        sql = "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        return int(self._rows(sql)[0][0])

    def outbox_between(self, since: datetime, until: datetime) -> dict[str, Any]:
        rows = self._rows(
            "SELECT status, count(*), count(*) FILTER (WHERE attempts > 1),"
            " count(*) FILTER (WHERE topic LIKE 'ai.%%') FROM core_outbox_event"
            " WHERE created_at >= %s AND created_at < %s GROUP BY status",
            [since, until],
        )
        return {
            "events": {str(r[0]): int(r[1]) for r in rows},
            "retried": sum(int(r[2]) for r in rows),
            "indexing": any(int(r[3]) for r in rows),
        }

    def notes_unindexed(self, since: datetime, until: datetime) -> dict[str, Any]:
        rows = self._rows(
            "SELECT count(*), count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM ai_knowledge_chunk c"
            " WHERE c.source_type = 'note' AND c.source_id = a.id)) FROM activities_activity a"
            " WHERE a.type = 'note' AND a.created_at >= %s AND a.created_at < %s"
            " AND a.description LIKE 'Load test note %%'",
            [since, until],
        )
        return {"notes": int(rows[0][0]), "notes_unindexed": int(rows[0][1])}

    def attachment_states(self, ids: list[str]) -> dict[str, Any]:
        rows = self._rows(
            "SELECT id::text, scan_status, purged_at IS NOT NULL FROM activities_attachment"
            " WHERE id = ANY(%s::uuid[])",
            [ids],
        )
        return {r[0]: {"scan_status": r[1], "purged": bool(r[2])} for r in rows}


async def raw_request(
    base: str,
    method: str,
    path: str,
    headers: dict[str, str],
    *,
    body: bytes = b"",
    declared: int | None = None,
    trickle_s: float = 0.0,
    timeout_s: float = 20.0,
    host_header: str = "",
    verify: bool | ssl.SSLContext | None = None,
) -> tuple[int, str]:
    """One HTTP/1.1 request on its own TLS connection, written by hand: a body can be declared
    and never sent, or sent slowly, which no client library does on purpose. Returns the
    status and the JSON envelope's code; status 0 when nothing came back in time. The Host
    header and certificate checking as capacity_test's target_host and tls_verify give them
    (by default: --base's own host; certificates unchecked only for a loopback --base)."""
    parts = urlsplit(base)
    host = parts.hostname or "localhost"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    context: ssl.SSLContext | None = None
    if parts.scheme == "https":
        if verify is None:
            verify = not capacity_module().is_loopback(host)
        if isinstance(verify, ssl.SSLContext):
            context = verify
        else:
            context = ssl.create_default_context()
            if not verify:  # this machine's stack: a self-signed certificate (or --insecure)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host_header or parts.netloc}",
        f"Content-Length: {len(body) if declared is None else declared}",
        "Connection: close",
        *(f"{k}: {v}" for k, v in headers.items()),
    ]
    writer: asyncio.StreamWriter | None = None
    try:
        reader, stream = await asyncio.wait_for(
            asyncio.open_connection(
                "127.0.0.1" if host == "localhost" else host,
                port,
                ssl=context,
                server_hostname=host if context else None,
            ),
            timeout_s,
        )
        writer = stream
        stream.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await stream.drain()
        sender = asyncio.create_task(_send_body(stream, body, trickle_s))
        try:
            status, payload = await asyncio.wait_for(_read_response(reader), timeout_s)
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await sender
    except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, IndexError):
        return 0, ""
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
    found = CODE.search(payload.decode("utf-8", "replace"))
    return status, found.group(1) if found else ""


async def _send_body(writer: asyncio.StreamWriter, body: bytes, trickle_s: float) -> None:
    if not body:
        return
    pieces = 8 if trickle_s > 0 else 1
    size = math.ceil(len(body) / pieces)
    try:
        for start in range(0, len(body), size):
            writer.write(body[start : start + size])
            await writer.drain()
            if trickle_s > 0:
                await asyncio.sleep(trickle_s / pieces)
    except (OSError, ConnectionError):
        return  # refused early: the answer is already on its way


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    payload = await reader.readexactly(length) if length else await reader.read(64 * KIB)
    return status, payload


async def edge_batch(
    base: str,
    count: int,
    path: str,
    headers: dict[str, str],
    *,
    body: bytes = b"",
    declared: int | None = None,
    trickle_s: float = 0.0,
    host_header: str = "",
    verify: bool | ssl.SSLContext | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    answers = await asyncio.gather(
        *(
            raw_request(
                base,
                "POST",
                path,
                headers,
                body=body,
                declared=declared,
                trickle_s=trickle_s,
                host_header=host_header,
                verify=verify,
            )
            for _ in range(count)
        )
    )
    return {
        "requests": count,
        "statuses": dict(Counter(str(status) for status, _ in answers)),
        "codes": dict(Counter(f"{status}:{code}" for status, code in answers if code)),
        "seconds": round(time.perf_counter() - started, 1),
    }


async def new_note(vu: Any) -> str | None:
    if not vu.opps:
        return None
    try:
        response = await vu.client.post(
            vu.path("activities"),
            json={
                "type": "note",
                "opportunity": next(iter(vu.opps)),
                "description": f"Resilience drill attachments {vu.token}",
            },
            headers={"Idempotency-Key": str(uuid.uuid4()), "X-CSRFToken": vu.csrf()},
            timeout=45,
        )
    except httpx.HTTPError:
        return None
    return str(response.json()["id"]) if response.status_code == 201 else None


async def upload(vu: Any, note: str, name: str, content: bytes) -> tuple[int, dict[str, Any]]:
    try:
        response = await vu.client.post(
            vu.path(f"activities/{note}/attachments"),
            content=content,
            headers={
                "X-Filename": name,
                "Content-Type": "application/octet-stream",
                "X-CSRFToken": vu.csrf(),
            },
            timeout=45,
        )
    except httpx.HTTPError:
        return 0, {}
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.status_code, data if isinstance(data, dict) else {}


async def download(vu: Any, attachment: str, sha256: str) -> tuple[int, str]:
    """(status, clean | wrong bytes | scanning | blocked | refused | error | no answer)."""
    try:
        response = await vu.client.get(vu.path(f"attachments/{attachment}/download"), timeout=45)
    except httpx.HTTPError:
        return 0, "no answer"
    if response.status_code == 200:
        same = hashlib.sha256(response.content).hexdigest() == sha256
        return 200, "clean" if same else "wrong bytes"
    if response.status_code == 422:
        text = response.text
        if "blocked" in text:
            return 422, "blocked"
        return 422, "scanning" if "being checked" in text else "refused"
    return response.status_code, "error"


class UploadPump:
    """Helpers upload a small file every few seconds through the drill and try to download it
    at once; afterwards every file is fetched until it is served intact (or time is up)."""

    def __init__(self, vus: list[Any], every_s: float = 4.0) -> None:
        self.vus = vus
        self.every_s = every_s
        self.records: list[dict[str, Any]] = []
        self.stop = asyncio.Event()
        self.tasks = [asyncio.create_task(self._pump(i)) for i in range(len(vus))]

    async def _pump(self, index: int) -> None:
        vu = self.vus[index]
        note = await new_note(vu)
        stored = sent = 0
        while note and not self.stop.is_set():
            sent += 1
            content = f"resilience drill {index}-{sent} {uuid.uuid4().hex}\n".encode() * 20
            at = time.time()
            status, data = await upload(vu, note, f"drill-{index}-{sent}.txt", content)
            record: dict[str, Any] = {
                "at": at,
                "helper": index,
                "status": status,
                "id": str(data["id"]) if status == 201 and "id" in data else None,
                "sha256": hashlib.sha256(content).hexdigest(),
                "downloads": [],
                "clean_at": None,
                "blocked": False,
            }
            self.records.append(record)
            if record["id"]:
                stored += 1
                await self._fetch(record)
                if stored % 8 == 0:  # a note holds 10 files at most
                    note = await new_note(vu)
            elif status == 422:
                note = await new_note(vu)
            await wait_or_stop(self.stop, self.every_s)

    async def _fetch(self, record: dict[str, Any]) -> None:
        at = time.time()
        status, reason = await download(self.vus[record["helper"]], record["id"], record["sha256"])
        record["downloads"].append([at, status, reason])
        if reason == "clean" and record["clean_at"] is None:
            record["clean_at"] = at
        record["blocked"] = record["blocked"] or reason == "blocked"

    async def settle(self, deadline: float) -> dict[str, Any]:
        self.stop.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while time.time() < deadline:
            waiting = [
                r for r in self.records if r["id"] and r["clean_at"] is None and not r["blocked"]
            ]
            if not waiting:
                break
            for record in waiting:
                await self._fetch(record)
            await asyncio.sleep(10)
        return {"uploads": self.records}


class DrillWork:
    """The drills' own work besides the load: uploads through the ClamAV outage, the edge
    probes, the end-to-end scan and the restore drill."""

    def __init__(
        self,
        options: argparse.Namespace,
        load: CapacityLoad | None,
        database: Database | None,
        driver: Driver,
        ask: Callable[[str, float], str | None],
    ) -> None:
        self.options = options
        self.load = load
        self.database = database
        self.driver = driver
        self.ask = ask

    async def _helpers(self, count: int) -> list[Any]:
        return await self.load.helpers(count) if self.load is not None else []

    async def start(self, drill: Drill) -> UploadPump | None:
        vus = await self._helpers(self.options.uploaders)
        return UploadPump(vus) if vus else None

    async def finish(self, handle: Any, deadline: float) -> dict[str, Any]:
        result: dict[str, Any] = await handle.settle(deadline)
        if self.database is not None:
            ids = [r["id"] for r in result["uploads"] if r["id"]]
            states = await asyncio.to_thread(self.database.attachment_states, ids) if ids else {}
            result["scan_states"] = dict(Counter(s["scan_status"] for s in states.values()))
        return result

    async def edge(self) -> dict[str, Any]:
        base = self.options.base
        ct = capacity_module()
        # The same Host header and certificate checking as the virtual users'.
        target: dict[str, Any] = {
            "host_header": ct.target_host(self.options),
            "verify": ct.tls_verify(self.options),
        }
        common = {"Origin": base, "Referer": base + "/"}
        as_json = {**common, "Content-Type": "application/json"}
        out: dict[str, Any] = {
            "json_too_large": await edge_batch(
                base, 1, f"{API}/workspaces/me/activities", as_json, declared=2 * MIB, **target
            ),
            "upload_too_large": await edge_batch(
                base,
                1,
                f"{API}/workspaces/me/activities/{uuid.uuid4()}/attachments",
                {**common, "Content-Type": "application/octet-stream", "X-Filename": "big.txt"},
                declared=12 * MIB,
                **target,
            ),
            "sign_in_burst": await edge_batch(
                base,
                80,
                f"{API}/auth/login",
                as_json,
                body=b'{"email": "nobody@example.invalid", "password": "not-a-password"}',
                **target,
            ),
            "bodies_in_flight": await edge_batch(
                base,
                16,
                f"{API}/workspaces/me/activities",
                as_json,
                body=b"x" * 64 * KIB,
                trickle_s=6,
                **target,
            ),
        }
        helpers = await self._helpers(1)
        note = await new_note(helpers[0]) if helpers else None
        if helpers and note:
            vu = helpers[0]
            cookies = "; ".join(f"{c.name}={c.value}" for c in vu.client.cookies.jar)
            out["uploads_in_flight"] = await edge_batch(
                base,
                8,
                f"{API}/workspaces/me/activities/{note}/attachments",
                {
                    **common,
                    "Cookie": cookies,
                    "X-CSRFToken": vu.csrf(),
                    "Content-Type": "application/octet-stream",
                    "X-Filename": "drill-edge.txt",
                },
                body=b"resilience drill edge upload\n" * (512 * KIB // 29),
                trickle_s=6,
                **target,
            )
        return out

    async def scan(self) -> dict[str, Any]:
        from tests.drills.clamav_drill import eicar

        helpers = await self._helpers(1)
        note = await new_note(helpers[0]) if helpers else None
        if not helpers or not note:
            return {"error": "no signed-in helper with a deal to attach to"}
        vu = helpers[0]
        files = {"clean": b"Resilience drill: a clean text file.\n" * 20, "eicar": eicar()}
        out: dict[str, Any] = {}
        for kind, content in files.items():
            status, data = await upload(vu, note, f"drill-{kind}.txt", content)
            out[kind] = {
                "upload": status,
                "id": str(data["id"]) if status == 201 and "id" in data else None,
                "sha256": hashlib.sha256(content).hexdigest(),
                "outcome": "pending" if status == 201 else "not uploaded",
                "seconds": None,
            }
        started = time.time()
        while time.time() < started + SCAN_TIMEOUT_S:
            pending = [v for v in out.values() if v["outcome"] == "pending"]
            if not pending:
                break
            for item in pending:
                status, reason = await download(vu, item["id"], item["sha256"])
                if reason in ("clean", "blocked", "wrong bytes"):
                    item["outcome"], item["seconds"] = reason, round(time.time() - started, 1)
            await asyncio.sleep(3)
        if out["eicar"]["outcome"] == "clean":
            out["eicar"]["outcome"] = "served"  # the test file came back: the scan failed open
        bad_id = out["eicar"]["id"]
        out["eicar"]["purged"] = None
        if self.database is not None and bad_id and out["eicar"]["outcome"] == "blocked":
            for _ in range(24):  # the purge is an outbox job of its own
                states = await asyncio.to_thread(self.database.attachment_states, [bad_id])
                if states.get(bad_id, {}).get("purged"):
                    out["eicar"]["purged"] = True
                    break
                await asyncio.sleep(5)
            else:
                out["eicar"]["purged"] = False
        return out

    async def restore_drill(self, drill: Drill) -> dict[str, Any]:
        if self.driver.is_manual(drill.service or "postgres"):
            answer = await asyncio.to_thread(
                self.ask,
                "\n>>> Run this environment's restore drill (docs/runbooks.md#restore-drill;"
                " point-in-time: backend/tests/drills/pitr_drill.py) and type PASS or FAIL.",
                self.options.manual_timeout,
            )
            result = answer.strip().upper() if answer else None
            return {"operator_reported": True, "result": result}
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        report = Path(f"restore-drill-{self.options.environment}-{stamp}.json").resolve()
        command = [
            sys.executable,
            str(BACKEND / "tests" / "drills" / "restore_drill.py"),
            "--container",
            self.options.restore_container,
            "--report",
            str(report),
        ]
        try:
            done = await asyncio.to_thread(run_bounded, command, RESTORE_DRILL_TIMEOUT_S)
        except DriverError as exc:
            return {"error": str(exc)}
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return {
            "exit_code": done.returncode,
            "result": data.get("result"),
            "backup": data.get("backup"),
            "restore": data.get("restore"),
            "report": str(report),
            "output_tail": (done.stdout + done.stderr)[-400:],
        }


# --- reports --------------------------------------------------------------------------------------
def render_markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    finished = report.get("finished_at", "(running)")
    lines = [
        f"# Resilience drills: {env.upper()}",
        "",
        f"Environment **{env}**; {report['started_at']} to {finished}; driver"
        f" `{report['driver']}`; {report.get('load', 'no load')}.",
    ]
    if env == "local":
        lines += ["", f"> {LOCAL_CAVEAT}"]
    lines += [
        "",
        "| Drill | Fault | Errors seen by users | p95 before / during / after | Recovery | Backlog"
        " | Integrity | Result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [summary_row(d, env) for d in report["drills"]]
    for drill in report["drills"]:
        lines += ["", f"## {drill['drill']}: {drill['verdict']} ({env})", "", drill["expected"]]
        lines += ["", "Pass criteria: " + "; ".join(drill["criteria"]) + ".", ""]
        for check in drill["checks"]:
            mark = {True: "PASS", False: "FAIL", None: "n/a"}[check["ok"]]
            lines.append(f"- **{mark}** {check['name']}: {check['detail']}")
    if report.get("notes"):
        lines += ["", "## Notes", "", *(f"- {n}" for n in report["notes"])]
    return "\n".join(lines) + "\n"


def summary_row(drill: dict[str, Any], env: str) -> str:
    m = drill.get("measured") or {}
    impact = m.get("fault_and_recovery")
    if impact:
        statuses = ", ".join(f"{k} x{v}" for k, v in impact["errors_by_status"].items())
        errors = f"{impact['errors']:,} of {impact['requests']:,}" + (
            f" ({statuses})" if statuses else ""
        )
        p95 = " / ".join(
            "-" if m[w]["p95_ms"] is None else f"{m[w]['p95_ms']:,.0f}"
            for w in ("before", "during", "after")
        )
        p95 += " ms"
    else:
        errors, p95 = "n/a", "n/a"
    rec = m.get("recovery") or {}
    recovery_text = (
        "n/a"
        if not rec
        else ("not within the window" if rec.get("seconds") is None else f"{rec['seconds']:g} s")
    )
    drain = m.get("backlog")
    if drain and drain.get("measured"):
        when = "not drained" if drain["seconds"] is None else f"drained in {drain['seconds']:g} s"
        backlog = f"peak {drain['peak']:.0f}, {when}, {drain['dead_added']:.0f} dead"
    else:
        backlog = "n/a"
    integrity = next((c for c in drill["checks"] if c["name"] == "integrity"), None)
    integrity_text = (
        "n/a"
        if integrity is None
        else {True: "clean", False: "PROBLEMS", None: "not checked"}[integrity["ok"]]
    )
    cells = [
        drill["drill"],
        drill["fault"],
        errors,
        p95,
        recovery_text,
        backlog,
        integrity_text,
        f"**{drill['verdict']}** ({env})",
    ]
    return "| " + " | ".join(c.replace("|", "/") for c in cells) + " |"


def output_paths(options: argparse.Namespace, started: float) -> tuple[Path, Path]:
    stamp = datetime.fromtimestamp(started, UTC).strftime("%Y%m%dT%H%M%SZ")
    report = Path(options.out or f"resilience-{options.environment}-{stamp}.json")
    markdown = Path(options.markdown) if options.markdown else report.with_suffix(".md")
    return report, markdown


def write_report(report: dict[str, Any], paths: tuple[Path, Path]) -> None:
    paths[0].write_text(json.dumps(report, indent=1, default=str), encoding="utf-8", newline="\n")
    paths[1].write_text(render_markdown(report), encoding="utf-8", newline="\n")


def public_options(options: argparse.Namespace) -> dict[str, Any]:
    shown = {k: v for k, v in vars(options).items() if k not in ("metrics_token",)}
    if shown.get("pg"):
        shown["pg"] = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", str(shown["pg"]))
    return shown


# --- the command line ---------------------------------------------------------------------------
def environment_name(value: str) -> str:
    if not ENVIRONMENT.match(value):
        raise argparse.ArgumentTypeError(
            "lower-case letters, digits and dashes, starting with a letter (local, staging, ...)"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="the drills, their expected behaviour and pass criteria")
    for name in ("plan", "run", "restore"):
        s = sub.add_parser(name)
        s.add_argument("--drills", default="all", help="all, or names separated by commas")
        s.add_argument("--target", default="", help="another service for the one drill selected")
        s.add_argument("--driver", choices=("docker", "manual"), default="docker")
        s.add_argument("--prefix", default="arkray-prod-", help="compose container name prefix")
        s.add_argument(
            "--container",
            action="append",
            default=[],
            metavar="SERVICE=NAME",
            help="a container named otherwise, e.g. postgres=arkray-postgres-1",
        )
        s.add_argument("--manual-services", default="", help="services the operator handles")
        s.add_argument("--docker-timeout", type=float, default=60.0)
        s.add_argument("--manual-timeout", type=float, default=600.0)
        s.add_argument("--baseline", type=float, default=60.0)
        s.add_argument("--recovery", type=float, default=None, help="replaces each drill's own")
        s.add_argument("--drain-timeout", type=float, default=None)
        s.add_argument("--quiesce", type=float, default=10.0)
        s.add_argument("--tolerance-pct", type=float, default=1.0)
    run = sub.choices["run"]
    run.add_argument("--environment", type=environment_name, required=True)
    run.add_argument("--users", default="users.json")
    run.add_argument("--sessions", default="")
    run.add_argument("--base", default="https://localhost:8443")
    # The target, as capacity_test.py takes it (its target_host, cookie_domain, tls_verify).
    run.add_argument(
        "--host",
        default="",
        help="Host header (default: --base's own; localhost:8443 for a loopback --base)",
    )
    run.add_argument(
        "--insecure",
        action="store_true",
        help="skip certificate checks (skipped by default only for a loopback --base)",
    )
    run.add_argument("--ca-file", default="", help="check certificates against this CA")
    run.add_argument("--pg", default="", help="SQL access for the integrity checks")
    run.add_argument("--vus", type=int, default=50)
    run.add_argument("--think-mean", type=float, default=2.0)
    run.add_argument("--uploaders", type=int, default=3)
    run.add_argument(
        "--health-url", default="", help="the API directly, e.g. http://127.0.0.1:8300"
    )
    run.add_argument("--health-host", default="localhost")
    run.add_argument("--metrics-token", default=os.environ.get("METRICS_TOKEN", ""))
    run.add_argument("--probe", choices=("auto", "http", "exec", "none"), default="auto")
    run.add_argument("--watch", default=WATCH)
    run.add_argument("--restore-container", default="arkray-postgres-1")
    run.add_argument("--out", default="")
    run.add_argument("--markdown", default="")
    return parser


def make_driver(
    options: argparse.Namespace, ask: Callable[[str, float], str | None]
) -> RoutedDriver:
    overrides = dict(item.split("=", 1) for item in options.container if "=" in item)
    manual_services = {s.strip() for s in options.manual_services.split(",") if s.strip()}
    docker = None
    if options.driver == "docker":
        found = shutil.which("docker") if options.command != "plan" else "docker"
        if found is None:
            raise SystemExit("docker isn't on PATH (or use --driver manual)")
        docker = DockerDriver(
            options.prefix, overrides, timeout_s=options.docker_timeout, docker=found
        )
    manual = (
        ManualDriver(ask, options.manual_timeout)
        if (options.driver == "manual" or manual_services)
        else None
    )
    return RoutedDriver(docker, manual, manual_services)


def timings_from(options: argparse.Namespace) -> Timings:
    return Timings(
        baseline_s=options.baseline,
        recovery_s=options.recovery,
        drain_timeout_s=options.drain_timeout,
        quiesce_s=options.quiesce,
        tolerance_pct=options.tolerance_pct,
    )


def estimate(drill: Drill, timings: Timings) -> tuple[float, float]:
    """Seconds a drill takes: (when everything recovers at once, at the latest)."""
    if drill.kind == "scan":
        return 30.0, SCAN_TIMEOUT_S + 60
    if drill.kind == "restore":
        return 120.0, RESTORE_DRILL_TIMEOUT_S
    recovery_s = drill.recovery_s if timings.recovery_s is None else timings.recovery_s
    drain = drill.drain_timeout_s if timings.drain_timeout_s is None else timings.drain_timeout_s
    fixed = timings.baseline_s + drill.hold_s + timings.quiesce_s + VERIFY_ALLOWANCE_S
    waits = drain if (drill.criteria.drain_scope or drill.workload) else recovery_s
    return fixed + recovery_s, fixed + max(recovery_s, waits)


def plan_text(drills: Sequence[Drill], driver: Driver, timings: Timings) -> str:
    lines: list[str] = []
    low_total = high_total = 0.0
    for drill in drills:
        low, high = estimate(drill, timings)
        low_total, high_total = low_total + low, high_total + high
        own = {"edge": "edge probes from this process", "scan": "two uploads from this process"}
        own["restore"] = "tests/drills/restore_drill.py (or the operator, when manual)"
        inject = "; ".join(driver.describe(a) for a in drill.inject) or own.get(drill.kind, "-")
        restore = "; ".join(driver.describe(a) for a in drill.restore) or "-"
        lines += [
            f"{drill.name}: {drill.title}",
            f"  fault after {timings.baseline_s:g} s: {inject}",
            f"  held {drill.hold_s:g} s, then: {restore}",
            f"  pass: {'; '.join(drill.pass_criteria())}",
            f"  about {low / 60:.1f}-{high / 60:.1f} min",
        ]
    lines.append(
        f"total: about {low_total / 60:.0f}-{high_total / 60:.0f} min, plus signing the users in"
        " (up to 5 min for 100 without saved sessions)"
    )
    return "\n".join(lines)


def print_catalogue() -> None:
    for drill in CATALOGUE:
        print(f"{drill.name}: {drill.title}\n  expected: {drill.expectation}")
        print("  pass: " + "; ".join(drill.pass_criteria()) + "\n")


ACTIVE: list[DrillRunner] = []


def restore_everything() -> None:
    for runner in ACTIVE:
        runner.restore_owed_now()


async def run_series(
    options: argparse.Namespace,
    drills: list[Drill],
    driver: RoutedDriver,
    ask: Callable[[str, float], str | None],
) -> dict[str, Any]:
    started = time.time()
    paths = output_paths(options, started)
    database = Database(options.pg) if options.pg else None
    needs_users = any(d.needs_load or d.kind in ("edge", "scan") or d.workload for d in drills)
    load = CapacityLoad(options) if needs_users else None
    probe_log = ProbeLog()
    source: HttpProbes | ExecProbes | None = None
    if options.probe in ("auto", "http") and options.health_url:
        source = HttpProbes(
            probe_log, options.health_url, options.health_host, options.metrics_token
        )
    elif (
        options.probe in ("auto", "exec")
        and driver.docker is not None
        and not driver.is_manual("backend")
    ):
        source = ExecProbes(probe_log, driver.docker)
    runner = DrillRunner(
        environment=options.environment,
        driver=driver,
        clock=RealClock(),
        timings=timings_from(options),
        load=load,
        probe_log=probe_log if source is not None else None,
        database=database,
        extras=DrillWork(options, load, database, driver, ask),
        watch=[s.strip() for s in options.watch.split(",") if s.strip()],
    )
    ACTIVE.append(runner)
    report: dict[str, Any] = {
        "environment": options.environment,
        "label": f"{options.environment.upper()}"
        + (" (local machine: not real hosting)" if options.environment == "local" else ""),
        "started_at": iso(started),
        "driver": driver.name,
        "load": f"{options.vus} virtual users, think time {options.think_mean:g} s"
        if needs_users
        else "no load",
        "probes": type(source).__name__ if source else "none",
        "host": platform.platform(),
        "options": public_options(options),
        "drills": [],
        "notes": [],
    }
    stop = asyncio.Event()
    background: list[asyncio.Task[Any]] = [asyncio.create_task(runner.watch_host(stop))]
    if source is not None:
        background.append(asyncio.create_task(source.run(stop)))
    try:
        if load is not None:
            if any(d.needs_load for d in drills):
                await load.start()
            if any(d.kind in ("edge", "scan") or d.workload for d in drills):
                await load.helpers(max(1, options.uploaders))
        if database is not None:
            deadlocks = await asyncio.to_thread(database.deadlocks)
            runner.verified_before = {"deadlocks": deadlocks}
        for drill in drills:
            try:
                await runner.gate(drill)
            except GateError as exc:
                report["notes"].append(f"stopped before {drill.name}: {exc}")
                print(f"!!! stopped: {exc}", flush=True)
                break
            report["drills"].append(await runner.run(drill))
            write_report(report, paths)
    finally:
        stop.set()
        if load is not None:
            await load.stop()
        await asyncio.gather(*background, return_exceptions=True)
        report["finished_at"] = iso(time.time())
        report["events"] = runner.events
        report["stalls"] = [[iso(at), seconds] for at, seconds in runner.stalls]
        write_report(report, paths)
        print(f"\nreport -> {paths[0]}  {paths[1]}", flush=True)
    return report


def _interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    if options.command == "list":
        print_catalogue()
        return 0
    drills = select_drills(options.drills, options.target)
    prompter = Prompter()
    driver = make_driver(options, prompter.ask)
    if options.command == "plan":
        print(plan_text(drills, driver, timings_from(options)))
        return 0
    if driver.manual is not None and not sys.stdin.isatty():
        raise SystemExit("the manual driver needs a terminal to confirm each step")
    if options.command == "restore":
        failed = 0
        for drill in drills:
            for action in drill.restore:
                try:
                    print(json.dumps(driver.apply(action)), flush=True)
                except DriverError as exc:
                    failed += 1
                    print(f"!!! {action.describe()}: {exc}", flush=True)
        return 1 if failed else 0
    # Bounded database connections everywhere, capacity_test's verify included (libpq reads it).
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    atexit.register(restore_everything)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _interrupt)
    try:
        report = asyncio.run(run_series(options, drills, driver, prompter.ask))
    except KeyboardInterrupt:
        print("\ninterrupted: restoring any fault still applied", flush=True)
        return 130
    finally:
        restore_everything()
    verdicts = Counter(d["verdict"] for d in report["drills"])
    print(f"{options.environment}: {dict(verdicts)}", flush=True)
    return 0 if set(verdicts) <= {"PASS"} and len(report["drills"]) == len(drills) else 1


if __name__ == "__main__":
    sys.exit(main())
