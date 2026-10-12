"""Removing a custom field from a personal pipeline, against a reassignment at the same moment
(deep security audit L-4; risk register R137; docs/pipeline.md#custom-fields).

Removing a field hides its values from everyone, so it is refused while a deal outside the
remover's workspace holds one (a deal whose lead was reassigned, still in the old owner's
pipeline). The check reads who owns the deals under the pipeline's FOR NO KEY UPDATE, and a
reassignment takes the personal pipelines holding the lead's deals FOR SHARE before it
commits (services.follow_lead_owner): one of them waits for the other, so a removal never
passes its check while a reassignment moving a deal out of the workspace commits under it.
Each race runs for real, in two threads and connections, one stopped at the decisive point;
without the reassignment's lock the other never waited and the field was removed while
another user's deal held a value (mutation-checked).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.db import OperationalError, connection, connections

from arkray.core.access import AccessScope
from arkray.core.errors import PermissionDeniedError
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import configuration, field_values, services
from arkray.pipeline.models import CustomField, Opportunity, Pipeline, Stage
from tests.factories import AdminFactory, LeadFactory, OpportunityFactory, UserFactory
from tests.helpers import run_concurrently

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]

OWN = AccessScope.own
ORG = AccessScope.organization
WAIT_S = 20.0  # the longest a stopped thread or a check waits: a test fails, never hangs
VALUE = "The other user will need this"


class Running:
    """`target` in its own thread and database connection: its result (or exception) once
    joined, and its server process, to see whether it waits for a lock."""

    def __init__(self, target: Callable[[], Any]) -> None:
        self.result: Any = None
        self.pid = 0
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()
        assert self._connected.wait(WAIT_S), "the thread never connected"

    def _run(self, target: Callable[[], Any]) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self._connected.set()
            self.result = target()
        except BaseException as exc:  # handed to the test
            self.result = exc
        finally:
            self._connected.set()
            connections.close_all()

    def join(self) -> Any:
        self._thread.join(WAIT_S * 2)
        assert not self._thread.is_alive(), "the thread is still running"
        return self.result

    def waits_for_a_lock(self) -> bool:
        """True once its session waits for a lock; False if it ends first (it never had to
        wait) or after WAIT_S."""
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline and self._thread.is_alive():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [self.pid]
                )
                row = cursor.fetchone()
            if row is not None and row[0] == "Lock":
                return True
            time.sleep(0.01)
        return False


class Stop:
    """Where a thread stops until the test lets it go on; `reached` once it is there."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self._go = threading.Event()

    def __call__(self) -> None:
        self.reached.set()
        assert self._go.wait(WAIT_S), "never let go"

    def go(self) -> None:
        self._go.set()


def stopping_after(function: Callable[..., Any], stop: Stop) -> Callable[..., Any]:
    def stopped(*args: Any, **kwargs: Any) -> Any:
        result = function(*args, **kwargs)
        stop()
        return result

    return stopped


def second_waits_for_first(
    first: Callable[[], Any], stop: Stop, second: Callable[[], Any]
) -> tuple[Any, Any]:
    """Run `first` until it stops, then `second`, which must wait for a lock `first` holds;
    let `first` go on. Returns both results (or exceptions)."""
    running = Running(first)
    waiting: Running | None = None
    try:
        assert stop.reached.wait(WAIT_S), f"the first never got there: {running.join()!r}"
        waiting = Running(second)
        assert waiting.waits_for_a_lock(), "the second didn't wait for the first to commit"
    finally:
        stop.go()
        first_result = running.join()
        second_result = waiting.join() if waiting is not None else None
    return first_result, second_result


@pytest.fixture
def world():
    """Rahul's own pipeline with a text field; his open deal and his won deal there, each
    with a value, and their leads (his); Priya, to whom an administrator reassigns them."""
    rahul, priya, admin = UserFactory(), UserFactory(), AdminFactory()
    configuration.create_pipeline(
        actor=rahul,
        scope=OWN(rahul.pk),
        name="Tenders",
        stages=[
            {"name": "Qualify", "type": "open", "probability": Decimal("10")},
            {"name": "Won", "type": "won"},
            {"name": "Lost", "type": "lost"},
        ],
        fields=[{"name": "Contact notes", "type": "text"}],
    )
    pipeline = Pipeline.objects.get(owner=rahul, name="Tenders")
    field = CustomField.objects.get(pipeline=pipeline)
    stages = {stage.name: stage for stage in Stage.objects.filter(pipeline=pipeline)}
    open_deal = OpportunityFactory(lead=LeadFactory(owner=rahul), stage=stages["Qualify"])
    won_deal = OpportunityFactory(lead=LeadFactory(owner=rahul), stage=stages["Won"])
    Opportunity.objects.filter(pk__in=[open_deal.pk, won_deal.pk]).update(
        custom_fields={str(field.pk): VALUE}
    )
    return {
        "rahul": rahul,
        "priya": priya,
        "admin": admin,
        "pipeline": pipeline,
        "field": field,
        "stages": stages,
        "open": open_deal,
        "won": won_deal,
    }


