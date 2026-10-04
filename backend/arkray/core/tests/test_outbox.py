import threading
from datetime import timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from arkray.core import outbox
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.models import OutboxEvent, OutboxStatus

pytestmark = pytest.mark.django_db

CALLS: list[dict[str, object]] = []
NO_DISPATCH = lambda event: None  # noqa: E731


@pytest.fixture(autouse=True)
def handlers(monkeypatch):
    """A fresh, isolated handler registry per test."""
    monkeypatch.setattr(outbox, "_HANDLERS", {})
    CALLS.clear()

    @outbox.handler("test.ok", queue="default", max_attempts=3)
    def ok(payload):
        CALLS.append(payload)

    @outbox.handler("test.boom", queue="default", max_attempts=3)
    def boom(payload):
        raise RuntimeError("provider unavailable")

    @outbox.handler("test.permanent", queue="email", max_attempts=3)
    def permanent(payload):
        raise outbox.PermanentFailure("record no longer exists")


def dispatched():
    sent: list[OutboxEvent] = []
    return sent, sent.append


def claim(event):
    """Relay once and return the event's current claim token."""
    outbox.relay(dispatch=NO_DISPATCH)
    event.refresh_from_db()
    return event.claim_token


def expire_leases():
    OutboxEvent.objects.filter(status=OutboxStatus.IN_FLIGHT).update(
        locked_until=timezone.now() - timedelta(seconds=1)
    )


class TestEnqueue:
    def test_event_is_part_of_the_callers_transaction(self):
        def failing_business_write():
            with transaction.atomic():
                outbox.enqueue("test.ok", {"id": 1})
                raise RuntimeError("business write failed")

        with pytest.raises(RuntimeError):
            failing_business_write()
        assert OutboxEvent.objects.count() == 0

    def test_unknown_topic_is_rejected(self):
        with pytest.raises(ImproperlyConfigured):
            outbox.enqueue("test.unregistered")

    def test_oversized_payload_is_rejected(self):
        with pytest.raises(ValueError, match="identifiers, not content"):
            outbox.enqueue("test.ok", {"blob": "x" * (outbox.MAX_PAYLOAD_BYTES + 1)})

    def test_records_queue_attempt_budget_and_correlation_id(self):
        token = bind_context(ExecutionContext(correlation_id="req-12345678"))
        try:
            event = outbox.enqueue("test.permanent", {"id": 7})
        finally:
            reset_context(token)
        assert event is not None
        assert (event.queue, event.max_attempts, event.correlation_id) == (
            "email",
            3,
            "req-12345678",
        )


class TestCoalescing:
    def test_identical_pending_work_is_coalesced(self):
        assert outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1") is not None
        assert outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1") is None
        assert OutboxEvent.objects.count() == 1

    def test_new_work_is_written_once_the_previous_is_in_flight(self):
        outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1")
        outbox.relay(dispatch=NO_DISPATCH)
        assert outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1") is not None

    def test_never_coalesces_into_an_event_that_is_backing_off_after_a_failure(self):
        """New work must not wait behind (or die with) a failing attempt."""
        failing = outbox.enqueue("test.boom", {"id": 1}, dedupe_key="lead:1")
        outbox.process_event(failing.pk, claim(failing))  # fails -> pending with backoff
        assert outbox.enqueue("test.boom", {"id": 1}, dedupe_key="lead:1") is not None

    @pytest.mark.django_db(transaction=True)
    def test_coalesced_event_cannot_run_before_the_callers_transaction_commits(self):
        """Otherwise the handler could read pre-commit state and the change would be lost."""
        outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1")  # committed
        relayed: list[int] = []

        def relay_from_another_connection():
            try:
                relayed.append(outbox.relay(dispatch=NO_DISPATCH))
            finally:
                connection.close()

        with transaction.atomic():
            assert outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1") is None
            worker = threading.Thread(target=relay_from_another_connection)
            worker.start()
            worker.join()
        assert relayed == [0]  # locked by the caller's transaction -> skipped
        assert outbox.relay(dispatch=NO_DISPATCH) == 1  # claimable after commit


