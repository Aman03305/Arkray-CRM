import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from arkray.core import idempotency
from arkray.core.models import IdempotencyRecord

pytestmark = pytest.mark.django_db

ACTOR, OTHER_ACTOR = uuid.uuid4(), uuid.uuid4()
KEY = uuid.uuid4()


def test_digest_is_stable_and_order_independent_for_mappings():
    assert idempotency.request_digest({"a": 1, "b": 2}) == idempotency.request_digest(
        {"b": 2, "a": 1}
    )
    assert idempotency.request_digest({"a": 1}) != idempotency.request_digest({"a": 2})


def test_nothing_to_replay_for_a_new_key():
    assert idempotency.replayed_resource(ACTOR, "op", KEY, "0" * 64) is None


def test_the_same_request_replays_the_remembered_resource():
    resource = uuid.uuid4()
    idempotency.remember(ACTOR, "op", KEY, "a" * 64, resource)
    assert idempotency.replayed_resource(ACTOR, "op", KEY, "a" * 64) == resource


def test_a_different_request_with_the_same_key_is_refused():
    idempotency.remember(ACTOR, "op", KEY, "a" * 64, uuid.uuid4())
    with pytest.raises(idempotency.IdempotencyKeyReused):
        idempotency.replayed_resource(ACTOR, "op", KEY, "b" * 64)


def test_keys_are_private_to_each_actor_and_operation():
    idempotency.remember(ACTOR, "op", KEY, "a" * 64, uuid.uuid4())
    assert idempotency.replayed_resource(OTHER_ACTOR, "op", KEY, "a" * 64) is None
    assert idempotency.replayed_resource(ACTOR, "other-op", KEY, "a" * 64) is None


def test_expired_records_are_not_replayed_and_are_purged_on_the_next_use():
    idempotency.remember(ACTOR, "op", KEY, "a" * 64, uuid.uuid4())
    IdempotencyRecord.objects.update(created_at=timezone.now() - timedelta(hours=25))
    assert idempotency.replayed_resource(ACTOR, "op", KEY, "b" * 64) is None
    fresh = uuid.uuid4()
    idempotency.remember(ACTOR, "op", KEY, "b" * 64, fresh)  # the key is usable again
    assert list(IdempotencyRecord.objects.values_list("resource_id", flat=True)) == [fresh]


def test_a_concurrent_duplicate_is_recognised_by_its_constraint():
    idempotency.remember(ACTOR, "op", KEY, "a" * 64, uuid.uuid4())
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        IdempotencyRecord.objects.create(
            actor_id=ACTOR, operation="op", key=KEY, request_hash="a" * 64, resource_id=uuid.uuid4()
        )
    assert idempotency.is_duplicate_key(caught.value)


def test_other_integrity_errors_are_not_mistaken_for_duplicates():
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        IdempotencyRecord.objects.create(
            actor_id=ACTOR,
            operation="op",
            key=KEY,
            request_hash="not-hex",
            resource_id=uuid.uuid4(),
        )
    assert not idempotency.is_duplicate_key(caught.value)