def remove_fields(world: dict[str, Any], *, delete: bool = False) -> Callable[[], Pipeline]:
    """Rahul removes every field of his pipeline (and with `delete`, their values)."""
    rahul, pipeline = world["rahul"], world["pipeline"]

    def remove() -> Pipeline:
        return configuration.replace_fields(
            actor=rahul,
            scope=OWN(rahul.pk),
            pipeline_id=pipeline.pk,
            version=Pipeline.objects.get(pk=pipeline.pk).version,
            fields=[],
            delete_removed_values=delete,
        )

    return remove


def reassign(world: dict[str, Any], deal: Opportunity) -> Callable[[], Lead]:
    """The administrator hands the deal's lead to Priya (its open deals follow it)."""
    admin, priya = world["admin"], world["priya"]

    def hand_over() -> Lead:
        lead = Lead.objects.get(pk=deal.lead_id)
        return lead_services.reassign_lead(
            actor=admin,
            scope=ORG(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=priya.pk,
        )

    return hand_over


def field_active(world: dict[str, Any]) -> bool:
    return bool(CustomField.objects.get(pk=world["field"].pk).is_active)


def owner_of_lead(deal: Opportunity) -> Any:
    return Lead.objects.get(pk=deal.lead_id).owner_id


def test_a_reassignment_waits_for_a_removal_past_its_check(world):
    stop = Stop()
    check = field_values.require_deletable
    with mock.patch.object(field_values, "require_deletable", stopping_after(check, stop)):
        removed, reassigned = second_waits_for_first(
            remove_fields(world), stop, reassign(world, world["open"])
        )
    assert isinstance(removed, Pipeline), removed
    assert isinstance(reassigned, Lead), reassigned
    # The field went while every deal holding a value was Rahul's; the deal moved after.
    assert not field_active(world)
    assert Opportunity.objects.get(pk=world["open"].pk).owner_id == world["priya"].pk


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("deal", ["open", "won"])
def test_a_removal_waits_for_a_reassignment_and_is_then_refused(world, deal, delete):
    """A won deal stays Rahul's, but its customer goes to Priya: it is outside his workspace
    too (the pipeline lock is taken even when no open deal moves)."""
    stop = Stop()
    follow = services.follow_lead_owner
    with mock.patch.object(services, "follow_lead_owner", stopping_after(follow, stop)):
        reassigned, removed = second_waits_for_first(
            reassign(world, world[deal]), stop, remove_fields(world, delete=delete)
        )
    assert isinstance(reassigned, Lead), reassigned
    assert isinstance(removed, PermissionDeniedError), removed
    assert field_active(world)
    held = Opportunity.objects.get(pk=world[deal].pk)
    assert held.custom_fields == {str(world["field"].pk): VALUE}
    assert owner_of_lead(held) == world["priya"].pk


def test_a_reassignment_of_a_shared_pipelines_deal_takes_no_pipeline_lock(world):
    """Shared pipelines are configured organisation-wide only, where nobody's deals are out
    of scope: their rows aren't locked (the default pipeline would be every reassignment's)."""
    lead = LeadFactory(owner=world["rahul"])
    deal = OpportunityFactory(lead=lead)
    assert deal.pipeline.owner_id is None
    with connection.cursor() as cursor:
        cursor.execute(services._SHARE_PERSONAL_PIPELINES_OF_LEAD, [lead.pk])
        assert cursor.fetchall() == []
        cursor.execute(services._SHARE_PERSONAL_PIPELINES_OF_LEAD, [world["open"].lead_id])
        assert cursor.fetchall() == [(world["pipeline"].pk,)]


@pytest.mark.parametrize("round_", range(4))
def test_removals_reassignments_and_deal_writes_at_once_never_deadlock(world, round_):
    """Every writer of the pipeline's deals at once: lock order lead -> deal -> pipeline (all
    shared but the configuration change, which locks no lead or deal), so none waits for
    another in a cycle; whatever wins, a removal that succeeded never left a value hidden
    from a workspace it didn't see when it checked."""
    rahul = world["rahul"]
    deal = world["open"]
    field_id = str(world["field"].pk)
    results = run_concurrently(
        remove_fields(world),
        reassign(world, deal),
        lambda: services.update_opportunity(
            actor=rahul,
            scope=OWN(rahul.pk),
            opportunity_id=deal.pk,
            version=deal.version,
            changes={"custom_fields": {field_id: "Rahul's edit"}},
        ),
        lambda: services.move_opportunity(
            actor=rahul,
            scope=OWN(rahul.pk),
            opportunity_id=world["won"].pk,
            version=world["won"].version,
            stage_id=world["stages"]["Qualify"].pk,
        ),
        lambda: services.update_opportunity(
            actor=rahul,
            scope=OWN(rahul.pk),
            opportunity_id=world["won"].pk,
            version=world["won"].version,
            changes={"value": Decimal("5")},
        ),
    )
    for result in results:
        assert not isinstance(result, OperationalError), result  # deadlock, lock timeout
        assert "deadlock" not in str(result).lower(), result
    removed, reassigned = results[0], results[1]
    assert isinstance(reassigned, Lead), reassigned
    if isinstance(removed, Pipeline):  # it went first: then every value was Rahul's
        assert not field_active(world)
    else:
        assert isinstance(removed, PermissionDeniedError), removed
        assert field_active(world)