class TestRelay:
    def test_dispatches_due_events_under_a_claim_with_the_dispatch_lease(self):
        event = outbox.enqueue("test.ok", {"id": 1})
        sent, dispatch = dispatched()
        before = timezone.now()
        assert outbox.relay(dispatch=dispatch) == 1
        assert [e.pk for e in sent] == [event.pk]
        event.refresh_from_db()
        assert event.status == OutboxStatus.IN_FLIGHT
        assert event.claim_token is not None
        assert sent[0].claim_token == event.claim_token
        assert event.locked_until >= before + timedelta(
            seconds=outbox.settings.OUTBOX_DISPATCH_LEASE_SECONDS - 5
        )

    def test_skips_events_not_yet_due(self):
        outbox.enqueue("test.ok", {"id": 1}, delay_seconds=60)
        sent, dispatch = dispatched()
        assert outbox.relay(dispatch=dispatch) == 0
        assert sent == []

    @override_settings(OUTBOX_MAX_IN_FLIGHT={"default": 2, "email": 1, "ai": 1})
    def test_in_flight_cap_bounds_the_broker_queue(self):
        for i in range(5):
            outbox.enqueue("test.ok", {"id": i})
        _, dispatch = dispatched()
        assert outbox.relay(dispatch=dispatch) == 2
        assert outbox.relay(dispatch=dispatch) == 0  # still two in flight -> no capacity
        assert OutboxEvent.objects.filter(status=OutboxStatus.PENDING).count() == 3

    def test_each_queues_sustained_ceiling_stays_bounded_and_above_its_measured_need(self):
        """Phase 10: the relay refills a queue once per tick, so min(cap, batch) / tick is
        its ceiling however fast the workers are. `ai_index` at 20 capped indexing at 4
        events/s while its one worker idled most of each tick; a bulk reassignment's
        re-indexing would have taken hours."""
        tick = outbox.settings.CELERY_BEAT_SCHEDULE["outbox-relay"]["schedule"]
        batch = outbox.settings.OUTBOX_RELAY_BATCH_SIZE
        caps = outbox.settings.OUTBOX_MAX_IN_FLIGHT
        ceiling = {queue: min(cap, batch) / tick for queue, cap in caps.items()}
        assert ceiling["ai_index"] >= 20
        assert ceiling["default"] >= 20
        assert all(cap <= 200 for cap in caps.values())  # still a bounded broker

    def test_broker_failure_releases_undispatched_events(self):
        for i in range(3):
            outbox.enqueue("test.ok", {"id": i})

        def broken(event):
            raise ConnectionError("redis down")

        assert outbox.relay(dispatch=broken) == 0
        released = OutboxEvent.objects.filter(status=OutboxStatus.PENDING, claim_token=None)
        assert released.count() == 3

    def test_expired_leases_are_recovered_and_reclaimed_under_a_new_token(self):
        event = outbox.enqueue("test.ok", {"id": 1})
        first = claim(event)
        expire_leases()
        assert claim(event) not in (None, first)

    def test_recovery_with_a_pending_duplicate_does_not_jam_the_relay(self):
        """Regression: a unique index over pending rows made lease recovery fail forever."""
        crashed = outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1")
        claim(crashed)  # the worker then dies without recording an outcome
        newer = outbox.enqueue("test.ok", {"id": 1}, dedupe_key="lead:1")
        expire_leases()
        sent, dispatch = dispatched()
        assert outbox.relay(dispatch=dispatch) == 2
        assert {e.pk for e in sent} == {crashed.pk, newer.pk}


