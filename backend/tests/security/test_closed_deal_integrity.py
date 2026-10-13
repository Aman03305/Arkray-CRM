"""A won or lost deal's financial record is frozen (adversarial review of 73ff1e3, M2;
docs/pipeline.md#closed-deals).

A salesperson used to `PATCH` a won deal's value from 100,000 to 9,999,999 with the current
version (200), and the audit kept only the field name. Now, once a deal is closed, every
normal edit path that would change what reporting and forecasting read of it (value,
probability, expected close date, opportunity date; services.CLOSED_FROZEN) is refused with
422 `deal_closed`, for every role, nothing written. Reopening makes them editable again;
closing freezes them again. An administrator corrects a closed deal through its own
operation (`POST .../correct`): a reason, the version, a row lock, and an append-only history
row per correction with the old and new values (pipeline_opportunity_correction, which the
application role may only read and insert), audited by field names only.
"""

from __future__ import annotations

import json
import secrets
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.db import has_append_only_trigger
from arkray.core.errors import AppendOnlyViolation, ConflictError
from arkray.core.models import HoldSubject, LegalHold
from arkray.pipeline import events, selectors, services
from arkray.pipeline.models import Opportunity, OpportunityCorrection, StageHistory
from tests.factories import AdminFactory, LeadFactory, UserFactory, default_stage
from tests.helpers import collected, run_concurrently, signed_in, without_request_id

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
ORG = AccessScope.organization
VALUE = Decimal("100000.00")
INFLATED = "9999999.00"
CLOSE = date(2026, 9, 30)
ARISEN = date(2026, 1, 15)


def closed_deal(owner: Any, status: str = "won", value: Decimal = VALUE) -> Opportunity:
    """A deal created and closed through the services (so it has its stage history)."""
    lead: Any = LeadFactory(owner=owner)
    opportunity = services.create_opportunity(
        actor=owner,
        scope=OWN(owner.pk),
        lead_id=lead.pk,
        fields={"value": value, "expected_close_date": CLOSE, "opportunity_date": ARISEN},
    ).opportunity
    return services.move_opportunity(
        actor=owner,
        scope=OWN(owner.pk),
        opportunity_id=opportunity.pk,
        version=opportunity.version,
        stage_id=default_stage(status).pk,
        lost_reason="Budget moved to next year." if status == "lost" else "",
    )


def url(opportunity: Opportunity, workspace: str = "me", action: str = "") -> str:
    base = f"/api/v1/workspaces/{workspace}/opportunities/{opportunity.pk}"
    return f"{base}/{action}" if action else base


def state(opportunity: Opportunity) -> tuple[Any, ...]:
    """The deal, its audit trail (opening a user's workspace is audited too: not counted) and
    the corrections."""
    row = Opportunity.objects.filter(pk=opportunity.pk).values().get()
    trail = AuditEvent.objects.filter(target_id=str(opportunity.pk)).count()
    return row, trail, OpportunityCorrection.objects.count()


def version(opportunity: Opportunity) -> int:
    return Opportunity.objects.values_list("version", flat=True).get(pk=opportunity.pk)


# Each frozen field with a change a salesperson might make to a closed deal.
FROZEN = [
    ("value", INFLATED),
    ("probability", "50.00"),
    ("expected_close_date", "2027-03-31"),
    ("expected_close_date", None),  # clearing it is a change too
    ("opportunity_date", "2025-12-01"),
]
FROZEN_IDS = ["value", "probability", "expected_close_date", "expected_close_cleared", "date"]


# --- the reported attack ---------------------------------------------------------------------
def test_a_sales_users_patch_of_a_won_deals_value_is_refused(user_a):
    """The review's probe: 200 and a 9,999,999 won deal before the fix."""
    deal = closed_deal(user_a)
    before = state(deal)
    response = signed_in(user_a).patch(
        url(deal), {"version": deal.version, "value": INFLATED}, format="json"
    )
    assert response.status_code == 422, response.content
    error = response.json()["error"]
    assert error["code"] == "deal_closed"
    assert error["details"] == {"fields": ["value"]}
    assert "Reopen" in error["message"]
    assert "administrator" in error["message"]
    assert state(deal) == before  # nothing written, versioned or audited


