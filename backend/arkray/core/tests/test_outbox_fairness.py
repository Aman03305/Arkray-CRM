"""Fair claiming in the outbox (review of 73ff1e3, M6; docs/reliability.md#fairness-between-users).

Every event carries a fairness key: the user whose request queued it ('' for system work),
inherited by the follow-up work its handler queues. The relay interleaves keys instead of
claiming first-come, first-served, so one account's backlog (50 queued re-indexings, a burst
of 10,000-character notes) can't hold everyone else's work behind it. Within one key the
order stays first-in, first-out.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from arkray.core import outbox
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.models import OutboxEvent, OutboxStatus

pytestmark = pytest.mark.django_db

USER_A = str(uuid4())
USER_B = str(uuid4())
USER_C = str(uuid4())
FOLLOW_UPS: list[OutboxEvent | None] = []


@pytest.fixture(autouse=True)
def handlers(monkeypatch):
    monkeypatch.setattr(outbox, "_HANDLERS", {})
    FOLLOW_UPS.clear()

    @outbox.handler("test.work", queue="default")
    def work(payload):
        pass

    @outbox.handler("test.chain", queue="default")
    def chain(payload):
        FOLLOW_UPS.append(outbox.enqueue("test.work", {"after": payload.get("n")}))


@contextmanager
def acting_as(user_id: str | None) -> Iterator[None]:
    """As a signed-in request runs (the middleware binds the context, authentication adds the
    user); None: a system job (beat, a management command)."""
    token = bind_context(ExecutionContext(correlation_id="req-1", user_id=user_id))
    try:
        yield
    finally:
        reset_context(token)


def queue(user_id: str | None, count: int, topic: str = "test.work") -> list[int]:
    queued: list[int] = []
    with acting_as(user_id):
        for n in range(count):
            event = outbox.enqueue(topic, {"n": n})
            assert event is not None
            queued.append(event.pk)
    return queued


def relayed() -> list[int]:
    """One relay tick; the ids it handed to the broker, in dispatch order."""
    sent: list[int] = []
    outbox.relay(dispatch=lambda event: sent.append(event.pk))
    return sent


def finish(event_ids: list[int]) -> None:
    for event in OutboxEvent.objects.filter(pk__in=event_ids):
        outbox.process_event(event.pk, str(event.claim_token))


class TestOneUserCannotStarveAnother:
    def test_a_job_queued_behind_another_users_50_is_claimed_at_once(self):
        """The reviewer's case: user A queues 50 jobs, then user B queues 1. First-come,
        first-served claimed B's after all 50 of A's (one indexing worker: minutes)."""
        a_jobs = queue(USER_A, 50)
        (b_job,) = queue(USER_B, 1)
        sent = relayed()
        assert sorted(sent) == sorted([*a_jobs, b_job])  # all of them, room permitting
        assert sent.index(b_job) <= 1  # interleaved: at most one of A's goes first
        assert [e for e in sent if e != b_job] == a_jobs  # A's own order is kept

    @override_settings(OUTBOX_MAX_IN_FLIGHT={"default": 5, "email": 5, "ai_index": 5})
    def test_with_the_queue_full_of_one_users_work_the_next_free_slot_goes_to_another(self):
        a_jobs = queue(USER_A, 50)
        first = relayed()
        assert first == a_jobs[:5]  # alone: A may use all the capacity there is
        (b_job,) = queue(USER_B, 1)
        assert relayed() == []  # no free slot
        finish(first[:1])  # one of A's is done: one slot
        assert relayed() == [b_job]  # B's, not A's sixth: A already holds the other four

    @override_settings(OUTBOX_MAX_IN_FLIGHT={"default": 3, "email": 3, "ai_index": 3})
    def test_users_with_backlogs_take_turns(self):
        a_jobs = queue(USER_A, 10)
        b_jobs = queue(USER_B, 10)
        queue(USER_C, 10)
        order: list[int] = []
        for _ in range(4):
            sent = relayed()
            order += sent
            finish(sent)
        owners = ["a" if e in a_jobs else "b" if e in b_jobs else "c" for e in order]
        assert owners == ["a", "b", "c"] * 4  # round robin, oldest first among equals
        assert [e for e in order if e in a_jobs] == a_jobs[:4]

    def test_a_new_users_job_waits_for_at_most_one_claim_per_busy_user(self):
        busy = [str(uuid4()) for _ in range(6)]
        for user in busy:
            queue(user, 30)
        (late,) = queue(USER_B, 1)
        sent = relayed()
        assert sent.index(late) <= len(busy)

    def test_system_work_is_one_more_participant(self):
        a_jobs = queue(USER_A, 20)
        (system,) = queue(None, 1)
        sent = relayed()
        assert sent.index(system) <= 1
        assert OutboxEvent.objects.get(pk=system).fairness_key == ""
        assert {e.fairness_key for e in OutboxEvent.objects.filter(pk__in=a_jobs)} == {USER_A}


class TestTheKeyFollowsTheWork:
    def test_follow_up_work_queued_by_a_handler_keeps_its_users_key(self):
        """A continuation (the next batch of a re-indexing, the next reconciliation batch)
        re-enters the queue as the user whose request started it, not as system work that
        would get a turn of its own."""
        (started,) = queue(USER_A, 1, topic="test.chain")
        sent = relayed()
        finish(sent)
        (follow_up,) = FOLLOW_UPS
        assert follow_up is not None
        assert follow_up.fairness_key == USER_A
        assert OutboxEvent.objects.get(pk=started).status == OutboxStatus.DONE

    def test_a_system_jobs_follow_up_stays_system_work(self):
        queue(None, 1, topic="test.chain")
        finish(relayed())
        (follow_up,) = FOLLOW_UPS
        assert follow_up is not None
        assert follow_up.fairness_key == ""

    def test_an_explicit_key_wins_over_the_context(self):
        with acting_as(USER_A):
            event = outbox.enqueue("test.work", {}, fairness_key=USER_B)
        assert event is not None
        assert event.fairness_key == USER_B

    def test_coalescing_ignores_the_key(self):
        """Five quick edits of one record by two people are still one re-index."""
        with acting_as(USER_A):
            first = outbox.enqueue("test.work", {"id": 1}, dedupe_key="lead:1")
        with acting_as(USER_B):
            assert outbox.enqueue("test.work", {"id": 1}, dedupe_key="lead:1") is None
        assert first is not None
        assert OutboxEvent.objects.get().fairness_key == USER_A


class TestEveryGuaranteeStillHolds:
    def test_a_retried_event_keeps_its_key_and_its_place_in_its_users_order(self):
        a_jobs = queue(USER_A, 3)
        sent = relayed()
        OutboxEvent.objects.filter(pk=sent[0]).update(
            status=OutboxStatus.PENDING, claim_token=None, locked_until=None
        )
        assert relayed() == [sent[0]]
        assert OutboxEvent.objects.get(pk=a_jobs[0]).fairness_key == USER_A

    def test_events_not_yet_due_are_never_claimed_whatever_their_key(self):
        with acting_as(USER_B):
            outbox.enqueue("test.work", {}, delay_seconds=60)
        a_jobs = queue(USER_A, 2)
        assert relayed() == a_jobs

    def test_a_dead_event_is_never_claimed(self):
        (dead,) = queue(USER_B, 1)
        OutboxEvent.objects.filter(pk=dead).update(
            status=OutboxStatus.DEAD, finished_at=OutboxEvent.objects.get(pk=dead).created_at
        )
        a_jobs = queue(USER_A, 2)
        assert relayed() == a_jobs


class TestCost:
    def test_claiming_reads_the_pending_index_however_long_a_backlog_is(self):
        """Keys by a loose index scan, each key's oldest due events in index order: never a
        scan of the table (done events included) or of one key's whole backlog.

        Sequential scans are switched off here because this test's table holds 31 rows, which
        the planner rightly reads whole; the question is whether the index can serve the query
        at all. At scale it chooses the index by itself: review round 1 measured this query
        at 13 ms on 220,000 pending events from 122 users (index-only scans of
        outbox_pending_fair_idx, no sequential scan)."""
        queue(USER_A, 30)
        queue(USER_B, 1)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(
                "EXPLAIN " + outbox._FAIR_CANDIDATES,
                {"queue": "default", "now": timezone.now(), "per_key": 100},
            )
            plan = "\n".join(row[0] for row in cursor.fetchall())
        assert "Seq Scan on core_outbox_event" not in plan, plan
        assert "outbox_pending_fair_idx" in plan, plan


class TestObservable:
    def test_the_metrics_count_users_and_the_busiest_ones_share_never_a_user(self):
        from arkray.core import metrics

        queue(USER_A, 6)
        queue(USER_B, 2)
        queue(None, 2)
        body = metrics.render(metrics.outbox())
        assert 'arkray_outbox_pending_keys{queue="default"} 3' in body
        assert 'arkray_outbox_top_key_share{queue="default"} 0.6' in body
        assert 'arkray_outbox_pending_keys{queue="email"} 0' in body
        assert 'arkray_outbox_top_key_share{queue="email"} 0' in body
        assert 'arkray_outbox_events{queue="default",status="pending"} 10' in body
        assert USER_A not in body
        assert USER_B not in body

    def test_the_operator_view_names_whose_backlog_fills_a_queue(self):
        queue(USER_A, 6)
        queue(None, 2)
        out = StringIO()
        call_command("outbox_requeue", "--status", stdout=out)
        lines = out.getvalue().splitlines()
        assert lines[0].split()[:5] == ["default", "8", "pending", "from", "2"]
        assert lines[1].split() == [USER_A, "6", "75.0%"]
        assert lines[2].split() == ["(system)", "2", "25.0%"]
        assert OutboxEvent.objects.filter(status=OutboxStatus.PENDING).count() == 8  # read only
