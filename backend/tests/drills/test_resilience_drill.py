"""tests/drills/resilience_drill.py's own logic, without Docker or a stack: a fake driver, a
fake load and a fake clock (docs/staging-validation.md#6-resilience-drills). What it can't
cover here (the real virtual users, the docker CLI, the edge, ClamAV) is the drill run itself.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
from typing import Any

import pytest

from tests.drills import resilience_drill as rd

ENVELOPE_503 = '{"error":{"code":"service_unavailable","message":"Arkray CRM is temporarily"'


# --- fakes -----------------------------------------------------------------------------------
class FakeClock:
    """Time moves only when the runner sleeps."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(seconds, 0.0)
        await asyncio.sleep(0)


class FailingClock(FakeClock):
    """Raises on the n-th sleep (the 2nd is a fault drill's hold)."""

    def __init__(self, fail_at: int, error: BaseException) -> None:
        super().__init__()
        self.fail_at, self.error = fail_at, error

    async def sleep(self, seconds: float) -> None:
        if len(self.sleeps) + 1 == self.fail_at:
            self.sleeps.append(seconds)
            raise self.error
        await super().sleep(seconds)


class BlockingClock(FakeClock):
    """Blocks forever on the n-th sleep, so the test can cancel the drill there."""

    def __init__(self, block_at: int) -> None:
        super().__init__()
        self.block_at = block_at
        self.reached: asyncio.Event | None = None

    async def sleep(self, seconds: float) -> None:
        if len(self.sleeps) + 1 == self.block_at:
            self.sleeps.append(seconds)
            assert self.reached is not None
            self.reached.set()
            await asyncio.Event().wait()
        await super().sleep(seconds)


