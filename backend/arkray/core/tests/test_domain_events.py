from dataclasses import dataclass

import pytest
from django.db import transaction

from arkray.core.domain_events import DomainEvent, publish, subscribe, subscribed
from arkray.core.models import IdempotencyRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class Pinged(DomainEvent):
    value: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PingedLoudly(Pinged):
    pass


def test_publishing_outside_a_transaction_is_a_programming_error():
    with pytest.raises(RuntimeError, match="inside the transaction"):
        publish(Pinged(value=1))


@pytest.mark.django_db
def test_subscribers_run_in_order_inside_the_transaction():
    calls = []
    with (
        subscribed(Pinged, lambda e: calls.append(("first", e.value))),
        subscribed(Pinged, lambda e: calls.append(("second", e.value))),
        transaction.atomic(),
    ):
        publish(Pinged(value=7))
    assert calls == [("first", 7), ("second", 7)]


@pytest.mark.django_db
def test_subscribers_match_the_exact_event_type_only():
    calls = []
    with subscribed(Pinged, calls.append), transaction.atomic():
        publish(PingedLoudly(value=1))
    assert calls == []


@pytest.mark.django_db(transaction=True)
def test_a_failing_subscriber_rolls_back_the_whole_change():
    def boom(_event):
        raise RuntimeError("subscriber failed")

    # A change and its event in one transaction: both statements belong inside.
    with subscribed(Pinged, boom), pytest.raises(RuntimeError), transaction.atomic():  # noqa: PT012
        IdempotencyRecord.objects.create(
            actor_id="5a1e4d2c-0000-4000-8000-00000000abcd",
            operation="test",
            key="5a1e4d2c-0000-4000-8000-00000000abce",
            request_hash="0" * 64,
            resource_id="5a1e4d2c-0000-4000-8000-00000000abcf",
        )
        publish(Pinged(value=1))
    assert not IdempotencyRecord.objects.exists()


@pytest.mark.django_db
def test_subscribed_removes_the_subscriber_afterwards():
    calls = []
    with subscribed(Pinged, calls.append):
        pass
    with transaction.atomic():
        publish(Pinged(value=1))
    assert calls == []


@pytest.mark.django_db
def test_subscribe_registers_a_permanent_subscriber():
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Registered(DomainEvent):
        pass

    calls = []

    @subscribe(Registered)
    def handler(event):
        calls.append(event)

    event = Registered()
    with transaction.atomic():
        publish(event)
    assert calls == [event]