@pytest.mark.parametrize("status", ["won", "lost"])
@pytest.mark.parametrize(("field", "change"), FROZEN, ids=FROZEN_IDS)
def test_every_frozen_field_is_refused_on_a_closed_deal(user_a, status, field, change):
    deal = closed_deal(user_a, status)
    before = state(deal)
    response = signed_in(user_a).patch(
        url(deal), {"version": deal.version, field: change}, format="json"
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "deal_closed"
    assert response.json()["error"]["details"] == {"fields": [field]}
    assert state(deal) == before


@pytest.mark.parametrize("status", ["won", "lost"])
@pytest.mark.parametrize(("field", "change"), FROZEN, ids=FROZEN_IDS)
def test_an_administrator_cannot_patch_them_either(admin, user_a, status, field, change):
    """The normal edit refuses everyone: an administrator corrects through POST .../correct
    (reason, history), never by a silent edit."""
    deal = closed_deal(user_a, status)
    before = state(deal)
    for workspace in ("all", str(user_a.pk)):
        response = signed_in(admin).patch(
            url(deal, workspace), {"version": deal.version, field: change}, format="json"
        )
        assert response.status_code == 422, (workspace, response.content)
        assert response.json()["error"]["code"] == "deal_closed"
    assert state(deal) == before


def test_one_answer_lists_every_frozen_field_sent(user_a):
    deal = closed_deal(user_a)
    response = signed_in(user_a).patch(
        url(deal),
        {"version": deal.version, "value": INFLATED, "opportunity_date": "2025-01-01"},
        format="json",
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {"fields": ["opportunity_date", "value"]}


def test_the_stored_values_sent_back_unchanged_are_no_change(user_a):
    """An edit form that sends the whole record back (or a retry) isn't refused for what it
    didn't change; the description of a closed deal stays editable."""
    deal = closed_deal(user_a)
    response = signed_in(user_a).patch(
        url(deal),
        {
            "version": deal.version,
            "value": "100000.00",
            "probability": None,  # the stage's default: a won deal's 100 %
            "expected_close_date": CLOSE.isoformat(),
            "opportunity_date": ARISEN.isoformat(),
            "description": "Installed in March.",
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    stored = Opportunity.objects.get(pk=deal.pk)
    assert (stored.value, stored.description) == (VALUE, "Installed in March.")


def test_a_probability_of_null_on_a_closed_deal_changes_nothing(user_a):
    deal = closed_deal(user_a, "lost")
    response = signed_in(user_a).patch(
        url(deal), {"version": deal.version, "probability": None}, format="json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["version"] == deal.version  # nothing to write


def test_text_and_custom_details_of_a_closed_deal_stay_editable(user_a):
    deal = closed_deal(user_a, "lost")
    response = signed_in(user_a).patch(
        url(deal),
        {"version": deal.version, "lost_reason": "Chose a cheaper analyser.", "work_load": "200"},
        format="json",
    )
    assert response.status_code == 200, response.content


def test_reopening_makes_them_editable_and_closing_freezes_them_again(user_a):
    deal = closed_deal(user_a)
    client = signed_in(user_a)
    reopened = services.move_opportunity(
        actor=user_a,
        scope=OWN(user_a.pk),
        opportunity_id=deal.pk,
        version=deal.version,
        stage_id=default_stage("proposal").pk,
    )
    response = client.patch(
        url(deal), {"version": reopened.version, "value": "120000.00"}, format="json"
    )
    assert response.status_code == 200, response.content
    won = services.move_opportunity(
        actor=user_a,
        scope=OWN(user_a.pk),
        opportunity_id=deal.pk,
        version=response.json()["version"],
        stage_id=default_stage("won").pk,
    )
    again = client.patch(url(deal), {"version": won.version, "value": INFLATED}, format="json")
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "deal_closed"
    # The stage history records the value each close happened at.
    closes = StageHistory.objects.filter(opportunity_id=deal.pk, to_status="won")
    assert sorted(closes.values_list("value", flat=True)) == [VALUE, Decimal("120000.00")]


def test_no_agreed_price_can_be_recorded_on_a_closed_deal(user_a):
    deal = closed_deal(user_a)
    response = signed_in(user_a).post(
        url(deal, action="negotiated-prices"),
        {"version": deal.version, "price": "50000.00", "agreed_cpt": "Rs 18"},
        format="json",
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "deal_closed"


def test_moves_archiving_and_reassignment_keep_their_own_rules(admin, user_a):
    """The freeze is the edit's only: a closed deal still archives and restores, still can't
    move to another closed stage or change owner (each with its own answer, not
    `deal_closed`), and still reopens."""
    deal = closed_deal(user_a)
    client = signed_in(user_a)
    archived = client.post(url(deal, action="archive"), {"version": deal.version}, format="json")
    assert archived.status_code == 200, archived.content
    restored = client.post(
        url(deal, action="restore"), {"version": archived.json()["version"]}, format="json"
    )
    assert restored.status_code == 200, restored.content
    current = restored.json()["version"]
    to_lost = client.post(
        url(deal, action="move"),
        {"version": current, "stage": str(default_stage("lost").pk)},
        format="json",
    )
    assert to_lost.status_code == 422
    assert to_lost.json()["error"]["message"] == services.CLOSED_TO_CLOSED
    reassigned = signed_in(admin).post(
        url(deal, "all", "assign"), {"version": current, "owner": str(admin.pk)}, format="json"
    )
    assert reassigned.status_code == 422
    assert reassigned.json()["error"]["message"] == services.CLOSED_OWNER
    reopened = client.post(
        url(deal, action="move"),
        {"version": current, "stage": str(default_stage("proposal").pk)},
        format="json",
    )
    assert reopened.status_code == 200, reopened.content
    assert reopened.json()["status"] == "open"


# --- reopening is traceable (review round 1, DEALS-2) -------------------------------------------
def test_reopen_edit_close_leaves_the_old_and_new_figures_in_the_stage_history(user_a):
    """Reopening is the salesperson's own way to change a closed deal's figures; every
    transition now records the deal's dates beside its value and probability, so the
    append-only stage history keeps both the figures it was won at and the new ones (the
    `opportunity.updated` audit names the fields only, and `closed_at` moves)."""
    deal = closed_deal(user_a)
    client = signed_in(user_a)
    reopened = services.move_opportunity(
        actor=user_a,
        scope=OWN(user_a.pk),
        opportunity_id=deal.pk,
        version=deal.version,
        stage_id=default_stage("proposal").pk,
    )
    edited = client.patch(
        url(deal),
        {
            "version": reopened.version,
            "value": "120000.00",
            "expected_close_date": "2027-01-31",
            "opportunity_date": "2026-02-01",
        },
        format="json",
    )
    assert edited.status_code == 200, edited.content
    services.move_opportunity(
        actor=user_a,
        scope=OWN(user_a.pk),
        opportunity_id=deal.pk,
        version=edited.json()["version"],
        stage_id=default_stage("won").pk,
    )
    rows = list(
        StageHistory.objects.filter(opportunity_id=deal.pk)
        .order_by("id")
        .values_list("to_status", "value", "probability", "expected_close_date", "opportunity_date")
    )
    assert rows == [
        ("open", VALUE, Decimal("10.00"), CLOSE, ARISEN),  # created
        ("won", VALUE, Decimal("100.00"), CLOSE, ARISEN),  # won at these figures
        ("open", VALUE, Decimal("50.00"), CLOSE, ARISEN),  # reopened, before the edit
        ("won", Decimal("120000.00"), Decimal("100.00"), date(2027, 1, 31), date(2026, 2, 1)),
    ]
    listed = client.get(url(deal, action="history")).json()["results"]
    assert (listed[0]["expected_close_date"], listed[0]["opportunity_date"]) == (
        "2027-01-31",
        "2026-02-01",
    )
    assert (listed[2]["expected_close_date"], listed[2]["opportunity_date"]) == (
        CLOSE.isoformat(),
        ARISEN.isoformat(),
    )


def test_the_previous_release_can_still_write_the_stage_history(user_a):
    """The new columns are nullable with no default to supply: an INSERT that doesn't name
    them (the previous release during a rolling deploy, or after a rollback) still works."""
    deal = closed_deal(user_a)
    stage = default_stage("won")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO pipeline_stage_history (opportunity_id, from_stage_id, to_stage_id,"
            " from_stage_name, to_stage_name, from_status, to_status, value, probability,"
            " lost_reason, actor_id, occurred_at) VALUES (%s, NULL, %s, '', %s, '', 'won',"
            " 1, 100, '', %s, now())",
            [deal.pk, stage.pk, stage.name, user_a.pk],
        )
    row = StageHistory.objects.filter(opportunity_id=deal.pk).order_by("-id").first()
    assert row is not None
    assert (row.expected_close_date, row.opportunity_date) == (None, None)


# --- an administrator's correction -------------------------------------------------------------
def correct(client: Any, deal: Opportunity, workspace: str = "all", **body: Any) -> Any:
    body.setdefault("version", version(deal))
    return client.post(url(deal, workspace, "correct"), body, format="json")


def test_an_administrator_corrects_a_won_deal_with_a_reason(admin, user_a):
    deal = closed_deal(user_a)
    with collected(events.OpportunityUpdated) as published:
        response = correct(
            signed_in(admin),
            deal,
            value="95000.00",
            opportunity_date="2026-01-10",
            reason="Credit note CN-118: 5% discount after installation.",
        )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["value"] == "95000.00"
    assert body["version"] == deal.version + 1
    row = OpportunityCorrection.objects.get(opportunity_id=deal.pk)
    assert sorted(row.fields) == ["opportunity_date", "value"]
    assert (row.value_before, row.value_after) == (VALUE, Decimal("95000.00"))
    assert (row.opportunity_date_before, row.opportunity_date_after) == (ARISEN, date(2026, 1, 10))
    assert row.expected_close_date_before == row.expected_close_date_after == CLOSE
    assert row.actor_id == admin.pk
    assert row.subject_user_id == user_a.pk
    assert row.reason == "Credit note CN-118: 5% discount after installation."
    assert row.status == "won"
    assert row.opportunity_version == deal.version + 1
    assert abs((timezone.now() - row.occurred_at).total_seconds()) < 60
    assert [e.fields for e in published] == [("opportunity_date", "value")]


def test_the_correction_history_lists_old_and_new_values(admin, user_a):
    deal = closed_deal(user_a, "lost")
    correct(signed_in(admin), deal, value="80000.00", reason="Typo in the quote.")
    correct(signed_in(admin), deal, expected_close_date=None, reason="Never had a close date.")
    for client, workspace in ((signed_in(user_a), "me"), (signed_in(admin), "all")):
        listing = client.get(url(deal, workspace, "corrections"))
        assert listing.status_code == 200, listing.content
        rows = listing.json()["results"]
        assert [r["fields"] for r in rows] == [["expected_close_date"], ["value"]]  # newest first
        assert rows[0]["changes"] == [
            {"field": "expected_close_date", "before": CLOSE.isoformat(), "after": None}
        ]
        assert rows[1]["changes"] == [
            {"field": "value", "before": "100000.00", "after": "80000.00"}
        ]
        assert rows[1]["reason"] == "Typo in the quote."
        assert rows[1]["actor"]["id"] == str(admin.pk)
        assert rows[1]["status"] == "lost"


def test_nobody_else_sees_the_corrections(admin, user_a, user_b):
    deal = closed_deal(user_a)
    correct(signed_in(admin), deal, value="90000.00", reason="Discount.")
    response = signed_in(user_b).get(url(deal, action="corrections"))
    missing = signed_in(user_b).get(
        "/api/v1/workspaces/me/opportunities/5a1e4d2c-0000-4000-8000-00000000abcd/corrections"
    )
    assert response.status_code == missing.status_code == 404
    assert without_request_id(response) == without_request_id(missing)


def test_a_sales_user_cannot_correct_even_their_own_deal(user_a):
    deal = closed_deal(user_a)
    before = state(deal)
    response = correct(signed_in(user_a), deal, "me", value=INFLATED, reason="Because.")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert state(deal) == before


def test_an_open_deal_is_edited_not_corrected(admin, user_a):
    lead = LeadFactory(owner=user_a)
    deal = services.create_opportunity(
        actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, fields={"value": VALUE}
    ).opportunity
    before = state(deal)
    response = correct(signed_in(admin), deal, value="1.00", reason="Not closed.")
    assert response.status_code == 422, response.content
    assert "edit" in response.json()["error"]["message"].lower()
    assert state(deal) == before


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"value": "1.00"}, "reason"),
        ({"value": "1.00", "reason": ""}, "reason"),
        ({"value": "1.00", "reason": "   "}, "reason"),
        ({"value": "1.00", "reason": "x" * 301}, "reason"),
        ({"value": "1.00", "reason": "Fine" + chr(0x202E) + "evil"}, "reason"),
        ({"reason": "Nothing named."}, "non_field_errors"),
        ({"value": "100000.00", "reason": "Same value."}, "non_field_errors"),
        ({"value": "-5.00", "reason": "Negative."}, "value"),
        ({"opportunity_date": None, "reason": "No date."}, "opportunity_date"),
    ],
    ids=[
        "no-reason",
        "blank-reason",
        "spaces-reason",
        "long-reason",
        "bidi-reason",
        "no-field",
        "no-change",
        "negative",
        "no-date",
    ],
)
def test_a_correction_needs_a_reason_and_a_real_change(admin, user_a, body, field):
    deal = closed_deal(user_a)
    before = state(deal)
    response = correct(signed_in(admin), deal, **body)
    assert response.status_code == 400, response.content
    assert field in response.json()["error"]["details"], response.json()
    assert state(deal) == before


def test_probability_is_not_correctable(admin, user_a):
    """Won is 100 % and lost 0 % (database constraints): nothing to correct."""
    deal = closed_deal(user_a)
    response = correct(signed_in(admin), deal, probability="90.00", reason="Unsure.")
    assert response.status_code == 400
    assert "probability" in json.dumps(response.json()["error"])


def test_a_multi_line_reason_is_kept_on_one_line(admin, user_a):
    deal = closed_deal(user_a)
    response = correct(signed_in(admin), deal, value="1.00", reason="Line one.\nLine two.")
    assert response.status_code == 200, response.content
    assert OpportunityCorrection.objects.get().reason == "Line one. Line two."


def test_a_stale_version_is_refused(admin, user_a):
    deal = closed_deal(user_a)
    before = state(deal)
    response = correct(
        signed_in(admin), deal, version=deal.version - 1, value="1.00", reason="Stale."
    )
    assert response.status_code == 409
    assert state(deal) == before


def test_an_archived_closed_deal_is_restored_before_it_is_corrected(admin, user_a):
    deal = closed_deal(user_a)
    Opportunity.objects.filter(pk=deal.pk).update(archived_at=timezone.now())
    response = correct(signed_in(admin), deal, value="1.00", reason="Archived.")
    assert response.status_code == 422
    assert OpportunityCorrection.objects.count() == 0


def test_a_correction_needs_a_recent_second_factor(admin, user_a):
    """Like every sensitive administrator operation (ADR-0033): an administrator with a second
    factor and no recent code gets 403 `mfa_required` (the web app asks and repeats)."""
    from tests.mfa_helpers import enrol

    enrol(admin)
    deal = closed_deal(user_a)
    response = correct(signed_in(admin), deal, value="1.00", reason="Needs MFA.")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "mfa_required"
    assert OpportunityCorrection.objects.count() == 0


def test_refused_in_a_support_session(admin, user_a):
    deal = closed_deal(user_a)
    client = signed_in(admin)
    started = client.post(
        "/api/v1/admin/support-sessions", {"user": str(user_a.pk), "reason": "T-1"}, format="json"
    )
    assert started.status_code in (200, 201), started.content
    response = correct(client, deal, str(user_a.pk), value="1.00", reason="Support.")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "support_session_active"
    assert OpportunityCorrection.objects.count() == 0


def test_an_administrator_in_a_users_workspace_may_correct(admin, user_a):
    deal = closed_deal(user_a)
    response = correct(signed_in(admin), deal, str(user_a.pk), value="1.00", reason="Fix.")
    assert response.status_code == 200, response.content
    assert AuditEvent.objects.get(action=services.AUDIT_CORRECTED).metadata["workspace"] == "user"


# --- integrity of the trail ---------------------------------------------------------------------
def test_audited_by_field_names_only(admin, user_a):
    deal = closed_deal(user_a)
    correct(signed_in(admin), deal, value="87654.00", reason="Discount D-77 agreed.")
    event = AuditEvent.objects.get(action=services.AUDIT_CORRECTED)
    assert event.actor_id == admin.pk
    assert event.target_id == str(deal.pk)
    assert event.subject_user_id == user_a.pk
    assert event.metadata["fields"] == ["value"]
    assert event.metadata["correction_id"] == str(OpportunityCorrection.objects.get().pk)
    trail = json.dumps(list(AuditEvent.objects.values("metadata")), default=str)
    for value in ("87654", "100000", "D-77"):
        assert value not in trail


def test_the_history_can_not_be_rewritten_through_the_orm(admin, user_a):
    deal = closed_deal(user_a)
    correct(signed_in(admin), deal, value="1.00", reason="Fix.")
    row = OpportunityCorrection.objects.get()
    row.reason = "rewritten"
    with pytest.raises(AppendOnlyViolation):
        row.save()
    with pytest.raises(AppendOnlyViolation):
        OpportunityCorrection.objects.update(reason="rewritten")
    with pytest.raises(AppendOnlyViolation):
        row.delete()


def _refused(statement: str) -> bool:
    """Whether PostgreSQL refuses `statement` as an insufficient privilege (the append-only
    trigger raises exactly that), inside a savepoint so the test goes on."""
    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT probe")
        try:
            cursor.execute(statement)
        except DatabaseError as exc:
            cursor.execute("ROLLBACK TO SAVEPOINT probe")
            return isinstance(exc.__cause__, psycopg.errors.InsufficientPrivilege)
        cursor.execute("RELEASE SAVEPOINT probe")
    return False


def test_the_history_can_not_be_rewritten_in_the_database(admin, user_a):
    """The trigger refuses an UPDATE or DELETE whatever the role (here the schema owner the
    tests run as; only a role that can disable it could, and the application role can't:
    the next test)."""
    deal = closed_deal(user_a)
    correct(signed_in(admin), deal, value="1.00", reason="Fix.")
    assert has_append_only_trigger(connection, "pipeline_opportunity_correction")
    assert _refused("UPDATE pipeline_opportunity_correction SET reason = 'x'")
    assert _refused("UPDATE pipeline_opportunity_correction SET value_after = 9999999")
    assert _refused("DELETE FROM pipeline_opportunity_correction")
    assert OpportunityCorrection.objects.get().reason == "Fix."


@pytest.mark.django_db(transaction=True)
def test_the_application_role_may_only_read_and_add_corrections():
    """grant_app_privileges finds the table by its trigger: SELECT and INSERT only, never
    UPDATE, DELETE, TRUNCATE or disabling the trigger (R74, docs/security.md#database-
    privileges; as tests/integration/test_database_roles.py checks the other histories)."""
    role, password = f"arkray_test_app_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    try:
        call_command("grant_app_privileges", role)
        settings = connection.settings_dict
        with psycopg.connect(
            host=settings["HOST"] or "127.0.0.1",
            port=settings["PORT"] or 5432,
            dbname=settings["NAME"],
            user=role,
            password=password,
            autocommit=True,
        ) as conn:
            conn.execute("SELECT count(*) FROM pipeline_opportunity_correction")
            for statement in (
                "UPDATE pipeline_opportunity_correction SET reason = '' WHERE false",
                "DELETE FROM pipeline_opportunity_correction WHERE false",
                "TRUNCATE pipeline_opportunity_correction",
                "ALTER TABLE pipeline_opportunity_correction DISABLE TRIGGER ALL",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
            privileges = conn.execute(
                "SELECT has_table_privilege(current_user, 'pipeline_opportunity_correction', p)"
                " FROM unnest(ARRAY['SELECT', 'INSERT']) AS p"
            ).fetchall()
            assert privileges == [(True,), (True,)]
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP OWNED BY {role}")
            cursor.execute(f"DROP ROLE {role}")


def test_a_correction_leaves_the_stage_history_alone(admin, user_a):
    deal = closed_deal(user_a)
    before = list(StageHistory.objects.filter(opportunity_id=deal.pk).order_by("id").values())
    correct(signed_in(admin), deal, value="1.00", reason="Fix.")
    after = list(StageHistory.objects.filter(opportunity_id=deal.pk).order_by("id").values())
    assert after == before
    assert after[-1]["value"] == VALUE  # the value the deal was won at, as it happened


def test_reports_reflect_the_corrected_value_and_agree(admin, user_a):
    """The board's won column, Ask Arkray's stage breakdown and the list read the live value:
    after a correction they all show it, and each other's figures."""
    first = closed_deal(user_a)
    closed_deal(user_a, value=Decimal("50000.00"))
    correct(signed_in(admin), first, value="75000.00", reason="Discount.")
    client = signed_in(user_a)
    board = client.get("/api/v1/workspaces/me/pipeline-board").json()
    won = next(c for c in board["columns"] if c["stage"]["key"] == "won")
    assert won["total_value"] == "125000.00"
    assert sorted(card["value"] for card in won["cards"]) == ["50000.00", "75000.00"]
    pipeline = selectors.pipeline_for_board(OWN(user_a.pk), None)
    breakdown = {
        total.stage.key: total.total_value
        for total in selectors.stage_breakdown(OWN(user_a.pk), pipeline)
    }
    assert breakdown["won"] == Decimal("125000.00")
    listing = client.get("/api/v1/workspaces/me/opportunities?status=won&ordering=-value").json()
    assert [row["value"] for row in listing["results"]] == ["75000.00", "50000.00"]


# --- holds, erasure and access requests ---------------------------------------------------------
def test_a_legal_hold_does_not_stop_a_correction(admin, user_a):
    """A hold freezes the customer's data (core.holds), not the money: a correction adds to
    the history, it overwrites none of the held text."""
    deal = closed_deal(user_a)
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD, subject_id=deal.lead_id, reference="C-9", placed_by=admin.pk
    )
    response = correct(signed_in(admin), deal, value="1.00", reason="Fix.")
    assert response.status_code == 200, response.content


def test_an_erased_customers_deal_takes_no_new_reason(admin, user_a):
    """Nothing new is added for an erased customer (deep security audit M-1): a correction's
    reason is new free text about their deal."""
    from arkray.privacy import services as privacy

    deal = closed_deal(user_a)
    privacy.erase(deal.lead_id, operator_id=admin.pk)
    response = correct(signed_in(admin), deal, value="1.00", reason="Fix.")
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "lead_erased"
    assert OpportunityCorrection.objects.count() == 0


def test_erasing_the_customer_redacts_the_reasons(admin, user_a):
    from arkray.privacy import services as privacy

    deal = closed_deal(user_a)
    correct(signed_in(admin), deal, value="1.00", reason="Ravi Kumar asked for a discount.")
    privacy.erase(deal.lead_id, operator_id=admin.pk)
    row = OpportunityCorrection.objects.get()
    assert row.reason == ""
    assert (row.value_before, row.value_after) == (VALUE, Decimal("1.00"))  # money stays


def test_an_access_request_export_includes_the_corrections(admin, user_a):
    from arkray.privacy import exports

    deal = closed_deal(user_a)
    correct(signed_in(admin), deal, value="1.00", reason="Ravi asked for a discount.")
    document, _ = exports.lead_document(deal.lead_id)
    assert document["deal_corrections"] == [
        {
            "opportunity_id": str(deal.pk),
            "fields": ["value"],
            "value_before": "100000.00",
            "value_after": "1.00",
            "expected_close_date_before": CLOSE.isoformat(),
            "expected_close_date_after": CLOSE.isoformat(),
            "opportunity_date_before": ARISEN.isoformat(),
            "opportunity_date_after": ARISEN.isoformat(),
            "reason": "Ravi asked for a discount.",
            "occurred_at": document["deal_corrections"][0]["occurred_at"],
        }
    ]


# --- races ----------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_concurrent_corrections_one_wins_the_other_conflicts():
    owner, administrator = UserFactory(), AdminFactory()
    deal = closed_deal(owner)
    results = run_concurrently(
        *(
            (
                lambda amount=amount: services.correct_closed_deal(
                    actor=administrator,
                    scope=ORG(administrator.pk),
                    opportunity_id=deal.pk,
                    version=deal.version,
                    reason=f"Set to {amount}.",
                    changes={"value": Decimal(amount)},
                )
            )
            for amount in ("1.00", "2.00")
        )
    )
    ok = [r for r in results if isinstance(r, Opportunity)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert (len(ok), len(conflicts)) == (1, 1), results
    rows = list(OpportunityCorrection.objects.filter(opportunity_id=deal.pk))
    assert len(rows) == 1  # no lost history, nothing half-written
    stored = Opportunity.objects.get(pk=deal.pk)
    assert stored.value == rows[0].value_after == ok[0].value
    assert stored.version == deal.version + 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_a_correction_racing_a_reopen_one_wins_the_other_conflicts():
    """Both lock the deal (lead first) and check its version: one goes first, the other
    finds a new version (409). Never a correction recorded on a deal reopened meanwhile."""
    owner, administrator = UserFactory(), AdminFactory()
    deal = closed_deal(owner)
    results = run_concurrently(
        lambda: services.correct_closed_deal(
            actor=administrator,
            scope=ORG(administrator.pk),
            opportunity_id=deal.pk,
            version=deal.version,
            reason="Discount.",
            changes={"value": Decimal("1.00")},
        ),
        lambda: services.move_opportunity(
            actor=owner,
            scope=OWN(owner.pk),
            opportunity_id=deal.pk,
            version=deal.version,
            stage_id=default_stage("proposal").pk,
        ),
    )
    corrected, reopened = results
    assert [isinstance(r, Opportunity) for r in results].count(True) == 1, results
    assert [isinstance(r, ConflictError) for r in results].count(True) == 1, results
    stored = Opportunity.objects.get(pk=deal.pk)
    corrections = list(OpportunityCorrection.objects.filter(opportunity_id=deal.pk))
    if isinstance(corrected, Opportunity):
        assert (stored.status, stored.value) == ("won", Decimal("1.00"))
        assert [c.status for c in corrections] == ["won"]
    else:
        assert isinstance(reopened, Opportunity)
        assert (stored.status, stored.value) == ("open", VALUE)
        assert corrections == []
    assert stored.version == deal.version + 1