class FakeDriver:
    name = "fake"

    def __init__(self, runner: rd.DrillRunner | None = None, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail or set()
        self.owed_at_inject: list[str] | None = None
        self.runner = runner

    def apply(self, action: rd.Action) -> dict[str, Any]:
        self.calls.append(action.describe())
        if self.runner is not None and self.owed_at_inject is None:
            self.owed_at_inject = [a.describe() for a in self.runner.owed]
        if action.describe() in self.fail:
            raise rd.DriverError(f"{action.describe()} timed out after 70 s")
        out: dict[str, Any] = {"action": action.describe(), "command": f"fake {action.describe()}"}
        if action.verb == "stop":
            out["exit_code"] = 0
        return out

    def describe(self, action: rd.Action) -> str:
        return f"fake {action.describe()}"

    def inspect(self, services: Any) -> None:
        return None

    def children(self, service: str) -> int | None:
        return 8

    def is_manual(self, service: str) -> bool:
        return False


class FakeLoad:
    """Twenty requests a second, all 200 in 40 ms, made up from the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.origin = clock.now()
        self.phases: list[str] = []
        self.active: list[bool] = []

    def set_phase(self, label: str) -> None:
        self.phases.append(label)

    def set_active(self, active: bool) -> None:
        self.active.append(active)

    def forget(self) -> None:
        pass

    def samples(self) -> list[rd.Sample]:
        count = int((self.clock.now() - self.origin) * 20)
        return [rd.Sample(self.origin + i / 20, "dashboard", 200, 40.0) for i in range(count)]

    def take_failures(self) -> list[dict[str, Any]]:
        return []

    def leak_counts(self) -> dict[str, int]:
        return {"leaks": 0, "probe_leaks": 0}

    async def verify(self) -> dict[str, Any] | None:
        return {"duplicate_logical_creates": 0, "deadlocks": 0, "dashboard_math": {}}


def drill(name: str) -> rd.Drill:
    return next(d for d in rd.CATALOGUE if d.name == name)


def runner_for(clock: FakeClock, fail: set[str] | None = None) -> tuple[rd.DrillRunner, FakeDriver]:
    driver = FakeDriver(fail=fail)
    runner = rd.DrillRunner(
        environment="local",
        driver=driver,
        clock=clock,
        timings=rd.Timings(),
        load=FakeLoad(clock),
        say=lambda text: None,
    )
    driver.runner = runner
    return runner, driver


def series(
    start: float, end: float, status: Any = 200, ms: Any = 40.0, name: str = "dashboard"
) -> list[rd.Sample]:
    """Twenty requests a second in [start, end); status and ms may be functions of time."""
    out = []
    t = start
    while t < end:
        out.append(
            rd.Sample(
                t + ms(t) / 1000 if callable(ms) else t + ms / 1000,
                name,
                status(t) if callable(status) else status,
                ms(t) if callable(ms) else ms,
            )
        )
        t += 0.05
    return out


def readiness(start: float, end: float, unready: tuple[float, float] | None) -> list[Any]:
    probes = []
    for second in range(int(start), int(end)):
        down = unready is not None and unready[0] <= second < unready[1]
        state = ("unavailable", 503) if down else ("ok", 200)
        probes.append(rd.ProbeReading(float(second), "ready", state[1], state[0], 3.0))
        probes.append(rd.ProbeReading(float(second), "live", 200, "ok", 1.0))
    return probes


def metrics(
    points: list[tuple[float, float]], dead: float = 0.0, old: tuple[float, ...] = ()
) -> list[rd.MetricsReading]:
    """Scrapes: pending as given; the oldest due event 40 s old while the backlog is large
    (or at the times in `old`), else 1 s; `dead` dead events from t=100 on."""
    base = {
        "outbox_in_flight": 3.0,
        "outbox_dead": 0.0,
        "outbox_oldest_due_s": 1.0,
        "outbox_oldest_undelivered_s": 0.0,
    }
    out = []
    for at, pending in points:
        values = {**base, "outbox_pending": pending}
        if at >= 100:
            values["outbox_dead"] = dead
        if pending > 50 or at in old:
            values["outbox_oldest_due_s"] = 40.0
        out.append(rd.MetricsReading(at, values))
    return out


def postgres_stop_observation(**changes: Any) -> rd.Observation:
    """The healthy shape of the drill: 503s while the database is away, readiness 503,
    liveness 200, the backlog back 15 s after the restore."""
    obs = rd.Observation(
        base_start=0.0,
        fault_at=60.0,
        restored_at=105.0,
        recovery_end=195.0,
        end=200.0,
        fault={
            "inject": [{"action": "stop postgres", "command": "docker stop postgres"}],
            "restore": [{"action": "start postgres", "command": "docker start postgres"}],
        },
        samples=series(0, 200, status=lambda t: 503 if 60.5 <= t < 109 else 200),
        failures=[{"stage": "postgres-stop:fault", "status": 503, "body": ENVELOPE_503}] * 5,
        probes=readiness(0, 200, (61, 108)),
        metrics=metrics(
            [(float(t), 5.0) for t in range(0, 60, 5)]
            + [(float(t), 5.0 + (t - 60) * 4) for t in range(60, 105, 5)]
            + [(110.0, 150.0), (115.0, 40.0), (120.0, 8.0), (125.0, 6.0), (130.0, 5.0)]
        ),
        watch_before={
            "backend": {"status": "running", "restarts": 0, "started_at": "a", "oom_killed": False},
            "postgres": {
                "status": "running",
                "restarts": 0,
                "started_at": "p",
                "oom_killed": False,
            },
        },
        watch_after={
            "backend": {"status": "running", "restarts": 0, "started_at": "a", "oom_killed": False},
            "postgres": {
                "status": "running",
                "restarts": 0,
                "started_at": "p2",
                "oom_killed": False,
            },
        },
        integrity={"checked": True, "problems": [], "leaks": {"leaks": 0, "probe_leaks": 0}},
    )
    for key, value in changes.items():
        setattr(obs, key, value)
    return obs


def checks_by_name(checks: list[rd.Check]) -> dict[str, rd.Check]:
    return {c.name: c for c in checks}


# --- what users saw --------------------------------------------------------------------------
def test_requests_are_classified_as_capacity_test_counts_them():
    from tests.performance import capacity_test

    cases = [
        ("dashboard", 200),
        ("move_stage", 409),
        ("search", 429),
        ("lead_detail", 0),
        ("create_opportunity", 503),
        ("lead_list", 401),
        ("probe_opp", 404),
        ("probe_move", 422),
        ("negotiation_revision", 400),
        ("patch_lead", 500),
    ]
    recorder = capacity_test.Recorder()
    recorder.stage = "hold"
    for index, (name, status) in enumerate(cases):
        recorder.add(name, status, 10.0 + index, 1)
    theirs = capacity_test.aggregate(recorder, "hold", pages=False)
    ours = rd.window_stats(
        [rd.Sample(s.at, s.name, s.status, s.ms) for s in recorder.samples], 0, 2e10
    )
    assert ours["errors"] == theirs["server_or_network_errors"] + theirs["unexpected_4xx"] == 4
    assert ours["throttled_429"] == theirs["throttled_429"] == 1
    assert ours["requests"] == theirs["n"] == len(cases) - 1
    assert ours["errors_by_status"] == {"0": 1, "401": 1, "500": 1, "503": 1}


def test_the_load_is_capacity_tests_own_virtual_users(tmp_path):
    """The parts of capacity_test.py the drill drives (its recorder, its run's switches): if
    they change there, this fails here rather than in the middle of a drill."""
    accounts = {
        "password": "not-a-password",
        "users": [{"index": 0, "id": "u0", "email": "u0@example.invalid", "role": "sales_user"}],
        "admins": [],
    }
    users = tmp_path / "users.json"
    users.write_text(json.dumps(accounts), encoding="utf-8")
    load = rd.CapacityLoad(
        rd.build_parser().parse_args(["run", "--environment", "local", "--users", str(users)])
    )
    load.set_phase("postgres-stop:fault")
    load.run.rec.add("dashboard", 503, 12.0, 0)
    load.run.rec.failures.append({"stage": "postgres-stop:fault", "status": 503, "body": "{}"})
    (sample,) = load.samples()
    assert (sample.name, sample.status, sample.ms) == ("dashboard", 503, 12.0)
    assert load.run.rec.samples[0].stage == "postgres-stop:fault"
    assert len(load.take_failures()) == 1
    assert load.take_failures() == []
    load.roster = [object(), object()]
    load.set_active(True)
    assert load.run.target_active == 2
    load.set_active(False)
    assert load.run.target_active == 0
    load.forget()
    assert load.samples() == []
    assert load.leak_counts() == {"leaks": 0, "probe_leaks": 0}
    assert callable(load.ct.verify)
    assert callable(load.ct.vu_roster)
    # The local target as before; another host passed through to capacity_test's virtual users.
    assert (load.run.host, load.run.tls) == ("localhost:8443", False)
    staging = rd.CapacityLoad(
        rd.build_parser().parse_args(
            [
                "run",
                "--environment",
                "staging",
                "--users",
                str(users),
                "--base",
                "https://crm-staging.example.com",
            ]
        )
    )
    assert (staging.run.host, staging.run.cookie_domain, staging.run.tls) == (
        "crm-staging.example.com",
        "crm-staging.example.com",
        True,
    )
    insecure = rd.build_parser().parse_args(
        [
            "run",
            "--environment",
            "staging",
            "--users",
            str(users),
            "--base",
            "https://crm-staging.example.com",
            "--insecure",
        ]
    )
    assert rd.CapacityLoad(insecure).run.tls is False


def test_sign_in_page_totals_and_post_drill_checks_are_not_the_users_work():
    for name in (
        "page:dashboard",
        "login",
        "login_mfa",
        "auth_csrf",
        "setup_board",
        "dashboard_final",
    ):
        assert rd.classify(rd.Sample(1.0, name, 503, 5.0)) == "ignored"


def test_window_stats_percentiles_and_rates():
    samples = [rd.Sample(float(i), "dashboard", 200, float(i + 1)) for i in range(100)]
    samples.append(rd.Sample(50.5, "search", 503, 7.0))
    stats = rd.window_stats(samples, 0.0, 200.0)
    assert stats["requests"] == 101
    assert (stats["p50_ms"], stats["p95_ms"], stats["p99_ms"], stats["max_ms"]) == (
        50.0,
        95.0,
        99.0,
        100.0,
    )
    assert stats["error_rate_pct"] == round(100 / 101, 3)
    assert rd.window_stats(samples, 500.0, 600.0)["error_rate_pct"] is None


def test_requests_in_flight_at_a_stop():
    long = rd.Sample(10.5, "dashboard", 200, 800.0)  # 9.7 -> 10.5
    short = rd.Sample(10.05, "dashboard", 200, 100.0)  # 9.95 -> 10.05
    before = rd.Sample(9.9, "dashboard", 200, 50.0)
    after = rd.Sample(10.4, "dashboard", 503, 50.0)  # began after the stop
    found = rd.in_flight([long, short, before, after], 10.0, rd.INFLIGHT_MIN_AGE_S)
    assert found == [long]
    assert rd.in_flight([long, short, before, after], 10.0, 0.0) == [long, short]


# --- recovery ----------------------------------------------------------------------------------
def test_recovery_is_the_later_of_readiness_and_the_error_rate():
    samples = series(0, 300, status=lambda t: 503 if 100 <= t < 152 else 200)
    probes = readiness(0, 300, (101, 150))
    found = rd.recovery(
        samples,
        probes,
        fault_at=100,
        restored_at=145,
        end=300,
        threshold_pct=1.0,
        healthy=frozenset({"ok"}),
    )
    # the last bad 5 s bucket is [150, 155): users settled at 155, readiness at 150
    assert (found.settled_at, found.ready_at, found.seconds) == (155.0, 150.0, 10.0)
    later = rd.recovery(
        samples,
        readiness(0, 300, (101, 170)),
        fault_at=100,
        restored_at=145,
        end=300,
        threshold_pct=1.0,
        healthy=frozenset({"ok"}),
    )
    assert (later.ready_at, later.seconds) == (170.0, 25.0)


def test_no_recovery_within_the_window_is_none_not_the_window():
    samples = series(0, 200, status=lambda t: 503 if t >= 100 else 200)
    found = rd.recovery(
        samples, [], fault_at=100, restored_at=145, end=200, threshold_pct=1.0, healthy=frozenset()
    )
    assert found.seconds is None
    assert "not back" in found.note


def test_without_readiness_probes_recovery_rests_on_user_errors_and_says_so():
    samples = series(0, 300, status=lambda t: 503 if 100 <= t < 132 else 200)
    found = rd.recovery(
        samples, [], fault_at=100, restored_at=130, end=300, threshold_pct=1.0, healthy=frozenset()
    )
    assert (found.seconds, found.readiness_probed) == (5.0, False)
    assert "readiness not probed" in found.note


def test_an_undisturbed_drill_recovers_at_once():
    found = rd.recovery(
        series(0, 300),
        readiness(0, 300, None),
        fault_at=100,
        restored_at=130,
        end=300,
        threshold_pct=1.0,
        healthy=frozenset({"ok"}),
    )
    assert found.seconds == 0.0


def test_a_silence_is_not_a_recovery():
    """Nothing completing (every request stuck) is as bad as errors."""
    samples = [s for s in series(0, 300) if not 100 <= s.at < 140]
    found = rd.recovery(
        samples, [], fault_at=100, restored_at=120, end=300, threshold_pct=1.0, healthy=frozenset()
    )
    assert found.settled_at == 140.0
    assert found.seconds == 20.0


def test_an_isolated_error_under_the_tolerance_does_not_count():
    samples = series(0, 300)
    samples.append(rd.Sample(200.0, "dashboard", 0, 2100.0))  # one in 100 in its bucket
    found = rd.recovery(
        samples, [], fault_at=100, restored_at=130, end=300, threshold_pct=1.0, healthy=frozenset()
    )
    assert found.seconds == 0.0


# --- the backlog -------------------------------------------------------------------------------
def test_backlog_drain_measured_against_its_own_baseline():
    readings = metrics(
        [(float(t), 5.0 + (t % 10)) for t in range(0, 60, 5)]
        + [(float(t), 300.0) for t in range(60, 105, 5)]
        + [(105.0, 280.0), (110.0, 200.0), (115.0, 30.0), (120.0, 12.0), (125.0, 8.0)],
        old=(115.0,),
    )
    drain = rd.backlog_drain(
        readings, base_start=0, fault_at=60, restored_at=105, end=200, scope="pending"
    )
    # baseline peak 10 (+20): 30 at 115 is under, but its oldest event is 40 s old
    assert (drain["baseline_peak"], drain["ceiling"], drain["peak"]) == (10.0, 30.0, 300.0)
    assert drain["seconds"] == 15.0
    assert drain["dead_added"] == 0


def test_a_backlog_that_never_drains_and_dead_events():
    readings = metrics(
        [(float(t), 5.0) for t in range(0, 60, 5)] + [(float(t), 400.0) for t in range(60, 200, 5)],
        dead=3.0,
    )
    drain = rd.backlog_drain(
        readings, base_start=0, fault_at=60, restored_at=105, end=200, scope="all"
    )
    assert drain["seconds"] is None
    assert drain["dead_added"] == 3.0
    assert not rd.backlog_drain(
        [], base_start=0, fault_at=60, restored_at=105, end=200, scope="all"
    )["measured"]


def test_metrics_text_is_parsed_over_every_queue():
    text = "\n".join(
        [
            "# HELP arkray_outbox_events Outbox events.",
            'arkray_outbox_events{queue="ai_index",status="pending"} 12',
            'arkray_outbox_events{queue="default",status="pending"} 3',
            'arkray_outbox_events{queue="ai_index",status="in_flight"} 4',
            'arkray_outbox_events{queue="email",status="dead"} 1',
            'arkray_outbox_oldest_due_seconds{queue="ai_index"} 7.5',
            'arkray_outbox_oldest_due_seconds{queue="email"} 2',
            'arkray_outbox_oldest_undelivered_seconds{queue="default"} 31.2',
            "arkray_db_up 0",
            "arkray_broker_up 1",
        ]
    )
    values = rd.parse_metrics(text)
    assert values["outbox_pending"] == 15
    assert values["outbox_in_flight"] == 4
    assert values["outbox_dead"] == 1
    assert values["outbox_oldest_due_s"] == 7.5
    assert values["outbox_oldest_undelivered_s"] == 31.2
    assert (values["db_up"], values["broker_up"]) == (0, 1)


# --- criteria ------------------------------------------------------------------------------------
def test_postgres_stop_passes_when_it_behaves_as_documented():
    _, checks = rd.evaluate(drill("postgres-stop"), postgres_stop_observation(), rd.Timings())
    assert rd.verdict(checks) == "PASS", [c for c in checks if c.ok is not True]
    found = checks_by_name(checks)
    assert "5 s after the restore" in found["recovery"].detail
    assert "15 s after the restore" in found["backlog drained"].detail


def test_a_500_or_a_401_while_the_database_is_away_fails_the_drill():
    for status in (500, 401):
        samples = series(0, 200, status=lambda t: 503 if 60.5 <= t < 109 else 200)
        samples.append(rd.Sample(70.0, "lead_list", status, 30.0))
        obs = postgres_stop_observation(samples=samples)
        _, checks = rd.evaluate(drill("postgres-stop"), obs, rd.Timings())
        assert rd.verdict(checks) == "FAIL"
        assert checks_by_name(checks)["error statuses"].ok is False


def test_a_slow_503_fails_the_bounded_wait():
    samples = series(0, 200, status=lambda t: 503 if 60.5 <= t < 109 else 200)
    samples.append(rd.Sample(95.0, "dashboard", 503, 30_500.0))
    _, checks = rd.evaluate(
        drill("postgres-stop"), postgres_stop_observation(samples=samples), rd.Timings()
    )
    assert checks_by_name(checks)["bounded waits"].ok is False


def test_readiness_that_never_failed_fails_the_postgres_drill():
    obs = postgres_stop_observation(probes=readiness(0, 200, None))
    _, checks = rd.evaluate(drill("postgres-stop"), obs, rd.Timings())
    assert checks_by_name(checks)["readiness"].ok is False


def test_what_could_not_be_measured_is_inconclusive_never_a_pass():
    for change in ({"probes": None, "metrics": None}, {"integrity": {"checked": False}}):
        _, checks = rd.evaluate(
            drill("postgres-stop"), postgres_stop_observation(**change), rd.Timings()
        )
        assert rd.verdict(checks) == "INCONCLUSIVE", change


def test_dead_events_and_restarts_fail_the_drill():
    obs = postgres_stop_observation(
        metrics=metrics([(float(t), 5.0) for t in range(0, 200, 5)], dead=2.0)
    )
    _, checks = rd.evaluate(drill("postgres-stop"), obs, rd.Timings())
    assert checks_by_name(checks)["backlog drained"].ok is False
    restarted = postgres_stop_observation()
    assert restarted.watch_after is not None
    restarted.watch_after["backend"] = {
        "status": "running",
        "restarts": 1,
        "started_at": "b",
        "oom_killed": False,
    }
    _, checks = rd.evaluate(drill("postgres-stop"), restarted, rd.Timings())
    assert checks_by_name(checks)["no restarts"].ok is False


def test_a_latency_blowout_fails_a_no_impact_drill():
    samples = series(0, 300, ms=lambda t: 5000.0 if 60 <= t < 90 else 40.0)
    obs = rd.Observation(
        0.0, 60.0, 90.0, 240.0, 240.0, {"inject": [], "restore": []}, samples=samples
    )
    _, checks = rd.evaluate(drill("redis-cache-stop"), obs, rd.Timings())
    assert checks_by_name(checks)["latency during the fault"].ok is False


def test_new_integrity_problems_are_attributed_to_the_drill_that_added_them():
    first = {
        "duplicate_logical_creates": 0,
        "lost_updates": 1,
        "deadlocks": 3,
        "dashboard_math": {},
    }
    assert rd.integrity_problems(first, {"deadlocks": 3}) == ["lost_updates 0 -> 1"]
    second = {**first, "impossible_states": {"negative_value": 0}}
    assert rd.integrity_problems(second, first) == []
    third = {**second, "deadlocks": 4, "dashboard_math": {"mismatch_count": 2}}
    assert rd.integrity_problems(third, second) == [
        "deadlocks 3 -> 4",
        "dashboard figures differ from SQL for 2 users",
    ]


def test_the_edge_refusals_must_carry_the_json_envelope():
    good = {"requests": 1, "statuses": {"413": 1}, "codes": {"413:file_too_large": 1}}
    bare = {"requests": 1, "statuses": {"413": 1}, "codes": {}}
    edge = {"json_too_large": good, "upload_too_large": bare}
    found = checks_by_name(rd.edge_checks(edge))
    assert found["JSON body over 1 MB refused"].ok is True
    assert found["upload over 11 MB refused"].ok is False
    assert found["uploads in flight limited"].ok is None


def test_a_file_served_before_its_scan_fails_the_clamav_drill():
    record = {
        "at": 70.0,
        "status": 201,
        "id": "a",
        "downloads": [[70.5, 200, "clean"]],
        "clean_at": 70.5,
        "blocked": False,
    }
    obs = rd.Observation(0.0, 60.0, 120.0, 210.0, 400.0, {})
    found = checks_by_name(rd.upload_checks({"uploads": [record]}, obs))
    assert found["unscanned files not served"].ok is False
    record["downloads"] = [[70.5, 422, "scanning"], [200.0, 200, "clean"]]
    record["clean_at"] = 200.0
    found = checks_by_name(rd.upload_checks({"uploads": [record]}, obs))
    assert all(c.ok for c in found.values()), found


def test_a_stalled_load_generator_makes_the_drill_inconclusive():
    obs = postgres_stop_observation(stalls=[(80.0, 37.0)])
    _, checks = rd.evaluate(drill("postgres-stop"), obs, rd.Timings())
    assert rd.verdict(checks) == "INCONCLUSIVE"
    assert "37 s" in checks_by_name(checks)["load generator steady"].detail


# --- always restored -------------------------------------------------------------------------
def test_the_restore_is_owed_before_the_fault_is_applied():
    clock = FakeClock()
    runner, driver = runner_for(clock)
    result = asyncio.run(runner.run(drill("redis-cache-stop")))
    assert driver.owed_at_inject == ["start redis-cache"]
    assert driver.calls == ["stop redis-cache", "start redis-cache"]
    assert runner.owed == []
    assert result["verdict"] == "INCONCLUSIVE"  # no readiness probe in this test: never a PASS
    assert clock.sleeps[:2] == [60.0, 30.0]  # the fault at its fixed offset, held 30 s


def test_a_crash_during_the_fault_still_restores_it():
    clock = FailingClock(fail_at=2, error=RuntimeError("the load generator died"))
    runner, driver = runner_for(clock)
    with pytest.raises(RuntimeError, match="died"):
        asyncio.run(runner.run(drill("postgres-stall")))
    assert driver.calls == ["pause postgres", "unpause postgres"]
    assert runner.owed == []


def test_ctrl_c_during_the_fault_still_restores_it():
    clock = BlockingClock(block_at=2)
    runner, driver = runner_for(clock)

    async def interrupt() -> None:
        clock.reached = asyncio.Event()
        task = asyncio.create_task(runner.run(drill("postgres-stop")))
        await clock.reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupt())
    assert driver.calls == ["stop postgres", "start postgres"]
    assert runner.owed == []


def test_a_fault_applied_halfway_is_restored_and_the_drill_fails():
    clock = FakeClock()
    runner, driver = runner_for(clock, fail={"stop postgres"})
    result = asyncio.run(runner.run(drill("postgres-stop")))
    assert driver.calls == ["stop postgres", "start postgres"]
    assert result["verdict"] == "FAIL"
    applied = next(c for c in result["checks"] if c["name"] == "fault applied")
    assert applied["ok"] is False
    assert "timed out" in applied["detail"]


def test_a_failed_restore_stays_owed_blocks_the_next_drill_and_is_retried_at_exit():
    clock = FakeClock()
    runner, driver = runner_for(clock, fail={"start redis"})
    result = asyncio.run(runner.run(drill("redis-broker-stop")))
    assert result["verdict"] == "FAIL"
    assert [a.describe() for a in runner.owed] == ["start redis"]
    with pytest.raises(rd.GateError, match="still owed"):
        asyncio.run(runner.gate(drill("redis-pause")))
    driver.fail.clear()
    runner.restore_owed_now()
    assert runner.owed == []
    assert driver.calls[-1] == "start redis"


def test_an_unconfirmed_manual_step_is_inconclusive_not_a_pass_or_a_fail():
    manual = rd.ManualDriver(lambda text, timeout: None, timeout_s=1)
    step = manual.apply(rd.Action("stop", "postgres"))
    assert step["confirmed"] is False
    assert "REFUSE connections" in step["command"]
    found = checks_by_name(rd.fault_checks({"inject": [step], "restore": []}))
    assert found["fault applied"].ok is None
    confirmed = rd.ManualDriver(lambda text, timeout: "", timeout_s=1).apply(rd.Action("stop", "x"))
    assert confirmed["confirmed"] is True


def test_managed_services_go_to_the_operator_and_the_rest_to_docker():
    docker = rd.DockerDriver("arkray-prod-")
    manual = rd.ManualDriver(lambda text, timeout: "", timeout_s=1)
    routed = rd.RoutedDriver(docker, manual, {"postgres", "redis"})
    assert routed.is_manual("postgres")
    assert not routed.is_manual("backend")
    assert routed.describe(rd.Action("pause", "postgres")).startswith("operator: STALL")
    assert routed.describe(rd.Action("stop", "beat")) == "docker stop --time 10 arkray-prod-beat-1"
    assert rd.RoutedDriver(None, manual, set()).is_manual("backend")


def test_operator_answers_arrive_or_time_out():
    assert rd.Prompter(io.StringIO("\n")).ask("go", 1.0) == ""
    assert rd.Prompter(io.StringIO("")).ask("go", 0.05) is None


# --- docker ------------------------------------------------------------------------------------
def fake_docker(
    stdout: str = "", code: int = 0, stderr: str = ""
) -> tuple[rd.DockerDriver, list[Any]]:
    calls: list[Any] = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((command, timeout))
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)

    return rd.DockerDriver("arkray-prod-", {"postgres": "arkray-postgres-1"}, runner=runner), calls


def test_every_docker_call_is_bounded_and_named_by_the_prefix():
    driver, calls = fake_docker(stdout="/arkray-postgres-1|exited|0|t|false|0\n")
    driver.apply(rd.Action("stop", "postgres", grace_s=30))
    driver.apply(rd.Action("pause", "redis-cache"))
    driver.apply(rd.Action("kill", "worker-index"))
    driver.inspect(["backend"])
    commands = [c for c, _ in calls]
    assert commands[0] == ["docker", "stop", "--time", "30", "arkray-postgres-1"]
    assert commands[1][:3] == ["docker", "inspect", "--format"]  # the stop's exit code
    assert ["docker", "pause", "arkray-prod-redis-cache-1"] in commands
    assert ["docker", "kill", "--signal", "KILL", "arkray-prod-worker-index-1"] in commands
    assert all(0 < timeout <= 90 for _, timeout in calls)
    assert calls[0][1] == 90  # the 30 s grace plus the CLI's own bound


def test_a_hung_docker_cli_becomes_a_driver_error(monkeypatch):
    def hang(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=kwargs["timeout"])

    monkeypatch.setattr(rd.subprocess, "run", hang)
    with pytest.raises(rd.DriverError, match="timed out after 5 s"):
        rd.run_bounded(["docker", "stop", "x"], 5)


def test_restoring_a_container_that_is_not_paused_is_not_a_failure():
    driver, _ = fake_docker(code=1, stderr="Error response: Container abc is not paused")
    assert driver.apply(rd.Action("unpause", "postgres"))["note"] == "was not paused"
    failing, _ = fake_docker(code=1, stderr="No such container")
    with pytest.raises(rd.DriverError, match="No such container"):
        failing.apply(rd.Action("start", "postgres"))


def test_container_state_is_read_from_docker_inspect():
    driver, _ = fake_docker(
        stdout="/arkray-prod-backend-1|running|2|2026-10-12T10:00:00Z|false|0\n"
        "/arkray-prod-nginx-1|running|0|2026-10-12T09:00:00Z|true|137\n"
    )
    found = driver.inspect(["backend", "nginx", "beat"])
    assert found["backend"] == {
        "container": "arkray-prod-backend-1",
        "status": "running",
        "restarts": 2,
        "started_at": "2026-10-12T10:00:00Z",
        "oom_killed": False,
        "exit_code": 0,
    }
    assert found["nginx"] is not None
    assert found["nginx"]["oom_killed"] is True
    assert found["beat"] is None  # not in this stack: skipped, not a failure


def test_a_killed_child_process_is_reported():
    driver, calls = fake_docker(stdout="4321 8\n")
    step = driver.apply(rd.Action("kill-child", "backend"))
    assert (step["killed_pid"], step["children_before"]) == (4321, 8)
    assert calls[0][0][:5] == ["docker", "exec", "arkray-prod-backend-1", "python", "-c"]
    assert "SIGKILL" in calls[0][0][5]


# --- the catalogue -----------------------------------------------------------------------------
def test_every_fault_is_undone_by_its_restore():
    undo = {"stop": "start", "kill": "start", "pause": "unpause"}
    names = [d.name for d in rd.CATALOGUE]
    assert len(names) == len(set(names))
    for d in rd.CATALOGUE:
        assert d.expectation
        assert d.pass_criteria()
        for action in d.inject:
            assert action.verb in rd.VERBS
            if action.verb in undo:
                assert rd.Action(undo[action.verb], action.service) in d.restore, d.name
        if d.kind != "fault":
            assert not d.inject, d.name


def test_drills_are_selected_by_name_and_retargeted_one_at_a_time():
    assert rd.select_drills("all") == list(rd.CATALOGUE)
    chosen = rd.select_drills("postgres-stall,redis-pause")
    assert [d.name for d in chosen] == ["postgres-stall", "redis-pause"]
    with pytest.raises(SystemExit):
        rd.select_drills("postgres-reboot")
    with pytest.raises(SystemExit):
        rd.select_drills("redis-pause,worker-kill", target="redis")
    (paused,) = rd.select_drills("redis-pause", target="redis")
    assert paused.inject == (rd.Action("pause", "redis"),)
    assert paused.restore == (rd.Action("unpause", "redis"),)


def test_the_plan_names_the_commands_and_a_duration():
    text = rd.plan_text(
        rd.select_drills("postgres-stop"), rd.DockerDriver("arkray-prod-"), rd.Timings()
    )
    assert "docker stop --time 30 arkray-prod-postgres-1" in text
    assert "docker start arkray-prod-postgres-1" in text
    assert "total: about" in text


# --- environment labelling ---------------------------------------------------------------------
def test_a_run_needs_an_environment_name():
    parser = rd.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--environment", "Staging (EU)"])
    assert parser.parse_args(["run", "--environment", "staging-eu"]).environment == "staging-eu"


def report_for(environment: str) -> dict[str, Any]:
    clock = FakeClock()
    runner, _ = runner_for(clock)
    runner.environment = environment
    result = asyncio.run(runner.run(drill("beat-restart")))
    return {
        "environment": environment,
        "started_at": "2026-10-12T20:00:00+00:00",
        "finished_at": "2026-10-12T20:05:00+00:00",
        "driver": "docker",
        "load": "50 virtual users",
        "drills": [result],
        "notes": [],
    }


def test_every_result_carries_its_environment():
    staging = rd.render_markdown(report_for("staging"))
    assert staging.startswith("# Resilience drills: STAGING")
    row = next(line for line in staging.splitlines() if line.startswith("| beat-restart"))
    assert row.endswith("(staging) |")
    assert rd.LOCAL_CAVEAT not in staging
    local = rd.render_markdown(report_for("local"))
    assert rd.LOCAL_CAVEAT in local
    assert "## beat-restart: INCONCLUSIVE (local)" in local
    assert json.loads(json.dumps(report_for("local"), default=str))["drills"][0]["environment"] == (
        "local"
    )


def test_default_report_names_carry_the_environment():
    options = rd.build_parser().parse_args(["run", "--environment", "staging"])
    report, markdown = rd.output_paths(options, 1_760_000_000.0)
    assert report.name.startswith("resilience-staging-")
    assert markdown.suffix == ".md"
    assert markdown.stem == report.stem


def test_secrets_stay_out_of_the_report():
    options = rd.build_parser().parse_args(
        [
            "run",
            "--environment",
            "local",
            "--pg",
            "postgres://arkray:hunter2-not-real@127.0.0.1:55432/arkray_load",
            "--metrics-token",
            "token-not-real",
        ]
    )
    shown = json.dumps(rd.public_options(options))
    assert "hunter2" not in shown
    assert "token-not-real" not in shown


# --- the hand-written requests -------------------------------------------------------------------
def test_a_declared_body_is_refused_before_it_is_sent():
    """raw_request against a local server that answers as nginx does to a body over its limit:
    at once, after the headers, without reading the body."""
    seen: list[bytes] = []

    async def server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        body = b'{"error":{"code":"file_too_large","message":"The request is too large."}}'
        writer.write(b"HTTP/1.1 413 Too Large\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    async def scenario() -> tuple[int, str]:
        listener = await asyncio.start_server(server, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener:
            return await rd.raw_request(
                f"http://127.0.0.1:{port}",
                "POST",
                "/api/v1/workspaces/me/activities",
                {"Content-Type": "application/json"},
                declared=2 * rd.MIB,
                timeout_s=5,
                host_header="crm-staging.example.com",  # what the edge drill passes on staging
            )

    assert asyncio.run(scenario()) == (413, "file_too_large")
    assert b"Content-Length: 2097152" in seen[0]
    assert b"\r\nHost: crm-staging.example.com\r\n" in seen[0]


def test_no_answer_in_time_is_status_zero():
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # never answers; ends when the client gives up and closes
        writer.close()

    async def scenario() -> tuple[int, str]:
        listener = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener:
            return await rd.raw_request(
                f"http://127.0.0.1:{port}", "POST", "/", {}, body=b"x" * 100, timeout_s=0.3
            )

    assert asyncio.run(scenario()) == (0, "")
