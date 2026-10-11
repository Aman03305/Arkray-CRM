"""GatedTask while the erasure ledger gate is closed (final audit ARCH-1): fail closed, but
never silently. A periodic tick is skipped (the next one runs), other work goes back to its
queue with a growing delay, a bounded number of times, then is dropped; every refusal is an
ERROR line naming the task and the gate's status; `ledger_gate_exempt` tasks run anyway."""

from __future__ import annotations

import logging

import pytest
from celery.exceptions import Retry

import config.celery as celery_module
from arkray.core import ledger, ledger_gate
from config.celery import GatedTask, app, reset_gate_log
from tests.factories import LeadFactory

KEY = "ledger-test-key-" + "x" * 32
ran: list[str] = []


@app.task(name="tests.gate.work")
def work(value: str) -> str:
    ran.append(value)
    return value


@app.task(name="tests.gate.repair", ledger_gate_exempt=True)
def repair() -> str:
    ran.append("repair")
    return "repaired"


@pytest.fixture(autouse=True)
def _clean():
    ran.clear()
    reset_gate_log()
    yield
    reset_gate_log()


@pytest.fixture
def gate(monkeypatch):
    """The gate's status as this process sees it, set by the test."""
    state = {"status": "ok", "checks": 0}

    def status(now: float | None = None) -> str:
        state["checks"] += 1
        return str(state["status"])

    monkeypatch.setattr(ledger_gate.GATE, "status", status)
    return state


def refusals(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "arkray.core.ledger_gate"]


def test_every_task_is_a_gated_task():
    assert isinstance(work, GatedTask)
    assert work.ledger_gate_exempt is False
    assert repair.ledger_gate_exempt is True


def test_an_open_gate_runs_the_task(gate, caplog):
    for status in sorted(ledger_gate.OPEN):
        gate["status"] = status
        assert work.apply(args=(status,)).get() == status
    assert ran == sorted(ledger_gate.OPEN)
    assert refusals(caplog) == []


def test_a_closed_gate_sends_work_back_a_bounded_number_of_times_then_drops_it(gate, caplog):
    gate["status"] = "ahead"
    with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
        # Eager: each retry runs at once (no broker), so the whole budget is spent here.
        assert work.apply(args=("x",)).get() is None
    assert ran == []  # never ran while closed
    # Each attempt asks the gate (is_open) and reads its status for the log line.
    assert gate["checks"] == 2 * (work.ledger_gate_max_retries + 1)
    lines = refusals(caplog)
    assert [r.getMessage() for r in lines] == [
        "background_task_deferred",  # once a minute per task: the rest counted, not logged
        "background_task_dropped",
    ]
    assert all(r.levelno == logging.ERROR for r in lines)
    deferred, dropped = lines
    assert (deferred.task_name, deferred.status, deferred.attempt, deferred.retry_in_s) == (
        "tests.gate.work",
        "ahead",
        1,
        60,
    )
    assert (dropped.task_name, dropped.status, dropped.attempt) == (
        "tests.gate.work",
        "ahead",
        work.ledger_gate_max_retries,
    )


@pytest.mark.parametrize(
    ("retries", "delay"), [(0, 60), (1, 120), (2, 240), (3, 300), (11, 300)], ids=str
)
def test_a_worker_requeues_with_a_growing_bounded_delay(gate, retries, delay):
    """As a worker runs it: celery's own retry, a copy queued with the countdown, the same
    queue and the message's own expiry (an Ask Arkray question's 90 s still applies)."""
    gate["status"] = "behind"
    work.push_request(
        id="task-1",
        args=("x",),
        kwargs={},
        retries=retries,
        called_directly=False,
        is_eager=False,
        expires="2026-10-11T12:00:00+00:00",
        delivery_info={"exchange": "", "routing_key": "default"},
    )
    try:
        with pytest.raises(Retry) as raised:
            work("x")
    finally:
        work.pop_request()
    assert raised.value.when == delay
    assert raised.value.sig.options["queue"] == "default"
    assert raised.value.sig.options["expires"] == "2026-10-11T12:00:00+00:00"
    assert raised.value.sig.options["retries"] == retries + 1
    assert ran == []