class TestProcess:
    def _in_flight(self, topic, payload=None):
        event = outbox.enqueue(topic, payload or {"id": 1})
        return event, claim(event)

    def test_success_marks_done(self):
        event, token = self._in_flight("test.ok", {"id": 42})
        outbox.process_event(event.pk, token)
        event.refresh_from_db()
        assert CALLS == [{"id": 42}]
        assert (event.status, event.attempts, event.claim_token) == (OutboxStatus.DONE, 1, None)
        assert event.finished_at is not None

    def test_start_switches_to_the_shorter_running_lease(self, monkeypatch):
        event, token = self._in_flight("test.ok")
        seen = {}

        def capture(payload):
            seen["lease"] = OutboxEvent.objects.get(pk=event.pk).locked_until

        spec = outbox.OutboxHandler("test.ok", capture, "default", 3)
        monkeypatch.setitem(outbox._HANDLERS, "test.ok", spec)
        before = timezone.now()
        outbox.process_event(event.pk, token)
        assert seen["lease"] <= before + timedelta(seconds=outbox.settings.OUTBOX_LEASE_SECONDS + 5)

    def test_stale_or_duplicate_messages_are_ignored(self):
        """A message from an earlier claim (requeue, redelivery) must not run or count."""
        event, stale_token = self._in_flight("test.ok")
        expire_leases()
        current_token = claim(event)
        outbox.process_event(event.pk, stale_token)
        event.refresh_from_db()
        assert (CALLS, event.attempts, event.status) == ([], 0, OutboxStatus.IN_FLIGHT)
        outbox.process_event(event.pk, current_token)
        outbox.process_event(event.pk, current_token)  # duplicate delivery after completion
        event.refresh_from_db()
        assert (len(CALLS), event.attempts, event.status) == (1, 1, OutboxStatus.DONE)

    def test_failure_schedules_retry_with_backoff(self):
        event, token = self._in_flight("test.boom")
        before = timezone.now()
        outbox.process_event(event.pk, token)
        event.refresh_from_db()
        assert (event.status, event.attempts, event.claim_token) == (OutboxStatus.PENDING, 1, None)
        assert "provider unavailable" in event.last_error
        assert event.available_at >= before + timedelta(seconds=5)  # base 10s, equal jitter

    def test_failure_with_a_pending_duplicate_still_schedules_a_retry(self):
        """Regression: returning to pending used to violate the pending-dedupe unique index."""
        event = outbox.enqueue("test.boom", {"id": 1}, dedupe_key="lead:1")
        token = claim(event)
        outbox.enqueue("test.boom", {"id": 1}, dedupe_key="lead:1")
        outbox.process_event(event.pk, token)
        event.refresh_from_db()
        assert event.status == OutboxStatus.PENDING

    def test_retries_are_bounded_then_dead(self):
        event = outbox.enqueue("test.boom", {"id": 1})
        for _ in range(event.max_attempts):
            OutboxEvent.objects.filter(pk=event.pk).update(available_at=timezone.now())
            outbox.process_event(event.pk, claim(event))
        event.refresh_from_db()
        assert (event.status, event.attempts) == (OutboxStatus.DEAD, 3)
        assert outbox.relay(dispatch=lambda e: pytest.fail("dead events are never retried")) == 0

    def test_permanent_failure_goes_dead_immediately(self):
        event, token = self._in_flight("test.permanent")
        outbox.process_event(event.pk, token)
        event.refresh_from_db()
        assert (event.status, event.attempts) == (OutboxStatus.DEAD, 1)

    def test_crash_looping_event_is_eventually_dead(self):
        """A worker killed mid-handler never records an outcome; attempts still count."""
        event, token = self._in_flight("test.ok")
        OutboxEvent.objects.filter(pk=event.pk).update(attempts=event.max_attempts)
        outbox.process_event(event.pk, token)
        event.refresh_from_db()
        assert event.status == OutboxStatus.DEAD
        assert CALLS == []

    def test_event_without_handler_goes_dead(self):
        event, token = self._in_flight("test.ok")
        outbox._HANDLERS.pop("test.ok")
        outbox.process_event(event.pk, token)
        event.refresh_from_db()
        assert event.status == OutboxStatus.DEAD

    def test_processing_an_unclaimed_event_is_a_no_op(self):
        event = outbox.enqueue("test.ok", {"id": 1})  # still pending
        outbox.process_event(event.pk, "00000000-0000-0000-0000-000000000000")
        event.refresh_from_db()
        assert (event.status, event.attempts) == (OutboxStatus.PENDING, 0)
        assert CALLS == []


class TestBackoff:
    def test_grows_exponentially_and_is_capped(self):
        top = lambda: 1.0  # noqa: E731
        assert outbox.backoff_seconds(1, rng=top) == 10
        assert outbox.backoff_seconds(2, rng=top) == 20
        assert outbox.backoff_seconds(3, rng=top) == 40
        assert outbox.backoff_seconds(30, rng=top) == 3600

    def test_jitter_never_goes_below_half(self):
        assert outbox.backoff_seconds(3, rng=lambda: 0.0) == 20


class TestCeleryWiring:
    def test_default_dispatch_publishes_to_the_broker(self):
        """relay() without an injected dispatcher uses Celery (in-memory broker in tests)."""
        outbox.enqueue("test.ok", {"id": 1})
        assert outbox.relay() == 1

    def test_process_task_runs_the_handler(self):
        from arkray.core.tasks import process_outbox_event

        event = outbox.enqueue("test.ok", {"id": 9})
        process_outbox_event(event.pk, str(claim(event)))
        event.refresh_from_db()
        assert event.status == OutboxStatus.DONE

    def test_relay_runs_on_its_own_queue(self):
        """So a backlog of real work can never stall claiming for every queue."""
        from config.celery import app

        route = app.amqp.router.route({}, "core.outbox.relay")
        assert route["queue"].name == "outbox"
