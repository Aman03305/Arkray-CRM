"""`manage.py outbox_requeue`: dead events back to work once their cause is fixed (Phase 10:
the operations guide promised it; the dead-events alert's runbook needs it)."""

from __future__ import annotations

import logging
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from arkray.core import outbox
from arkray.core.models import OutboxEvent, OutboxStatus

pytestmark = pytest.mark.django_db

FIXED: dict[str, bool] = {"fixed": False}
RAN: list[dict[str, object]] = []


@pytest.fixture(autouse=True)
def handlers(monkeypatch):
    monkeypatch.setattr(outbox, "_HANDLERS", {})
    FIXED["fixed"] = False
    RAN.clear()

    @outbox.handler("test.flaky", queue="email", max_attempts=1)
    def flaky(payload):
        if not FIXED["fixed"]:
            raise RuntimeError("SMTP down")
        RAN.append(payload)

    @outbox.handler("test.other", queue="default", max_attempts=1)
    def other(payload):
        raise RuntimeError("still broken")


def run_due() -> None:
    outbox.relay(dispatch=lambda event: outbox.process_event(event.pk, str(event.claim_token)))


def died(topic: str, payload: dict[str, object], *, dedupe_key: str = "") -> OutboxEvent:
    event = outbox.enqueue(topic, payload, dedupe_key=dedupe_key)
    assert event is not None
    run_due()
    event.refresh_from_db()
    assert event.status == OutboxStatus.DEAD
    return event


def requeue(*args: str) -> str:
    out = StringIO()
    call_command("outbox_requeue", *args, stdout=out)
    return out.getvalue()


def test_a_dry_run_lists_the_dead_events_and_changes_nothing():
    event = died("test.flaky", {"id": 1})
    output = requeue("--topic", "test.flaky")
    assert str(event.pk) in output
    assert "SMTP down" in output
    assert "Dry run" in output
    event.refresh_from_db()
    assert event.status == OutboxStatus.DEAD


def test_requeued_events_run_again_once_the_cause_is_fixed(caplog):
    first, second = died("test.flaky", {"id": 1}), died("test.flaky", {"id": 2})
    untouched = died("test.other", {"id": 3})
    FIXED["fixed"] = True
    with caplog.at_level(logging.WARNING, logger="arkray.core.outbox"):
        output = requeue("--topic", "test.flaky", "--yes")
    assert "Re-queued 2" in output
    for event in (first, second):
        event.refresh_from_db()
        assert (event.status, event.attempts, event.finished_at) == (OutboxStatus.PENDING, 0, None)
        assert event.available_at <= timezone.now()
    run_due()
    assert sorted(p["id"] for p in RAN) == [1, 2]
    assert OutboxEvent.objects.get(pk=first.pk).status == OutboxStatus.DONE
    assert OutboxEvent.objects.get(pk=untouched.pk).status == OutboxStatus.DEAD
    assert "outbox_events_requeued" in [r.getMessage() for r in caplog.records]


def test_filters_combine_and_select_exactly():
    by_id = died("test.flaky", {"id": 1})
    other_id = died("test.flaky", {"id": 2})
    requeue("--id", str(by_id.pk), "--yes")
    assert OutboxEvent.objects.get(pk=by_id.pk).status == OutboxStatus.PENDING
    assert OutboxEvent.objects.get(pk=other_id.pk).status == OutboxStatus.DEAD
    later = timezone.now().isoformat()
    newer = died("test.other", {"id": 3})
    requeue("--queue", "default", "--since", later, "--yes")
    assert OutboxEvent.objects.get(pk=newer.pk).status == OutboxStatus.PENDING
    assert OutboxEvent.objects.get(pk=other_id.pk).status == OutboxStatus.DEAD


