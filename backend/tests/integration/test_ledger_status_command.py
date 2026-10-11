"""`manage.py ledger_status`, the Celery containers' health check (final audit ARCH-1): exit 0
only while this process's ledger gate is open and, with --ping-worker, the named worker on
this host answers."""

from __future__ import annotations

import socket
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from arkray.core import ledger_gate
from config.celery import app


@pytest.fixture
def gate(monkeypatch):
    state = {"status": "ok"}
    monkeypatch.setattr(ledger_gate.GATE, "status", lambda now=None: state["status"])
    return state


def run(*args: str) -> str:
    out = StringIO()
    call_command("ledger_status", *args, stdout=out)
    return out.getvalue()


def test_an_open_gate_is_healthy(gate):
    for status in sorted(ledger_gate.OPEN):
        gate["status"] = status
        assert run() == f"erasure_ledger_gate={status}\n"


@pytest.mark.parametrize("status", ["behind", "ahead", "diverged", "tampered", "unavailable"])
def test_a_closed_gate_is_unhealthy(gate, status):
    gate["status"] = status
    with pytest.raises(CommandError, match=f"closed \\({status}\\)"):
        run()


def test_a_worker_that_answers_its_ping_is_healthy(gate, monkeypatch):
    node = f"ai@{socket.gethostname()}"
    asked = {}

    def ping(destination, timeout):
        asked.update(destination=destination, timeout=timeout)
        return [{node: {"ok": "pong"}}]

    monkeypatch.setattr(app.control, "ping", ping)
    assert run("--ping-worker", "ai").splitlines() == [
        "erasure_ledger_gate=ok",
        f"celery_worker={node} ok",
    ]
    assert asked == {"destination": [node], "timeout": 5.0}


def test_a_worker_that_doesnt_answer_is_unhealthy(gate, monkeypatch):
    monkeypatch.setattr(app.control, "ping", lambda destination, timeout: [])
    with pytest.raises(CommandError, match="didn't answer"):
        run("--ping-worker", "index")


def test_a_closed_gate_fails_before_the_ping(gate, monkeypatch):
    gate["status"] = "ahead"
    monkeypatch.setattr(app.control, "ping", lambda **_: pytest.fail("pinged"))
    with pytest.raises(CommandError):
        run("--ping-worker", "celery")


@pytest.mark.django_db
def test_the_real_gate_without_a_ledger_configured_is_open(settings):
    settings.ERASURE_LEDGER_URL = ""
    ledger_gate.GATE.reset()
    try:
        assert run() == "erasure_ledger_gate=disabled\n"
    finally:
        ledger_gate.GATE.reset()