def test_out_of_retries_in_a_worker_the_task_is_dropped_with_an_error(gate, caplog):
    gate["status"] = "unavailable"
    work.push_request(
        id="task-2",
        retries=work.ledger_gate_max_retries,
        called_directly=False,
        is_eager=False,
        delivery_info={"exchange": "", "routing_key": "default"},
    )
    try:
        with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
            assert work("x") is None
    finally:
        work.pop_request()
    assert [r.getMessage() for r in refusals(caplog)] == ["background_task_dropped"]
    assert ran == []


def test_a_periodic_tick_is_skipped_not_retried(gate, caplog):
    from arkray.core.tasks import relay_outbox
    from arkray.privacy.tasks import housekeeping

    gate["status"] = "tampered"
    with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
        assert relay_outbox.apply().get() is None
        assert housekeeping.apply().get() is None
    assert gate["checks"] == 4  # one attempt each (is_open + the log's status): no retries
    lines = refusals(caplog)
    assert [(r.getMessage(), r.task_name, r.status) for r in lines] == [
        ("background_task_skipped", "core.outbox.relay", "tampered"),
        ("background_task_skipped", "privacy.housekeeping", "tampered"),
    ]


def test_the_decision_is_ledger_gate_is_open(gate, monkeypatch):
    """Open or closed is `ledger_gate.is_open()`; the status is read for the log only."""
    monkeypatch.setattr(ledger_gate, "is_open", lambda: False)
    assert work.apply(args=("x",)).get() is None
    assert ran == []


def test_a_task_called_as_a_function_is_refused_too(gate, caplog):
    gate["status"] = "diverged"
    with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
        assert work("x") is None
    assert ran == []
    assert [r.getMessage() for r in refusals(caplog)] == ["background_task_dropped"]


def test_an_exempt_task_runs_while_the_gate_is_closed(gate, caplog):
    gate["status"] = "ahead"
    with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
        assert repair.apply().get() == "repaired"
    assert ran == ["repair"]
    assert gate["checks"] == 0
    assert refusals(caplog) == []


def test_the_ledger_reconciliation_runs_while_the_gate_is_closed(gate, monkeypatch):
    """privacy.reconcile_ledger is what reopens the gate: a beat entry, and exempt."""
    from arkray.privacy import reconcile

    task = app.tasks["privacy.reconcile_ledger"]
    assert isinstance(task, GatedTask)
    assert task.ledger_gate_exempt is True
    assert any(e["task"] == task.name for e in app.conf.beat_schedule.values())
    monkeypatch.setattr(reconcile, "reconcile", lambda: "reconciled")
    gate["status"] = "behind"
    assert task.apply().get() == "reconciled"
    assert gate["checks"] == 0


def test_refusals_are_rate_limited_per_task_and_count_what_was_left_out(gate, caplog, monkeypatch):
    from arkray.core.tasks import relay_outbox

    gate["status"] = "behind"
    with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
        for _ in range(3):
            relay_outbox.apply()
        assert len(refusals(caplog)) == 1
        monkeypatch.setattr(celery_module, "GATE_LOG_INTERVAL_S", 0.0)
        relay_outbox.apply()
    first, second = refusals(caplog)
    assert (first.count, second.count) == (0, 2)


@pytest.mark.django_db
def test_a_worker_whose_ledger_is_missing_entries_refuses_instead_of_running(
    settings, tmp_path, admin, user_a, caplog
):
    """The audited case end to end: the shared ledger has an entry, this worker's view of
    it (the image's empty directory, no volume) doesn't: "ahead", an ERROR, nothing run."""
    from arkray.privacy import services

    shared, empty = tmp_path / "volume", tmp_path / "image-dir"
    shared.mkdir()
    empty.mkdir()
    settings.ERASURE_LEDGER_URL = shared.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    ledger_gate.GATE.reset()
    try:
        services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
        assert ledger.applied()[0] == 1
        assert work.apply(args=("with the volume",)).get() == "with the volume"

        settings.ERASURE_LEDGER_URL = empty.as_uri()
        ledger_gate.GATE.reset()
        with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
            assert work.apply(args=("without it",)).get() is None
        assert ran == ["with the volume"]
        statuses = {r.status for r in refusals(caplog) if r.getMessage().startswith("background")}
        assert statuses == {"ahead"}
    finally:
        ledger_gate.GATE.reset()