def test_work_already_queued_again_and_blanked_payloads_are_skipped():
    blanked = died("test.flaky", {"id": 2})
    OutboxEvent.objects.filter(pk=blanked.pk).update(payload={})
    superseded = died("test.flaky", {"id": 1}, dedupe_key="lead:1")
    outbox.enqueue("test.flaky", {"id": 1}, dedupe_key="lead:1")  # the same work, pending
    output = requeue("--topic", "test.flaky", "--yes")
    assert "Re-queued 0" in output
    assert "1 already pending again" in output
    assert "1 whose payload is gone" in output
    for event in (superseded, blanked):
        assert OutboxEvent.objects.get(pk=event.pk).status == OutboxStatus.DEAD


def test_the_same_work_dead_twice_runs_once():
    older = died("test.flaky", {"id": 1}, dedupe_key="lead:1")
    newer = died("test.flaky", {"id": 1}, dedupe_key="lead:1")
    result = outbox.requeue_dead([older.pk, newer.pk])
    assert (result.requeued, result.superseded) == ([newer.pk], [older.pk])


def test_only_dead_events_are_ever_touched():
    pending = outbox.enqueue("test.flaky", {"id": 1})
    result = outbox.requeue_dead([pending.pk])
    assert result.requeued == []
    pending.refresh_from_db()
    assert (pending.status, pending.attempts) == (OutboxStatus.PENDING, 0)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "Name the events"),
        (("--since", "2026-10-01T00:00:00"), "time zone"),
        (("--since", "yesterday"), "time zone"),
        (("--topic", "x", "--limit", "0"), "at least 1"),
    ],
)
def test_requeuing_every_dead_event_needs_an_explicit_choice(args, message):
    with pytest.raises(CommandError, match=message):
        requeue(*args)


# --- Retention (Phase 10 review: finished events were never deleted) -----------------------
def finished(status: str, days_ago: float) -> OutboxEvent:
    when = timezone.now() - timedelta(days=days_ago)
    return OutboxEvent.objects.create(
        topic="test.flaky", queue="email", status=status, finished_at=when, payload={"id": 1}
    )


def test_housekeeping_purges_old_done_events_and_keeps_the_rest(settings):
    from arkray.core.tasks import housekeeping

    settings.OUTBOX_DONE_RETENTION_DAYS = 7
    old_done = [finished(OutboxStatus.DONE, 8) for _ in range(3)]
    recent_done = finished(OutboxStatus.DONE, 6)
    old_dead = finished(OutboxStatus.DEAD, 30)  # waits for an operator
    pending = outbox.enqueue("test.flaky", {"id": 2})
    assert housekeeping()["outbox_events"] == 3
    remaining = set(OutboxEvent.objects.values_list("pk", flat=True))
    assert not remaining & {event.pk for event in old_done}
    assert {recent_done.pk, old_dead.pk, pending.pk} <= remaining
    assert housekeeping()["outbox_events"] == 0  # safe to run twice


def test_a_purge_is_bounded_per_run(settings):
    settings.OUTBOX_PURGE_BATCH = 2
    settings.OUTBOX_PURGE_MAX_BATCHES = 2
    for _ in range(7):
        finished(OutboxStatus.DONE, 30)
    cutoff = timezone.now() - timedelta(days=7)
    assert outbox.purge_done(finished_before=cutoff) == 4
    assert outbox.purge_done(finished_before=cutoff) == 3


def test_a_stored_error_never_keeps_an_email_address():
    """Phase 11 review: an SMTP rejection quotes the recipient, and dead events are kept."""
    import smtplib

    exc = smtplib.SMTPRecipientsRefused({"priya.k@lab.example": (550, b"no such user")})
    described = outbox._describe(exc)
    assert "priya.k@lab.example" not in described
    assert "[email]" in described
    assert described.startswith("SMTPRecipientsRefused")


def test_the_slow_schedules_are_wall_clock_times():
    """Beat's schedule file lives in tmpfs: interval timers restarted with every release."""
    from celery.schedules import crontab
    from django.conf import settings

    for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
        if name != "outbox-relay":
            assert isinstance(entry["schedule"], crontab), name
